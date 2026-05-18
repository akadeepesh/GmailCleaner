"""
GmailCleaner — Web Dashboard & Webhook Server
A lightweight Flask application serving two purposes:

  1. Dashboard (GET routes) — review pending actions, inbox stats, run history
  2. Webhook (POST /webhook/pubsub) — receive Gmail Pub/Sub push notifications

Routes:
  GET  /                         — Dashboard overview
  GET  /pending                  — Review pending actions (unsubscribe + delete groups)
  GET  /stats                    — Inbox activity chart (7-day)
  GET  /history                  — Recent run history
  GET  /health                   — Health check (Cloud Run liveness probe)
  POST /webhook/pubsub           — Pub/Sub push endpoint
  POST /api/confirm              — Confirm or reject a pending action (HTMX-friendly)
  POST /api/watch/renew          — Manually trigger watch renewal

Security:
  - Dashboard routes require a secret token in the Authorization header or
    as a ?token= query param (set via DASHBOARD_SECRET env var).
  - Webhook route verifies the Pub/Sub OIDC JWT before processing.
  - All user-supplied data in templates is auto-escaped by Jinja2.
"""

import json
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    abort,
)

from auth.oauth import get_credentials
from engines.unsubscribe import UnsubscribeEngine
from engines.triage import TriageEngine
from gmail.client import GmailClient
from output.reporter import load_pending_actions
from pubsub.webhook import PubSubNotification, WebhookHandler, verify_pubsub_token
from pubsub.watcher import WatchManager
from store.db import Database

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────

