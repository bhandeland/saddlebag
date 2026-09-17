# Usage Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record how saddlebag is used (reads and injections) and show that,
plus store and pipeline health, in a multi-line SessionStart banner and a new
`bag stats` command.

**Architecture:** Two metadata-only log tables (migration 026) are written by
the services every frontend already calls, only when the frontend passes an
explicit `source`. A new `services/stats.py` collects every section
independently (a failing section becomes `Unavailable`) and renders pure
text lines shared by the banner and the CLI.

**Tech Stack:** Python 3.14, psycopg 3, Postgres 18, Typer, FastMCP, pytest.

**Spec:** `docs/superpowers/specs/2026-09-16-usage-stats-design.md` - read it
before any task. Where this plan's prose and a code block disagree, the code
block wins.

## Global Constraints

- Layering: frontends parse and format only; every policy lives in
  `services/`; all SQL lives in `backends/postgres/store.py`; nothing
  outside `session.py`/`backends/` imports psycopg.
- Any f-string SQL goes through `as_sql()` and interpolates only module
  constants; every value is a parameter.
- Timestamps use `clock_timestamp()`, never `now()`.
- Logs store query **length**, never query text.
- A log insert must never fail or poison the caller: wrap it in
  `store.transaction()` (a savepoint inside an open transaction) and swallow
  every exception.
- Nothing is logged unless `source` is passed. Tests, the eval script and
  internal callers pass nothing.
- stdout is sacred: no `print()` (ruff `T20`); diagnostics go to stderr via
  `hookio.debug`.
- Prose, comments, rendered text: spaced hyphen ` - ` or `·`, never an em
  dash.
- Comments explain *why*, at this repo's density.
- `make check` is the only verification command. Never hand-fix anything
  ruff fixes. A green run with `db` skips proves nothing - check
  `docker compose ps` and the skip count.
- Any test whose path reaches `hook.session_start`/`main` or
  `bag hook context` must stub `hookio.spawn_*` (see
  `tests/test_hook_context_cli.py`'s `env` fixture).
- `live_dsn` tests: add `access_log, injection_log` to the truncate in
  `tests/conftest.py::_reset_live_db` (Task 1) - they cascade from
  `principals` anyway, but the conftest comment asks for explicit names when
  in doubt.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/saddlebag/backends/postgres/migrations/026_usage_logs.sql` | create | `access_log`, `injection_log` |
| `src/saddlebag/domain.py` | modify | `AccessRecord`, `InjectionRecord`, `EntryCounts`, `AccessSummary`, `InjectionSummary`, `PipelineCounts` |
| `src/saddlebag/store.py` | modify | Protocol: 2 writes, 5 reads |
| `src/saddlebag/backends/postgres/store.py` | modify | SQL for the above |
| `src/saddlebag/services/usage.py` | create | fail-soft `log_access`, `log_injection`, `session_id_from_env` |
| `src/saddlebag/services/search.py` | modify | `find(source=, session_id=, log_project=)` |
| `src/saddlebag/services/kb.py` | modify | `Rendered.entry_ids` |
| `src/saddlebag/services/context.py` | modify | `Injection.entry_ids`, `injection(log=)`, `banner(got, stats=None)` |
| `src/saddlebag/services/stats.py` | create | `collect`, `render`, `to_dict` |
| `src/saddlebag/cli.py` | modify | wire `search`/`get`/`handoff latest`/`hook context`; new `stats` command |
| `src/saddlebag/mcp_server.py` | modify | wire `recall`/`get_entry`; fix session id env name |
| `src/saddlebag/agents/claude_code/hook.py` | modify | log injection, collect stats, pass to banner |
| `CLAUDE.md` | modify | "Usage statistics" section |
| tests | create | `test_usage_store.py`, `test_usage_service.py`, `test_usage_wiring.py`, `test_injection_log.py`, `test_stats_store.py`, `test_stats_render.py`, `test_stats_collect.py`, `test_stats_cli.py`; extend `test_banner.py`, `test_hook.py` |

---

### Task 1: Migration 026 and the log writes

**Files:**
- Create: `src/saddlebag/backends/postgres/migrations/026_usage_logs.sql`
- Modify: `src/saddlebag/domain.py` (append records)
- Modify: `src/saddlebag/store.py` (Protocol, new `# usage` block before `transaction`)
- Modify: `src/saddlebag/backends/postgres/store.py` (new `# ---- usage ----` block)
- Modify: `tests/conftest.py` (`_reset_live_db` truncate list)
- Test: `tests/test_usage_store.py`

**Interfaces:**
- Produces:
  - `domain.AccessRecord(owner_id: UUID, source: str, op: str, hits: int, entry_ids: tuple[UUID, ...], elapsed_ms: int, project: str | None = None, session_id: str | None = None, query_len: int | None = None, tier: str | None = None)` - frozen, slots.
  - `domain.InjectionRecord(owner_id: UUID, project: str, harness: str, session_id: str | None, found: bool, rules: int, notes: int, chars: int, budget_chars: int, entry_ids: tuple[UUID, ...])` - frozen, slots; property `tokens_est -> int` = `chars // 4`.
  - `Store.log_access(record: AccessRecord) -> None`
  - `Store.log_injection(record: InjectionRecord) -> None`

- [ ] **Step 1: Write the migration**

```sql
-- Usage metadata: what saddlebag was asked and what it handed out. Written by
-- the services only when a frontend passes an explicit `source`, so tests,
-- the eval script and internal callers never appear here.
--
-- METADATA ONLY. The query's length is stored, never its text, and no entry
-- content is copied - ids only. That is what lets these tables sit outside
-- the per-project record opt-in (services/record.py): they describe
-- saddlebag's own use, not the user's work.
--
-- Nothing prunes these. Rows are small (one per read, one per session
-- start) and `bag events prune` deliberately does not touch them.

create table access_log (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text,
  session_id text,
  source text not null check (source in ('cli', 'mcp', 'hook')),
  op text not null check (op in ('search', 'get', 'handoff')),
  query_len int,
  -- The tier that produced the returned hits, 'none' for an empty search,
  -- null for a listing query or a non-search op.
  tier text check (tier in ('exact', 'semantic', 'fuzzy', 'none')),
  hits int not null,
  entry_ids uuid[] not null default '{}',
  elapsed_ms int not null,
  at timestamptz not null default clock_timestamp()
);

create index access_log_at_idx on access_log (owner_id, at);
-- The follow-through join: which injected ids did this session open later.
create index access_log_session_idx on access_log (owner_id, session_id)
  where session_id is not null;

create table injection_log (
  id uuid primary key,
  owner_id uuid not null references principals(id),
  project text not null,
  harness text not null,
  session_id text,
  found boolean not null,
  rules int not null,
  notes int not null,
  chars int not null,
  -- chars / 4, an estimate and always labelled as one. Stored rather than
  -- derived so a future better estimate does not rewrite history.
  tokens_est int not null,
  budget_chars int not null,
  entry_ids uuid[] not null default '{}',
  at timestamptz not null default clock_timestamp()
);

create index injection_log_at_idx on injection_log (owner_id, at);
```

- [ ] **Step 2: Add the domain records** (append to `domain.py`)

```python
@dataclass(frozen=True, slots=True)
class AccessRecord:
    """One read of the store by a frontend - see 026_usage_logs.sql.

    `query_len`, never the query: these rows sit outside the record opt-in
    precisely because they carry no content.
    """

    owner_id: UUID
    source: str
    op: str
    hits: int
    entry_ids: tuple[UUID, ...]
    elapsed_ms: int
    project: str | None = None
    session_id: str | None = None
    query_len: int | None = None
    tier: str | None = None


@dataclass(frozen=True, slots=True)
class InjectionRecord:
    """One session start that reached the database."""

    owner_id: UUID
    project: str
    harness: str
    session_id: str | None
    found: bool
    rules: int
    notes: int
    chars: int
    budget_chars: int
    entry_ids: tuple[UUID, ...]

    @property
    def tokens_est(self) -> int:
        # Four characters a token is the usual rough figure for English
        # prose. Labelled an estimate everywhere it is shown.
        return self.chars // 4
```

- [ ] **Step 3: Write the failing store test** (`tests/test_usage_store.py`)

```python
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
```

- [ ] **Step 4: Run it, expect FAIL** (`AttributeError: ... log_access`)

Run: `uv run pytest tests/test_usage_store.py -q`

- [ ] **Step 5: Implement the Protocol methods and SQL**

In `store.py`, add before `transaction`:

```python
    # usage (026) - metadata only; see services/usage.py for the fail-soft
    # wrapper every caller goes through.
    def log_access(self, record: AccessRecord) -> None: ...
    def log_injection(self, record: InjectionRecord) -> None: ...
```

(import `AccessRecord`, `InjectionRecord` from `saddlebag.domain`.)

In `backends/postgres/store.py`, add a block before `try_advisory_lock`:

```python
    # ---------------- usage ----------------

    def log_access(self, record: AccessRecord) -> None:
        with self._cur() as cur:
            cur.execute(
                "insert into access_log (id, owner_id, project, session_id, "
                "source, op, query_len, tier, hits, entry_ids, elapsed_ms) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    new_id(),
                    record.owner_id,
                    record.project,
                    record.session_id,
                    record.source,
                    record.op,
                    record.query_len,
                    record.tier,
                    record.hits,
                    list(record.entry_ids),
                    record.elapsed_ms,
                ),
            )

    def log_injection(self, record: InjectionRecord) -> None:
        with self._cur() as cur:
            cur.execute(
                "insert into injection_log (id, owner_id, project, harness, "
                "session_id, found, rules, notes, chars, tokens_est, "
                "budget_chars, entry_ids) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    new_id(),
                    record.owner_id,
                    record.project,
                    record.harness,
                    record.session_id,
                    record.found,
                    record.rules,
                    record.notes,
                    record.chars,
                    record.tokens_est,
                    record.budget_chars,
                    list(record.entry_ids),
                ),
            )
```

In `tests/conftest.py::_reset_live_db`, change the truncate string to start
`"truncate access_log, injection_log, collection_members, ..."` and add a
line to the comment: the usage logs are named explicitly although they
cascade from `principals`.

- [ ] **Step 6: Run, expect PASS; then `make check`**

Run: `uv run pytest tests/test_usage_store.py -q` then `make check`.
Also `bag db up` against the dev database so later manual checks work.

- [ ] **Step 7: Commit**

```bash
git add src/saddlebag/backends/postgres/migrations/026_usage_logs.sql src/saddlebag/domain.py src/saddlebag/store.py src/saddlebag/backends/postgres/store.py tests/conftest.py tests/test_usage_store.py
git commit -m "Add access and injection usage logs"
```

---

### Task 2: `services/usage.py` and logging in `search.find`

**Files:**
- Create: `src/saddlebag/services/usage.py`
- Modify: `src/saddlebag/services/search.py` (`find`)
- Test: `tests/test_usage_service.py`

**Interfaces:**
- Consumes: `Store.log_access`, `Store.log_injection`, `Store.transaction`, `AccessRecord`, `InjectionRecord` (Task 1).
- Produces:
  - `usage.SESSION_ENV = "CLAUDE_CODE_SESSION_ID"`
  - `usage.session_id_from_env(env: Mapping[str, str]) -> str | None`
  - `usage.log_access(store, record: AccessRecord, note: Callable[[str], None] | None = None) -> None` - never raises.
  - `usage.log_injection(store, record: InjectionRecord, note: Callable[[str], None] | None = None) -> None` - never raises.
  - `usage.elapsed_ms(started: float) -> int` (from `time.perf_counter()`).
  - `search.find(..., source: str | None = None, session_id: str | None = None, log_project: str | None = None)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_usage_service.py`)

