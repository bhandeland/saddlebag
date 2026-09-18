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
    assert labels == [
        "store",
        "recall",
        "inject",
        "vectors",
        "extract",
        "pipes",
        "recent",
    ]


@pytest.mark.db
def test_json_is_one_object_with_stable_keys(env: str) -> None:
    result = runner.invoke(app, ["stats", "--project", "demo", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert set(doc) == {
        "project",
        "window_seconds",
        "now",
        "store",
        "retrieval",
        "injection",
        "vectors",
        "extraction",
        "pipelines",
        "recent",
        "unavailable",
    }


@pytest.mark.db
def test_window_parses_hours_and_days(env: str) -> None:
    doc = json.loads(
        runner.invoke(
            app, ["stats", "--project", "demo", "--json", "--window", "24h"]
        ).output
    )
    assert doc["window_seconds"] == 86400


def test_a_bad_window_is_refused() -> None:
    result = runner.invoke(app, ["stats", "--window", "soon"])
    assert result.exit_code != 0


def test_an_unreachable_database_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAG_DSN", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))
    assert runner.invoke(app, ["stats", "--project", "demo"]).exit_code == 1
