"""
GmailCleaner — small persistent app state for the desktop app.

<home>/state.json holds the few things the app must remember between
launches that are not settings:

    last_scheduled_run   {account_key: ISO time}   when each scheduled scan last started
    scheduler_paused     bool                      "Pause automatic scans" from the tray
    updates              {checked_at, latest, url, dismissed}

Every write is atomic (temp file + os.replace). With path=None (legacy CLI,
Cloud Run) the state lives in memory only, so nothing new is written there.
"""

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from core.fsutil import atomic_write_text
from core.paths import app_home

logger = logging.getLogger(__name__)

STATE_NAME = "state.json"


def state_path() -> Optional[Path]:
    home = app_home()
    return home / STATE_NAME if home is not None else None


class AppState:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._memory: dict = {}

    def load(self) -> dict:
        with self._lock:
            if self.path is None:
                return copy.deepcopy(self._memory)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError) as exc:
                logger.warning("Ignoring unreadable state file %s: %s", self.path, exc)
                return {}
            return data if isinstance(data, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def update(self, fn: Callable[[dict], None]) -> dict:
        """Read, let fn modify the dict in place, write back. Returns the new state."""
        with self._lock:
            data = self.load()
            fn(data)
            if self.path is None:
                self._memory = copy.deepcopy(data)
            else:
                atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True))
            return data

    def set(self, key: str, value: Any) -> None:
        self.update(lambda d: d.__setitem__(key, value))
