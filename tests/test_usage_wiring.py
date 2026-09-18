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
