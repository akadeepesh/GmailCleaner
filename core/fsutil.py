"""
GmailCleaner — small filesystem helpers shared by the GUI-facing modules.

Every file the dashboard writes (settings overrides, secrets fallback,
uploaded credentials, OAuth tokens) goes through atomic_write_text so a crash
or a concurrent reader never sees a half-written file.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional, Union


def atomic_write_text(
    path: Union[str, Path],
    text: str,
    *,
    mode: Optional[int] = None,
    encoding: str = "utf-8",
) -> Path:
    """
    Write text to path atomically (temp file in the same dir + os.replace).

    Args:
        path:  Destination file. Parent directories are created.
        text:  File contents.
        mode:  Optional permission bits (e.g. 0o600), applied to the temp file
               before it is moved into place so the secret is never readable
               by others, even briefly.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target