```python
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
    assert conn.execute("select tier, hits from access_log").fetchall() == [
        ("none", 0)
    ]


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
```

- [ ] **Step 2: Run, expect FAIL** (`ImportError: usage`)

Run: `uv run pytest tests/test_usage_service.py -q`

- [ ] **Step 3: Write `services/usage.py`**

```python
"""Usage logging - the recording half of `bag stats`.

Every write here is fail-soft, and that is the whole contract: a search, a
`get`, a session start must never fail because its log row could not be
written. The insert runs inside `store.transaction()`, which is a savepoint
when a transaction is already open, so a failed insert rolls back only
itself and leaves the caller's transaction usable. Without the savepoint a
failed statement leaves the connection in InFailedSqlTransaction and the
caller's own next statement is the one that raises.

Nothing here decides WHETHER to log. The caller passing a record is the
decision, and callers only build one when a frontend passed `source` - see
`search.find`.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping

from saddlebag.domain import AccessRecord, InjectionRecord
from saddlebag.store import Store

#: What Claude Code sets in every process it starts - Bash tool calls and
#: MCP servers alike. Measured 2026-09-16: `CLAUDE_SESSION_ID`, which
#: mcp_server.py read until this change, is not set at all, so every entry
#: an MCP `remember` wrote carried a null session id.
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


def session_id_from_env(env: Mapping[str, str]) -> str | None:
    """The calling session's id, or None. Never guessed."""
    return env.get(SESSION_ENV) or None


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _default_note(reason: str) -> None:
    # Same gate as hookio.debug, without importing the hook module into a
    # service: silent unless BAG_HOOK_DEBUG is set, and stderr only, because
    # stdout may be the MCP protocol stream.
    if os.environ.get("BAG_HOOK_DEBUG"):
        sys.stderr.write(f"saddlebag: {reason}\n")


def log_access(
    store: Store, record: AccessRecord, note: Callable[[str], None] | None = None
) -> None:
    say = note or _default_note
    try:
        with store.transaction():
            store.log_access(record)
    except Exception as exc:
        say(f"could not write access_log: {type(exc).__name__}: {exc}")


def log_injection(
    store: Store, record: InjectionRecord, note: Callable[[str], None] | None = None
) -> None:
    say = note or _default_note
    try:
        with store.transaction():
            store.log_injection(record)
    except Exception as exc:
        say(f"could not write injection_log: {type(exc).__name__}: {exc}")
```

Note `_default_note` is looked up at call time via `note or _default_note`
inside each function, so the test's `monkeypatch.setattr(usage,
"_default_note", ...)` takes effect.

- [ ] **Step 4: Log in `search.find`**

Change the signature (add after `embed_model`):

```python
    source: str | None = None,
    session_id: str | None = None,
    log_project: str | None = None,
```

Add to the docstring: "`source` turns on usage logging (`services/usage`);
None - every test, the eval script, every internal caller - logs nothing.
`log_project` is the project the frontend resolved for the log row, which
is not `query.project`: a search usually has no project filter."

Restructure the body so every return path goes through one helper. Replace
the section from `hits = store.search(query, owner_id)` to the end of
`find` with:

```python
    started = time.perf_counter()
    text = (query.text or "").strip()
    hits = store.search(query, owner_id)
    if not hits and text:
        hits = _semantic(
            store, owner_id, query, text, embedder, semantic_threshold, embed_model
        )
        if not hits:
            hits = store.fuzzy_search(query, owner_id, fuzzy_threshold)

    if source is not None:
        usage.log_access(
            store,
            AccessRecord(
                owner_id=owner_id,
                source=source,
                op="search",
                hits=len(hits),
                entry_ids=tuple(h.entry.id for h in hits),
                elapsed_ms=usage.elapsed_ms(started),
                project=log_project,
                session_id=session_id,
                # A listing query (no text) has neither a length nor a tier:
                # nothing was matched, only filtered.
                query_len=len(text) if text else None,
                tier=(str(hits[0].match) if hits else "none") if text else None,
            ),
        )
    return hits
