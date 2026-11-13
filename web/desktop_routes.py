"""
GmailCleaner — routes that only exist in the desktop app.

  POST /api/app/quit            {"force": bool}  quit (asks first if a job is running)
  POST /api/app/open-logs       open the log folder in the file manager
  POST /api/updates/dismiss     hide the "new version" banner for this version
  POST /api/schedule/pause      {"paused": bool} pause / resume automatic scans
  POST /api/settings/autostart  {"enabled": bool} "Start when I log in"

All return 404 outside desktop mode (Cloud Run, `python -m web.app`), and go
through the same secret + CSRF checks as every other POST.

The desktop package is imported lazily: it isn't in the Docker images.
"""

import logging
import os
import subprocess
import sys
import threading
from functools import wraps
from pathlib import Path

from flask import Flask, abort, current_app, jsonify, request

from core import updates
from core.paths import app_home
from core.scheduler import PAUSED_KEY
from web.common import is_desktop, jobs, json_error, require_token

logger = logging.getLogger(__name__)


def desktop_only(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_desktop():
            abort(404)
        return f(*args, **kwargs)
    return decorated


def state():
    return current_app.config["STATE"]


def autostart_info() -> dict:
    from desktop import autostart

    supported = autostart.is_supported()
    enabled = False
    if supported:
        try:
            enabled = autostart.is_enabled()
        except OSError:
            logger.warning("Could not read the autostart setting", exc_info=True)
    return {"supported": supported, "enabled": enabled}


def log_dir() -> Path:
    home = app_home()
    return (home if home is not None else Path(".")) / "logs"


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])  # noqa: S603, S607
    else:
        subprocess.Popen(["xdg-open", str(path)])  # noqa: S603, S607


def register_desktop_routes(app: Flask) -> None:

    @app.post("/api/app/quit")
    @require_token
    @desktop_only
    def api_quit():
        on_quit = app.config.get("ON_QUIT")
        if on_quit is None:
            return json_error("Quit isn't available here", 400)
        data = request.get_json(silent=True) or {}
        if jobs().any_active() and data.get("force") is not True:
            return json_error("A scan is running. Quit anyway?", 409, running=True)
        # Quit after this response has been sent.
        threading.Timer(0.5, on_quit).start()
        return jsonify({"status": "quitting"})

    @app.post("/api/app/open-logs")
    @require_token
    @desktop_only
    def api_open_logs():
        try:
            app.config.get("OPEN_FOLDER", open_folder)(log_dir())
        except OSError as exc:
            logger.warning("Could not open the log folder: %s", exc)
            return json_error(f"Couldn't open the folder. It is at: {log_dir()}", 500)
        return jsonify({"status": "ok"})

    @app.post("/api/updates/dismiss")
    @require_token
    @desktop_only
    def api_updates_dismiss():
        updates.dismiss(state())
        return jsonify({"status": "ok"})

    @app.post("/api/schedule/pause")
    @require_token
    @desktop_only
    def api_schedule_pause():
        paused = (request.get_json(silent=True) or {}).get("paused")
        if not isinstance(paused, bool):
            return json_error("paused must be true or false", 400)
        state().set(PAUSED_KEY, paused)
        logger.info("Automatic scans %s from the dashboard", "paused" if paused else "resumed")
        return jsonify({"status": "ok", "paused": paused})

    @app.post("/api/settings/autostart")
    @require_token
    @desktop_only
    def api_autostart():
        from desktop import autostart

        enabled = (request.get_json(silent=True) or {}).get("enabled")
        if not isinstance(enabled, bool):
            return json_error("enabled must be true or false", 400)
        if not autostart.is_supported():
            return json_error("Starting at log-in is available in the installed app.", 400)
        try:
            now = autostart.set_enabled(enabled)
        except OSError as exc:
            logger.warning("Could not change the autostart setting: %s", exc)
            return json_error("Couldn't change this setting on your computer.", 500)
        return jsonify({"status": "ok", "enabled": now})
