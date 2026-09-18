"""Session starts are logged by both harness paths, and only when asked."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import CollectionQuery, Entry, Kind, Origin, Principal, new_id
from saddlebag.services import context, kb
from tests.conftest import one, scalar

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def _kb_with_rule(store: PostgresStore, owner: Principal) -> Entry:
    rule = store.put_entry(
        Entry(
            id=new_id(),
            kind=Kind.RULE,
            title="Use spaced hyphens",
            summary="Never em dashes.",
            body="because",
            owner_id=owner.id,
            project="demo",
            origin=Origin.HUMAN,
        )
    )
    kb.create(
        store,
        owner.id,
        slug="demo",
        title="demo",
        query=CollectionQuery(project="demo"),
    )
    return rule


def test_injection_with_log_writes_a_row(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    rule = _kb_with_rule(store, owner)
    got = context.injection(
        store,
        owner.id,
        "demo",
        16000,
        log=context.InjectionLog(harness="claude-code", session_id="s1"),
    )
    assert got.entry_ids == (rule.id,)
    row = one(
        conn.execute(
            "select project, harness, session_id, found, rules, notes, chars, "
            "entry_ids from injection_log"
        )
    )
    assert row == ("demo", "claude-code", "s1", True, 1, 0, len(got.text), [rule.id])


def test_injection_without_log_writes_nothing(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    _kb_with_rule(store, owner)
    context.injection(store, owner.id, "demo", 16000)
    assert scalar(conn.execute("select count(*) from injection_log")) == 0


def test_a_missing_knowledge_base_is_logged_as_not_found(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    context.injection(
        store, owner.id, "nope", 16000, log=context.InjectionLog("cursor", None)
    )
    assert one(conn.execute("select found, rules from injection_log")) == (False, 0)


def test_an_over_budget_knowledge_base_logs_nothing(
    store: PostgresStore, owner: Principal, conn: psycopg.Connection[Any]
) -> None:
    _kb_with_rule(store, owner)
    with pytest.raises(kb.RulesExceedBudget):
        context.injection(
            store, owner.id, "demo", 10, log=context.InjectionLog("claude-code", "s1")
        )
    assert scalar(conn.execute("select count(*) from injection_log")) == 0
