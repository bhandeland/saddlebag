"""`bag stats` and the SessionStart banner's stats lines.

`collect` gathers facts; `render` and `to_dict` format them. Every section
is collected on its own and a failure becomes `Unavailable(reason)`: in the
hook, one slow or broken query must cost one line, not the banner - and
`bag stats` reports "I could not tell" as a line rather than an exit code,
the same rule `bag doctor` follows for UNCHECKED.

Rendering is pure and shared, so the banner and the CLI can never disagree
about what a number means - the same reason every policy decision in this
package lives in a service rather than in a frontend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from saddlebag.config import Config
from saddlebag.domain import (
    AccessSummary,
    Entry,
    EntryCounts,
    IngestRun,
    InjectionSummary,
    MemoryRun,
    PipelineCounts,
    Query,
    TranscriptRun,
)
from saddlebag.services import (
    extraction,
    handoff,
    ingest,
    kb,
    memory,
    search,
    transcripts,
)
from saddlebag.store import Store

#: Per section, not per collection. The SessionStart hook has ten seconds
#: for everything it does, and a section that cannot answer inside 1.5s is
#: better reported as unavailable than paid for by every other section.
STATEMENT_TIMEOUT_MS = 1500

LABEL_WIDTH = 10

#: The search tiers in the order `search.find` runs them, which is the
#: order a reader compares them in. Not derived from the summary's dict:
#: its key order is whatever the query returned.
TIER_ORDER = ("exact", "semantic", "fuzzy", "none")

#: Longer titles are cut rather than wrapped - the recent list is a column
#: in a banner, and a wrapped title costs a line the banner does not have.
MAX_TITLE = 60

T = TypeVar("T")


@dataclass(frozen=True)
class Unavailable:
    """A section that could not be collected, and why.

    Never a zero and never silence: a zero read count and a read count
    nobody could measure are different statements, and conflating them is
    how a broken banner reads as a quiet one.
    """

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
    #: None means this project has no knowledge base - an ordinary state,
    #: not a failure, so it renders as a missing clause rather than as an
    #: unavailable section.
    budget_fraction: float | None


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


#: The `Stats` fields that are sections, in render and `to_dict` order.
#: One tuple for both, so the two can never disagree about what a section
#: is or which order they come in.
SECTIONS = (
    "store",
    "retrieval",
    "injection",
    "vectors",
    "extraction",
    "pipelines",
    "recent",
)


# ---------------- rendering ----------------


def _label(name: str, text: str) -> str:
    return f"{name:<{LABEL_WIDTH}}{text}"


def _pct(n: int | float, d: int | float) -> str:
    """A whole-number percentage, or `-` for a zero denominator.

    Floored rather than rounded, so a rate can never overstate what it
    measured: 37 hits out of 40 searches reads 92%, not the 93% rounding
    would give it. Every number saddlebag shows a user errs toward claiming
    less than it knows, and a hit rate is no exception.

    The epsilon is the opposite case and is not a rounding fudge: a
    fraction that arrives as a float - the budget does - can be 0.29,
    whose product with 100 is 28.999999999999996, and flooring that
    reports 28%, which is not conservative but wrong. The epsilon is far
    smaller than any difference a whole percent can show, so it corrects
    binary representation and nothing else. A test pins 0.29 rendering as
    29% against anyone simplifying it away.
    """
    return "-" if not d else f"{int(100 * n / d + 1e-9)}%"


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


def _unavailable(name: str, section: Unavailable) -> list[str]:
    return [_label(name, f"unavailable ({section.reason})")]


def _render_store(counts: EntryCounts, project: str) -> list[str]:
    return [
        _label(
            "store",
            f"{counts.live} live ({project} {counts.project_live})"
            f" · {counts.by_kind.get('rule', 0)} rules"
            f" · {counts.collections} kbs"
            f" · {counts.projects} projects"
            f" · {counts.superseded} superseded",
        )
    ]


def _render_recall(
    summary: AccessSummary,
    inj_sessions: int | None,
    now: datetime,
    window: timedelta,
) -> list[str]:
    period = _period(summary.first_at, now, window)
    if period is None:
        return [_label("recall", "no reads logged yet")]

    # Reading sessions out of the session starts that were logged: the
    # interesting figure is what fraction of sessions consulted the store
    # at all, and it is unanswerable when the injection half is unavailable.
    sessions = f"{summary.sessions}"
    if inj_sessions is not None:
        sessions = f"{summary.sessions}/{inj_sessions}"

    parts = [
        f"{period}: {summary.reads} reads in {sessions} sessions",
        f"hit {_pct(summary.search_hits, summary.searches)}"
        f" ({summary.search_hits}/{summary.searches})",
    ]
    tiers = " ".join(
        f"{tier} {_pct(summary.tiers[tier], summary.searches)}"
        for tier in TIER_ORDER
        if summary.tiers.get(tier)
    )
    if tiers:
        parts.append(tiers)
    if summary.p50_ms is not None:
        parts.append(f"p50 {summary.p50_ms}ms")
    return [_label("recall", " · ".join(parts))]


def _render_inject(injected: Injected, now: datetime, window: timedelta) -> list[str]:
    summary = injected.summary
    period = _period(summary.first_at, now, window)
    if period is None:
        return [_label("inject", "no session starts logged yet")]

    parts = [
        f"{period}: {summary.sessions} sessions",
        f"{summary.mean_rules:.0f} rules {summary.mean_notes:.0f} notes avg",
        # Always labelled an estimate: it is a character count divided by a
        # constant, not anything a tokeniser produced.
        f"{_tokens(summary.mean_tokens)} tokens avg (est)",
    ]
    if injected.budget_fraction is not None:
        parts.append(f"budget {_pct(injected.budget_fraction, 1)}")
    parts.append(
        f"follow-through {_pct(summary.opened, summary.injected)}"
        f" ({summary.opened}/{summary.injected})"
    )
    return [_label("inject", " · ".join(parts))]


def _render_vectors(vectors: Vectors) -> list[str]:
    # The part after the last "/" - "BAAI/bge-small-en-v1.5" is the name a
    # user typed into BAG_EMBED_MODEL, and the vendor half of it says
    # nothing a reader of one line needs.
    short = vectors.model.split("/")[-1]
    return [
        _label(
            "vectors",
            f"{vectors.embedded}/{vectors.total} embedded ({short})"
            f" · backlog {vectors.total - vectors.embedded}",
        )
    ]


def _render_extract(extract: Extraction, window: timedelta) -> list[str]:
    return [
        _label(
            "extract",
            f"{extract.jobs.get('done', 0)} done"
            f" · {extract.jobs.get('failed', 0)} failed"
            f" · {extract.awaiting} waiting"
            f" · {window.days}d +{extract.extracted} entries ({extract.model})",
        )
    ]


def _run_age(run: TranscriptRun | MemoryRun | IngestRun | None, now: datetime) -> str:
    """How long ago a pipeline run started, as one phrase.

    Three distinct answers rather than two. "never" is no run row at all;
    "unknown" is a row whose `started_at` is None, which the run dataclasses
    permit and which is not the same statement - saying "never" there would
    claim a pipeline had not run when its own row says it did.
    """
    if run is None:
        return "never"
    if run.started_at is None:
        return "unknown"
    age = handoff.age_phrase(run.started_at, now)
    return age if run.finished_at is not None else f"{age} (did not finish)"


def _render_pipes(pipelines: Pipelines, now: datetime) -> list[str]:
    counts = pipelines.counts
    text = (
        f"transcripts {counts.transcript_sessions} sessions"
        f" {counts.transcript_subagents} subagents,"
        f" last {_run_age(counts.last_transcript_run, now)}"
        f" · memory last {_run_age(counts.last_memory_run, now)}"
        f" · ingest last {_run_age(counts.last_ingest_run, now)}"
        f" · {pipelines.advisories} advisories"
    )
    # The advisory lines themselves live in `bag record status`; this count
    # is only useful if it says where to read them.
    if pipelines.advisories:
        text += " - run bag record status"
    return [_label("pipes", text)]


def _render_recent(entries: list[Entry], now: datetime) -> list[str]:
    if not entries:
        return [_label("recent", "nothing written yet")]
    lines: list[str] = []
    for entry in entries:
        # "unknown", the same word `_run_age` uses for a row that cannot
        # say when it happened: an entry with no `created_at` has not
        # come back from the store yet.
        age = (
            handoff.age_phrase(entry.created_at, now) if entry.created_at else "unknown"
        )
        title = entry.title
        if len(title) > MAX_TITLE:
            title = f"{title[: MAX_TITLE - 3]}..."
        text = f"{age}  {entry.kind}  {str(entry.id)[:8]}  {title}  [{entry.origin}]"
        lines.append(_label("recent" if not lines else "", text))
    return lines


#: Section name -> the label it prints under and how to render it. One
#: table keyed on `SECTIONS`, so a section added to `Stats` and to
#: `SECTIONS` - which `to_dict` also walks - raises a KeyError here rather
#: than quietly rendering nothing, the same failure `search.DEFAULT_ORIGINS`
#: warns about for origins.
#:
#: The labels are deliberately not the field names: "recall" and "inject"
#: say what happened, "retrieval" and "injection" name the log tables.
#: Each renderer takes its own section plus the whole `Stats`, because
#: several need `now`, the window, or - for recall - another section, and
#: the section is `Any` because this one table holds seven different types.
_RENDERERS: dict[str, tuple[str, Callable[[Any, Stats], list[str]]]] = {
    "store": ("store", lambda section, stats: _render_store(section, stats.project)),
    "retrieval": (
        "recall",
        lambda section, stats: _render_recall(
            section,
            stats.injection.summary.sessions
            if isinstance(stats.injection, Injected)
            else None,
            stats.now,
            stats.window,
        ),
    ),
    "injection": (
        "inject",
        lambda section, stats: _render_inject(section, stats.now, stats.window),
    ),
    "vectors": ("vectors", lambda section, stats: _render_vectors(section)),
    "extraction": (
        "extract",
        lambda section, stats: _render_extract(section, stats.window),
    ),
    "pipelines": ("pipes", lambda section, stats: _render_pipes(section, stats.now)),
    "recent": ("recent", lambda section, stats: _render_recent(section, stats.now)),
}


def render(stats: Stats) -> list[str]:
    """One or more lines per section, in `SECTIONS` order.

    Returns lines rather than printing them: stdout belongs to the MCP
    protocol under the stdio transport, and the banner's caller writes JSON.
    """
    lines: list[str] = []
    for name in SECTIONS:
        label, render_one = _RENDERERS[name]
        section = getattr(stats, name)
        if isinstance(section, Unavailable):
            lines.extend(_unavailable(label, section))
            continue
        lines.extend(render_one(section, stats))
    return lines


def _jsonable(value: Any) -> Any:
    """Datetimes, UUIDs and enums as strings, recursively.

    `asdict` hands back the dataclass fields as they are, and the run rows
    nested inside `PipelineCounts` carry all three.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _entry_dict(entry: Entry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "kind": str(entry.kind),
        "origin": str(entry.origin),
        "project": entry.project,
        "title": entry.title,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


def to_dict(stats: Stats) -> dict[str, Any]:
    """The same keys in every state.

    An unavailable section is null, with its reason under `unavailable`, so
    a consumer checks a field for null rather than branching on which keys
    arrived - the rule `bag memory status --json` already follows.
    """
    out: dict[str, Any] = {
        "project": stats.project,
        "window_seconds": int(stats.window.total_seconds()),
        "now": stats.now.isoformat(),
    }
    unavailable: dict[str, str] = {}
    for name in SECTIONS:
        section = getattr(stats, name)
        if isinstance(section, Unavailable):
            out[name] = None
            unavailable[name] = section.reason
        elif name == "recent":
            # Six fields, not the whole Entry: a stats consumer wants to
            # name what was written, and the body belongs one `bag get`
            # away rather than inlined into every stats document.
            out[name] = [_entry_dict(entry) for entry in section]
        else:
            out[name] = _jsonable(asdict(section))
    out["unavailable"] = unavailable
    return out


# ---------------- collection ----------------


def _budget(store: Store, owner_id: UUID, project: str, max_chars: int) -> float | None:
    """How much of `BAG_MAX_CHARS` this project's rules already spend.

    Measures rules plus header, never the rendered block's length, for the
    reason `kb.budget_advisories` gives: notes are dropped whole to make
    room, so a shipped block is pinned at or under the budget by
    construction and its length says nothing about the failure.

    A project with no knowledge base is an ordinary state - the commonest
    one, in fact - so it is None rather than an unavailable section.

    `kb.budget` already is that computation, and its `fraction` carries a
    zero guard naming this exact caller: `max_chars` comes from user
    config, and a status line must never be the thing that raises. A
    second copy of get -> resolve -> rules_chars here would have divided
    by a `BAG_MAX_CHARS=0` and turned the whole injection section into
    `unavailable (ZeroDivisionError)`.
    """
    try:
        return kb.budget(store, owner_id, project, max_chars).fraction
    except kb.CollectionNotFound:
        return None


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
    """Every section, each one collected on its own.

    No embedder is ever constructed here - `store.vector_coverage` answers
    coverage from the model name alone. Building a `LocalEmbedder` imports
    fastembed, builds an ONNX session and can download ~130MB, and a
    session start must never pay for that, the same policy
    `services.search.shared_embedder` and `embed.backfill_if_pending` apply.
    """
    since = now - window

    def section(fn: Callable[[], T]) -> T | Unavailable:
        # Each section in its own savepoint so a failed statement costs that
        # section only - without it the transaction is aborted and every
        # later section fails too, which is the banner going dark again.
        #
        # The timeout is set inside the savepoint for the same reason it is
        # set at all: `set local` lasts until the enclosing transaction
        # ends, not the savepoint, and every section wants the same value.
        try:
            with store.transaction():
                store.set_statement_timeout(STATEMENT_TIMEOUT_MS)
                return fn()
        except Exception as exc:
            return Unavailable(f"{type(exc).__name__}: {exc}".splitlines()[0][:120])

    def injection() -> Injected:
        return Injected(
            summary=store.injection_summary(owner_id, since, project),
            budget_fraction=_budget(store, owner_id, project, config.max_chars),
        )

    def vectors() -> Vectors:
        # The same population search reads, so "total" is the number of
        # entries a semantic search could ever reach. `limit` is required
        # by `Query` and ignored by `vector_coverage`, which puts it in the
        # params and never in the SQL.
        embedded, total = store.vector_coverage(
            Query(origins=list(search.DEFAULT_ORIGINS), limit=1),
            owner_id,
            config.embed_model,
        )
        return Vectors(embedded=embedded, total=total, model=config.embed_model)

    def extract() -> Extraction:
        # 1000 bounds the discovery query, not the number returned (see
        # `awaiting_sessions`), so a backlog past a thousand candidates
        # reports low. Acceptable for one line of a banner: the figure is
        # there to say "extraction has work", and `bag record status` is
        # where a real backlog is investigated.
        awaiting = extraction.awaiting_sessions(
            store, owner_id, config.idle_minutes * 60, 1000
        )
        # `pipeline_counts` again, deliberately: the pipelines section
        # reads the same row, and sharing one result would mean a failure
        # in either section taking the other with it - which is the whole
        # point of collecting them apart.
        return Extraction(
            jobs=store.extract_job_counts(owner_id),
            awaiting=len(awaiting),
            extracted=store.pipeline_counts(owner_id, since).extracted_since,
            model=config.extract_model,
        )

    def pipelines() -> Pipelines:
        # A count, not the lines: `bag record status` is where the advisory
        # text belongs, and this is the pointer to it.
        advisories = (
            len(memory.advisories(store, owner_id))
            + len(
                ingest.advisories(
                    store, owner_id, current_project=project, root=current_root
                )
            )
            + len(transcripts.advisories(store, owner_id))
        )
        return Pipelines(
            counts=store.pipeline_counts(owner_id, since), advisories=advisories
        )

    return Stats(
        project=project,
        window=window,
        now=now,
        store=section(lambda: store.entry_counts(owner_id, project)),
        retrieval=section(lambda: store.access_summary(owner_id, since, project)),
        injection=section(injection),
        vectors=section(vectors),
        extraction=section(extract),
        pipelines=section(pipelines),
        recent=section(lambda: store.recent_entries(owner_id, recent)),
    )
