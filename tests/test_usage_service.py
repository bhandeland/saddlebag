"""Usage logging is opt-in per call and can never cost the caller."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import AccessRecord, Entry, Kind, Origin, Principal, Query, new_id
from saddlebag.services import usage
from saddlebag.services.search import find
from tests.conftest import scalar

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def _entry(store: PostgresStore, owner: Principal, title: str) -> Entry:
    return store.put_entry(
        Entry(
            id=new_id(),
            kind=Kind.NOTE,
            title=title,
            body="pgvector cosine distance",
            owner_id=owner.id,
            project="demo",
            origin=Origin.HUMAN,
        )
    )


def _count(conn: psycopg.Connection[Any]) -> int:
    return scalar(conn.execute("select count(*) from access_log"))


def test_find_without_source_logs_nothing(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    _entry(store, owner, "vectors")
    find(store, owner.id, Query(text="pgvector"), embedder=None)
    assert _count(conn) == 0


def test_find_with_source_logs_tier_hits_and_length(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    e = _entry(store, owner, "vectors")
    hits = find(
        store,
        owner.id,
        Query(text="pgvector"),
        embedder=None,
        source="cli",
        session_id="s1",
        log_project="demo",
    )
    assert [h.entry.id for h in hits] == [e.id]
    row = conn.execute(
        "select source, op, tier, hits, query_len, session_id, project, entry_ids "
        "from access_log"
    ).fetchall()
    assert row == [("cli", "search", "exact", 1, 8, "s1", "demo", [e.id])]


def test_an_empty_search_logs_tier_none(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    find(store, owner.id, Query(text="zzqqxx"), embedder=None, source="mcp")
    assert conn.execute("select tier, hits from access_log").fetchall() == [("none", 0)]


def test_a_listing_query_logs_a_null_tier(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    _entry(store, owner, "vectors")
    find(store, owner.id, Query(text=None), embedder=None, source="cli")
    assert conn.execute("select tier, query_len from access_log").fetchall() == [
        (None, None)
    ]


def test_a_failing_log_insert_does_not_fail_or_poison_the_search(
    store: PostgresStore,
    owner: Principal,
    conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e = _entry(store, owner, "vectors")

    def broken(record: AccessRecord) -> None:
        # A real SQL failure, so the transaction genuinely aborts and only
        # the savepoint can save it.
        conn.execute("select * from no_such_table")

    monkeypatch.setattr(store, "log_access", broken)
    notes: list[str] = []
    monkeypatch.setattr(usage, "_default_note", notes.append)
    hits = find(store, owner.id, Query(text="pgvector"), embedder=None, source="cli")
    assert [h.entry.id for h in hits] == [e.id]
    # The connection is still usable: the savepoint rolled back only itself.
    assert scalar(conn.execute("select count(*) from entries")) == 1
    assert notes and "UndefinedTable" in notes[0]


def test_session_id_comes_from_the_claude_code_variable() -> None:
    assert usage.session_id_from_env({"CLAUDE_CODE_SESSION_ID": "abc"}) == "abc"
    assert usage.session_id_from_env({"CLAUDE_SESSION_ID": "abc"}) is None
    assert usage.session_id_from_env({"CLAUDE_CODE_SESSION_ID": ""}) is None