```

Keep the existing comments about the no-text listing case and the tier
order next to the restructured lines. The early `if query.limit <= 0:
return []` stays unlogged (nothing was asked of the store).

`SearchStore` (the narrow protocol `find` takes) must gain `log_access` and
`transaction`; check its definition near the top of `search.py` and add:

```python
    def log_access(self, record: AccessRecord) -> None: ...
    def transaction(self) -> AbstractContextManager[Any]: ...
```

Any test double implementing `SearchStore` must then implement both; grep
`tests/` for classes passed to `find` and add no-op methods
(`def log_access(self, record): pass`, `def transaction(self): return
contextlib.nullcontext()`).

Imports in `search.py`: `import time`, `from saddlebag.domain import
AccessRecord`, `from saddlebag.services import usage`.

- [ ] **Step 5: Run, expect PASS**

Run: `uv run pytest tests/test_usage_service.py -q`

- [ ] **Step 6: Watch the savepoint guard go red**

On a scratch copy of the tree (not the working tree): replace
`with store.transaction():` in `log_access` with `if True:`, clear
`__pycache__` (`find . -name __pycache__ -exec rm -rf {} +`), run
`test_a_failing_log_insert_does_not_fail_or_poison_the_search`, and confirm
it FAILS (`InFailedSqlTransaction`). Then restore. Record the result in the
task report.

- [ ] **Step 7: `make check`, commit**

```bash
git add src/saddlebag/services/usage.py src/saddlebag/services/search.py tests/
git commit -m "Log searches when a frontend names itself"
```

---

### Task 3: Frontend read wiring and the MCP session id fix

**Files:**
- Modify: `src/saddlebag/cli.py` (`search` ~L398, `get` ~L512, `handoff_latest` ~L1744)
- Modify: `src/saddlebag/mcp_server.py` (`_session_id` ~L64, `recall_tool`, `get_entry_tool`)
- Test: `tests/test_usage_wiring.py`

**Interfaces:**
- Consumes: `usage.log_access`, `usage.session_id_from_env`, `usage.elapsed_ms`, `find(source=, session_id=, log_project=)` (Task 2).

- [ ] **Step 1: Write the failing tests** (`tests/test_usage_wiring.py`)

```python
"""Each frontend names itself, and its session, when it reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from saddlebag import mcp_server
from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.cli import app
from saddlebag.domain import Entry, Kind, Origin, new_id

runner = CliRunner()
pytestmark = pytest.mark.db


@pytest.fixture
def entry_id(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        e = store.put_entry(
            Entry(
                id=new_id(),
                kind=Kind.NOTE,
                title="vectors",
                body="pgvector cosine distance",
                owner_id=owner.id,
                project="demo",
                origin=Origin.HUMAN,
            )
        )
        c.commit()
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    return str(e.id)


def _rows(dsn: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as c:
        return c.execute(
            "select source, op, session_id, hits from access_log order by at"
        ).fetchall()


def test_cli_search_get_and_handoff_are_logged(entry_id: str, live_dsn: str) -> None:
    assert runner.invoke(app, ["search", "pgvector"]).exit_code == 0
    assert runner.invoke(app, ["get", entry_id]).exit_code == 0
    assert runner.invoke(app, ["handoff", "latest", "--project", "demo"]).exit_code == 0
    assert _rows(live_dsn) == [
        ("cli", "search", "sess-1", 1),
        ("cli", "get", "sess-1", 1),
        ("cli", "handoff", "sess-1", 0),
    ]


def test_mcp_recall_and_get_entry_are_logged(entry_id: str, live_dsn: str) -> None:
    mcp_server.recall_tool("pgvector")
    mcp_server.get_entry_tool(entry_id)
    assert _rows(live_dsn) == [
        ("mcp", "search", "sess-1", 1),
        ("mcp", "get", "sess-1", 1),
    ]


def test_mcp_session_id_reads_the_variable_claude_code_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-9")
    assert mcp_server._session_id() == "sess-9"
```

If `mcp_server` has an `_http_mode` module flag, make sure the test runs
with it False (it defaults to False; do not change it).

- [ ] **Step 2: Run, expect FAIL** (no rows)

Run: `uv run pytest tests/test_usage_wiring.py -q`

- [ ] **Step 3: Wire the CLI**

`search`: pass to `find(...)`

```python
            source="cli",
            session_id=usage.session_id_from_env(os.environ),
            log_project=_default_project(),
```

`get`: time the lookup and log it inside the session block:

```python
    with _session() as s:
        started = time.perf_counter()
        entry = s.store.get_entry(parsed, s.owner.id)
        usage.log_access(
            s.store,
            AccessRecord(
                owner_id=s.owner.id,
                source="cli",
                op="get",
                hits=0 if entry is None else 1,
                entry_ids=() if entry is None else (entry.id,),
                elapsed_ms=usage.elapsed_ms(started),
                project=_default_project(),
                session_id=usage.session_id_from_env(os.environ),
            ),
        )
```

`handoff_latest`: same shape around `handoff_svc.latest(...)`, `op="handoff"`,
`project=name`, logging only when `latest` did not raise (put the log call
after the `try/except ValueError` block, still inside `with _session()`).

Imports in `cli.py`: `import time` (if absent), `from saddlebag.domain
import AccessRecord`, `from saddlebag.services import usage`.

- [ ] **Step 4: Wire MCP and fix the env name**

`_session_id`: return `usage.session_id_from_env(os.environ)` (keep the
`_http_mode` early return and its comment). Replace the first comment line
with: "Claude Code sets CLAUDE_CODE_SESSION_ID in the server's environment -
see usage.SESSION_ENV. The server is started once per Claude Code process,
so after /clear this is the id the process started with, not the new one;
still the right session far more often than null is."

`recall_tool`: pass `source="mcp", session_id=_session_id(),
log_project=_default_project()` to `find`. (Use the module's existing
default-project helper; its name is the function whose body contains
`if _pinned_project is not None`.)

`get_entry_tool`: log `op="get"`, `source="mcp"` as in the CLI, after the
lookup, for both found and not-found (not for the invalid-UUID error, which
never reached the store).

- [ ] **Step 5: Run, expect PASS; `make check`; commit**

```bash
git add src/saddlebag/cli.py src/saddlebag/mcp_server.py tests/test_usage_wiring.py
git commit -m "Log CLI and MCP reads, and read the session id Claude Code sets"
```

---

### Task 4: Injection logging

**Files:**
- Modify: `src/saddlebag/services/kb.py` (`Rendered`, `render_block`)
- Modify: `src/saddlebag/services/context.py` (`Injection`, `injection`, new `InjectionLog`)
- Modify: `src/saddlebag/agents/claude_code/hook.py` (`session_start`)
- Modify: `src/saddlebag/cli.py` (`hook_context`)
- Modify: `tests/test_banner.py` (helper gains `entry_ids=()`)
- Test: `tests/test_injection_log.py`

**Interfaces:**
- Consumes: `usage.log_injection`, `InjectionRecord` (Tasks 1-2).
- Produces:
  - `kb.Rendered.entry_ids: tuple[UUID, ...]` (rules first, then the notes that fit, in render order).
  - `context.InjectionLog(harness: str, session_id: str | None)` - frozen dataclass.
  - `context.Injection.entry_ids: tuple[UUID, ...]` (new last field, default `()`).
  - `context.injection(..., log: InjectionLog | None = None)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_injection_log.py`)

```python
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
```

Add to `tests/test_hook_context_cli.py` (its `env` fixture already stubs
the spawns and returns the DSN; `_seed_kb` and `repo` exist there):

```python
def test_hook_context_logs_the_injection_with_its_harness(
    env: str, repo: Path
) -> None:
    _seed_kb(env)
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "claude-code"],
        input=json.dumps({"cwd": str(repo), "session_id": "sess-7"}),
    )
    assert result.exit_code == 0
    assert "Lint rule" in result.stdout
    with psycopg.connect(env) as c:
        assert c.execute(
            "select harness, session_id, found, rules from injection_log"
        ).fetchall() == [("claude-code", "sess-7", True, 1)]
```

- [ ] **Step 2: Run, expect FAIL**

Run: `uv run pytest tests/test_injection_log.py tests/test_hook_context_cli.py -q`

- [ ] **Step 3: `Rendered.entry_ids`**

Add the field `entry_ids: tuple[UUID, ...]` to `Rendered` (docstring: "What
the block carried, for the injection log - rules, then the notes that
fit."). In `render_block`, track the included notes alongside `body_parts`
(a parallel `included_ids: list[UUID]`, popped together with `body_parts`)
and return:

```python
        entry_ids=tuple(e.id for e in entries if e.kind == Kind.RULE)
        + tuple(included_ids),
```

- [ ] **Step 4: `context.injection(log=)`**

```python
@dataclass(frozen=True)
class InjectionLog:
    """Who is asking, for the injection log. Passing one is the opt-in."""

    harness: str
    session_id: str | None
```

Add `entry_ids: tuple[UUID, ...] = ()` as the last field of `Injection`.
In `injection()`, capture `ids = block_.entry_ids` where the block renders
(default `()`), build the `Injection`, then:

```python
    if log is not None:
        usage.log_injection(
            store,
            InjectionRecord(
                owner_id=owner_id,
                project=project,
                harness=log.harness,
                session_id=log.session_id,
                found=found,
                rules=rules,
                notes=notes,
                chars=len(got.text),
                budget_chars=max_chars,
                entry_ids=ids,
            ),
            note=say,
        )
    return got
```

`RulesExceedBudget` raises out of `render_block` before this point, so an
over-budget session is never logged - say so in a comment. Thread `log`
through `block()` too (optional, default None).

- [ ] **Step 5: Pass it from both harness paths**

`hook.session_start`: `log=context.InjectionLog("claude-code",
identity.session_id)`.

`cli.hook_context`: `context.block(..., log=context.InjectionLog(agent,
identity.session_id))`.

- [ ] **Step 6: Run, expect PASS; `make check`; commit**

```bash
git add src/saddlebag/services/kb.py src/saddlebag/services/context.py src/saddlebag/agents/claude_code/hook.py src/saddlebag/cli.py tests/
git commit -m "Log what each session start injected"
```

---

### Task 5: Store reads for stats

**Files:**
- Modify: `src/saddlebag/domain.py`, `src/saddlebag/store.py`, `src/saddlebag/backends/postgres/store.py`
- Test: `tests/test_stats_store.py`

**Interfaces:**
- Produces (domain, frozen slots dataclasses):

```python
@dataclass(frozen=True, slots=True)
class EntryCounts:
    live: int
    project_live: int
    by_kind: dict[str, int]
    by_origin: dict[str, int]
    superseded: int
    collections: int
    projects: int


@dataclass(frozen=True, slots=True)
class AccessSummary:
    first_at: datetime | None  # earliest access_log row ever, for "since"
    reads: int
    by_source: dict[str, int]
    by_op: dict[str, int]
    searches: int
    search_hits: int  # searches with hits > 0
    tiers: dict[str, int]  # searches only, tier -> count, incl. "none"
    p50_ms: int | None
    sessions: int  # distinct non-null session ids that read


@dataclass(frozen=True, slots=True)
class InjectionSummary:
    first_at: datetime | None
    sessions: int
    mean_rules: float
    mean_notes: float
    mean_tokens: float
    injected: int  # sum of cardinality(entry_ids) over sessions with an id
    opened: int  # of those, opened later in the same session


@dataclass(frozen=True, slots=True)
class PipelineCounts:
    transcript_sessions: int
    transcript_subagents: int
    last_transcript_run: TranscriptRun | None
    last_memory_run: MemoryRun | None
    last_ingest_run: IngestRun | None
    extracted_since: int
```

- Store methods:
  - `entry_counts(owner_id: UUID, project: str | None) -> EntryCounts`
  - `access_summary(owner_id: UUID, since: datetime, project: str | None) -> AccessSummary`
  - `injection_summary(owner_id: UUID, since: datetime, project: str | None) -> InjectionSummary`
  - `recent_entries(owner_id: UUID, limit: int) -> list[Entry]` (newest `created_at` first, live and superseded alike, every origin - it is "what was written")
  - `pipeline_counts(owner_id: UUID, since: datetime) -> PipelineCounts` (runs are the newest across all projects)

- [ ] **Step 1: Write the failing tests** (`tests/test_stats_store.py`)

```python
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
    InjectionRecord,
    Kind,
    Origin,
    Principal,
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


def _read(store, owner, *, session="s1", op="search", tier="exact", hits=1,
          ids=(), ms=10, source="cli", project="demo"):
    store.log_access(
        AccessRecord(
            owner_id=owner.id, source=source, op=op, hits=hits, entry_ids=ids,
            elapsed_ms=ms, project=project, session_id=session,
            query_len=5 if op == "search" else None,
            tier=tier if op == "search" else None,
        )
    )


def _inject(store, owner, *, session="s1", ids=(), chars=400, project="demo"):
    store.log_injection(
        InjectionRecord(
            owner_id=owner.id, project=project, harness="claude-code",
            session_id=session, found=True, rules=len(ids), notes=0,
            chars=chars, budget_chars=16000, entry_ids=ids,
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
    assert got.p50_ms == 15
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
    _read(store, other, session="s1", op="get", ids=(b,))  # other principal
    got = store.injection_summary(me.id, LONG_AGO, None)
    assert got.sessions == 2
    assert (got.injected, got.opened) == (2, 1)
    assert got.mean_tokens == pytest.approx((200 + 100) / 2)


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
```

Add one more test that starts a memory run, an ingest run and a transcript
run for `other` and asserts all three `last_*` stay None for `me` (use
`store.start_memory_run` / `start_ingest_run` / `start_transcript_run`;
read their signatures in `store.py`).

- [ ] **Step 2: Run, expect FAIL**

Run: `uv run pytest tests/test_stats_store.py -q`

- [ ] **Step 3: Implement** (Protocol stubs in `store.py` under `# usage`, SQL below in the `usage` block)

```python
    def entry_counts(self, owner_id: UUID, project: str | None) -> EntryCounts:
        with self._cur() as cur:
            cur.execute(
                """
                select
                  count(*) filter (where superseded_by is null) as live,
                  count(*) filter (where superseded_by is null
                                   and project = %(project)s) as project_live,
                  count(*) filter (where superseded_by is not null) as superseded,
                  count(distinct project) filter (where superseded_by is null)
                    as projects,
                  (select count(*) from collections where owner_id = %(owner)s)
                    as collections
                from entries where owner_id = %(owner)s
                """,
                {"owner": owner_id, "project": project},
            )
            head = _one(cur)
            cur.execute(
                "select kind::text as k, count(*) as n from entries "
                "where owner_id = %s and superseded_by is null group by 1",
                (owner_id,),
            )
            kinds = {r["k"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute(
                "select origin::text as k, count(*) as n from entries "
                "where owner_id = %s and superseded_by is null group by 1",
                (owner_id,),
            )
            origins = {r["k"]: int(r["n"]) for r in cur.fetchall()}
        return EntryCounts(
            live=int(head["live"]),
            project_live=int(head["project_live"]),
            by_kind=kinds,
            by_origin=origins,
            superseded=int(head["superseded"]),
            collections=int(head["collections"]),
            projects=int(head["projects"]),
        )

    def access_summary(
        self, owner_id: UUID, since: datetime, project: str | None
    ) -> AccessSummary:
        # `project is null` in the parameter means "every project". Written as
        # one predicate rather than two SQL strings so the owner filter exists
        # exactly once.
        scope = (
            "owner_id = %(owner)s and at >= %(since)s "
            "and (%(project)s::text is null or project = %(project)s)"
        )
        args = {"owner": owner_id, "since": since, "project": project}
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select
                  count(*) as reads,
                  count(*) filter (where op = 'search') as searches,
                  count(*) filter (where op = 'search' and hits > 0) as search_hits,
                  percentile_cont(0.5) within group (order by elapsed_ms) as p50,
                  count(distinct session_id) as sessions,
                  (select min(at) from access_log where owner_id = %(owner)s)
                    as first_at
                from access_log where {scope}
                """),
                args,
            )
            head = _one(cur)
            by: dict[str, dict[str, int]] = {}
            for column in ("source", "op", "tier"):
                cur.execute(
                    as_sql(f"""
                    select {column} as k, count(*) as n from access_log
                     where {scope} and {column} is not null group by 1
                    """),
                    args,
                )
                by[column] = {r["k"]: int(r["n"]) for r in cur.fetchall()}
        return AccessSummary(
            first_at=head["first_at"],
            reads=int(head["reads"]),
            by_source=by["source"],
            by_op=by["op"],
            searches=int(head["searches"]),
            search_hits=int(head["search_hits"]),
            tiers=by["tier"],
            p50_ms=None if head["p50"] is None else round(head["p50"]),
            sessions=int(head["sessions"]),
        )