def create_app(config: Optional[dict] = None) -> Flask:
    """
    Flask application factory.

    Args:
        config: Optional config dict to override environment-based settings.

    Returns:
        Configured Flask app ready to serve.
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # ── Config ────────────────────────────────────────────────────────────────
    cfg = config or _load_config()
    app.config["GMAILCLEANER"] = cfg
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", os.urandom(32))

    # ── Services ──────────────────────────────────────────────────────────────
    db_path = cfg.get("database", {}).get("path", "data/gmailcleaner.db")
    db = Database(db_path)
    app.config["DB"] = db

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
      - ?token=<token> query parameter

    If DASHBOARD_SECRET is not set, authentication is disabled
    (suitable for local development only).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = os.getenv("DASHBOARD_SECRET", "")
        if not secret:
            return f(*args, **kwargs)  # Auth disabled

        token = (
            request.headers.get("Authorization", "").replace("Bearer ", "")
            or request.args.get("token", "")
        )
        if token != secret:
            abort(401)
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────────────────────────────────────────
# Route registration
# ──────────────────────────────────────────────────────────────────────────────

def _register_routes(app: Flask, db: Database, cfg: dict) -> None:

    # ── Health check ──────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        """Cloud Run liveness probe — must return 200 quickly."""
        return jsonify({"status": "ok", "service": "gmailcleaner-dashboard"})

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.get("/")
    @_require_token
    def dashboard():
        summary = db.get_dashboard_summary()
        stats = db.get_inbox_stats(days=7)
        watch_reg = db.get_watch_registration()
        return render_template(
            "dashboard.html",
            summary=summary,
            stats=stats,
            watch_reg=watch_reg,
            now=_now_str(),
        )

    # ── Pending actions ───────────────────────────────────────────────────────

    @app.get("/pending")
    @_require_token
    def pending():
        report = load_pending_actions()
        if not report:
            report = {"unsubscribe": [], "delete_groups": [], "triage": {}, "summary": {}}
        return render_template(
            "pending.html",
            report=report,
            now=_now_str(),
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    @app.get("/stats")
    @_require_token
    def stats():
        inbox_stats = db.get_inbox_stats(days=14)
        triage_counts = _get_triage_counts(db)
        sender_stats = _get_sender_stats(db)
        return render_template(
            "stats.html",
            inbox_stats=inbox_stats,
            triage_counts=triage_counts,
            sender_stats=sender_stats,
            now=_now_str(),
        )

    # ── Run history ───────────────────────────────────────────────────────────

    @app.get("/history")
    @_require_token
    def history():
        runs = _get_recent_runs(db, limit=20)
        return render_template(
            "history.html",
            runs=runs,
            now=_now_str(),
        )

    # ── API: confirm / reject action ──────────────────────────────────────────

    @app.post("/api/confirm")
    @_require_token
    def api_confirm():
        """
        Toggle confirmed=true/false on a pending action in pending_actions.json.

        Request body (JSON):
          { "action_type": "unsubscribe"|"delete", "target": "<domain or group_id>", "confirmed": true|false }

        Returns the updated action item so HTMX can swap it in-place.
        """
        data = request.get_json(silent=True) or {}
        action_type = data.get("action_type")
        target = data.get("target")
        confirmed = bool(data.get("confirmed", True))

        if not action_type or not target:
            return jsonify({"error": "action_type and target are required"}), 400

        report = load_pending_actions()
        if not report:
            return jsonify({"error": "No pending actions found"}), 404

        updated = False
        section = "unsubscribe" if action_type == "unsubscribe" else "delete_groups"
        id_field = "domain" if action_type == "unsubscribe" else "group_id"

        for item in report.get(section, []):
            if item.get(id_field) == target:
                item["confirmed"] = confirmed
                updated = True
                break

        if not updated:
            return jsonify({"error": f"Target {target!r} not found"}), 404

        # Write the updated report back to disk.
        from pathlib import Path
        import json
        Path("output/pending_actions.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        return jsonify({"status": "ok", "target": target, "confirmed": confirmed})

    # ── API: renew watch ──────────────────────────────────────────────────────

    @app.post("/api/watch/renew")
    @_require_token
    def api_watch_renew():
        """Manually trigger a Gmail watch renewal."""
        topic = cfg.get("pubsub", {}).get("topic_name", "")
        if not topic:
            return jsonify({"error": "pubsub.topic_name not configured"}), 400

        try:
            creds = get_credentials(
                os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials.json"),
                os.getenv("GOOGLE_TOKEN_PATH", "config/token.json"),
            )
            client = GmailClient(creds)
            manager = WatchManager(client, db, topic)
            reg = manager.register()
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
        Returns 400 for malformed payloads (also acknowledges — we can't fix them).
        Returns 401 for unverified JWT (Pub/Sub will retry).
        """
        # Verify the Google-signed OIDC JWT.
        audience = os.getenv("CLOUD_RUN_SERVICE_URL", "")
        if audience and not verify_pubsub_token(
            request.headers.get("Authorization"), audience
        ):
            logger.warning("Webhook: JWT verification failed — rejecting")
            return Response("Unauthorized", status=401)

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

        try:
            creds = get_credentials(
                os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials.json"),
                os.getenv("GOOGLE_TOKEN_PATH", "config/token.json"),
            )
            client = GmailClient(creds)
            unsub_engine = UnsubscribeEngine(db=db, min_email_count=1)
            triage_engine = _build_triage_engine(db, cfg)

            handler = WebhookHandler(
                client=client,
                db=db,
                unsub_engine=unsub_engine,
                triage_engine=triage_engine,
            )
            result = handler.handle(notification)
            return jsonify(result)

        except Exception as exc:
            logger.error("Webhook processing error: %s", exc, exc_info=True)
            # Return 200 so Pub/Sub doesn't retry endlessly on persistent errors.
            return jsonify({"status": "error", "reason": str(exc)})


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    import yaml
    path = Path("config/settings.yaml")
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _get_triage_counts(db: Database) -> dict:
    """Aggregate triage bucket counts from the DB."""
    buckets = ["NEEDS_REPLY", "FYI", "PROMOTIONS", "JUNK", "UNCLASSIFIED"]
    counts = {}
    with db._connect() as conn:
        for bucket in buckets:
            row = conn.execute(
                "SELECT COUNT(*) FROM triage_results WHERE bucket = ?", (bucket,)
            ).fetchone()
            counts[bucket] = row[0]
    return counts


def _get_sender_stats(db: Database) -> dict:
    """Return top senders and unsubscribe stats."""
    with db._connect() as conn:
        top = conn.execute(
            "SELECT domain, display_name, email_count FROM senders ORDER BY email_count DESC LIMIT 10"
        ).fetchall()
        total_unsub = conn.execute(
            "SELECT COUNT(*) FROM senders WHERE unsubscribed_at IS NOT NULL"
        ).fetchone()[0]
    return {
        "top_senders": [dict(r) for r in top],
        "total_unsubscribed": total_unsub,
    }


def _get_recent_runs(db: Database, limit: int = 20) -> list[dict]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def _build_triage_engine(db: Database, cfg: dict) -> Optional[TriageEngine]:
    """Build a TriageEngine if triage is enabled and GEMINI_API_KEY is set."""
    if not cfg.get("triage", {}).get("enabled", False):
        return None
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        from ai.gemini import GeminiClient
        gemini = GeminiClient(api_key=api_key, model=cfg["triage"].get("model", "gemini-1.5-flash"))
        return TriageEngine(
            gemini_client=gemini,
            db=db,
            confidence_threshold=cfg["triage"].get("confidence_threshold", 0.75),
        )
    except Exception as exc:
        logger.warning("Could not build triage engine for webhook: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Entry point (local dev)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)