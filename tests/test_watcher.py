"""
Tests for pubsub/watcher.py

Covers:
  - register(): calls watch_inbox on INBOX and persists the registration
  - register(): Gmail API errors surface as RuntimeError
  - ensure_active(): returns the stored registration when it is not expiring
  - ensure_active(): re-registers when the watch expires within 24h or is missing
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from pubsub.watcher import WatchManager
from store.db import Database

TOPIC = "projects/p/topics/t"


def _expiry_ms(delta: timedelta) -> str:
    return str(int((datetime.now(timezone.utc) + delta).timestamp() * 1000))


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "w.db"))


@pytest.fixture
def client():
    c = MagicMock()
    c.watch_inbox.return_value = {"historyId": "555", "expiration": _expiry_ms(timedelta(days=7))}
    return c


def test_register_persists(client, db):
    reg = WatchManager(client, db, TOPIC).register()
    client.watch_inbox.assert_called_once_with(topic_name=TOPIC, label_ids=["INBOX"])
    assert reg.history_id == "555"
    assert db.get_watch_registration()["topic_name"] == TOPIC


def test_register_api_error_raises_runtime_error(client, db):
    resp = MagicMock(status=403, reason="forbidden")
    client.watch_inbox.side_effect = HttpError(resp=resp, content=b"denied")
    with pytest.raises(RuntimeError, match="Pub/Sub Publisher"):
        WatchManager(client, db, TOPIC).register()


def test_ensure_active_keeps_fresh_watch(client, db):
    manager = WatchManager(client, db, TOPIC)
    manager.register()
    client.watch_inbox.reset_mock()

    reg = manager.ensure_active()

    client.watch_inbox.assert_not_called()
    assert reg.history_id == "555"


def test_ensure_active_renews_expiring_watch(client, db):
    client.watch_inbox.return_value = {"historyId": "1", "expiration": _expiry_ms(timedelta(hours=2))}
    manager = WatchManager(client, db, TOPIC)
    manager.register()
    client.watch_inbox.return_value = {"historyId": "2", "expiration": _expiry_ms(timedelta(days=7))}

    reg = manager.ensure_active()

    assert reg.history_id == "2"
    assert client.watch_inbox.call_count == 2


def test_ensure_active_registers_when_missing(client, db):
    reg = WatchManager(client, db, TOPIC).ensure_active()
    assert reg.history_id == "555"
