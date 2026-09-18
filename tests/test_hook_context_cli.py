"""`bag hook context` - the harness-neutral half of SessionStart.

Same contract as `bag record event` (tests/test_record_cli.py): a hook
entry point in everything but name, so it must exit 0 unconditionally and
print nothing but the block itself to stdout. Every early return explains
itself only through BAG_HOOK_DEBUG on stderr.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from saddlebag.agents.base import Identity
from saddlebag.agents.claude_code.adapter import ClaudeCodeAdapter
from saddlebag.agents.cursor.adapter import ROOT_KEY, CursorAdapter
from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.cli import app

runner = CliRunner()

pytestmark = pytest.mark.db


@pytest.fixture
def env(live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()
    monkeypatch.setenv("BAG_DSN", live_dsn)
    monkeypatch.setenv("BAG_USER_ID", "brandon")
    monkeypatch.setenv("BAG_CONFIG", str(tmp_path / "none.toml"))

    # `hook context` spawns four detached `bag` processes in a `finally`
    # on every path (extraction, re-ingest, the memory sync and the
    # transcript refresh). Pointed at this live test
    # database, those processes outlive the test and race conftest's
    # truncate-cascade for locks on the same tables - a deadlock seen twice
    # on this branch. Every test gets the no-op stub by default; the tests
    # that assert on spawning install their own recorder afterwards, which
    # wins because monkeypatch applies in call order.
    def no_spawn(env: Mapping[str, str]) -> bool:
        return False

    monkeypatch.setattr("saddlebag.hookio.spawn_process", no_spawn)
    monkeypatch.setattr("saddlebag.hookio.spawn_ingest", no_spawn)
    monkeypatch.setattr("saddlebag.hookio.spawn_memory", no_spawn)
    monkeypatch.setattr("saddlebag.hookio.spawn_transcripts", no_spawn)
    return live_dsn


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "myrepo"
    root.mkdir()
    return root


def _seed_kb(dsn: str, *, project: str = "myrepo") -> None:
    """A knowledge base whose slug matches `repo`'s directory name, with one
    rule in it - so a test can assert the command's stdout actually carries
    that rule, not merely that the command ran without crashing."""
    from saddlebag.domain import CollectionQuery, Kind
    from saddlebag.services import kb
    from saddlebag.services.write import remember

    with psycopg.connect(dsn) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        kb.create(
            store,
            owner.id,
            slug=project,
            title=project,
            query=CollectionQuery(project=project),
        )
        remember(
            store,
            owner.id,
            title="Lint rule",
            body="always run ruff",
            summary="Run ruff linter",
            kind=Kind.RULE,
            project=project,
        )
        c.commit()


def test_context_exits_zero_on_garbage_stdin(env: str) -> None:
    """Fail-soft: malformed stdin must not be why the block is missing."""
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_garbage_stdin_under_hook_debug(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAG_HOOK_DEBUG", "1")
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "not valid JSON" in result.stderr


def test_context_exits_zero_for_an_unknown_agent(env: str, repo: Path) -> None:
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "no-such-agent"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_an_unknown_agent_under_hook_debug(
    env: str, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    monkeypatch.setenv("BAG_HOOK_DEBUG", "1")
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "no-such-agent"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no-such-agent" in result.stderr


def test_context_exits_zero_when_the_payload_carries_no_cwd(env: str) -> None:
    """No cwd means identity.project is None - an ordinary "cannot place
    this session" answer, not an error."""
    result = runner.invoke(
        app, ["hook", "context"], input=json.dumps({"session_id": "x"})
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_context_explains_a_missing_cwd_under_hook_debug(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAG_HOOK_DEBUG", "1")
    result = runner.invoke(
        app, ["hook", "context"], input=json.dumps({"session_id": "x"})
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no project" in result.stderr.lower() or "cwd" in result.stderr.lower()


def test_an_adapter_whose_identity_capability_raises_degrades(
    env: str, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """Same contract as event()/env_settings()/settings_path(): a broken
    third-party adapter must never be why the block is missing for
    everyone - it just costs the block, silently."""

    def boom(
        self: ClaudeCodeAdapter, env: Mapping[str, str], payload: dict[str, Any]
    ) -> Identity:
        raise RuntimeError("boom")

    monkeypatch.setattr(ClaudeCodeAdapter, "identity", boom)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert result.stdout == ""


def test_an_adapter_whose_identity_capability_raises_explains_itself(
    env: str, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    monkeypatch.setenv("BAG_HOOK_DEBUG", "1")

    def boom(
        self: ClaudeCodeAdapter, env: Mapping[str, str], payload: dict[str, Any]
    ) -> Identity:
        raise RuntimeError("boom")

    monkeypatch.setattr(ClaudeCodeAdapter, "identity", boom)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert "identity" in result.stderr
    assert "boom" in result.stderr


def test_context_prints_the_knowledge_base_for_the_session(
    env: str, repo: Path
) -> None:
    """The happy path: this is the command's only reason to exist, and
    nothing above pins it - every other test here uses a repo with no
    knowledge base, so a deleted `typer.echo(...)` would leave them all
    green."""
    _seed_kb(env, project=repo.name)

    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )

    assert result.exit_code == 0
    assert "Lint rule" in result.stdout
    assert "Run ruff linter" in result.stdout


def test_hook_context_logs_the_injection_with_its_harness(env: str, repo: Path) -> None:
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


def test_context_reads_a_named_agent_and_matches_the_default(
    env: str, repo: Path
) -> None:
    """The opencode adapter reads sessionID, not session_id - a payload
    shape difference `--agent` exists to absorb. This is the injection-half
    equivalent of `bag record event`'s --agent tests: with a knowledge
    base actually in place, the two adapters must read different keys out
    of different payloads and still produce byte-identical output - proving
    --agent reached a distinct, working adapter rather than merely failing
    to find one (which would also print nothing and exit 0)."""
    _seed_kb(env, project=repo.name)

    claude_code = runner.invoke(
        app,
        ["hook", "context", "--agent", "claude-code"],
        input=json.dumps({"cwd": str(repo), "session_id": "x"}),
    )
    opencode = runner.invoke(
        app,
        ["hook", "context", "--agent", "opencode"],
        input=json.dumps({"cwd": str(repo), "sessionID": "x"}),
    )

    assert claude_code.exit_code == 0
    assert opencode.exit_code == 0
    assert claude_code.stdout != ""
    assert claude_code.stdout == opencode.stdout


def test_an_adapter_whose_inject_capability_raises_degrades_to_stdout(
    env: str, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """Same contract as identity()/event()/env_settings()/settings_path(): a
    broken inject() must not be why the block never reaches the harness -
    it just falls back to the stdout path every other adapter already
    uses."""
    _seed_kb(env, project=repo.name)

    def boom(
        self: CursorAdapter,
        block: str,
        payload: dict[str, Any],
        note: Callable[[str], None] | None = None,
    ) -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(CursorAdapter, "inject", boom)
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "cursor"],
        input=json.dumps({ROOT_KEY: [str(repo)], "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert "Lint rule" in result.stdout


def test_an_adapter_whose_inject_capability_raises_explains_itself(
    env: str, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    monkeypatch.setenv("BAG_HOOK_DEBUG", "1")
    _seed_kb(env, project=repo.name)

    def boom(
        self: CursorAdapter,
        block: str,
        payload: dict[str, Any],
        note: Callable[[str], None] | None = None,
    ) -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(CursorAdapter, "inject", boom)
    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "cursor"],
        input=json.dumps({ROOT_KEY: [str(repo)], "session_id": "x"}),
    )
    assert result.exit_code == 0
    assert "inject" in result.stderr
    assert "boom" in result.stderr


def test_an_adapter_with_inject_does_not_print_the_block_to_stdout(
    env: str, repo: Path
) -> None:
    """Cursor cannot read stdout - printing the block there anyway would be
    noise nobody reads, not a useful fallback. inject() being present and
    succeeding means delivery already happened, so stdout stays empty and
    the block lands in the rules file instead."""
    _seed_kb(env, project=repo.name)

    result = runner.invoke(
        app,
        ["hook", "context", "--agent", "cursor"],
        input=json.dumps({ROOT_KEY: [str(repo)], "session_id": "x"}),
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    written = repo / ".cursor" / "rules" / "saddlebag.mdc"
    assert written.exists()
    assert "Lint rule" in written.read_text()


# --- Extraction is triggered from here, not only from Claude Code ---------
#
# `bag events process` used to be spawned from exactly one place, Claude
# Code's SessionStart hook, so a Cursor-only or opencode-only install
# recorded events forever and never extracted one. `hook context` is the
# session-start analogue every other harness already calls once per session,
# which makes it the one trigger all three share.


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch the spawn where `hook context` looks it up, and record calls."""
    calls: list[dict[str, Any]] = []

    def fake(env: Mapping[str, str]) -> bool:
        calls.append(dict(env))
        return True

    monkeypatch.setattr("saddlebag.hookio.spawn_process", fake)
    return calls


def test_context_spawns_the_extraction_processor(
    env: str, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy(monkeypatch)
    _seed_kb(env)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "s1"}),
    )
    assert result.exit_code == 0
    assert len(calls) == 1


def test_context_spawns_the_processor_even_when_no_project_resolves(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backlog is global, not this session's project.

    `bag events process` works off every extractable session for the
    owner, so whether THIS payload produced a block has no bearing on
    whether there is extraction work waiting. Claude Code spawns
    regardless of whether its block rendered; this must match.
    """
    calls = _spy(monkeypatch)
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert result.stdout == ""
    assert len(calls) == 1


def test_context_spawns_the_memory_sync(
    env: str, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third spawn, from the same `finally` and for the same reason.

    A designated project's memory directory goes stale otherwise, and
    nothing but a session start reliably happens. Whether the spawned
    refresh has anything to do is its own question - an undesignated
    project exits 0 having done nothing.
    """
    calls: list[dict[str, Any]] = []

    def record_spawn(env: Mapping[str, str]) -> bool:
        calls.append(dict(env))
        return True

    monkeypatch.setattr("saddlebag.hookio.spawn_memory", record_spawn)
    _seed_kb(env)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "s1"}),
    )
    assert result.exit_code == 0
    assert len(calls) == 1


def test_context_spawns_the_memory_sync_even_when_no_project_resolves(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the `finally`, like its two siblings, so every early return
    reaches it - unusable stdin included."""
    calls: list[dict[str, Any]] = []

    def record_spawn(env: Mapping[str, str]) -> bool:
        calls.append(dict(env))
        return True

    monkeypatch.setattr("saddlebag.hookio.spawn_memory", record_spawn)
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert len(calls) == 1


def test_context_spawns_the_transcript_refresh(
    env: str, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth spawn, from the same `finally` and for the same reason.

    A claimed transcript directory goes unread otherwise, and nothing but a
    session start reliably happens. Whether the spawned refresh has anything
    to do is its own question - a project with no claimed directory exits 0
    having done nothing.
    """
    calls: list[dict[str, Any]] = []

    def record_spawn(env: Mapping[str, str]) -> bool:
        calls.append(dict(env))
        return True

    monkeypatch.setattr("saddlebag.hookio.spawn_transcripts", record_spawn)
    _seed_kb(env)
    result = runner.invoke(
        app,
        ["hook", "context"],
        input=json.dumps({"cwd": str(repo), "session_id": "s1"}),
    )
    assert result.exit_code == 0
    assert len(calls) == 1


def test_hook_context_spawns_transcripts_even_when_it_returns_no_block(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The call sits in a `finally`, like its three siblings.

    The backlog is global: whether THIS payload produced a block says
    nothing about whether there are transcripts waiting.
    """
    calls: list[dict[str, Any]] = []

    def record_spawn(env: Mapping[str, str]) -> bool:
        calls.append(dict(env))
        return True

    monkeypatch.setattr("saddlebag.hookio.spawn_transcripts", record_spawn)
    result = runner.invoke(app, ["hook", "context"], input="not json")
    assert result.exit_code == 0
    assert len(calls) == 1


def test_spawn_process_refuses_to_run_inside_the_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recursion guard, exercised directly on the shared helper.

    The extractor spawns `claude -p`, whose own hooks would otherwise spawn
    another extractor, which reads the events that run recorded, without
    bound. CHILD_ENV_VAR is what stops it. Asserted against spawn_process
    itself rather than through the CLI, because the command legitimately
    shells out to git to resolve the project - trapping every Popen would
    catch that instead and pass for the wrong reason.
    """
    from saddlebag import hookio
    from saddlebag.extract.base import CHILD_ENV_VAR

    def explode(*a: object, **k: object) -> None:
        raise AssertionError("spawned a processor inside the extractor")

    monkeypatch.setattr("subprocess.Popen", explode)
    assert hookio.spawn_process({CHILD_ENV_VAR: "1"}) is False


def test_spawn_process_launches_the_processor_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not be the only reason it ever returns False."""
    from saddlebag import hookio

    seen: dict[str, Any] = {}

    def fake_popen(argv: Sequence[str], **kwargs: object) -> object:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    assert hookio.spawn_process({}) is True
    assert seen["argv"] == ["bag", "events", "process"]
    assert seen["kwargs"]["start_new_session"] is True
