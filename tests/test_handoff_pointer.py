import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from saddlebag.agents.claude_code import hook
from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import CollectionQuery
from saddlebag.services import handoff, kb, write

pytestmark = pytest.mark.db

BODY = "## Done\nx\n\n## In flight\n\n## Next steps\n\n## Gotchas\n"


@pytest.fixture
def live(
    live_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, str]:
    import psycopg

    with psycopg.connect(live_dsn) as c:
        migrate(c)
        c.commit()

    # session_start spawns four detached `bag` processes in a `finally` on
    # every path. Pointed at this live test database they outlive the test
    # and race conftest's truncate-cascade for table locks - the same
    # deadlock test_hook_context_cli.py's env fixture stubs against.
    def no_spawn(env: Mapping[str, str]) -> bool:
        return False

    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_process", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_ingest", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_memory", no_spawn)
    monkeypatch.setattr("saddlebag.agents.claude_code.hook.spawn_transcripts", no_spawn)
    return {
        "BAG_DSN": live_dsn,
        "BAG_USER_ID": "brandon",
        "BAG_CONFIG": str(tmp_path / "none.toml"),
    }


def _seed(
    dsn: str, *, with_kb: bool, with_handoff: bool, project: str = "saddlebag"
) -> None:
    import psycopg

    with psycopg.connect(dsn) as c:
        store = PostgresStore(c)
        owner = store.ensure_principal("brandon")
        if with_kb:
            kb.create(
                store,
                owner.id,
                slug=project,
                title=project,
                query=CollectionQuery(project=project),
            )
            write.remember(
                store, owner.id, title="A note", body="body", project=project
            )
        if with_handoff:
            handoff.write(store, owner.id, project=project, topic="ci", body=BODY)
        c.commit()


def _payload(cwd: Path) -> str:
    return json.dumps({"cwd": str(cwd)})


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository, since the project comes from git, not the dir."""
    import subprocess

    d = tmp_path / "saddlebag"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    return d


def test_the_pointer_is_appended_to_the_context_block(
    live: dict[str, str], repo: Path
) -> None:
    _seed(live["BAG_DSN"], with_kb=True, with_handoff=True)
    result = hook.session_start(_payload(repo), env=live)
    assert result is not None
    got, _lines = result
    assert "A note" in got.text
    assert "Handoff available: ci" in got.text
    assert "bag-prime ci" in got.text


def test_the_pointer_appears_with_no_knowledge_base_at_all(
    live: dict[str, str], repo: Path
) -> None:
    _seed(live["BAG_DSN"], with_kb=False, with_handoff=True)
    result = hook.session_start(_payload(repo), env=live)
    assert result is not None
    got, _lines = result
    assert "Handoff available: ci" in got.text


def test_no_handoff_means_no_pointer(live: dict[str, str], repo: Path) -> None:
    _seed(live["BAG_DSN"], with_kb=True, with_handoff=False)
    result = hook.session_start(_payload(repo), env=live)
    assert result is not None
    got, _lines = result
    assert "Handoff available" not in got.text


def test_nothing_at_all_still_returns_empty(live: dict[str, str], repo: Path) -> None:
    _seed(live["BAG_DSN"], with_kb=False, with_handoff=False)
    result = hook.session_start(_payload(repo), env=live)
    # The database answered, so this is an Injection with nothing in it -
    # None is reserved for a hook that never got that far.
    assert result is not None
    got, _lines = result
    assert got.text == ""
