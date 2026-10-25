"""
GmailCleaner — Web Dashboard & Webhook Server
A lightweight Flask application serving two purposes:

  1. Dashboard (GET routes) — review pending actions, inbox stats, run history
  2. Webhook (POST /webhook/pubsub) — receive Gmail Pub/Sub push notifications

Routes:
  GET  /                         — Dashboard overview
  GET  /pending                  — Review, confirm and execute pending actions
  GET  /stats                    — Inbox activity chart (14-day)
  GET  /history                  — Recent run history
  GET  /export/<dataset>.csv     — Inbox health CSV export (summary|senders|actions|runs)
  GET  /health                   — Health check (Cloud Run liveness probe)
  POST /webhook/pubsub           — Pub/Sub push endpoint
  POST /api/confirm              — Confirm or reject a pending action
  POST /api/execute              — Execute all confirmed actions
  POST /api/watch/renew          — Renew the Gmail watch (called daily by Cloud Scheduler)

Security:
  - Dashboard routes require the DASHBOARD_SECRET token, supplied as
    `Authorization: Bearer <token>`, a `?token=` query param (which then sets
    an HttpOnly, SameSite=Strict cookie so navigation keeps working), or that cookie.
  - The webhook verifies the Pub/Sub OIDC JWT when PUBSUB_AUDIENCE is set.
  - All user-supplied data in templates is auto-escaped by Jinja2.
"""

import hmac
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
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
from core.settings import load_config, resolve_config
from engines.triage import TriageEngine
from engines.unsubscribe import UnsubscribeEngine
from gmail.client import GmailClient
from gmail.executor import GmailExecutor, confirmed_items, execute_report
from output.exporter import DATASETS, to_csv
from output.reporter import load_pending_actions, save_pending_actions
from pubsub.watcher import WatchManager
from pubsub.webhook import PubSubNotification, WebhookHandler, verify_pubsub_token
from store.db import Database
from version import __version__

logger = logging.getLogger(__name__)

_TOKEN_COOKIE = "gc_token"


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────

def create_app(config: Optional[dict] = None) -> Flask:
    """
    Flask application factory.

    Args:
        config: Optional raw config dict (as parsed from settings.yaml). When
                omitted, config/settings.yaml is loaded. Environment overrides
                are applied in both cases.

    Returns:
        Configured Flask app ready to serve.
    """
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent),
        static_folder=None,
    )

    # ── Config ────────────────────────────────────────────────────────────────
    cfg = resolve_config(config) if config is not None else load_config()
    app.config["GMAILCLEANER"] = cfg
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)

    if not os.getenv("DASHBOARD_SECRET"):
        logger.warning(
            "DASHBOARD_SECRET is not set — dashboard authentication is DISABLED. "
            "Only run like this on a trusted local machine."
        )

    # ── Services ──────────────────────────────────────────────────────────────
    db = Database(cfg["database"]["path"])
    app.config["DB"] = db

    @app.context_processor
    def _inject_globals():
        return {"version": __version__, "account": cfg.get("account")}

    # ── Routes ───────────────────────────────────────────────────────────────
    _register_routes(app, db, cfg)

    return app


# ──────────────────────────────────────────────────────────────────────────────
# Auth decorator
# ──────────────────────────────────────────────────────────────────────────────

