"""The knowledge base context block, harness-neutral.

Used to live inside the Claude Code hook (agents/claude_code/hook.py), which
meant a second harness could record events (services/record.py, already
harness-neutral) but had no way to be told anything - injection was Claude
Code only. This is the decision moved out to where the layering says it
belongs: a service takes a store and a project and returns a string, opening
no session and reading no payload, because those two things are exactly what
differs between harnesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from uuid import UUID

from saddlebag.domain import InjectionRecord
from saddlebag.services import kb, record, usage
from saddlebag.store import Store


@dataclass(frozen=True)
class Handoff:
    topic: str
    age: str


@dataclass(frozen=True)
class InjectionLog:
    """Who is asking, for the injection log. Passing one is the opt-in.

    Without it `injection()` writes nothing - the same bargain `search.find`
    strikes with `source`: internal callers and tests that construct an
    `Injection` directly must not show up in usage statistics.
    """

    harness: str
    session_id: str | None


@dataclass(frozen=True)
class Injection:
    """What one session was handed, as facts rather than as a block.

    `text` is the block itself - what `block()` returns. The rest exists so
    a frontend can tell the user what happened without parsing it: which
    knowledge base (`project`), whether one existed (`found`), how many
    rules and notes the block actually carried (`rules`, `notes` - notes are
    dropped whole for budget, so this is the renderer's count, not the
    resolver's), whether the project records events, the live handoff, and
    which entries the block carried (`entry_ids`, for the injection log).
    """

    text: str
    project: str
    found: bool
    rules: int
    notes: int
    recording: bool
    handoff: Handoff | None
    entry_ids: tuple[UUID, ...] = ()


def block(
    store: Store,
    owner_id: UUID,
    project: str,
    max_chars: int,
    note: Callable[[str], None] | None = None,
    owner_handle: str | None = None,
    log: InjectionLog | None = None,
) -> str:
    """The knowledge base context block for one project, or "".

    `injection()` with only the text kept - what every caller that has no
    channel to a human wants (`bag hook context`, opencode, Cursor).
    """
    return injection(store, owner_id, project, max_chars, note, owner_handle, log).text


def injection(
    store: Store,
    owner_id: UUID,
    project: str,
    max_chars: int,
    note: Callable[[str], None] | None = None,
    owner_handle: str | None = None,
    log: InjectionLog | None = None,
) -> Injection:
    """The context block for one project, plus the facts about it.

    Returns "" rather than raising for a project with no knowledge base:
    every caller is a fail-soft hook, and a missing knowledge base is an
    ordinary state, not an error.

    `note` is how the reason escapes. The debug messages this replaces went
    straight to BAG_HOOK_DEBUG, which needs `env` - and a service that
    took `env` to decide where to print would be a service formatting
    output. So the service says what happened and the frontend decides
    where it goes: the Claude Code hook passes its `_debug`, the CLI passes
    its own, and a test passes a list's `append`.

    `owner_handle` is optional, separately from `note`, because it is data
    the service does not otherwise need - the caller already has an open
    session with the owner's handle on it (`s.owner.handle`), and naming the
    principal in the "no such knowledge base" message is the single most
    useful diagnostic there is for it: a wrong or unexpected
    BAG_USER_ID is one of the likeliest reasons for silent injection.
    Passing "" or leaving it unset just drops that clause; it never changes
    whether a caller is told anything.

    `log` is the opt-in for the injection log (see `InjectionLog`): passing
    one writes a row through `usage.log_injection`, itself fail-soft, so a
    broken log write costs only the log, never the session. Without it
    nothing is written, which is what keeps internal callers and tests that
    build an `Injection` directly out of usage statistics.

    `kb.render_block` raises `RulesExceedBudget` before this function knows
    anything worth logging - an over-budget knowledge base was never
    rendered, so there is no `chars` or `entry_ids` to write, and the raise
    is left to propagate uncaught rather than caught and logged as a
    failure.
    """
    say = note or (lambda _reason: None)

    rendered = ""
    found = False
    rules = notes = 0
    ids: tuple[UUID, ...] = ()
    try:
        collection = kb.get(store, owner_id, project)
    except kb.CollectionNotFound:
        for_principal = f" for principal '{owner_handle}'" if owner_handle else ""
        say(
            f"no knowledge base with slug '{project}'{for_principal}. "
            "The hook injects the knowledge base whose slug matches the "
            f"repository name - create one with `bag kb new {project}`.",
        )
    else:
        found = True
        entries = kb.resolve(store, owner_id, project)
        if entries:
            block_ = kb.render_block(collection, entries, max_chars)
            rendered, rules, notes = block_.text, block_.rules, block_.notes
            ids = block_.entry_ids
        else:
            say(f"knowledge base '{project}' matched no entries")

    # Appended after render, outside max_chars on purpose: it is a fixed
    # ~20 tokens, and making it compete with rules for the budget would be
    # absurd. It is also emitted for a project with no knowledge base at
    # all, which is why the block is built rather than returned early.
    live = live_handoff(store, owner_id, project)
    pointer = handoff_pointer(live)
    got = Injection(
        text="\n".join(part for part in (rendered, pointer) if part),
        project=project,
        found=found,
        rules=rules,
        notes=notes,
        recording=_recording(store, owner_id, project),
        handoff=live,
        entry_ids=ids,
    )
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


def _recording(store: Store, owner_id: UUID, project: str) -> bool:
    # A fact for the banner only. Same bargain as `live_handoff`: a helper
    # that can throw would turn a working knowledge base into no output at
    # all, and "off" is the safe answer for a question that could not be
    # asked.
    try:
        return record.is_enabled(store, owner_id, project)
    except Exception:
        return False


def live_handoff(
    store: Store, owner_id: UUID, project: str, now: datetime | None = None
) -> Handoff | None:
    """The live handoff for a project, as topic and age, or None.

    Never raises: block()'s caller treats any exception as silence, but a
    helper that can throw turns a working knowledge base into no output at
    all, which is a worse failure than a missing pointer.
    """
    try:
        from saddlebag.services import handoff

        entry = handoff.latest(store, owner_id, project=project)
        if entry is None or entry.created_at is None:
            return None
        topic = handoff.topic_of(entry) or project
        age = handoff.age_phrase(
            entry.created_at, now or datetime.now(tz=entry.created_at.tzinfo)
        )
        return Handoff(topic=topic, age=age)
    except Exception:
        return None


def handoff_pointer(live: Handoff | None) -> str:
    """One line naming the live handoff, or ""."""
    if live is None:
        return ""
    return f"Handoff available: {live.topic} ({live.age}) - run bag-prime {live.topic}"


def banner(got: Injection) -> str:
    """One line telling the user what this session was handed.

    Rendered from the facts, not parsed back out of the block, and here in
    the service so any frontend with a channel to a human prints the same
    line. Claude Code shows it prefixed with `SessionStart:startup says:`,
    which is why it is terse and why there is no version stamp.

    A missing knowledge base is the one state that gets more words: it is
    the likeliest reason a session gets no context, the hook has always
    swallowed it, and the fix is one command.
    """
    if not got.found:
        kb_part = f"no knowledge base '{got.project}' (bag kb new {got.project})"
    elif got.rules == 0 and got.notes == 0:
        kb_part = f"kb {got.project}: empty"
    else:
        counts = f"{_count(got.rules, 'rule')}, {_count(got.notes, 'note')}"
        kb_part = f"kb {got.project}: {counts}"

    parts = [
        "saddlebag",
        kb_part,
        f"recording {'on' if got.recording else 'off'}",
    ]
    if got.handoff is not None:
        parts.append(f"handoff: {got.handoff.topic} ({got.handoff.age})")
    return " · ".join(parts)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
