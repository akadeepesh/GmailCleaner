"""
GmailCleaner — Web Dashboard & Webhook Server
A lightweight Flask application serving two purposes:

  1. Dashboard (GET routes) — review pending actions, inbox stats, run history
  2. Webhook (POST /webhook/pubsub) — receive Gmail Pub/Sub push notifications

Routes:
  GET  /                         — Dashboard overview (scan button, progress)
  GET  /pending                  — Review, confirm and execute pending actions
  GET  /stats                    — Inbox activity chart (14-day) and CSV downloads
  GET  /history                  — Recent run history
  GET  /help                     — Plain-language FAQ
  GET  /export/<dataset>.csv     — Inbox health CSV export (summary|senders|actions|runs)
  GET  /health                   — Health check (Cloud Run liveness probe)
  POST /webhook/pubsub           — Pub/Sub push endpoint
  POST /api/confirm              — Confirm or reject one or more pending actions
  POST /api/execute              — Execute confirmed actions ({"background": true} → job)
  POST /api/scan                 — Start a scan job
  GET  /api/jobs, /api/jobs/<id> — Background job status
  POST /api/account/switch       — Choose the account shown in the dashboard
  POST /api/watch/renew          — Renew the Gmail watch (called daily by Cloud Scheduler)
  Settings and sign-in routes live in web/settings_routes.py and web/auth_routes.py.

Accounts:
  The account is chosen per request (gc_account cookie / sidebar switcher)
  from the signed-in accounts; see web/accounts.py. With GMAILCLEANER_ACCOUNT
  set (Cloud Run) the dashboard is pinned to that one account, as before.

Security:
  - Dashboard routes require the DASHBOARD_SECRET token, supplied as
    `Authorization: Bearer <token>`, a `?token=` query param (which then sets
    an HttpOnly, SameSite=Strict cookie so navigation keeps working), or that cookie.
  - Desktop mode additionally requires the secret, allows only loopback Host
    headers and requires an X-GC-Request header on every POST (web/common.py).
  - The webhook verifies the Pub/Sub OIDC JWT when PUBSUB_AUDIENCE is set.
  - All user-supplied data in templates is auto-escaped by Jinja2.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    make_response,
    render_template,
    request,
)

from auth.oauth import get_credentials
from core import updates
from core.jobs import JobRunner, user_message
from core.paths import is_desktop_mode, is_headless, resource_path
from core.pipeline import Busy, PipelineError, execution_result, run_execute, run_scan
from core.secrets import get_gemini_key
from core.state import AppState, state_path
from engines.unsubscribe import UnsubscribeEngine
from gmail.client import GmailClient
from gmail.executor import confirmed_items
from output.exporter import DATASETS, to_csv
from output.reporter import load_pending_actions, save_pending_actions
from pubsub.watcher import WatchManager
from pubsub.webhook import PubSubNotification, WebhookHandler, build_triage_engine, verify_pubsub_token
from version import __version__
from web.accounts import AccountRegistry, key_for
from web.common import (
    ACCOUNT_COOKIE,
    TOKEN_COOKIE,  # noqa: F401  (re-exported for callers of the old module layout)
    current_account,
    current_ctx,
    install_security,
    is_desktop,
    jobs,
    json_error,
    registry,
    require_token,
)

logger = logging.getLogger(__name__)

_require_token = require_token  # backwards-compatible name


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────

def create_app(config: Optional[dict] = None) -> Flask:
    """
    Flask application factory.

    Args:
        config: Optional raw config dict (as parsed from settings.yaml). When
                omitted, config/settings.yaml (+ user overrides) is loaded.
                Environment overrides are applied in both cases.

    Returns:
        Configured Flask app ready to serve.

    Raises:
        RuntimeError: In desktop mode without a DASHBOARD_SECRET.
    """
    app = Flask(
        __name__,
        template_folder=str(resource_path("web")),
        static_folder=None,
    )

    desktop = is_desktop_mode()
    app.config["DESKTOP"] = desktop
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"   # must survive the redirect back from Google
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    port = os.getenv("GMAILCLEANER_PORT", "")
    app.config["GC_PORT"] = int(port) if port.isdigit() else None

    if desktop and not os.getenv("DASHBOARD_SECRET"):
        raise RuntimeError(
            "Desktop mode needs DASHBOARD_SECRET. Start the app with `python -m desktop`."
        )
    if not os.getenv("DASHBOARD_SECRET"):
        logger.warning(
            "DASHBOARD_SECRET is not set — dashboard authentication is DISABLED. "
            "Only run like this on a trusted local machine."
        )

    # ── Services ──────────────────────────────────────────────────────────────
    accounts = AccountRegistry(config)
    app.config["ACCOUNTS"] = accounts
    app.config["JOBS"] = JobRunner()
    # Remembered between launches in desktop mode; memory-only elsewhere.
    app.config["STATE"] = AppState(state_path() if desktop else None)
    app.config.setdefault("ON_QUIT", None)   # set by the desktop launcher
    _publish_default_context(app)

    @app.context_processor
    def _inject_globals():
        reg = registry()
        try:
            ctx = current_ctx()
            account_list = reg.list_accounts()
        except Exception:  # never let the sidebar break a page
            logger.warning("Could not resolve accounts for the sidebar", exc_info=True)
            ctx, account_list = None, []
        return {
            "version": __version__,
            "account": ctx.name if ctx else None,
            "account_key": ctx.key if ctx else None,
            "accounts": account_list,
            "pinned": bool(reg.pinned),
            "desktop": is_desktop(),
            "headless": is_headless(),
            "update_banner": _update_banner(app, ctx),
            "can_quit": is_desktop() and app.config.get("ON_QUIT") is not None,
        }

    app.jinja_env.filters["ago"] = _ago

    install_security(app)
    _register_routes(app)

    from web.auth_routes import register_auth_routes
    from web.desktop_routes import register_desktop_routes
    from web.settings_routes import register_settings_routes

    register_auth_routes(app)
    register_settings_routes(app)
    register_desktop_routes(app)
    return app


def _publish_default_context(app: Flask) -> None:
    """
    Expose the default account's cfg/db as app.config["GMAILCLEANER"] / ["DB"]
    (kept for backwards compatibility with embedding code and tests).
    """
    ctx = app.config["ACCOUNTS"].get(None)
    app.config["GMAILCLEANER"] = ctx.cfg
    app.config["DB"] = ctx.db


def _update_banner(app: Flask, ctx) -> Optional[dict]:
    """The cached "new version" banner (desktop only; never hits the network)."""
    if not app.config.get("DESKTOP") or ctx is None:
        return None
    try:
        enabled = bool((ctx.cfg.get("updates") or {}).get("check", True))
        return updates.banner(app.config["STATE"], enabled=enabled)
    except Exception:
        logger.warning("Could not read the update banner", exc_info=True)
        return None


def invalidate_contexts(app: Flask) -> None:
    """Apply changed settings: rebuild every account context on next use."""
    app.config["ACCOUNTS"].invalidate()
    _publish_default_context(app)


# ──────────────────────────────────────────────────────────────────────────────
# Route registration
# ──────────────────────────────────────────────────────────────────────────────

def _register_routes(app: Flask) -> None:

    def pending_path() -> str:
        return current_ctx().cfg["output"]["pending_actions_path"]

    def load_report() -> dict:
        report = load_pending_actions(pending_path(), missing_ok=True)
        return report or {"unsubscribe": [], "delete_groups": [], "triage": {}, "summary": {}}

    # ── Health check ──────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        """Cloud Run liveness probe — must return 200 quickly."""
        return jsonify({"status": "ok", "service": "gmailcleaner-dashboard", "version": __version__})

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.get("/")
    @require_token
    def dashboard():
        ctx = current_ctx()
        report = load_report()
        unsub_ready, delete_ready = confirmed_items(report)
        account_info = registry().find(ctx.key)
        return render_template(
            "dashboard.html",
            active="dashboard",
            summary=ctx.db.get_dashboard_summary(),
            stats=ctx.db.get_inbox_stats(days=7),
            watch_reg=ctx.db.get_watch_registration(),
            suggestions=len(report.get("unsubscribe", [])) + len(report.get("delete_groups", [])),
            ready_count=len(unsub_ready) + len(delete_ready),
            signed_in=bool(account_info and account_info.signed_in) or bool(os.getenv("GOOGLE_TOKEN_JSON")),
            welcome=request.args.get("welcome") == "1",
            active_job=jobs().active(ctx.name),
            ai_ready=bool(ctx.cfg.get("triage", {}).get("enabled")) and bool(get_gemini_key()),
            now=_now_str(),
        )

    # ── Pending actions ───────────────────────────────────────────────────────

    @app.get("/pending")
    @require_token
    def pending():
        report = load_report()
        unsub_ready, delete_ready = confirmed_items(report)
        lookback = current_ctx().cfg.get("scan", {}).get("lookback_days", 30)
        return render_template(
            "pending.html",
            active="pending",
            report=report,
            ready_count=len(unsub_ready) + len(delete_ready),
            ready_unsub=len(unsub_ready),
            ready_trash=sum(i.get("email_count", 0) for i in delete_ready),
            ready_groups=len(delete_ready),
            lookback_days=lookback,
            session_path=pending_path(),
            active_job=jobs().active(current_ctx().name),
            now=_now_str(),
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    @app.get("/stats")
    @require_token
    def stats():
        db = current_ctx().db
        return render_template(
            "stats.html",
            active="stats",
            inbox_stats=db.get_inbox_stats(days=14),
            triage_counts=db.get_triage_counts(),
            sender_stats={
                "top_senders": db.get_top_senders(limit=10),
                "total_unsubscribed": db.get_dashboard_summary()["unsubscribed"],
            },
            datasets=DATASETS,
            now=_now_str(),
        )

    # ── Run history ───────────────────────────────────────────────────────────

    @app.get("/history")
    @require_token
    def history():
        return render_template(
            "history.html",
            active="history",
            runs=current_ctx().db.get_recent_runs(limit=20),
            now=_now_str(),
        )

    # ── Help ──────────────────────────────────────────────────────────────────

    @app.get("/help")
    @require_token
    def help_page():
        return render_template("help.html", active="help", now=_now_str())

    # ── CSV export ────────────────────────────────────────────────────────────

    @app.get("/export/<dataset>.csv")
    @require_token
    def export_dataset(dataset: str):
        if dataset not in DATASETS:
            abort(404)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return Response(
            to_csv(current_ctx().db, dataset),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="gmailcleaner-{dataset}-{stamp}.csv"'
            },
        )

    # ── API: confirm / reject action ──────────────────────────────────────────

    @app.post("/api/confirm")
    @require_token
    def api_confirm():
        """
        Set confirmed=true/false on pending actions in pending_actions.json.

        Request body (JSON):
          { "action_type": "unsubscribe"|"delete", "target": "<domain or group_id>", "confirmed": true|false }
          or, for "Select all", "targets": [...] instead of "target".
        """
        data = request.get_json(silent=True) or {}
        action_type = data.get("action_type")
        target = data.get("target")
        targets = data.get("targets")
        confirmed = bool(data.get("confirmed", True))

        many = isinstance(targets, list)
        if many:
            targets = [t for t in targets if isinstance(t, str) and t]
        if action_type not in ("unsubscribe", "delete") or not (targets if many else target):
            return jsonify({"error": "action_type (unsubscribe|delete) and target are required"}), 400

        report = load_pending_actions(pending_path(), missing_ok=True)
        if not report:
            return jsonify({"error": "No pending actions found"}), 404

        section = "unsubscribe" if action_type == "unsubscribe" else "delete_groups"
        id_field = "domain" if action_type == "unsubscribe" else "group_id"
        by_id = {i.get(id_field): i for i in report.get(section, [])}

        if not many:
            item = by_id.get(target)
            if item is None:
                return jsonify({"error": f"Target {target!r} not found"}), 404
            if item.get("executed_at"):
                return jsonify({"error": f"Target {target!r} was already executed"}), 409
            item["confirmed"] = confirmed
            save_pending_actions(report, pending_path())
            return jsonify({"status": "ok", "target": target, "confirmed": confirmed})

        updated = []
        for t in targets:
            item = by_id.get(t)
            if item is not None and not item.get("executed_at"):
                item["confirmed"] = confirmed
                updated.append(t)
        if not updated:
            return jsonify({"error": "None of those items can be changed"}), 404
        save_pending_actions(report, pending_path())
        return jsonify({"status": "ok", "targets": updated, "confirmed": confirmed})

    # ── API: execute confirmed actions ────────────────────────────────────────

    @app.post("/api/execute")
    @require_token
    def api_execute():
        """
        Execute every confirmed, not-yet-executed action in the pending report.

        By default this runs synchronously and returns the summary (as it always
        has). With {"background": true} it starts a job and returns {job_id};
        the job's result has the same keys.
        """
        data = request.get_json(silent=True) or {}
        ctx = current_ctx()
        report = load_pending_actions(pending_path(), missing_ok=True)
        if not report:
            return jsonify({"error": "No pending actions found"}), 404

        unsub_items, delete_items = confirmed_items(report)
        if not unsub_items and not delete_items:
            return jsonify({"error": "No confirmed actions to execute"}), 400

        if data.get("background") is True:
            def job(progress):
                client = GmailClient(_credentials(ctx.cfg, interactive=False))
                summary = run_execute(ctx.cfg, progress=progress, client=client, db=ctx.db)
                return execution_result(summary)

            job_id, created = jobs().submit(ctx.name, "execute", job)
            return jsonify({"job_id": job_id, "created": created}), 202

        try:
            client = GmailClient(_credentials(ctx.cfg))
            summary = run_execute(ctx.cfg, client=client, db=ctx.db)
        except Busy as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.error("Execute failed: %s", exc, exc_info=True)
            return jsonify({"error": user_message(exc)}), 500

        return jsonify(execution_result(summary))

    # ── API: scan ─────────────────────────────────────────────────────────────

    @app.post("/api/scan")
    @require_token
    def api_scan():
        """
        Start a scan job. Body: {"execute_trusted": bool}. With execute_trusted,
        actions matching trusted rules are executed (only when confirm_first is
        off — the rules engine enforces that, the GUI can't bypass it).
        """
        data = request.get_json(silent=True) or {}
        execute_trusted = data.get("execute_trusted", False)
        if not isinstance(execute_trusted, bool):
            return json_error("execute_trusted must be true or false", 400)
        job_id, created = start_scan(app, current_account(), execute_trusted=execute_trusted)
        return jsonify({"job_id": job_id, "created": created}), 202

    # ── API: jobs ─────────────────────────────────────────────────────────────

    @app.get("/api/jobs/<job_id>")
    @require_token
    def api_job(job_id: str):
        job = jobs().get(job_id)
        if job is None:
            return json_error("Job not found", 404)
        return jsonify(job)

    @app.get("/api/jobs")
    @require_token
    def api_jobs():
        if "account" in request.args:
            key = request.args.get("account") or None
            found = registry().find(key) if key else None
            return jsonify({"jobs": jobs().list(found.name if found else current_account())})
        return jsonify({"jobs": jobs().list(current_account())})

    # ── API: account switch ───────────────────────────────────────────────────

    @app.post("/api/account/switch")
    @require_token
    def api_account_switch():
        data = request.get_json(silent=True) or {}
        key = data.get("account")
        reg = registry()
        if reg.pinned:
            return json_error("This dashboard is fixed to one account", 400)
        found = reg.find(key) if isinstance(key, str) else None
        if found is None:
            return json_error("Unknown account", 404)
        resp = make_response(jsonify({"status": "ok", "account": found.key, "label": found.label}))
        set_account_cookie(resp, found.key)
        return resp

    # ── API: renew watch ──────────────────────────────────────────────────────

    @app.post("/api/watch/renew")
    @require_token
    def api_watch_renew():
        """Renew the Gmail watch (Gmail expires watches after 7 days)."""
        ctx = current_ctx()
        topic = ctx.cfg.get("pubsub", {}).get("topic_name", "")
        if not topic:
            return jsonify({"error": "pubsub.topic_name not configured"}), 400

        try:
            client = GmailClient(_credentials(ctx.cfg))
            manager = WatchManager(client, ctx.db, topic)
            reg = manager.register()
            if not ctx.db.get_last_history_id():
                ctx.db.save_history_id(reg.history_id)
            return jsonify({
                "status": "renewed",
                "history_id": reg.history_id,
                "expires": reg.expiration_ms,
            })
        except Exception as exc:
            logger.error("Watch renewal failed: %s", exc)
            return jsonify({"error": user_message(exc)}), 500

    # ── Webhook: Pub/Sub push ─────────────────────────────────────────────────

    @app.post("/webhook/pubsub")
    def webhook_pubsub():
        """
        Receive Gmail Pub/Sub push notifications.

        Returns 200 to acknowledge (prevents Pub/Sub retry).
        Returns 400 for empty / non-JSON payloads.
        Returns 401 for unverified JWT (Pub/Sub will retry).
        """
        audience = os.getenv("PUBSUB_AUDIENCE") or os.getenv("CLOUD_RUN_SERVICE_URL", "")
        if audience:
            if not verify_pubsub_token(
                request.headers.get("Authorization"),
                audience,
                expected_email=os.getenv("PUBSUB_SERVICE_ACCOUNT") or None,
            ):
                logger.warning("Webhook: JWT verification failed — rejecting")
                return Response("Unauthorized", status=401)
        elif os.getenv("K_SERVICE"):
            logger.warning("Webhook: PUBSUB_AUDIENCE is not set — push requests are NOT verified")

        body = request.get_json(silent=True)
        if not body:
            logger.warning("Webhook: empty or non-JSON body")
            return Response("Bad Request", status=400)

        try:
            notification = PubSubNotification.from_request_body(body)
        except ValueError as exc:
            logger.warning("Webhook: malformed notification — %s", exc)
            # Return 200 to acknowledge (Pub/Sub won't retry malformed messages usefully)
            return jsonify({"status": "ignored", "reason": str(exc)})

        # Push notifications always belong to the dashboard's own account.
        ctx = registry().get(None)
        triage_engine = None
        try:
            client = GmailClient(_credentials(ctx.cfg))
            unsub_engine = UnsubscribeEngine(
                db=ctx.db,
                min_email_count=1,
                blocked_senders=ctx.cfg.get("blocked_senders", []),
            )
            triage_engine = _build_triage_engine(ctx.db, ctx.cfg)

            handler = WebhookHandler(
                client=client,
                db=ctx.db,
                unsub_engine=unsub_engine,
                triage_engine=triage_engine,
                pending_actions_path=ctx.cfg["output"]["pending_actions_path"],
            )
            return jsonify(handler.handle(notification))

        except Exception as exc:
            logger.error("Webhook processing error: %s", exc, exc_info=True)
            # Return 200 so Pub/Sub doesn't retry endlessly on persistent errors.
            return jsonify({"status": "error", "reason": user_message(exc)})
        finally:
            if triage_engine is not None:
                triage_engine.close()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def start_scan(
    app: Flask,
    account: Optional[str],
    *,
    execute_trusted: bool = False,
    on_finish=None,
) -> tuple[str, bool]:
    """
    Submit a scan job for an account (dashboard button, scheduler, tray).

    With execute_trusted, actions matching trusted rules are carried out; the
    rules engine only allows that when confirm_first is off.

    Returns (job_id, created) as JobRunner.submit does.
    """
    ctx = app.config["ACCOUNTS"].get(account)

    def job(progress):
        client = GmailClient(_credentials(ctx.cfg, interactive=False))
        result = run_scan(ctx.cfg, dry_run=not execute_trusted, progress=progress,
                          client=client, db=ctx.db)
        return result.to_dict()

    return app.config["JOBS"].submit(ctx.name, "scan", job, on_finish=on_finish)


def set_account_cookie(resp, key: str) -> None:
    resp.set_cookie(ACCOUNT_COOKIE, key, httponly=True, samesite="Lax",
                    max_age=365 * 24 * 3600)


def _credentials(cfg: dict, interactive: Optional[bool] = None):
    """
    Saved credentials for an account. Desktop mode and background jobs never
    open an interactive consent flow: they raise SignInRequired instead.
    """
    if interactive is None:
        interactive = not is_desktop_mode()
    auth_cfg = cfg.get("auth", {})
    args = (
        auth_cfg.get("credentials_path", "config/credentials.json"),
        auth_cfg.get("token_path", "config/token.json"),
    )
    if interactive:
        return get_credentials(*args)
    return get_credentials(*args, interactive=False)


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ago(iso: Optional[str]) -> str:
    """'2024-05-16T10:00:00+00:00' → '3 hours ago'."""
    if not iso:
        return "never"
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - when).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


_build_triage_engine = build_triage_engine  # backwards-compatible name


# ──────────────────────────────────────────────────────────────────────────────
# Entry point (local dev)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", 8080)), debug=False)


__all__ = ["create_app", "invalidate_contexts", "set_account_cookie", "start_scan", "key_for", "PipelineError"]