def _require_token(f):
    """
    Decorator that checks for a valid dashboard secret token.

    Accepts the token as:
      - Authorization: Bearer <token>
      - ?token=<token> query parameter (also sets the session cookie)
      - the gc_token cookie

    If DASHBOARD_SECRET is not set, authentication is disabled
    (suitable for local development only).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = os.getenv("DASHBOARD_SECRET", "")
        if not secret:
            return f(*args, **kwargs)  # Auth disabled

        auth_header = request.headers.get("Authorization", "")
        header_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        query_token = request.args.get("token", "")
        cookie_token = request.cookies.get(_TOKEN_COOKIE, "")

        if not any(_safe_equals(t, secret) for t in (header_token, query_token, cookie_token)):
            abort(401)

        response = make_response(f(*args, **kwargs))
        if query_token and _safe_equals(query_token, secret):
            response.set_cookie(
                _TOKEN_COOKIE,
                secret,
                httponly=True,
                samesite="Strict",
                secure=request.is_secure
                or request.headers.get("X-Forwarded-Proto", "") == "https",
                max_age=30 * 24 * 3600,
            )
        return response
    return decorated


def _safe_equals(candidate: str, secret: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), secret.encode())


# ──────────────────────────────────────────────────────────────────────────────
# Route registration
# ──────────────────────────────────────────────────────────────────────────────

def _register_routes(app: Flask, db: Database, cfg: dict) -> None:

    def pending_path() -> str:
        return cfg["output"]["pending_actions_path"]

    # ── Health check ──────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        """Cloud Run liveness probe — must return 200 quickly."""
        return jsonify({"status": "ok", "service": "gmailcleaner-dashboard", "version": __version__})

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.get("/")
    @_require_token
    def dashboard():
        return render_template(
            "dashboard.html",
            active="dashboard",
            summary=db.get_dashboard_summary(),
            stats=db.get_inbox_stats(days=7),
            watch_reg=db.get_watch_registration(),
            now=_now_str(),
        )

    # ── Pending actions ───────────────────────────────────────────────────────

    @app.get("/pending")
    @_require_token
    def pending():
        report = load_pending_actions(pending_path(), missing_ok=True)
        if not report:
            report = {"unsubscribe": [], "delete_groups": [], "triage": {}, "summary": {}}
        unsub_ready, delete_ready = confirmed_items(report)
        return render_template(
            "pending.html",
            active="pending",
            report=report,
            ready_count=len(unsub_ready) + len(delete_ready),
            session_path=pending_path(),
            now=_now_str(),
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    @app.get("/stats")
    @_require_token
    def stats():
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
    @_require_token
    def history():
        return render_template(
            "history.html",
            active="history",
            runs=db.get_recent_runs(limit=20),
            now=_now_str(),
        )

    # ── CSV export ────────────────────────────────────────────────────────────

    @app.get("/export/<dataset>.csv")
    @_require_token
    def export_dataset(dataset: str):
        if dataset not in DATASETS:
            abort(404)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return Response(
            to_csv(db, dataset),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="gmailcleaner-{dataset}-{stamp}.csv"'
            },
        )

    # ── API: confirm / reject action ──────────────────────────────────────────

    @app.post("/api/confirm")
    @_require_token
    def api_confirm():
        """
        Set confirmed=true/false on a pending action in pending_actions.json.

        Request body (JSON):
          { "action_type": "unsubscribe"|"delete", "target": "<domain or group_id>", "confirmed": true|false }
        """
        data = request.get_json(silent=True) or {}
        action_type = data.get("action_type")
        target = data.get("target")
        confirmed = bool(data.get("confirmed", True))

        if action_type not in ("unsubscribe", "delete") or not target:
            return jsonify({"error": "action_type (unsubscribe|delete) and target are required"}), 400

        report = load_pending_actions(pending_path(), missing_ok=True)
        if not report:
            return jsonify({"error": "No pending actions found"}), 404

        section = "unsubscribe" if action_type == "unsubscribe" else "delete_groups"
        id_field = "domain" if action_type == "unsubscribe" else "group_id"

        item = next((i for i in report.get(section, []) if i.get(id_field) == target), None)
        if item is None:
            return jsonify({"error": f"Target {target!r} not found"}), 404
        if item.get("executed_at"):
            return jsonify({"error": f"Target {target!r} was already executed"}), 409

        item["confirmed"] = confirmed
        save_pending_actions(report, pending_path())
        return jsonify({"status": "ok", "target": target, "confirmed": confirmed})

    # ── API: execute confirmed actions ────────────────────────────────────────

    @app.post("/api/execute")
    @_require_token
    def api_execute():
        """Execute every confirmed, not-yet-executed action in the pending report."""
        report = load_pending_actions(pending_path(), missing_ok=True)
        if not report:
            return jsonify({"error": "No pending actions found"}), 404

        unsub_items, delete_items = confirmed_items(report)
        if not unsub_items and not delete_items:
            return jsonify({"error": "No confirmed actions to execute"}), 400

        try:
            client = GmailClient(_credentials(cfg))
            with GmailExecutor(client, db) as executor:
                summary = execute_report(executor, db, report)
        except Exception as exc:
            logger.error("Execute failed: %s", exc, exc_info=True)
            return jsonify({"error": str(exc)}), 500
        finally:
            # Persist executed_at stamps even if execution stopped part-way.
            save_pending_actions(report, pending_path())

        return jsonify({
            "status": "executed",
            "unsubscribed": summary.total_unsubscribed,
            "trashed": summary.total_deleted,
            "failed": summary.total_failed,
        })

    # ── API: renew watch ──────────────────────────────────────────────────────

    @app.post("/api/watch/renew")
    @_require_token
    def api_watch_renew():
        """Renew the Gmail watch (Gmail expires watches after 7 days)."""
        topic = cfg.get("pubsub", {}).get("topic_name", "")
        if not topic:
            return jsonify({"error": "pubsub.topic_name not configured"}), 400

        try:
            client = GmailClient(_credentials(cfg))
            manager = WatchManager(client, db, topic)
            reg = manager.register()
            if not db.get_last_history_id():
                db.save_history_id(reg.history_id)
            return jsonify({
                "status": "renewed",
                "history_id": reg.history_id,
                "expires": reg.expiration_ms,
            })
        except Exception as exc:
            logger.error("Watch renewal failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

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

        triage_engine = None
        try:
            client = GmailClient(_credentials(cfg))
            unsub_engine = UnsubscribeEngine(
                db=db,
                min_email_count=1,
                blocked_senders=cfg.get("blocked_senders", []),
            )
            triage_engine = _build_triage_engine(db, cfg)

            handler = WebhookHandler(
                client=client,
                db=db,
                unsub_engine=unsub_engine,
                triage_engine=triage_engine,
                pending_actions_path=pending_path(),
            )
            return jsonify(handler.handle(notification))

        except Exception as exc:
            logger.error("Webhook processing error: %s", exc, exc_info=True)
            # Return 200 so Pub/Sub doesn't retry endlessly on persistent errors.
            return jsonify({"status": "error", "reason": str(exc)})
        finally:
            if triage_engine is not None:
                triage_engine.close()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _credentials(cfg: dict):
    auth_cfg = cfg.get("auth", {})
    return get_credentials(
        auth_cfg.get("credentials_path", "config/credentials.json"),
        auth_cfg.get("token_path", "config/token.json"),
    )


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _build_triage_engine(db: Database, cfg: dict) -> Optional[TriageEngine]:
    """Build a TriageEngine if triage is enabled and GEMINI_API_KEY is set."""
    triage_cfg = cfg.get("triage", {})
    if not triage_cfg.get("enabled", False):
        return None
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        from ai.gemini import GEMINI_MODEL, GeminiClient
        gemini = GeminiClient(api_key=api_key, model=triage_cfg.get("model", GEMINI_MODEL))
        return TriageEngine(
            gemini_client=gemini,
            db=db,
            confidence_threshold=triage_cfg.get("confidence_threshold", 0.75),
        )
    except Exception as exc:
        logger.warning("Could not build triage engine for webhook: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Entry point (local dev)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", 8080)), debug=False)