```

`column` is interpolated from a literal tuple in this function - a module
constant in spirit; say so in a comment above the loop, since `as_sql`'s
rule is "only constants".

`p50` is `percentile_cont` over every read in scope: the test's four
`demo` rows are 5, 10, 20, 30 ms, so the median is (10 + 20) / 2 = 15.

```python
    def injection_summary(
        self, owner_id: UUID, since: datetime, project: str | None
    ) -> InjectionSummary:
        with self._cur() as cur:
            cur.execute(
                """
                with inj as (
                  select * from injection_log
                   where owner_id = %(owner)s and at >= %(since)s
                     and (%(project)s::text is null or project = %(project)s)
                ),
                ids as (
                  select i.session_id, i.at, u.id as entry_id
                    from inj i, unnest(i.entry_ids) as u(id)
                   where i.session_id is not null
                )
                select
                  (select count(*) from inj) as sessions,
                  (select avg(rules) from inj) as mean_rules,
                  (select avg(notes) from inj) as mean_notes,
                  (select avg(tokens_est) from inj) as mean_tokens,
                  (select count(*) from ids) as injected,
                  (select count(*) from ids
                    where exists (
                      select 1 from access_log a
                       where a.owner_id = %(owner)s
                         and a.session_id = ids.session_id
                         and a.at >= ids.at
                         and ids.entry_id = any(a.entry_ids))) as opened,
                  (select min(at) from injection_log where owner_id = %(owner)s)
                    as first_at
                """,
                {"owner": owner_id, "since": since, "project": project},
            )
            row = _one(cur)
        return InjectionSummary(
            first_at=row["first_at"],
            sessions=int(row["sessions"]),
            mean_rules=float(row["mean_rules"] or 0),
            mean_notes=float(row["mean_notes"] or 0),
            mean_tokens=float(row["mean_tokens"] or 0),
            injected=int(row["injected"]),
            opened=int(row["opened"]),
        )
