"""
GmailCleaner — "a new version is available" check for the desktop app.

At most once every 24 hours (and only when `updates.check` is on) the app
asks GitHub for the latest release:

    GET https://api.github.com/repos/akadeepesh/GmailCleaner/releases/latest

with a generic User-Agent (GmailCleaner/<version>) and nothing else: no
account, no machine id. The answer is cached in the app state; the dashboard
reads only that cache, so pages never wait on the network. There is no
auto-update: the banner just links to the release page.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from packaging.version import InvalidVersion, Version

from core.state import AppState
from version import __version__

logger = logging.getLogger(__name__)

REPO = "akadeepesh/GmailCleaner"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
CHECK_EVERY = timedelta(hours=24)
TIMEOUT = 5.0
STATE_KEY = "updates"

HttpGet = Callable[..., Any]


def _parse(tag: str) -> Optional[Version]:
    try:
        return Version(str(tag).strip().lstrip("vV"))
    except InvalidVersion:
        return None


def maybe_check(
    state: AppState,
    *,
    enabled: bool = True,
    current: str = __version__,
    http_get: Optional[HttpGet] = None,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """
    Refresh the cached latest release if the last check is over 24h old.

    Returns the cached info ({latest, url, checked_at}) after the call, or
    None when checks are disabled. Network errors are logged and the check is
    retried after the normal interval (so an offline machine isn't hammered).
    """
    if not enabled:
        return None
    now = now or datetime.now(timezone.utc)
    cached = state.get(STATE_KEY) or {}
    checked_at = _parse_time(cached.get("checked_at"))
    if checked_at is not None and now - checked_at < CHECK_EVERY:
        return cached

    info = {"checked_at": now.isoformat(timespec="seconds"),
            "latest": cached.get("latest"), "url": cached.get("url")}
    try:
        if http_get is None:
            import httpx
            http_get = httpx.get
        resp = http_get(
            LATEST_URL,
            headers={"User-Agent": f"GmailCleaner/{current}",
                     "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
            follow_redirects=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            if _parse(data.get("tag_name", "")) is not None:
                info["latest"] = str(data["tag_name"]).lstrip("vV")
                info["url"] = _safe_url(data.get("html_url"))
        elif resp.status_code != 404:   # 404 = no releases yet
            logger.info("Update check: GitHub answered HTTP %s", resp.status_code)
    except Exception as exc:
        logger.info("Update check failed (%s); will try again tomorrow", type(exc).__name__)

    def apply(data: dict) -> None:
        merged = dict(data.get(STATE_KEY) or {})
        merged.update(info)
        data[STATE_KEY] = merged
    return state.update(apply)[STATE_KEY]


def banner(state: AppState, *, enabled: bool = True, current: str = __version__) -> Optional[dict]:
    """{version, url} when a newer, not-dismissed release is cached; else None."""
    if not enabled:
        return None
    cached = state.get(STATE_KEY) or {}
    latest, mine = _parse(cached.get("latest") or ""), _parse(current)
    if latest is None or mine is None or latest <= mine:
        return None
    if cached.get("dismissed") == cached.get("latest"):
        return None
    return {"version": cached["latest"], "url": cached.get("url") or RELEASES_PAGE}


def dismiss(state: AppState) -> None:
    """Hide the banner until an even newer release appears."""
    def apply(data: dict) -> None:
        cached = dict(data.get(STATE_KEY) or {})
        cached["dismissed"] = cached.get("latest")
        data[STATE_KEY] = cached
    state.update(apply)


def _safe_url(url: Any) -> str:
    """Only ever link to this repo's GitHub pages."""
    if isinstance(url, str) and url.startswith(f"https://github.com/{REPO}/"):
        return url
    return RELEASES_PAGE


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
