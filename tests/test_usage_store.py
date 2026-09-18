"""The two usage-log writes. Metadata only - no query text column exists."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import AccessRecord, InjectionRecord, Principal, new_id
from tests.conftest import one

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_log_access_writes_one_row(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    ids = (new_id(), new_id())
    store.log_access(
        AccessRecord(
            owner_id=owner.id,
            source="cli",
            op="search",
            hits=2,
            entry_ids=ids,
            elapsed_ms=12,
            project="demo",
            session_id="s1",
            query_len=9,
            tier="exact",
        )
    )
    row = one(
        conn.execute(
            "select source, op, hits, entry_ids, elapsed_ms, project, "
            "session_id, query_len, tier from access_log"
        )
    )
    assert row == ("cli", "search", 2, list(ids), 12, "demo", "s1", 9, "exact")


def test_access_log_has_no_column_that_could_hold_query_text(
    conn: psycopg.Connection[Any], store: PostgresStore
) -> None:
    cols = {
        r[0]
        for r in conn.execute(
            "select column_name from information_schema.columns "
            "where table_name = 'access_log'"
        )
    }
    assert "query" not in cols and "query_text" not in cols


def test_log_injection_stores_the_token_estimate(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    store.log_injection(
        InjectionRecord(
            owner_id=owner.id,
            project="demo",
            harness="claude-code",
            session_id="s1",
            found=True,
            rules=3,
            notes=1,
            chars=4003,
            budget_chars=16000,
            entry_ids=(new_id(),),
        )
    )
    row = one(
        conn.execute(
            "select project, harness, found, rules, notes, chars, tokens_est, "
            "budget_chars, cardinality(entry_ids) from injection_log"
        )
    )
    assert row == ("demo", "claude-code", True, 3, 1, 4003, 1000, 16000, 1)
