"""
Tests for core/updates.py

Covers:
  - At most one request per 24h; disabled → no request
  - Generic User-Agent, 5 s timeout, no redirects
  - Banner only for a newer, non-dismissed version; dismiss
  - Release links restricted to this repo; bad tags, 404 and network errors
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from core import updates
from core.state import AppState

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
RELEASE = {"tag_name": "v1.2.0",
           "html_url": "https://github.com/akadeepesh/GmailCleaner/releases/tag/v1.2.0"}


def http(status=200, data=None, exc=None):
    get = MagicMock()
    if exc:
        get.side_effect = exc
    else:
        get.return_value = MagicMock(status_code=status, json=MagicMock(return_value=data or RELEASE))
    return get


def test_checks_and_caches():
    state, get = AppState(None), http()
    info = updates.maybe_check(state, current="1.0.1", http_get=get, now=NOW)
    assert info["latest"] == "1.2.0" and info["url"].endswith("/v1.2.0")
    url = get.call_args.args[0]
    kwargs = get.call_args.kwargs
    assert url == updates.LATEST_URL
    assert kwargs["headers"]["User-Agent"] == "GmailCleaner/1.0.1"
    assert kwargs["timeout"] == 5.0 and kwargs["follow_redirects"] is False
    # Within 24h: no new request.
    updates.maybe_check(state, current="1.0.1", http_get=get, now=NOW + timedelta(hours=23))
    assert get.call_count == 1
    updates.maybe_check(state, current="1.0.1", http_get=get, now=NOW + timedelta(hours=25))
    assert get.call_count == 2


def test_disabled_makes_no_request():
    get = http()
    assert updates.maybe_check(AppState(None), enabled=False, http_get=get, now=NOW) is None
    get.assert_not_called()


def test_banner_only_when_newer():
    state = AppState(None)
    updates.maybe_check(state, current="1.0.1", http_get=http(), now=NOW)
    assert updates.banner(state, current="1.0.1") == {"version": "1.2.0", "url": RELEASE["html_url"]}
    assert updates.banner(state, current="1.2.0") is None
    assert updates.banner(state, current="2.0.0") is None
    assert updates.banner(state, current="1.0.1", enabled=False) is None


def test_dismiss_until_newer_release():
    state = AppState(None)
    updates.maybe_check(state, current="1.0.1", http_get=http(), now=NOW)
    updates.dismiss(state)
    assert updates.banner(state, current="1.0.1") is None
    newer = http(data={"tag_name": "v1.3.0", "html_url": RELEASE["html_url"]})
    updates.maybe_check(state, current="1.0.1", http_get=newer, now=NOW + timedelta(days=2))
    assert updates.banner(state, current="1.0.1")["version"] == "1.3.0"


def test_foreign_link_replaced():
    state = AppState(None)
    updates.maybe_check(state, http_get=http(data={"tag_name": "v9.0.0", "html_url": "https://evil.example/x"}),
                        now=NOW)
    assert updates.banner(state, current="1.0.1")["url"] == updates.RELEASES_PAGE


def test_bad_tag_ignored():
    state = AppState(None)
    updates.maybe_check(state, http_get=http(data={"tag_name": "nightly"}), now=NOW)
    assert updates.banner(state, current="1.0.1") is None


def test_errors_recorded_as_checked():
    state, get = AppState(None), http(exc=ConnectionError("offline"))
    info = updates.maybe_check(state, http_get=get, now=NOW)
    assert info["checked_at"] and info["latest"] is None
    updates.maybe_check(state, http_get=get, now=NOW + timedelta(hours=1))
    assert get.call_count == 1   # an offline machine isn't retried every tick


def test_no_releases_yet():
    state = AppState(None)
    updates.maybe_check(state, http_get=http(status=404), now=NOW)
    assert updates.banner(state, current="1.0.1") is None