```

Follow-through in the test: `a` opened in s1 counts; `b` opened only in s2
does not. A `search` row whose ids include an injected id also counts as
"opened" - intended, since a recall returning it surfaced it again; say so
in a comment on the `exists` clause.

```python
    def recent_entries(self, owner_id: UUID, limit: int) -> list[Entry]:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {entry_columns()} from entries
                 where owner_id = %s
                 order by created_at desc, id desc
                 limit %s
                """),
                (owner_id, limit),
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def pipeline_counts(self, owner_id: UUID, since: datetime) -> PipelineCounts:
        with self._cur() as cur:
            cur.execute(
                """
                select
                  count(*) filter (where agent_id is null) as sessions,
                  count(*) filter (where agent_id is not null) as subagents
                from transcripts where owner_id = %s
                """,
                (owner_id,),
            )
            t = _one(cur)
            cur.execute(
                "select count(*) as n from entries where owner_id = %s "
                "and origin = 'extracted' and created_at >= %s",
                (owner_id, since),
            )
            extracted = int(_one(cur)["n"])
            cur.execute(
                as_sql(f"""
                select {transcript_run_columns()} from transcript_runs
                 where owner_id = %s order by started_at desc limit 1
                """),
                (owner_id,),
            )
            tr = cur.fetchone()
            cur.execute(
                as_sql(f"""
                select {memory_run_columns()} from memory_runs
                 where owner_id = %s order by started_at desc limit 1
                """),
                (owner_id,),
            )
            mr = cur.fetchone()
            cur.execute(
                as_sql(f"""
                select {ingest_run_columns()} from ingest_runs
                 where owner_id = %s order by started_at desc limit 1
                """),
                (owner_id,),
            )
            ir = cur.fetchone()
        return PipelineCounts(
            transcript_sessions=int(t["sessions"]),
            transcript_subagents=int(t["subagents"]),
            last_transcript_run=_row_to_transcript_run(tr) if tr else None,
            last_memory_run=_row_to_memory_run(mr) if mr else None,
            last_ingest_run=_row_to_ingest_run(ir) if ir else None,
            extracted_since=extracted,
        )
```

Check the real column names before running: `transcripts.agent_id`
(migration 024), `memory_runs.started_at`, `ingest_runs.started_at`, and
whether `memory_run_columns()` / `ingest_run_columns()` take an alias.

- [ ] **Step 4: Run, expect PASS; `make check`; commit**

```bash
git add src/saddlebag/domain.py src/saddlebag/store.py src/saddlebag/backends/postgres/store.py tests/test_stats_store.py
git commit -m "Add the store reads behind bag stats"
```

---

### Task 6: `services/stats.py` - collect and render

**Files:**
- Create: `src/saddlebag/services/stats.py`
- Test: `tests/test_stats_render.py` (pure), `tests/test_stats_collect.py` (db)

**Interfaces:**
- Consumes: Task 5 reads; `store.vector_coverage`, `store.extract_job_counts`, `extraction.awaiting_sessions`, `kb.get`, `kb.resolve`, `kb.rules_chars`, `ingest.advisories`, `memory.advisories`, `transcripts.advisories`, `handoff.age_phrase`, `search.DEFAULT_ORIGINS`.
- Produces:

```python
@dataclass(frozen=True)
class Unavailable:
    reason: str

@dataclass(frozen=True)
class Vectors:
    embedded: int
    total: int
    model: str

@dataclass(frozen=True)
class Extraction:
    jobs: dict[str, int]
    awaiting: int
    extracted: int
    model: str

@dataclass(frozen=True)
class Injected:
    summary: InjectionSummary
    budget_fraction: float | None  # None: no knowledge base for this project

@dataclass(frozen=True)
class Pipelines:
    counts: PipelineCounts
    advisories: int  # memory + ingest + transcripts advisory lines

@dataclass(frozen=True)
class Stats:
    project: str
    window: timedelta
    now: datetime
    store: EntryCounts | Unavailable
    retrieval: AccessSummary | Unavailable
    injection: Injected | Unavailable
    vectors: Vectors | Unavailable
    extraction: Extraction | Unavailable
    pipelines: Pipelines | Unavailable
    recent: list[Entry] | Unavailable

def collect(store: Store, owner_id: UUID, project: str, config: Config, *,
            now: datetime, window: timedelta = timedelta(days=7),
            recent: int = 10, current_root: Path | None = None) -> Stats
def render(stats: Stats) -> list[str]
def to_dict(stats: Stats) -> dict[str, Any]
STATEMENT_TIMEOUT_MS = 1500
```

- [ ] **Step 1: Write the pure render tests** (`tests/test_stats_render.py`)

```python
"""Rendering is pure: no db marker, runs on CI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from saddlebag.domain import (
    AccessSummary,
    Entry,
    EntryCounts,
    InjectionSummary,
    Kind,
    Origin,
    PipelineCounts,
)
from saddlebag.services import stats as st

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
WEEK_AGO = NOW - timedelta(days=30)


