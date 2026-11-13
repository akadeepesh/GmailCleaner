"""
GmailCleaner — desktop launcher.

    python -m desktop [--home DIR] [--port N] [--no-browser]

Starts the dashboard on 127.0.0.1 with waitress and opens it in the default
browser. Every launch gets a fresh random secret, delivered to the browser
once through ?token= (which sets an HttpOnly cookie), so other local programs
and websites can't drive the app. All data lives in the app home:

    Windows  %LOCALAPPDATA%\\GmailCleaner
    macOS    ~/Library/Application Support/GmailCleaner
    Linux    ~/.local/share/GmailCleaner

Ctrl+C quits. (The tray icon and scheduled scans arrive in a later phase.)
"""

import argparse
import logging
import os
import secrets
import socket
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFERRED_PORT = 8765
logger = logging.getLogger("gmailcleaner.desktop")


def default_home() -> Path:
    import platformdirs

    return Path(platformdirs.user_data_path("GmailCleaner", appauthor=False))


def pick_port(preferred: int = PREFERRED_PORT) -> int:
    """The preferred port if it is free on 127.0.0.1, else a random free one."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("No free local port")


def setup_logging(home: Path, level: str = "INFO") -> None:
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "gmailcleaner.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        handlers=[file_handler, console], force=True)
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def prepare_environment(home: Path, port: int) -> str:
    """Point the app at its home and create this launch's secret. Returns the secret."""
    home.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    os.environ["GMAILCLEANER_HOME"] = str(home)
    os.environ["DASHBOARD_SECRET"] = secret
    os.environ["GMAILCLEANER_PORT"] = str(port)
    return secret


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m desktop", description="Run GmailCleaner locally.")
    parser.add_argument("--home", help="Folder for GmailCleaner's data (default: your user data folder)")
    parser.add_argument("--port", type=int, default=0, help=f"Local port (default: {PREFERRED_PORT} or a free one)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    home = Path(args.home or os.getenv("GMAILCLEANER_HOME") or default_home()).expanduser().resolve()
    port = args.port or pick_port()
    secret = prepare_environment(home, port)
    setup_logging(home, args.log_level)

    from waitress import create_server

    from web.app import create_app

    app = create_app()
    server = create_server(app, host="127.0.0.1", port=port, threads=8, ident="GmailCleaner")
    thread = threading.Thread(target=server.run, name="gc-waitress", daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}/"
    launch_url = f"{base}?token={secret}"
    logger.info("GmailCleaner is running at %s (data folder: %s). Press Ctrl+C to quit.", base, home)
    opened = False if args.no_browser else webbrowser.open(launch_url)
    if not opened:
        # Printed only to this console, never logged to the file.
        print(f"\nOpen this link in your browser:\n  {launch_url}\n", flush=True)

    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        logger.info("Shutting down…")
    finally:
        jobs = app.config["JOBS"]
        if jobs.any_active():
            logger.info("Waiting up to 30 seconds for the running scan to finish…")
            jobs.shutdown(timeout=30)
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
