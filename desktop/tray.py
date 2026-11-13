"""
GmailCleaner — the system tray icon.

Menu:
  Open GmailCleaner        (default: click / double-click the icon)
  Scan now                 every signed-in account, suggestions only
  Pause automatic scans    checkbox
  Start when I log in      checkbox (installed app only)
  Quit

pystray and Pillow are imported only here, and only when the tray starts, so
the CLI, the web server and the Docker images never need them. If the tray
can't start (no tray on this desktop, missing packages) the launcher keeps
running without it.
"""

import logging
from typing import Optional, Protocol

from core.paths import resource_path

logger = logging.getLogger(__name__)

ICON_PATH = "desktop/assets/icon.png"


class TrayController(Protocol):
    def open_browser(self) -> None: ...
    def scan_now(self) -> None: ...
    def toggle_pause(self) -> None: ...
    def toggle_autostart(self) -> None: ...
    def request_quit(self) -> None: ...

    @property
    def paused(self) -> bool: ...

    @property
    def autostart_supported(self) -> bool: ...

    @property
    def autostart_enabled(self) -> bool: ...


def load_icon_image():
    from PIL import Image

    path = resource_path(ICON_PATH)
    if path.is_file():
        return Image.open(path)
    from desktop.icon import draw

    return draw(64)


class Tray:
    def __init__(self, controller: TrayController) -> None:
        self.controller = controller
        self.icon = None

    def build(self) -> None:
        """Create the icon. Raises if pystray/Pillow or a tray are unavailable."""
        import pystray

        c = self.controller
        autostart_label = (
            "Start when I log in" if c.autostart_supported
            else "Start when I log in (installed app only)"
        )
        menu = pystray.Menu(
            pystray.MenuItem("Open GmailCleaner", lambda: c.open_browser(), default=True),
            pystray.MenuItem("Scan now", lambda: c.scan_now()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause automatic scans", lambda: c.toggle_pause(),
                             checked=lambda item: c.paused),
            pystray.MenuItem(autostart_label, lambda: c.toggle_autostart(),
                             checked=lambda item: c.autostart_enabled,
                             enabled=c.autostart_supported),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: c.request_quit()),
        )
        self.icon = pystray.Icon("GmailCleaner", load_icon_image(), "GmailCleaner", menu)

    def run(self) -> None:
        """Run the tray's event loop (blocks until stop())."""
        self.icon.run()

    def notify(self, title: str, message: str) -> None:
        icon = self.icon
        if icon is None or not getattr(icon, "HAS_NOTIFICATION", False):
            return
        icon.notify(message, title)

    def refresh(self) -> None:
        if self.icon is not None:
            self.icon.update_menu()

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                logger.debug("Tray stop failed", exc_info=True)


def try_create(controller: TrayController) -> Optional[Tray]:
    """A ready Tray, or None (with a logged reason) when no tray is possible."""
    tray = Tray(controller)
    try:
        tray.build()
    except Exception as exc:
        logger.warning("No tray icon (%s: %s). GmailCleaner keeps running without it.",
                       type(exc).__name__, exc)
        return None
    return tray
