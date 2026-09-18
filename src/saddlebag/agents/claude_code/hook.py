"""Claude Code hook entry points: SessionStart, PostToolUse/SessionEnd
recording, and the UserPromptSubmit session-size reminder.

Fail-soft is a hard requirement across all of them: bounded work, exit 0
unconditionally, print nothing on error. A knowledge tool must never be why a
session will not start, a tool call will not run, or a prompt will not send.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from saddlebag import session_size as session_size_mod
from saddlebag.agents.claude_code.adapter import ClaudeCodeAdapter
from saddlebag.config import load
from saddlebag.extract.base import CHILD_ENV_VAR
from saddlebag.hookio import debug as _debug
from saddlebag.hookio import (
    spawn_ingest,
    spawn_memory,
    spawn_process,
    spawn_transcripts,
)
from saddlebag.services import context, record, stats
from saddlebag.session import open_session


def session_start(
    stdin_text: str, env: Mapping[str, str]
) -> tuple[context.Injection, list[str] | None] | None:
    """What the session's project gets, and the stats lines for the banner
    (or None if collecting them failed), or None for any problem with the
    injection itself.

    None is the fail-soft answer - nothing reached the database, so there
    is nothing true to tell anyone. A project with no knowledge base is not
    that: the database answered, and the `Injection` says so with an empty
    block and `found=False`, which is how the user comes to see it.
    """
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError, AttributeError:
        _debug(env, "stdin was not valid JSON")
        return None

    try:
        identity = ClaudeCodeAdapter().identity(env, payload)
        if not identity.project:
            _debug(env, "the hook payload carried no cwd")
            return None

        config = load(env=env)
        with open_session(config) as s:
            got = context.injection(
                s.store,
                s.owner.id,
                identity.project,
                config.max_chars,
                note=lambda reason: _debug(env, reason),
                owner_handle=s.owner.handle,
                log=context.InjectionLog("claude-code", identity.session_id),
            )
            # Stats are a nicety on top of the injection, collected in the
            # same session - a session start must not open a second
            # connection. Ordering matters: `injection()` above can raise
            # `RulesExceedBudget`, caught by the `except Exception` below,
            # which must stay silence rather than a banner with stats and
            # no block. Collecting stats after `got` is what keeps that
            # true - moving this earlier would collect stats for a session
            # that never gets a block at all.
            payload_cwd = payload.get("cwd")
            try:
                lines = stats.render(
                    stats.collect(
                        s.store,
                        s.owner.id,
                        identity.project,
                        config,
                        now=datetime.now(timezone.utc),
                        # A session start pays for every candidate it
                        # counts, so the banner asks for far fewer than a
                        # typed command does.
                        awaiting_limit=stats.BANNER_AWAITING_LIMIT,
                        current_root=Path(payload_cwd)
                        if isinstance(payload_cwd, str)
                        else None,
                    )
                )
            except Exception as exc:
                # Stats are a nicety on top of the injection; losing them
                # must never cost the block. One line, today's banner.
                _debug(env, f"stats: {type(exc).__name__}: {exc}")
                lines = None
            return got, lines
    except Exception as exc:
        # Any failure at all - unreachable database, missing migrations, an
        # over-budget knowledge base - is silence, never a broken session.
        _debug(env, f"{type(exc).__name__}: {exc}")
        return None


def render_output(got: context.Injection, stats_lines: list[str] | None = None) -> str:
    """The JSON document Claude Code reads from a SessionStart hook.

    JSON rather than the bare block because it is the only shape that
    carries two things: `additionalContext`, which is the block and goes to
    the model exactly as plain stdout used to, and `systemMessage`, which
    Claude Code prints to the user as `SessionStart:startup says: ...`. Once
    stdout is JSON, plain text is no longer read as context, so the block
    must travel inside it - never write both.

    `stats_lines` reaches only `systemMessage`, via `context.banner` -
    `additionalContext` is unchanged whether or not stats were collected,
    so the model pays nothing for this feature.
    """
    specific: dict[str, str] = {"hookEventName": "SessionStart"}
    if got.text:
        specific["additionalContext"] = got.text
    return json.dumps(
        {
            "hookSpecificOutput": specific,
            "systemMessage": context.banner(got, stats_lines),
        },
        ensure_ascii=False,
    )


def main() -> int:
    try:
        env = dict(os.environ)
        result = session_start(sys.stdin.read(), env=env)
        if result is not None:
            got, lines = result
            sys.stdout.write(render_output(got, lines))
        spawn_process(env)
        spawn_ingest(env)
        spawn_memory(env)
        spawn_transcripts(env)
    except Exception:
        pass
    return 0


def record_event(stdin_text: str, env: Mapping[str, str]) -> None:
    """Record one Claude Code hook payload as an event, or do nothing.

    This is the PostToolUse hook - it runs once per tool call - and, since
    `ClaudeCodeAdapter.event()` maps both payload shapes, it is also what
    `bag hook session-end` now points at: a `session_end` event shortens
    the idle wait extraction runs on, but the pipeline no longer needs it,
    which is what lets a harness without a SessionEnd hook lose nothing but
    time.

    Same fail-soft contract as every other hook: malformed stdin, an
    unreachable database, a project that has not opted in - all silent, all
    reported only under BAG_HOOK_DEBUG, and none of them ever raise past
    here.
    """
    if env.get(CHILD_ENV_VAR):
        # The extractor's own `claude -p` child would otherwise record the
        # extraction itself as events, which the next extraction would then
        # read - an unbounded feedback loop. Both hooks check this; do not
        # add a third copy that checks something else.
        _debug(env, "inside an extraction child; not recording its events")
        return

    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError, AttributeError:
        _debug(env, "stdin was not valid JSON")
        return

    try:
        adapter = ClaudeCodeAdapter()
        harness_event = adapter.event(env, payload)
        if harness_event is None:
            _debug(env, "payload was not an event worth recording")
            return

        config = load(env=env)
        with open_session(config) as s:
            result = record.record(s.store, s.owner.id, harness_event, adapter.name)
        if result is None:
            _debug(
                env,
                f"recording is not enabled for project "
                f"{harness_event.project!r}. Enable it with `bag record "
                f"enable --project {harness_event.project}`.",
            )
    except Exception as exc:
        _debug(env, f"{type(exc).__name__}: {exc}")


def main_record_event() -> int:
    try:
        record_event(sys.stdin.read(), env=dict(os.environ))
    except Exception:
        pass
    return 0


def session_size(stdin_text: str, env: Mapping[str, str]) -> str:
    """A reminder to hand off, or "" - which is most prompts.

    Same fail-soft contract as the other hooks, and one more reason for it:
    this runs on every single user prompt.
    """
    if env.get(CHILD_ENV_VAR):
        return ""

    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError, AttributeError:
        _debug(env, "stdin was not valid JSON")
        return ""

    try:
        transcript_path = payload.get("transcript_path")
        if not transcript_path:
            _debug(env, "the hook payload carried no transcript_path")
            return ""
        session_id = payload.get("session_id") or "unknown"

        config = load(env=env)
        count = session_size_mod.count_turns(transcript_path)
        last = session_size_mod.read_last_warned(session_id)
        if not session_size_mod.should_warn(
            count, last, config.turn_warn_at, config.turn_warn_every
        ):
            return ""
        session_size_mod.record_warned(session_id, count)
        return session_size_mod.reminder(count)
    except Exception as exc:
        _debug(env, f"{type(exc).__name__}: {exc}")
        return ""


def main_session_size() -> int:
    try:
        text = session_size(sys.stdin.read(), env=dict(os.environ))
        if text:
            sys.stdout.write(text)
    except Exception:
        pass
    return 0
