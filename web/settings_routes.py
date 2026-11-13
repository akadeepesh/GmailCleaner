"""
GmailCleaner — the Settings page.

Routes:
  GET  /settings                  — settings grouped into plain-language cards
  POST /api/settings              — save a partial {dotted.key: value} update
  POST /api/settings/gemini-key   — store or remove the Gemini key (keychain)
  POST /api/settings/test-gemini  — check a key without saving it
  POST /api/settings/credentials  — upload your own Desktop-app OAuth client

Only whitelisted keys are accepted (core/user_settings.py), values are type-
and range-checked, and changes are written to the user overrides file, never
to settings.yaml. Settings are shared by all accounts.

On a headless server (Cloud Run) settings come from settings.yaml and env
vars, so the page is read-only and every POST returns 403.
"""

import logging

from flask import Flask, jsonify, render_template, request

from ai.gemini import GEMINI_MODEL, check_api_key
from auth.oauth import resolve_client_config, save_user_client_config, user_client_path
from core.paths import is_headless
from core.scheduler import PAUSED_KEY
from core.secrets import get_gemini_key, key_hint, set_gemini_key
from core.settings import save_user_overrides
from core.user_settings import SettingsValidationError, current_values
from web.common import current_ctx, is_desktop, json_error, require_token
from web.desktop_routes import autostart_info

logger = logging.getLogger(__name__)

_READ_ONLY = "Settings on this server come from settings.yaml and environment variables."


def register_settings_routes(app: Flask) -> None:

    @app.get("/settings")
    @require_token
    def settings_page():
        cfg = current_ctx().cfg
        return render_template(
            "settings.html",
            active="settings",
            values=current_values(cfg),
            gemini=key_hint(),
            read_only=is_headless(),
            user_client=user_client_path().is_file(),
            has_client=resolve_client_config() is not None,
            paused=bool(app.config["STATE"].get(PAUSED_KEY, False)),
            autostart=autostart_info() if is_desktop() else {"supported": False, "enabled": False},
        )

    @app.post("/api/settings")
    @require_token
    def api_settings():
        if is_headless():
            return json_error(_READ_ONLY, 403)
        changes = request.get_json(silent=True)
        if not isinstance(changes, dict) or not changes:
            return json_error("Nothing to save", 400)
        try:
            save_user_overrides(changes)
        except SettingsValidationError as exc:
            return jsonify({"error": "Some settings need fixing", "errors": exc.errors}), 400

        from web.app import invalidate_contexts

        invalidate_contexts(app)
        return jsonify({"status": "ok", "values": current_values(current_ctx_fresh())})

    @app.post("/api/settings/gemini-key")
    @require_token
    def api_gemini_key():
        if is_headless():
            return json_error(_READ_ONLY, 403)
        data = request.get_json(silent=True) or {}
        key = data.get("key")
        if key is not None and not isinstance(key, str):
            return json_error("The key must be text", 400)
        if key and (len(key) > 200 or any(c.isspace() for c in key.strip())):
            return json_error("That doesn't look like a Gemini key", 400)
        stored_in = set_gemini_key(key)
        return jsonify({"status": "ok", "stored_in": stored_in, **key_hint()})

    @app.post("/api/settings/test-gemini")
    @require_token
    def api_test_gemini():
        data = request.get_json(silent=True) or {}
        key = data.get("key") or get_gemini_key()
        if not isinstance(key, str):
            return json_error("The key must be text", 400)
        model = current_ctx().cfg.get("triage", {}).get("model") or GEMINI_MODEL
        ok, message = check_api_key(key.strip(), model=model)
        return jsonify({"ok": ok, "message": message})

    @app.post("/api/settings/credentials")
    @require_token
    def api_credentials():
        if is_headless():
            return json_error(_READ_ONLY, 403)
        data = request.get_json(silent=True) or {}
        client = data.get("client")
        try:
            save_user_client_config(client)
        except ValueError as exc:
            return json_error(str(exc), 400)
        logger.info("Saved a user-supplied OAuth client")
        return jsonify({"status": "ok"})


def current_ctx_fresh():
    """The current account's context rebuilt after a settings change."""
    from flask import g

    g.pop("gc_ctx", None)
    return current_ctx().cfg
