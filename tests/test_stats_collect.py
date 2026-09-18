"""collect() degrades per section."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.config import load
from saddlebag.domain import CollectionQuery, Kind
from saddlebag.services import kb
from saddlebag.services import stats as st

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


def test_collect_on_an_empty_store_has_every_section(
    store: PostgresStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    owner = store.ensure_principal("brandon")
    got = st.collect(store, owner.id, "demo", load(), now=datetime.now(timezone.utc))
    for name in st.SECTIONS:
        assert not isinstance(getattr(got, name), st.Unavailable), name
    assert isinstance(got.injection, st.Injected)
    assert got.injection.budget_fraction is None  # no knowledge base


def test_a_zero_budget_is_a_null_fraction_rather_than_a_dead_section(
    store: PostgresStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`BAG_MAX_CHARS=0` must not divide by zero.

    The fraction goes through `kb.Budget.fraction`, whose zero guard names
    this caller: `max_chars` is user config, and a status line must never
    be the thing that raises. Computing it here instead turned the whole
    injection section into `unavailable (ZeroDivisionError)`.
    """
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("BAG_MAX_CHARS", "0")
    owner = store.ensure_principal("brandon")
    config = load()
    assert config.max_chars == 0
    kb.create(
        store,
        owner.id,
        slug="demo",
        title="demo",
        project="demo",
        query=CollectionQuery(kinds=[Kind.RULE]),
    )

    got = st.collect(store, owner.id, "demo", config, now=datetime.now(timezone.utc))

    assert isinstance(got.injection, st.Injected)
    assert got.injection.budget_fraction == 0.0


def test_a_section_that_raises_becomes_unavailable_and_the_rest_survive(
    store: PostgresStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conn: psycopg.Connection[Any],
) -> None:
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    owner = store.ensure_principal("brandon")

    def broken(*a: object, **k: object) -> None:
        conn.execute("select * from no_such_table")  # aborts the transaction

    monkeypatch.setattr(store, "access_summary", broken)
    got = st.collect(store, owner.id, "demo", load(), now=datetime.now(timezone.utc))
    assert isinstance(got.retrieval, st.Unavailable)
    assert "UndefinedTable" in got.retrieval.reason
    # The sections collected after the broken one still answer: each runs in
    # its own savepoint, so the aborted statement costs one line.
    assert not isinstance(got.store, st.Unavailable)
    assert not isinstance(got.recent, st.Unavailable)