def _stats(**kw: object) -> st.Stats:
    base: dict[str, object] = dict(
        project="demo",
        window=timedelta(days=7),
        now=NOW,
        store=EntryCounts(
            live=1204, project_live=612, by_kind={"rule": 41, "note": 1100, "doc": 63},
            by_origin={"human": 900, "extracted": 304}, superseded=318,
            collections=9, projects=12,
        ),
        retrieval=AccessSummary(
            first_at=WEEK_AGO, reads=48, by_source={"cli": 40, "mcp": 8},
            by_op={"search": 40, "get": 6, "handoff": 2}, searches=40,
            search_hits=37, tiers={"exact": 22, "semantic": 16, "fuzzy": 1, "none": 1},
            p50_ms=180, sessions=11,
        ),
        injection=st.Injected(
            summary=InjectionSummary(
                first_at=WEEK_AGO, sessions=14, mean_rules=22.0, mean_notes=8.0,
                mean_tokens=4100.0, injected=400, opened=24,
            ),
            budget_fraction=0.61,
        ),
        vectors=st.Vectors(embedded=1158, total=1204, model="BAAI/bge-small-en-v1.5"),
        extraction=st.Extraction(
            jobs={"done": 43}, awaiting=0, extracted=12, model="sonnet"
        ),
        pipelines=st.Pipelines(
            counts=PipelineCounts(
                transcript_sessions=155, transcript_subagents=288,
                last_transcript_run=None, last_memory_run=None,
                last_ingest_run=None, extracted_since=12,
            ),
            advisories=0,
        ),
        recent=[
            Entry(
                id=UUID("01a0ac4c-9ad9-75f9-9819-2cca6840d456"), kind=Kind.NOTE,
                title="Reranker follow-up", body="", owner_id=UUID(int=1),
                origin=Origin.AGENT, project="demo",
                created_at=NOW - timedelta(minutes=2),
            )
        ],
    )
    base.update(kw)
    return st.Stats(**base)  # type: ignore[arg-type]


def _line(lines: list[str], label: str) -> str:
    return next(l for l in lines if l.startswith(label))


def test_every_section_has_a_labelled_line() -> None:
    lines = st.render(_stats())
    for label in ("store", "recall", "inject", "vectors", "extract", "pipes", "recent"):
        assert _line(lines, label)


def test_store_line() -> None:
    assert _line(st.render(_stats()), "store") == (
        "store     1204 live (demo 612) · 41 rules · 9 kbs · 12 projects"
        " · 318 superseded"
    )


def test_recall_line_carries_rate_tiers_and_latency() -> None:
    line = _line(st.render(_stats()), "recall")
    assert line == (
        "recall    7d: 48 reads in 11/14 sessions · hit 92% (37/40)"
        " · exact 55% semantic 40% fuzzy 2% none 2% · p50 180ms"
    )


def test_inject_line_labels_the_estimate_and_follow_through() -> None:
    line = _line(st.render(_stats()), "inject")
    assert line == (
        "inject    7d: 14 sessions · 22 rules 8 notes avg · ~4.1k tokens avg (est)"
        " · budget 61% · follow-through 6% (24/400)"
    )


def test_a_young_log_says_since_instead_of_the_window() -> None:
    young = NOW - timedelta(days=2)
    s = _stats(
        retrieval=AccessSummary(
            first_at=young, reads=1, by_source={"cli": 1}, by_op={"search": 1},
            searches=1, search_hits=1, tiers={"exact": 1}, p50_ms=5, sessions=1,
        )
    )
    assert _line(st.render(s), "recall").startswith("recall    since 2026-09-14:")


def test_no_log_rows_yet_says_so() -> None:
    s = _stats(
        retrieval=AccessSummary(
            first_at=None, reads=0, by_source={}, by_op={}, searches=0,
            search_hits=0, tiers={}, p50_ms=None, sessions=0,
        )
    )
    assert _line(st.render(s), "recall") == "recall    no reads logged yet"


def test_an_unavailable_section_says_why_and_the_rest_render() -> None:
    lines = st.render(_stats(vectors=st.Unavailable("timeout")))
    assert _line(lines, "vectors") == "vectors   unavailable (timeout)"
    assert _line(lines, "store")


def test_recent_lists_age_kind_id_prefix_and_title() -> None:
    lines = st.render(_stats())
    assert _line(lines, "recent") == (
        "recent    2m ago  note  01a0ac4c  Reranker follow-up  [agent]"
    )


def test_no_em_dashes_anywhere() -> None:
    assert "—" not in "\n".join(st.render(_stats()))


def test_to_dict_has_the_same_keys_whether_or_not_sections_are_available() -> None:
    full = st.to_dict(_stats())
    broken = st.to_dict(
        _stats(**{k: st.Unavailable("x") for k in (
            "store", "retrieval", "injection", "vectors",
            "extraction", "pipelines", "recent",
        )})
    )
    assert full.keys() == broken.keys()
    assert broken["vectors"] is None
    assert broken["unavailable"]["vectors"] == "x"
```

Percentages round half-up to whole numbers: `int(100 * n / d + 0.5)`; a zero
denominator renders `-`.

- [ ] **Step 2: Run, expect FAIL**; **Step 3: implement `render`/`to_dict`**

```python
"""`bag stats` and the SessionStart banner's stats lines.

`collect` gathers facts; `render` and `to_dict` format them. Every section
is collected on its own and a failure becomes `Unavailable(reason)`: in the
hook, one slow or broken query must cost one line, not the banner - and
`bag stats` reports "I could not tell" as a line rather than an exit code,
the same rule `bag doctor` follows for UNCHECKED.
"""

LABEL_WIDTH = 10


def _label(name: str, text: str) -> str:
    return f"{name:<{LABEL_WIDTH}}{text}"


def _pct(n: int | float, d: int | float) -> str:
    return "-" if not d else f"{int(100 * n / d + 0.5)}%"


def _period(first_at: datetime | None, now: datetime, window: timedelta) -> str | None:
    """'7d', 'since <date>' when the log is younger than the window, or None
    when nothing has been logged at all."""
    if first_at is None:
        return None
    if first_at > now - window:
        return f"since {first_at.astimezone().date().isoformat()}"
    days = window.days
    return f"{days}d" if days else f"{int(window.total_seconds() // 3600)}h"


def _tokens(n: float) -> str:
    return f"~{n / 1000:.1f}k" if n >= 1000 else f"~{int(n)}"
