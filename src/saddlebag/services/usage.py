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
from contextlib import AbstractContextManager
from typing import Any, Protocol

from saddlebag.domain import AccessRecord, InjectionRecord
from saddlebag.store import Store


class _AccessStore(Protocol):
    """The two members `log_access` actually calls, narrow like
    `search.SearchStore`.

    `search.find` calls this with its own `SearchStore` - a Protocol naming
    only the methods `find` uses, not the full `Store`. `SearchStore`
    structurally has `log_access` and `transaction` (see search.py), but it
    does not have the rest of `Store`'s surface, so passing it where a
    parameter is typed `Store` is a real narrowing pyrefly is right to
    reject: nothing here guarantees the wider contract holds. `log_injection`
    keeps the full `Store` because its only caller (Task 4, a session-start
    hook) always has one.
    """

    def log_access(self, record: AccessRecord) -> None: ...
    def transaction(self) -> AbstractContextManager[Any]: ...


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
    store: _AccessStore, record: AccessRecord, note: Callable[[str], None] | None = None
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
