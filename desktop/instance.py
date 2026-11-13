"""
GmailCleaner — only one copy of the desktop app runs per user.

The running app holds <home>/app.lock and writes <home>/runtime.json
({port, secret, pid}, readable only by the user). A second launch can't get
the lock, so it reads runtime.json, opens the browser on the running app
and exits.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout

from core.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

LOCK_NAME = "app.lock"
RUNTIME_NAME = "runtime.json"


class SingleInstance:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.runtime_path = self.home / RUNTIME_NAME
        self._lock = FileLock(str(self.home / LOCK_NAME), thread_local=False)
        self._held = False

    def acquire(self) -> bool:
        """True if this process is now the only instance."""
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            return False
        self._held = True
        return True

    def write_runtime(self, port: int, secret: str) -> None:
        atomic_write_text(
            self.runtime_path,
            json.dumps({"port": port, "secret": secret, "pid": os.getpid()}),
            mode=0o600,
        )

    def read_runtime(self) -> Optional[dict]:
        """The running instance's {port, secret, pid}, or None if missing/invalid."""
        try:
            data = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        port, secret = data.get("port"), data.get("secret")
        if not isinstance(port, int) or not (0 < port < 65536) or not isinstance(secret, str) or not secret:
            return None
        return data

    def running_url(self) -> Optional[str]:
        data = self.read_runtime()
        if data is None:
            return None
        return f"http://127.0.0.1:{data['port']}/?token={data['secret']}"

    def release(self) -> None:
        if not self._held:
            return
        self.runtime_path.unlink(missing_ok=True)
        self._lock.release()
        self._held = False