```

Implement one `_render_<section>` per section returning its line(s), and
`render` concatenating them in order store, recall, inject, vectors,
extract, pipes, recent. Exact formats (they are what the tests pin):

- store: `store     {live} live ({project} {project_live}) · {rules} rules · {collections} kbs · {projects} projects · {superseded} superseded` where rules = `by_kind.get("rule", 0)`.
- recall: `recall    {period}: {reads} reads in {sessions}/{inj_sessions} sessions · hit {pct} ({hits}/{searches}) · exact X% semantic Y% fuzzy Z% none W% · p50 {ms}ms`. `inj_sessions` is the injection summary's `sessions` when available, else omit `/{…}`. Tier percentages are of `searches`, in the fixed order exact, semantic, fuzzy, none, omitting tiers with count 0. `p50` omitted when None. With `first_at is None`: `recall    no reads logged yet`.
- inject: `inject    {period}: {sessions} sessions · {rules:.0f} rules {notes:.0f} notes avg · {tokens} tokens avg (est) · budget {pct} · follow-through {pct} ({opened}/{injected})`. `budget …` omitted when `budget_fraction` is None. No rows: `inject    no session starts logged yet`.
- vectors: `vectors   {embedded}/{total} embedded ({short model}) · backlog {total-embedded}`; short model = the part after the last `/`, with a trailing `-en-v1.5` style suffix left alone (just `split("/")[-1]`).
- extract: `extract   {done} done · {failed} failed · {awaiting} waiting · {period_or_window} +{extracted} entries ({model})`, jobs read with `.get(k, 0)` for `done`/`failed`; the period is `f"{window.days}d"`.
- pipes: `pipes     transcripts {sessions} sessions {subagents} subagents, last {age} · memory last {age} · ingest last {age} · {n} advisories`, `age` = `handoff.age_phrase(run.started_at, now)` or `never`; a run with `finished_at is None` appends ` (did not finish)`; `n advisories` is followed by ` - run bag record status` when n > 0.
- recent: first line `recent    {age}  {kind}  {id[:8]}  {title}  [{origin}]`, following lines the same body indented 10 spaces; `age` is `handoff.age_phrase(created_at, now)` (e.g. `2m ago`). Titles longer than 60 chars are cut to 57 plus `...`. Empty list: `recent    nothing written yet`.
- Unavailable: `{label}unavailable ({reason})`.

`to_dict`: keys `project, window_seconds, now, store, retrieval, injection,
vectors, extraction, pipelines, recent, unavailable`. Available sections
serialize with `dataclasses.asdict` then datetimes via `.isoformat()` and
UUIDs via `str` (write a small recursive `_jsonable`); `recent` is a list
of `{id, kind, origin, project, title, created_at}`; `unavailable` maps
section name to reason for every Unavailable section (empty dict when
none).

- [ ] **Step 4: Write the collect test** (`tests/test_stats_collect.py`, db)

```python
"""collect() degrades per section."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.config import load
from saddlebag.services import stats as st

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


def test_collect_on_an_empty_store_has_every_section(
    store: PostgresStore, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    owner = store.ensure_principal("brandon")
    got = st.collect(
        store, owner.id, "demo", load(), now=datetime.now(timezone.utc)
    )
    for name in ("store", "retrieval", "injection", "vectors",
                 "extraction", "pipelines", "recent"):
        assert not isinstance(getattr(got, name), st.Unavailable), name
    assert isinstance(got.injection, st.Injected)
    assert got.injection.budget_fraction is None  # no knowledge base


def test_a_section_that_raises_becomes_unavailable_and_the_rest_survive(
    store: PostgresStore, tmp_path, monkeypatch, conn
) -> None:
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    owner = store.ensure_principal("brandon")

    def broken(*a: object, **k: object) -> None:
        conn.execute("select * from no_such_table")  # aborts the transaction

    monkeypatch.setattr(store, "access_summary", broken)
    got = st.collect(store, owner.id, "demo", load(), now=datetime.now(timezone.utc))
    assert isinstance(got.retrieval, st.Unavailable)
    assert "UndefinedTable" in got.retrieval.reason
    assert not isinstance(got.store, st.Unavailable)
    assert not isinstance(got.recent, st.Unavailable)
```

- [ ] **Step 5: Implement `collect`**

```python
def collect(
    store: Store,
    owner_id: UUID,
    project: str,
    config: Config,
    *,
    now: datetime,
    window: timedelta = timedelta(days=7),
    recent: int = 10,
    current_root: Path | None = None,
) -> Stats:
    since = now - window

    def section(fn: Callable[[], T]) -> T | Unavailable:
        # Each section in its own savepoint so a failed statement costs that
        # section only - without it the transaction is aborted and every
        # later section fails too, which is the banner going dark again.
        try:
            with store.transaction():
                _statement_timeout(store)
                return fn()
        except Exception as exc:
            return Unavailable(f"{type(exc).__name__}: {exc}".splitlines()[0][:120])

    retrieval = section(lambda: store.access_summary(owner_id, since, project))
    ...
```

- `_statement_timeout(store)`: `store.set_statement_timeout(STATEMENT_TIMEOUT_MS)` - add this tiny method to the Protocol and Postgres store (`set local statement_timeout = %s` does not accept parameters; use `cur.execute(as_sql(f"set local statement_timeout = {int(ms)}"))` with a comment that `int()` makes the interpolation safe). `set local` ends with the savepoint's enclosing transaction, not the savepoint - that is fine: every section sets the same value.
- store: `store.entry_counts(owner_id, project)`.
- injection: summary via `store.injection_summary(owner_id, since, project)`; budget via:

```python
def _budget(store, owner_id, project, max_chars) -> float | None:
    try:
        collection = kb.get(store, owner_id, project)
    except kb.CollectionNotFound:
        return None
    return kb.rules_chars(collection, kb.resolve(store, owner_id, project)) / max_chars
```

- vectors: `embedded, total = store.vector_coverage(Query(origins=list(search.DEFAULT_ORIGINS), limit=1), owner_id, config.embed_model)`. Check `vector_coverage`'s filter ignores `limit` (it builds `where` from `_entry_filters`, which only puts `limit` in params) - confirm, then use it.
- extraction: `Extraction(jobs=store.extract_job_counts(owner_id), awaiting=len(extraction.awaiting_sessions(store, owner_id, config.idle_minutes * 60, 1000)), extracted=store.pipeline_counts(owner_id, since).extracted_since, model=config.extract_model)`. Check the real `Config` attribute names for idle minutes and the extraction model in `config.py` and use those.
- pipelines: `Pipelines(counts=store.pipeline_counts(owner_id, since), advisories=len(memory.advisories(store, owner_id)) + len(ingest.advisories(store, owner_id, current_project=project, root=current_root)) + len(transcripts.advisories(store, owner_id)))`.
- recent: `store.recent_entries(owner_id, recent)`.

Import `T = TypeVar("T")` at module level.

- [ ] **Step 6: Run both test files, expect PASS; `make check`; commit**

```bash
git add src/saddlebag/services/stats.py src/saddlebag/store.py src/saddlebag/backends/postgres/store.py tests/test_stats_render.py tests/test_stats_collect.py
git commit -m "Collect and render usage statistics"
```

---

### Task 7: Stats in the SessionStart banner

**Files:**
- Modify: `src/saddlebag/services/context.py` (`banner`)
- Modify: `src/saddlebag/agents/claude_code/hook.py` (`session_start`, `render_output`, `main`)
- Modify: `tests/test_banner.py`, `tests/test_hook.py`

**Interfaces:**
- Consumes: `stats.collect`, `stats.render`, `stats.Stats` (Task 6).
- Produces: `context.banner(got: Injection, stats_lines: list[str] | None = None) -> str`; `hook.session_start(...) -> tuple[Injection, list[str] | None] | None`; `hook.render_output(got: Injection, stats_lines: list[str] | None = None) -> str`.

Passing rendered lines (not `Stats`) keeps `context` free of a `stats`
import cycle (`stats` imports `kb`; `context` imports `kb` too - fine - but
`banner` has no need for the objects).

- [ ] **Step 1: Failing tests**

`tests/test_banner.py`:

```python
def test_stats_lines_follow_the_first_line_unchanged() -> None:
    got = injection(rules=2)
    plain = banner(got)
    assert banner(got, ["store     1 live", "recall    no reads logged yet"]) == (
        plain + "\nstore     1 live\nrecall    no reads logged yet"
    )


def test_no_stats_is_exactly_todays_banner() -> None:
    got = injection(rules=2)
    assert banner(got, None) == banner(got) == banner(got, [])
```

`tests/test_hook.py` (db, with spawn stubs - copy the fixture pattern from
`tests/test_hook_context_cli.py`): run `main()` against a live database with
a knowledge base and assert:

```python
    doc = json.loads(capsys.readouterr().out)
    assert doc["systemMessage"].splitlines()[0].startswith("saddlebag · kb ")
    assert any(l.startswith("store ") for l in doc["systemMessage"].splitlines())
    # The model's context is untouched by stats.
    assert "store " not in doc["hookSpecificOutput"]["additionalContext"]
```

and a second test that monkeypatches `saddlebag.services.stats.collect` to
raise and asserts `systemMessage` is a single line and
`additionalContext` is byte-identical to the first test's.

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

`banner`: build the existing line as `head`, then
`return "\n".join([head, *(stats_lines or [])])`. Docstring gains: "The
stats lines are appended verbatim; they reach the user only -
`additionalContext` never carries them, so the model pays nothing."

`hook.session_start`: inside the same `with open_session(config) as s:`,
after computing `got`:

```python
            try:
                lines = stats.render(
                    stats.collect(
                        s.store, s.owner.id, identity.project, config,
                        now=datetime.now(timezone.utc),
                        current_root=Path(payload_cwd) if payload_cwd else None,
                    )
                )
            except Exception as exc:
                # Stats are a nicety on top of the injection; losing them
                # must never cost the block. One line, today's banner.
                _debug(env, f"stats: {type(exc).__name__}: {exc}")
                lines = None
            return got, lines
```

`payload_cwd` is `payload.get("cwd")` if it is a str. Update `render_output`
to take `stats_lines` and pass them to `banner`; update `main` to unpack the
tuple. Update every existing caller/test of `session_start` for the tuple
return (grep `session_start(` in `tests/`).

**Ordering matters:** `injection()` can raise `RulesExceedBudget`; the
existing `except Exception` returns None and the banner stays silent - do
not move stats collection before injection.

- [ ] **Step 4: Run, expect PASS; `make check`; commit**

```bash
git add src/saddlebag/services/context.py src/saddlebag/agents/claude_code/hook.py tests/test_banner.py tests/test_hook.py
git commit -m "Show usage statistics in the SessionStart banner"
```

---

### Task 8: `bag stats`

**Files:**
- Modify: `src/saddlebag/cli.py` (new top-level command near `search`)
- Test: `tests/test_stats_cli.py`

**Interfaces:**
- Consumes: `stats.collect`, `stats.render`, `stats.to_dict`.

- [ ] **Step 1: Failing tests**

```python
"""`bag stats` - loud about the database, quiet about a section."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.cli import app

