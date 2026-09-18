"""The stats reads. Every one is checked with a second principal's rows
present, because a fresh fixture guarantees their absence and so cannot
catch a missing owner filter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import (
    AccessRecord,
    Entry,
    IngestTrigger,
    InjectionRecord,
    Kind,
    MemoryTrigger,
    Origin,
    Principal,
    TranscriptTrigger,
    new_id,
)

pytestmark = pytest.mark.db

LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def me(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


@pytest.fixture
def other(store: PostgresStore) -> Principal:
    return store.ensure_principal("someone-else")


def _entry(store, owner, *, kind=Kind.NOTE, origin=Origin.HUMAN, project="demo"):
    return store.put_entry(
        Entry(
            id=new_id(),
            kind=kind,
            title=f"t {new_id()}",
            summary="s" if kind == Kind.RULE else None,
            body="b",
            owner_id=owner.id,
            project=project,
            origin=origin,
        )
    )


def _read(
    store,
    owner,
    *,
    session="s1",
    op="search",
    tier="exact",
    hits=1,
    ids=(),
    ms=10,
    source="cli",
    project="demo",
):
    store.log_access(
        AccessRecord(
            owner_id=owner.id,
            source=source,
            op=op,
            hits=hits,
            entry_ids=ids,
            elapsed_ms=ms,
            project=project,
            session_id=session,
            query_len=5 if op == "search" else None,
            tier=tier if op == "search" else None,
        )
    )


def _inject(store, owner, *, session="s1", ids=(), chars=400, project="demo"):
    store.log_injection(
        InjectionRecord(
            owner_id=owner.id,
            project=project,
            harness="claude-code",
            session_id=session,
            found=True,
            rules=len(ids),
            notes=0,
            chars=chars,
            budget_chars=16000,
            entry_ids=ids,
        )
    )


def test_entry_counts_split_by_kind_origin_and_project(store, me, other) -> None:
    _entry(store, me, kind=Kind.RULE)
    _entry(store, me, origin=Origin.EXTRACTED)
    _entry(store, me, project="elsewhere")
    old = _entry(store, me)
    new = _entry(store, me)
    store.set_superseded(old.id, new.id, me.id)
    _entry(store, other)
    got = store.entry_counts(me.id, "demo")
    assert got.live == 4
    assert got.project_live == 3
    assert got.by_kind == {"rule": 1, "note": 3}
    assert got.by_origin == {"human": 3, "extracted": 1}
    assert got.superseded == 1
    assert got.projects == 2


def test_access_summary(store, me, other) -> None:
    _read(store, me, tier="exact", ms=10)
    _read(store, me, tier="semantic", ms=30, session="s2")
    _read(store, me, tier="none", hits=0, ms=20)
    _read(store, me, op="get", ms=5, source="mcp")
    _read(store, me, project="elsewhere")
    _read(store, other)
    got = store.access_summary(me.id, LONG_AGO, "demo")
    assert got.reads == 4
    assert got.by_source == {"cli": 3, "mcp": 1}
    assert got.by_op == {"search": 3, "get": 1}
    assert (got.searches, got.search_hits) == (3, 2)
    assert got.tiers == {"exact": 1, "semantic": 1, "none": 1}
    # Searches only: the 5ms `get` is not part of what the recall line
    # calls p50, or a session that read more single-row entries would
    # report faster searches.
    assert got.p50_ms == 20
    assert got.sessions == 2
    assert store.access_summary(me.id, LONG_AGO, None).reads == 5


def test_access_summary_respects_the_window(store, me) -> None:
    _read(store, me)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    got = store.access_summary(me.id, future, None)
    assert got.reads == 0 and got.p50_ms is None
    assert got.first_at is not None  # "since" is about all time, not the window


def test_follow_through_counts_injected_ids_opened_later_in_that_session(
    store, me, other
) -> None:
    a, b, c = new_id(), new_id(), new_id()
    _inject(store, me, session="s1", ids=(a, b), chars=800)
    _read(store, me, session="s1", op="get", ids=(a,))
    _read(store, me, session="s2", op="get", ids=(b,))  # different session
    _inject(store, me, session=None, ids=(c,))  # no session: not countable
    _inject(store, other, session="s9", ids=(a,))  # other principal
    _read(store, other, session="s1", op="get", ids=(b,))  # other principal
    got = store.injection_summary(me.id, LONG_AGO, None)
    # One session: the row with no session id names no session to count,
    # and the other principal's is not ours.
    assert got.sessions == 1
    assert (got.injected, got.opened) == (2, 1)
    # The means still average rows - two of them, both mine.
    assert got.mean_tokens == pytest.approx((200 + 100) / 2)


def test_one_session_injected_several_times_is_one_session(store, me) -> None:
    """Claude Code fires SessionStart on startup, resume, clear AND compact.

    Counting rows called three injections three sessions and, worse,
    counted the same session's entry ids three times in `injected` while
    `opened` counted each once - dragging follow-through toward zero by how
    often the user compacted.
    """
    a, b = new_id(), new_id()
    for _ in range(3):
        _inject(store, me, session="s1", ids=(a, b))
    _read(store, me, session="s1", op="get", ids=(a,))
    got = store.injection_summary(me.id, LONG_AGO, None)
    assert got.sessions == 1
    assert (got.injected, got.opened) == (2, 1)


def test_the_session_ratio_counts_injected_sessions_that_then_read(
    store, me, other
) -> None:
    """Both halves come from `injection_log`, so they are one population.

    A session that read without ever being injected - every cursor and
    opencode session, whose reads carry no session id - is outside the
    ratio rather than inflating its denominator.
    """
    _inject(store, me, session="s1", ids=())
    _inject(store, me, session="s2", ids=())
    _read(store, me, session="s1")
    _read(store, me, session="s3")  # read, never injected: outside the ratio
    _inject(store, other, session="s4", ids=())
    _read(store, other, session="s4")
    got = store.injection_summary(me.id, LONG_AGO, None)
    assert (got.sessions_read, got.sessions) == (1, 2)
    assert got.sessions_read <= got.sessions


def test_a_read_before_its_injection_is_not_follow_through(store, me) -> None:
    _read(store, me, session="s1")
    _inject(store, me, session="s1", ids=())
    got = store.injection_summary(me.id, LONG_AGO, None)
    assert (got.sessions_read, got.sessions) == (0, 1)


def test_since_is_scoped_to_the_project_the_counts_are(store, me) -> None:
    """`min(at)` carries the project predicate the rest of the query does.

    Owner-wide, a project logged for the first time today reads as
    `7d: 0 reads` on an install that has been logging for months - the
    "a fresh install looks like nobody recalls anything" failure the
    `since` spelling exists to prevent, one level down.
    """
    _read(store, me, project="elsewhere")
    _inject(store, me, project="elsewhere")
    assert store.access_summary(me.id, LONG_AGO, "demo").first_at is None
    assert store.injection_summary(me.id, LONG_AGO, "demo").first_at is None
    assert store.access_summary(me.id, LONG_AGO, "elsewhere").first_at is not None
    assert store.injection_summary(me.id, LONG_AGO, "elsewhere").first_at is not None
    assert store.access_summary(me.id, LONG_AGO, None).first_at is not None


def test_recent_entries_newest_first_and_owner_scoped(store, me, other) -> None:
    first = _entry(store, me)
    second = _entry(store, me)
    _entry(store, other)
    assert [e.id for e in store.recent_entries(me.id, 10)] == [second.id, first.id]
    assert len(store.recent_entries(me.id, 1)) == 1


def test_pipeline_counts_on_an_empty_store(store, me) -> None:
    got = store.pipeline_counts(me.id, LONG_AGO)
    assert got.transcript_sessions == 0
    assert got.last_memory_run is None and got.last_ingest_run is None
    assert got.last_transcript_run is None and got.extracted_since == 0


def test_pipeline_counts_last_runs_are_owner_scoped(store, me, other) -> None:
    store.start_memory_run(other.id, "demo", MemoryTrigger.MANUAL)
    store.start_ingest_run(other.id, "demo", IngestTrigger.MANUAL)
    store.start_transcript_run(other.id, "demo", TranscriptTrigger.MANUAL)
    got = store.pipeline_counts(me.id, LONG_AGO)
    assert got.last_memory_run is None
    assert got.last_ingest_run is None
    assert got.last_transcript_run is None
