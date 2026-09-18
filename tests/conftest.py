"""Database fixtures.

A scratch database is created once per session and dropped at the end. Each
test runs inside a transaction that is rolled back, so tests are isolated
without paying for schema setup every time.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any, TypeVar

import psycopg
import pytest

from saddlebag.backends.postgres.sqltext import as_sql
from saddlebag.embed import DEFAULT_EMBED_MODEL

ADMIN_DSN = os.environ.get(
    "BAG_TEST_DSN", "postgresql://saddlebag:saddlebag@localhost:5433/saddlebag"
)

SKIP_REASON = (
    "Postgres is not reachable at %s. Start it with `docker compose up -d` "
    "(and make sure Docker itself is running)." % ADMIN_DSN
)


T = TypeVar("T")


def found(value: T | None) -> T:
    """The value of an `X | None` the test has just caused to exist.

    The lookups here return None for a real miss - no such entry, no run
    recorded, recording not enabled - and a test that has just written the
    thing is asserting it is there. Saying so at the lookup names that
    failure where it happens, rather than as an AttributeError on the
    field access after it.
    """
    assert value is not None, "expected a value, got None"
    return value


def one(cur: psycopg.Cursor[Any]) -> Any:
    """The single row of a query the caller has already decided returns one.

    `fetchone()` is typed `Row | None` because a query may match nothing.
    Asserting here names the real failure - the row this test wrote is not
    there - instead of a NoneType error at whatever unpacks it.
    """
    row = cur.fetchone()
    assert row is not None, "expected exactly one row, got none"
    return row


def scalar(cur: psycopg.Cursor[Any]) -> Any:
    """The one value of a one-row, one-column query.

    `fetchone()` is typed `Row | None` because a query may match nothing, and
    a test writing `.fetchone()[0]` has already decided this one matches.
    Saying so here makes that an assertion with a message instead of a
    `NoneType is not subscriptable` twenty lines from whatever actually went
    wrong - which is the same reason the row is asserted rather than cast.
    """
    return one(cur)[0]


def _server_is_up() -> bool:
    try:
        with psycopg.connect(ADMIN_DSN, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="session")
def db_dsn() -> Iterator[str]:
    if not _server_is_up():
        pytest.skip(SKIP_REASON)

    name = f"saddlebag_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(as_sql(f'create database "{name}"'))
    try:
        yield ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = %s",
                (name,),
            )
            admin.execute(as_sql(f'drop database if exists "{name}"'))


@pytest.fixture
def conn(db_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection whose work is rolled back when the test ends."""
    with psycopg.connect(db_dsn) as c:
        yield c
        c.rollback()


@pytest.fixture(scope="session")
def live_dsn() -> Iterator[str]:
    """A SECOND scratch database, for tests that must commit.

    The CLI, MCP, and hook tests open their own connections through
    open_session(), so their setup has to be committed to be visible. If they
    shared `db_dsn`, a committed migration would make the migration tests
    ("nothing pending") pass or fail depending on test order.
    """
    if not _server_is_up():
        pytest.skip(SKIP_REASON)

    name = f"saddlebag_live_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(as_sql(f'create database "{name}"'))
    try:
        yield ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = %s",
                (name,),
            )
            admin.execute(as_sql(f'drop database if exists "{name}"'))


@pytest.fixture(autouse=True)
def _reset_live_db(request: pytest.FixtureRequest) -> Iterator[None]:
    """Commit-based tests share one database; reset it between them.

    `live_dsn` is session-scoped and never rolled back (see its docstring),
    so every test that commits through it - CLI, MCP, hook tests - leaves
    its rows behind for the next one. Two tests that happen to use the same
    sample data can then see each other's leftovers. Truncate before any
    test that asked for `live_dsn`, and leave everything else untouched.
    """
    if "live_dsn" not in request.fixturenames:
        yield
        return
    dsn = request.getfixturevalue("live_dsn")
    with psycopg.connect(dsn, autocommit=True) as c:
        if scalar(c.execute("select to_regclass('public.entries')")) is not None:
            c.execute(
                # entry_events and events go too: they are written by the
                # record and events tests through the same shared database,
                # and one test's leftover events are another's phantom
                # session to extract.
                #
                # ingest_runs, ingest_settings and memory_settings are not
                # named here - they cascade from `principals` via their own
                # foreign key (e.g. `ingest_runs.owner_id references
                # principals(id)`), which this truncate's `cascade` follows.
                # A future migration that dropped that FK would silently
                # break isolation between tests with no test going red, so
                # if one of these tables ever stops cascading, add it here
                # explicitly rather than assuming it still does. access_log
                # and injection_log are named explicitly for the same reason
                # as the others in this list, although they too cascade from
                # `principals`.
                "truncate access_log, injection_log, collection_members, "
                "collections, entry_events, events, extract_jobs, "
                "record_settings, entries, principals "
                "restart identity cascade"
            )
    yield


@pytest.fixture(autouse=True)
def _no_shared_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test ever builds the real embedder through the shared accessor.

    `services.search.find` builds one on demand when the caller does not
    supply an embedder, memoising it in a module-level dict. That is right in
    production and wrong in a test suite: it would make a run depend on a
    ~130MB model download, and the cache would carry one test's embedder into
    the next.

    Replacing the cache with a fresh dict per test kills the leak, and
    seeding it with None for the default model means an unspecified embedder
    behaves like an uninstalled one - the two-tier degradation. Tests that
    want a semantic tier pass their own stub explicitly, which never touches
    this cache.
    """
    monkeypatch.setattr(
        "saddlebag.services.search._EMBEDDERS", {DEFAULT_EMBED_MODEL: None}
    )