runner = CliRunner()


@pytest.fixture
def env(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    return live_dsn


@pytest.mark.db
def test_prints_every_section(env: str) -> None:
    result = runner.invoke(app, ["stats", "--project", "demo"])
    assert result.exit_code == 0, result.output
    labels = [line.split()[0] for line in result.output.splitlines() if line[:1] != " "]
    assert labels == ["store", "recall", "inject", "vectors", "extract", "pipes", "recent"]


@pytest.mark.db
def test_json_is_one_object_with_stable_keys(env: str) -> None:
    result = runner.invoke(app, ["stats", "--project", "demo", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert set(doc) == {
        "project", "window_seconds", "now", "store", "retrieval", "injection",
        "vectors", "extraction", "pipelines", "recent", "unavailable",
    }


@pytest.mark.db
def test_window_parses_hours_and_days(env: str) -> None:
    doc = json.loads(
        runner.invoke(app, ["stats", "--project", "demo", "--json", "--window", "24h"]).output
    )
    assert doc["window_seconds"] == 86400


def test_a_bad_window_is_refused() -> None:
    result = runner.invoke(app, ["stats", "--window", "soon"])
    assert result.exit_code != 0


def test_an_unreachable_database_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BAG_DSN", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    assert runner.invoke(app, ["stats", "--project", "demo"]).exit_code == 1
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

```python
def _window(value: str) -> timedelta:
    """`7d`, `24h`, `30d`. Anything else is refused - a typo silently read as
    some default window would report numbers for a period nobody asked for."""
    match = re.fullmatch(r"(\d+)([dh])", value.strip())
    if not match or int(match.group(1)) == 0:
        raise typer.BadParameter("use a number followed by d or h, e.g. 7d or 24h")
    n = int(match.group(1))
    return timedelta(days=n) if match.group(2) == "d" else timedelta(hours=n)


@app.command()
def stats(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    window: Annotated[str, typer.Option("--window")] = "7d",
    recent: Annotated[int, typer.Option("--recent")] = 10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """How saddlebag is being used, and whether its pipelines keep up.

    Reads and session starts are counted from when usage logging began; a
    younger log says "since <date>" rather than claiming a quiet week. A
    section that could not be read prints "unavailable" and still exits 0.
    """
    from saddlebag.services import stats as stats_svc

    period = _window(window)
    name = _require_project(project or _default_project())
    root = _toplevel_or_none()
    with _session() as s:
        got = stats_svc.collect(
            s.store, s.owner.id, name, s.config,
            now=datetime.now(timezone.utc), window=period, recent=recent,
            current_root=root,
        )
    if as_json:
        typer.echo(json.dumps(stats_svc.to_dict(got), indent=2))
        return
    for line in stats_svc.render(got):
        typer.echo(line)
```

`_window` must run before `_session()` so a bad value fails without a
database (the test relies on it). `_toplevel_or_none`: use whatever
`reingest status` already calls to get the working tree root (grep
`current_project=` in `cli.py` and reuse that exact expression).

- [ ] **Step 4: Run, expect PASS; `make check`; commit**

```bash
git add src/saddlebag/cli.py tests/test_stats_cli.py
git commit -m "Add bag stats"
```

---

### Task 9: CLAUDE.md, live check, and whole-branch review

**Files:**
- Modify: `CLAUDE.md` (new `### Usage statistics` section after "The context block budget"; one line in "Hooks are fail-soft" noting the banner's stats lines)

- [ ] **Step 1: Write the section**

It must record, in this repo's voice (why, at length, spaced hyphens):
- the two logs, metadata only, query length never text, ids never content -
  and that this is why they sit outside the record opt-in;
- logging happens only when a frontend passes `source`; tests, the eval
  script and internal callers are never counted, and a new frontend must
  pass one or be invisible;
- every log write is savepointed and fail-soft (`services/usage.py`), and
  why the savepoint is load-bearing;
- `CLAUDE_CODE_SESSION_ID` is the variable Claude Code sets; the MCP server
  used to read `CLAUDE_SESSION_ID`, which is never set, so MCP-written
  entries before 026 carry no session id; after `/clear` the MCP server's
  id is the one its process started with;
- follow-through is a proxy - a rule obeyed from its summary is never
  opened;
- stats are fail-soft per section in the hook (savepoint +
  `statement_timeout`), loud about the database in `bag stats`, and never
  reach `additionalContext`;
- the "since" rule and that nothing is backfilled.

- [ ] **Step 2: `make check`** - green, and the `db` skip count is zero
  (`docker compose ps` first).

- [ ] **Step 3: Live check against the dev database**

```bash
bag db up
bag search pgvector >/dev/null
bag stats
bag stats --json | jq '.unavailable'
echo '{"cwd":"'"$PWD"'","session_id":"manual-check"}' | bag hook session-start | jq -r .systemMessage
```

Expected: `db up` applies 026; `bag stats` prints all seven labelled
sections with `recall    since 2026-09-16: 1 reads ...`; `unavailable` is
`{}`; the hook's `systemMessage` is the old first line followed by the
stats lines. Paste the output into the task report.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Record the usage statistics design in CLAUDE.md"
```

- [ ] **Step 5: Final whole-branch review** on the most capable model, with
  the full diff as a file, the spec, and the deferred-minors list; require a
  named verdict file.
