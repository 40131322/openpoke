"""Unit tests for structured trigger identity (kind/thread_id) and reply detection."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Make sure the server package is importable from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.services.triggers.store import TriggerStore
from server.services.triggers.service import TriggerService
from server.services.gmail.processing import ProcessedEmail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> TriggerStore:
    return TriggerStore(tmp_path / "triggers.db")


def _make_service(tmp_path: Path) -> TriggerService:
    return TriggerService(_make_store(tmp_path))


def _utc(offset_seconds: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


def _email(
    *,
    id: str = "msg1",
    thread_id: str = "thread1",
    sender: str = "other@example.com",
    timestamp: datetime | None = None,
) -> ProcessedEmail:
    return ProcessedEmail(
        id=id,
        thread_id=thread_id,
        query="",
        subject="Re: hello",
        sender=sender,
        recipient="me@example.com",
        timestamp=timestamp or _utc(),
        label_ids=[],
        clean_text="",
        has_attachments=False,
        attachment_count=0,
        attachment_filenames=[],
    )


# ---------------------------------------------------------------------------
# TriggerStore — schema migration
# ---------------------------------------------------------------------------

class TestStoreMigration:
    def test_new_db_has_kind_and_thread_id(self, tmp_path):
        store = _make_store(tmp_path)
        record = store.insert(
            {
                "agent_name": "Agent",
                "payload": "check it",
                "kind": "checkback",
                "thread_id": "abc123",
                "status": "active",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        )
        fetched = store.fetch_one(record, "Agent")
        assert fetched is not None
        assert fetched.kind == "checkback"
        assert fetched.thread_id == "abc123"

    def test_kind_and_thread_id_default_to_none(self, tmp_path):
        store = _make_store(tmp_path)
        trigger_id = store.insert(
            {
                "agent_name": "Agent",
                "payload": "plain trigger",
                "status": "active",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        )
        fetched = store.fetch_one(trigger_id, "Agent")
        assert fetched is not None
        assert fetched.kind is None
        assert fetched.thread_id is None

    def test_migration_adds_columns_to_existing_db(self, tmp_path):
        """Simulate a pre-migration DB that has all original columns but not kind/thread_id."""
        import sqlite3
        db = tmp_path / "triggers.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE triggers ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "agent_name TEXT NOT NULL,"
            "payload TEXT NOT NULL,"
            "start_time TEXT,"
            "next_trigger TEXT,"
            "recurrence_rule TEXT,"
            "timezone TEXT,"
            "status TEXT NOT NULL DEFAULT 'active',"
            "last_error TEXT,"
            "created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO triggers (agent_name, payload, status, created_at, updated_at)"
            " VALUES ('Agent', 'old trigger', 'active', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Opening the store should migrate the schema without error
        store = TriggerStore(db)
        fetched = store.fetch_one(1, "Agent")
        assert fetched is not None
        assert fetched.kind is None
        assert fetched.thread_id is None


# ---------------------------------------------------------------------------
# TriggerStore — fetch_by_thread / complete_by_thread
# ---------------------------------------------------------------------------

class TestStoreThreadQueries:
    def _insert(self, store: TriggerStore, *, thread_id: str, status: str = "active") -> int:
        return store.insert(
            {
                "agent_name": "Agent",
                "payload": "follow up",
                "kind": "checkback",
                "thread_id": thread_id,
                "status": status,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        )

    def test_fetch_by_thread_returns_active_rows(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store, thread_id="t1")
        self._insert(store, thread_id="t1")
        self._insert(store, thread_id="t2")
        results = store.fetch_by_thread("Agent", "t1")
        assert len(results) == 2
        assert all(r.thread_id == "t1" for r in results)

    def test_fetch_by_thread_excludes_completed(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store, thread_id="t1", status="completed")
        results = store.fetch_by_thread("Agent", "t1")
        assert results == []

    def test_fetch_by_thread_wrong_agent_returns_empty(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store, thread_id="t1")
        assert store.fetch_by_thread("OtherAgent", "t1") == []

    def test_complete_by_thread_marks_active_rows(self, tmp_path):
        store = _make_store(tmp_path)
        id1 = self._insert(store, thread_id="t1")
        id2 = self._insert(store, thread_id="t1")
        count = store.complete_by_thread("Agent", "t1")
        assert count == 2
        assert store.fetch_one(id1, "Agent").status == "completed"
        assert store.fetch_one(id2, "Agent").status == "completed"

    def test_complete_by_thread_ignores_other_threads(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store, thread_id="t1")
        other_id = self._insert(store, thread_id="t2")
        store.complete_by_thread("Agent", "t1")
        assert store.fetch_one(other_id, "Agent").status == "active"

    def test_complete_by_thread_returns_zero_when_nothing_to_cancel(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.complete_by_thread("Agent", "no-such-thread") == 0


# ---------------------------------------------------------------------------
# TriggerService — create_trigger with kind/thread_id
# ---------------------------------------------------------------------------

class TestServiceCreateTrigger:
    def test_kind_and_thread_id_are_stored(self, tmp_path):
        svc = _make_service(tmp_path)
        record = svc.create_trigger(
            agent_name="Agent",
            payload="check email",
            kind="checkback",
            thread_id="thread-xyz",
            start_time=_utc(60).isoformat(),
        )
        assert record.kind == "checkback"
        assert record.thread_id == "thread-xyz"

    def test_omitting_kind_and_thread_id_gives_none(self, tmp_path):
        svc = _make_service(tmp_path)
        record = svc.create_trigger(
            agent_name="Agent",
            payload="generic reminder",
            start_time=_utc(60).isoformat(),
        )
        assert record.kind is None
        assert record.thread_id is None

    def test_dedup_query_via_service(self, tmp_path):
        """Before creating a checkback, ask if one already exists for the thread."""
        svc = _make_service(tmp_path)
        svc.create_trigger(
            agent_name="Agent",
            payload="check email",
            kind="checkback",
            thread_id="thread-abc",
            start_time=_utc(3600).isoformat(),
        )
        existing = svc.fetch_by_thread(agent_name="Agent", thread_id="thread-abc")
        assert len(existing) == 1
        assert existing[0].kind == "checkback"

    def test_complete_by_thread_via_service(self, tmp_path):
        """When a reply arrives, cancel the checkback by thread_id."""
        svc = _make_service(tmp_path)
        svc.create_trigger(
            agent_name="Agent",
            payload="check email",
            kind="checkback",
            thread_id="thread-abc",
            start_time=_utc(3600).isoformat(),
        )
        cancelled = svc.complete_by_thread(agent_name="Agent", thread_id="thread-abc")
        assert cancelled == 1
        remaining = svc.fetch_by_thread(agent_name="Agent", thread_id="thread-abc")
        assert remaining == []


# ---------------------------------------------------------------------------
# Reply detection — _sender_address (pure function)
# ---------------------------------------------------------------------------

class TestSenderAddress:
    def _addr(self, s: str) -> str:
        from server.services.gmail.client import _sender_address
        return _sender_address(s)

    def test_plain_address(self):
        assert self._addr("bob@example.com") == "bob@example.com"

    def test_display_name_format(self):
        assert self._addr("Bob Smith <bob@example.com>") == "bob@example.com"

    def test_case_normalised(self):
        assert self._addr("Bob@Example.COM") == "bob@example.com"

    def test_angle_bracket_case_normalised(self):
        assert self._addr("Bob <BOB@EXAMPLE.COM>") == "bob@example.com"


# ---------------------------------------------------------------------------
# Reply detection — has_inbound_reply_since (mocked fetch_thread)
# ---------------------------------------------------------------------------

class TestHasInboundReplySince:
    SELF = "me@example.com"

    def _call(self, messages, sent_at):
        from server.services.gmail.client import has_inbound_reply_since
        with patch("server.services.gmail.client.fetch_thread", return_value=messages):
            return has_inbound_reply_since("thread1", sent_at, self.SELF)

    def test_reply_from_other_after_sent_at_returns_true(self):
        sent = _utc(-120)
        messages = [_email(sender="other@example.com", timestamp=_utc(-60))]
        assert self._call(messages, sent) is True

    def test_no_messages_returns_false(self):
        assert self._call([], _utc()) is False

    def test_reply_before_sent_at_returns_false(self):
        sent = _utc()
        messages = [_email(sender="other@example.com", timestamp=_utc(-300))]
        assert self._call(messages, sent) is False

    def test_own_message_after_sent_at_returns_false(self):
        sent = _utc(-120)
        messages = [_email(sender=self.SELF, timestamp=_utc(-60))]
        assert self._call(messages, sent) is False

    def test_own_message_display_name_format_returns_false(self):
        sent = _utc(-120)
        messages = [_email(sender=f"My Name <{self.SELF}>", timestamp=_utc(-60))]
        assert self._call(messages, sent) is False

    def test_mixed_thread_only_other_counts(self):
        sent = _utc(-300)
        messages = [
            _email(id="m1", sender=self.SELF, timestamp=_utc(-200)),   # own follow-up
            _email(id="m2", sender="other@example.com", timestamp=_utc(-100)),  # their reply
        ]
        assert self._call(messages, sent) is True

    def test_fetch_thread_failure_returns_false(self):
        from server.services.gmail.client import has_inbound_reply_since
        with patch("server.services.gmail.client.fetch_thread", return_value=[]):
            assert has_inbound_reply_since("thread1", _utc(), self.SELF) is False
