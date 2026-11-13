"""
GmailCleaner — desktop launcher.

    python -m desktop [--home DIR] [--port N] [--no-browser] [--background]

Starts the dashboard on 127.0.0.1 with waitress, a system tray icon, the
automatic-scan scheduler and the new-mail poller, then opens the dashboard in
the default browser. Every launch gets a fresh random secret, delivered to
the browser once through ?token= (which sets an HttpOnly cookie), so other
local programs and websites can't drive the app. All data lives in the app
home:

    Windows  %LOCALAPPDATA%\\GmailCleaner
    macOS    ~/Library/Application Support/GmailCleaner
    Linux    ~/.local/share/GmailCleaner

Only one copy runs per user: launching again opens the running app.
--background (used by "Start when I log in") starts in the tray without
opening the browser. Quit from the tray, the sidebar, or with Ctrl+C.
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
QUIT_WAIT_SECONDS = 30
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
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:   # a windowed build has no console
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        handlers.append(console)
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        handlers=handlers, force=True)
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3", "httpx", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def say(message: str) -> None:
    """Print to the console if there is one (never to the log file)."""
    if sys.stdout is not None:
        try:
            print(message, flush=True)
        except OSError:
            pass


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
    parser.add_argument("--background", action="store_true",
                        help="Start in the tray without opening the browser (used at log-in)")
    parser.add_argument("--no-tray", action="store_true", help="Don't show a tray icon")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


# ──────────────────────────────────────────────────────────────────────────────
# The running app
# ──────────────────────────────────────────────────────────────────────────────

class DesktopApp:
    """Owns the server, background services and tray; the tray's controller."""

    def __init__(self, home: Path, port: int, secret: str) -> None:
        self.home = home
        self.port = port
        self.secret = secret
        self.base_url = f"http://127.0.0.1:{port}/"
        self.launch_url = f"{self.base_url}?token={secret}"
        self.app = None
        self.server = None
        self.services = None
        self.tray = None
        self.instance = None
        self._server_thread: Optional[threading.Thread] = None
        self._quit_lock = threading.Lock()
        self._quitting = False
        self.done = threading.Event()

    # ── Startup ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        from waitress import create_server

        from desktop.services import Services
        from web.app import create_app

        self.app = create_app()
        self.app.config["ON_QUIT"] = self.quit
        self.server = create_server(self.app, host="127.0.0.1", port=self.port,
                                    threads=8, ident="GmailCleaner")
        self._server_thread = threading.Thread(target=self.server.run, name="gc-waitress", daemon=True)
        self._server_thread.start()
        self.services = Services(self.app, notify=self.notify)
        self.services.start()

    # ── Tray controller ───────────────────────────────────────────────────────

    def open_browser(self, extra: str = "") -> bool:
        url = self.launch_url + extra
        opened = webbrowser.open(url)
        if not opened:
            say(f"\nOpen this link in your browser:\n  {url}\n")
        return opened

    def scan_now(self) -> None:
        self.services.scan_now()

    @property
    def paused(self) -> bool:
        return self.services.paused if self.services else False

    def toggle_pause(self) -> None:
        self.services.set_paused(not self.services.paused)
        if self.tray:
            self.tray.refresh()

    @property
    def autostart_supported(self) -> bool:
        from desktop import autostart

        return autostart.is_supported()

    @property
    def autostart_enabled(self) -> bool:
        from desktop import autostart

        try:
            return autostart.is_supported() and autostart.is_enabled()
        except OSError:
            return False

    def toggle_autostart(self) -> None:
        from desktop import autostart

        if not autostart.is_supported():
            return
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except OSError:
            logger.warning("Could not change the autostart setting", exc_info=True)
        if self.tray:
            self.tray.refresh()

    def request_quit(self) -> None:
        """Tray Quit: ask in the browser first if a scan is running."""
        if self.app is not None and self.app.config["JOBS"].any_active():
            self.open_browser("&confirm_quit=1")
            return
        self.quit()

    def notify(self, title: str, message: str) -> None:
        if self.tray is not None:
            self.tray.notify(title, message)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def quit(self) -> None:
        """Stop everything. Safe to call more than once and from any thread."""
        with self._quit_lock:
            if self._quitting:
                return
            self._quitting = True
        logger.info("Shutting down…")
        try:
            if self.services is not None:
                self.services.stop()
            if self.app is not None:
                jobs = self.app.config["JOBS"]
                if jobs.any_active():
                    logger.info("Waiting up to %d seconds for the running scan to finish…",
                                QUIT_WAIT_SECONDS)
                    jobs.shutdown(timeout=QUIT_WAIT_SECONDS)
            if self.server is not None:
                self.server.close()
        finally:
            if self.instance is not None:
                self.instance.release()
            if self.tray is not None:
                self.tray.stop()
            self.done.set()

    def wait(self) -> None:
        """Block the calling thread until quit; Ctrl+C quits."""
        try:
            while not self.done.is_set():
                self.done.wait(0.5)
        except KeyboardInterrupt:
            self.quit()


def run_tray(desktop: DesktopApp, tray) -> None:
    """
    macOS needs the tray loop on the main thread. Elsewhere it runs in a
    thread so the main thread stays free for Ctrl+C.
    """
    def loop():
        try:
            tray.run()
        except Exception as exc:
            logger.warning("The tray icon stopped (%s); GmailCleaner keeps running.", exc)
            desktop.tray = None

    if sys.platform == "darwin":
        loop()
        desktop.wait()
    else:
        threading.Thread(target=loop, name="gc-tray", daemon=True).start()
        desktop.wait()


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    home = Path(args.home or os.getenv("GMAILCLEANER_HOME") or default_home()).expanduser().resolve()

    from desktop.instance import SingleInstance

    instance = SingleInstance(home)
    if not instance.acquire():
        url = instance.running_url()
        if url is None:
            say("GmailCleaner is already starting. Try again in a few seconds.")
            return 1
        if args.background or args.no_browser:
            say("GmailCleaner is already running.")
        else:
            webbrowser.open(url)
            say("GmailCleaner is already running; opened it in your browser.")
        return 0

    port = args.port or pick_port()
    secret = prepare_environment(home, port)
    setup_logging(home, args.log_level)

    desktop = DesktopApp(home, port, secret)
    desktop.instance = instance
    try:
        desktop.start()
    except BaseException:
        instance.release()
        raise
    instance.write_runtime(port, secret)
    logger.info("GmailCleaner is running at %s (data folder: %s).", desktop.base_url, home)

    if not (args.no_tray):
        from desktop.tray import try_create

        desktop.tray = try_create(desktop)
    if desktop.tray is None:
        say("Press Ctrl+C to quit.")

    if not (args.no_browser or args.background):
        desktop.open_browser()

    if desktop.tray is not None:
        run_tray(desktop, desktop.tray)
    else:
        desktop.wait()
    desktop.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
