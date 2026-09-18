import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Never, override

import pytest

from saddlebag.agents.claude_code.hook import main, session_start
from saddlebag.backends.postgres.migrate import migrate


def test_returns_empty_when_the_database_is_unreachable():
    payload = json.dumps({"cwd": "/tmp/whatever", "session_id": "s1"})
    out = session_start(
        payload, env={"BAG_DSN": "postgresql://nobody@127.0.0.1:1/none"}
    )
    assert out is None


def test_returns_empty_on_malformed_stdin():
    assert session_start("{not json", env={}) is None


def test_returns_empty_on_empty_stdin():
    assert session_start("", env={}) is None


def test_main_exits_zero_when_the_database_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BAG_DSN", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": "/tmp/x"})))
    assert main() == 0
    assert capsys.readouterr().out == ""


def test_main_exits_zero_on_garbage_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert main() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.db
def test_injects_the_project_knowledge_base(
    live_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import psycopg

    from saddlebag.services import kb
    from saddlebag.services.write import remember
    from saddlebag.session import open_session

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))

    # session_start spawns three detached `bag` processes in a `finally` on
    # every path. Pointed at this live test database they outlive the test
    # and race conftest's truncate-cascade for table locks - the same
    # deadlock test_hook_context_cli.py's env fixture stubs against.
    def no_spawn(env: Mapping[str, str]) -> bool:
        return False

    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_process", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_ingest", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_memory", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_transcripts", no_spawn)

    project_dir = tmp_path / "myproj"
    project_dir.mkdir()

    with open_session() as s:
        from saddlebag.domain import CollectionQuery, Kind

        kb.create(
            s.store,
            s.owner.id,
            slug="myproj",
            title="myproj",
            query=CollectionQuery(project="myproj"),
        )
        remember(
            s.store,
            s.owner.id,
            title="Lint rule",
            body="always run ruff",
            summary="Run ruff linter",
            kind=Kind.RULE,
            project="myproj",
        )
        s.conn.commit()

    result = session_start(
        json.dumps({"cwd": str(project_dir), "session_id": "s1"}),
        env={
            "BAG_DSN": live_dsn,
            "BAG_USER_ID": "brandon",
            "BAG_CONFIG": str(tmp_path / "none.toml"),
        },
    )
    assert result is not None
    got, _lines = result
    assert "Run ruff linter" in got.text

    # The same session, through main(): what Claude Code actually reads. A
    # JSON document, because that is the only shape that carries both a
    # line for the user (systemMessage) and the block for the model
    # (additionalContext) - plain stdout can only do the second.
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(project_dir), "session_id": "s1"})),
    )
    assert main() == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Run ruff linter" in doc["hookSpecificOutput"]["additionalContext"]
    # Stats lines land only in systemMessage - the database is reachable
    # here, so the banner may carry them after the first line.
    assert doc["systemMessage"].startswith(
        "saddlebag · kb myproj: 1 rule, 0 notes · recording off"
    )


@pytest.mark.db
def test_the_banner_carries_stats_and_the_model_pays_nothing_for_them(
    live_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stats reach only systemMessage - additionalContext is what the model
    is billed for, and this feature must cost it nothing."""
    import psycopg

    from saddlebag.services import kb
    from saddlebag.services.write import remember
    from saddlebag.session import open_session

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))

    def no_spawn(env: Mapping[str, str]) -> bool:
        return False

    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_process", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_ingest", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_memory", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_transcripts", no_spawn)

    project_dir = tmp_path / "myproj"
    project_dir.mkdir()

    with open_session() as s:
        from saddlebag.domain import CollectionQuery, Kind

        kb.create(
            s.store,
            s.owner.id,
            slug="myproj",
            title="myproj",
            query=CollectionQuery(project="myproj"),
        )
        remember(
            s.store,
            s.owner.id,
            title="Lint rule",
            body="always run ruff",
            summary="Run ruff linter",
            kind=Kind.RULE,
            project="myproj",
        )
        s.conn.commit()

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(project_dir), "session_id": "s1"})),
    )
    assert main() == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["systemMessage"].splitlines()[0].startswith("saddlebag · kb ")
    assert any(line.startswith("store ") for line in doc["systemMessage"].splitlines())
    # The model's context is untouched by stats.
    assert "store " not in doc["hookSpecificOutput"]["additionalContext"]


@pytest.mark.db
def test_a_broken_stats_collection_leaves_exactly_todays_banner(
    live_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stats are a nicety on top of the injection - a failure inside
    collection must cost the stats lines only, never the block or the
    banner's first line, and additionalContext must be untouched."""
    import psycopg

    from saddlebag.services import kb
    from saddlebag.services.write import remember
    from saddlebag.session import open_session

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))

    def no_spawn(env: Mapping[str, str]) -> bool:
        return False

    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_process", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_ingest", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_memory", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_transcripts", no_spawn)

    project_dir = tmp_path / "myproj"
    project_dir.mkdir()

    with open_session() as s:
        from saddlebag.domain import CollectionQuery, Kind

        kb.create(
            s.store,
            s.owner.id,
            slug="myproj",
            title="myproj",
            query=CollectionQuery(project="myproj"),
        )
        remember(
            s.store,
            s.owner.id,
            title="Lint rule",
            body="always run ruff",
            summary="Run ruff linter",
            kind=Kind.RULE,
            project="myproj",
        )
        s.conn.commit()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("stats exploded")

    monkeypatch.setattr("saddlebag.services.stats.collect", boom)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(project_dir), "session_id": "s1"})),
    )
    assert main() == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["systemMessage"] == (
        "saddlebag · kb myproj: 1 rule, 0 notes · recording off"
    )
    assert "\n" not in doc["systemMessage"]
    assert "Run ruff linter" in doc["hookSpecificOutput"]["additionalContext"]


@pytest.mark.db
def test_returns_empty_when_the_project_has_no_knowledge_base(
    live_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    def no_spawn(env: Mapping[str, str]) -> bool:
        return False

    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_process", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_ingest", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_memory", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_transcripts", no_spawn)
    env = {
        "BAG_DSN": live_dsn,
        "BAG_USER_ID": "brandon",
        "BAG_CONFIG": str(tmp_path / "none.toml"),
    }
    result = session_start(json.dumps({"cwd": str(tmp_path / "unknown-proj")}), env=env)
    # No block for the model - but the database answered, so the user gets
    # a banner saying which knowledge base was looked for and not found.
    assert result is not None
    got, _lines = result
    assert got.text == ""
    assert got.found is False


def test_debug_is_silent_unless_asked_for(capsys: pytest.CaptureFixture[str]) -> None:
    payload = json.dumps({"cwd": "/tmp/whatever", "session_id": "s1"})
    out = session_start(
        payload, env={"BAG_DSN": "postgresql://nobody@127.0.0.1:1/none"}
    )
    captured = capsys.readouterr()
    assert out is None
    assert captured.out == ""
    assert captured.err == ""


def test_main_is_silent_on_both_streams_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BAG_HOOK_DEBUG", raising=False)
    monkeypatch.setenv("BAG_DSN", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": "/tmp/x"})))
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_debug_explains_an_unreachable_database_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = json.dumps({"cwd": "/tmp/whatever", "session_id": "s1"})
    out = session_start(
        payload,
        env={
            "BAG_DSN": "postgresql://nobody@127.0.0.1:1/none",
            "BAG_HOOK_DEBUG": "1",
        },
    )
    captured = capsys.readouterr()
    assert out is None
    assert captured.out == ""
    assert "bag hook" in captured.err


def test_debug_never_breaks_fail_soft(capsys: pytest.CaptureFixture[str]) -> None:
    """Even if writing the diagnostic blows up, the hook still returns None."""

    class Exploding(dict[str, str]):
        @override
        def get(self, key: str, default: object = None, /) -> Never:
            raise RuntimeError("boom")

    assert session_start(json.dumps({"cwd": "/tmp/x"}), env=Exploding()) is None


@pytest.mark.db
def test_debug_names_the_missing_knowledge_base(
    live_dsn: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    def no_spawn(env: Mapping[str, str]) -> bool:
        return False

    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_process", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_ingest", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_memory", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_transcripts", no_spawn)
    env = {
        "BAG_DSN": live_dsn,
        "BAG_USER_ID": "brandon",
        "BAG_CONFIG": str(tmp_path / "none.toml"),
        "BAG_HOOK_DEBUG": "1",
    }
    result = session_start(json.dumps({"cwd": str(tmp_path / "unknown-proj")}), env=env)
    err = capsys.readouterr().err
    assert result is not None
    got, _lines = result
    assert got.text == ""
    assert "unknown-proj" in err


def test_main_guard_survives_a_failure_inside_session_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() has its own try/except as a second layer. Prove it works.

    Both other main() tests pass even if this guard is deleted, because
    session_start's own guard already covers them. This one bypasses that by
    making session_start itself raise.
    """
    import saddlebag.agents.claude_code.hook as hook

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("session_start exploded")

    monkeypatch.setattr(hook, "session_start", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"cwd": "/tmp/x"}'))

    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_returns_zero_when_stdin_itself_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook must exit 0 even if reading stdin fails."""
    import saddlebag.agents.claude_code.hook as hook

    class ExplodingStdin:
        def read(self) -> str:
            raise OSError("stdin is gone")

    monkeypatch.setattr("sys.stdin", ExplodingStdin())
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
