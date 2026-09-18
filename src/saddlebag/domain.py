"""Domain types. Pure data - no I/O, no SQL, no formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid7


class Kind(StrEnum):
    NOTE = "note"
    DOC = "doc"
    RULE = "rule"


class Scope(StrEnum):
    PERSONAL = "personal"
    TEAM = "team"


class Origin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    #: Written by the extractor from a session's events. Was 'capture'.
    EXTRACTED = "extracted"
    HANDOFF = "handoff"
    #: A chunk of a markdown document loaded by `bag ingest`. In
    #: DEFAULT_ORIGINS: this is the reasoning layer, and it is what ingest
    #: exists to make searchable.
    INGESTED = "ingested"
    #: An ingested chunk held out of default results - implementation plans,
    #: whose text is mostly source code that now lives in src/. Not in
    #: DEFAULT_ORIGINS; reachable with --archived.
    ARCHIVED = "archived"
    #: Knowledge loaded from another tool's store by `bag import`. In
    #: DEFAULT_ORIGINS, so it is searchable; deliberately NOT in
    #: INJECTED_ORIGINS, so it never renders into a context block. See
    #: docs/superpowers/specs/2026-09-09-claude-mem-import-design.md.
    IMPORTED = "imported"


#: Origins `kb.resolve` renders into the session context block - the only
#: two an agent or a human writes directly. Kept as one tuple rather than
#: duplicated in `kb.resolve` and `services/write.remember` because those
#: two checks are one rule ("can this rule ever reach a context block?")
#: seen from two sides, and a rule enforced against the wrong set (e.g. an
#: EXTRACTED rule, which kb.resolve already filters out) is enforcement
#: with no purpose - it fails a live pipeline for a rule that can never
#: render.
INJECTED_ORIGINS = (Origin.HUMAN, Origin.AGENT)


class Match(StrEnum):
    """How a hit matched, and therefore how much to trust it.

    Search runs three tiers and never blends them, so exactly one of these
    describes every hit in a result set. One field rather than an
    accumulating set of booleans: `fuzzy` alone could not distinguish a
    semantic match from a trigram one, and those deserve different trust.
    """

    EXACT = "exact"
    SEMANTIC = "semantic"
    FUZZY = "fuzzy"


class JobStatus(StrEnum):
    """The extract spool's status, and extract_jobs' own type (see
    migrations/009_extract_jobs.sql): reusing capture_status would tie an
    enum the retired table still carries to a migration that has nothing to
    do with it."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PrincipalKind(StrEnum):
    USER = "user"
    TEAM = "team"


def new_id() -> UUID:
    """Time-sortable id. uuid7 is stdlib on Python 3.14."""
    return uuid7()


@dataclass(slots=True)
class Principal:
    id: UUID
    handle: str
    display_name: str | None = None
    kind: PrincipalKind = PrincipalKind.USER
    created_at: datetime | None = None


@dataclass(slots=True)
class Entry:
    id: UUID
    kind: Kind
    title: str
    body: str
    owner_id: UUID
    project: str | None = None
    #: One line stating what this entry is. Two readers: the `description`
    #: field of a Claude Code memory file's frontmatter, which is where it
    #: shipped, and - since 2026-09-04 - the context block, which renders
    #: it INSTEAD OF the body for a rule. Required on rules for that
    #: reason (see services/write.RuleNeedsSummary); nullable everywhere
    #: else, and a rule that predates the requirement renders title-only.
    summary: str | None = None
    scope: Scope = Scope.PERSONAL
    tags: list[str] = field(default_factory=list)
    links: list[UUID] = field(default_factory=list)
    agent: str | None = None
    session_id: str | None = None
    origin: Origin = Origin.AGENT
    superseded_by: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class CollectionQuery:
    """The 'smart' half of a collection's membership."""

    tags: list[str] = field(default_factory=list)
    kinds: list[Kind] = field(default_factory=list)
    project: str | None = None

    def is_empty(self) -> bool:
        return not self.tags and not self.kinds and self.project is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tags": list(self.tags),
            "kinds": [str(k) for k in self.kinds],
            "project": self.project,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CollectionQuery:
        data = data or {}
        return cls(
            tags=list(data.get("tags") or []),
            kinds=[Kind(k) for k in (data.get("kinds") or [])],
            project=data.get("project"),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CollectionQuery):
            return NotImplemented
        return self.to_dict() == other.to_dict()


@dataclass(slots=True, frozen=True)
class MemoryDesignation:
    """A project's memory export: which collection, and from where.

    `working_dir` is the directory the designation was made from, and it is
    here because the two halves are keyed on different things - the
    designation on the project, Claude Code's memory directory on the
    absolute working directory. A worktree and its main checkout share a
    project and have two separate memory directories, so neither key
    derives the other and the answer has to be recorded rather than
    computed. None for a designation made before it was recorded: an
    honest gap, not a default.
    """

    project: str
    collection: str
    working_dir: str | None = None


@dataclass(slots=True, frozen=True)
class IngestDesignation:
    """A project's re-ingest set: which paths, and whether they are archive.

    `archive` is part of the identity rather than a field alongside the
    paths, because a project's specs and its plans are two separate
    invocations with different origins - the table holds at most two rows
    per project, one for each.

    Paths are repo-relative and resolved against the git root at refresh
    time. Storing them absolute would tie a designation to the machine that
    made it; ingest already resolves its project from the git common dir,
    so the root is always derivable where it is needed.
    """

    project: str
    paths: tuple[str, ...]
    archive: bool = False


class IngestTrigger(StrEnum):
    """Who started an ingest run: the spawned refresh, or a person."""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass(slots=True)
class IngestRun:
    """One ingest invocation's record - see 017_ingest_runs.sql.

    `finished_at` is None for a row whose process died before finishing.
    `failures` and `twins` are lists of plain dicts, the shape they are
    stored in, because the only readers are a status renderer and `--json`.
    """

    id: UUID
    owner_id: UUID
    project: str
    trigger: IngestTrigger
    archive: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    swept: int = 0
    embedded: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    twins: list[dict[str, Any]] = field(default_factory=list)
    embed_error: str | None = None


class MemoryTrigger(StrEnum):
    """Who started a memory sync: a person, or the spawned hook."""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass(slots=True)
class MemoryRun:
    """One `bag memory sync` invocation's record - see 018_memory_runs.sql.

    `finished_at` is None for a row whose process died before finishing.
    The four lists are plain dicts and lists, the shape they are stored in,
    because the only readers are a status renderer and `--json`.
    """

    id: UUID
    owner_id: UUID
    project: str
    trigger: MemoryTrigger
    started_at: datetime | None = None
    finished_at: datetime | None = None
    adopted: int = 0
    healed: int = 0
    edited: int = 0
    regenerated: int = 0
    deleted: int = 0
    unchanged: int = 0
    renamed: list[list[str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    sidecars: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Collection:
    id: UUID
    slug: str
    title: str
    owner_id: UUID
    description: str | None = None
    project: str | None = None
    scope: Scope = Scope.PERSONAL
    query: CollectionQuery = field(default_factory=CollectionQuery)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Query:
    """A search request. Owner filtering is applied by the store, not here."""

    text: str | None = None
    kinds: list[Kind] = field(default_factory=list)
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    since: datetime | None = None
    include_superseded: bool = False
    origins: list[Origin] = field(default_factory=list)
    """Include only these origins. Empty means all origins."""
    limit: int = 20


@dataclass(slots=True)
class Hit:
    """A search result.

    `match` says which tier answered. Callers must be able to tell the
    difference: an agent handed an approximate match with no marker would
    cite it as certain.
    """

    entry: Entry
    rank: float
    snippet: str
    match: Match = Match.EXACT


@dataclass(slots=True)
class ExtractJob:
    """One session queued for extraction from its events.

    Keyed on (owner_id, project, harness, session_id), not a transcript path
    - see 009_extract_jobs.sql. `covers_through` is the watermark: set on
    finish, it is the newest occurred_at among the events this run actually
    read, and is what lets a resumed session be re-queued for only its new
    events instead of being invisible forever.
    """

    id: UUID
    owner_id: UUID
    project: str
    harness: str
    session_id: str
    covers_through: datetime | None = None
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    error: str | None = None
    entries_written: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EventKind(StrEnum):
    """What a harness handed us. Small and closed on purpose.

    Everything harness-specific lives in the payload, unparsed: a harness
    that changes its payload shape must not be able to break the write path.
    A fourth value is deliberately deferred until something writes one -
    adding an enum value later is cheap, and guessing now invites a label
    nothing ever produces.
    """

    TOOL_CALL = "tool_call"
    MESSAGE = "message"
    SESSION_END = "session_end"


@dataclass(slots=True)
class Event:
    """One thing that happened, as raw as it reached us."""

    id: UUID
    owner_id: UUID
    project: str
    harness: str
    session_id: str
    kind: EventKind
    payload: dict[str, Any]
    tool: str | None = None
    occurred_at: datetime | None = None
    recorded_at: datetime | None = None


@dataclass(slots=True)
class HarnessStats:
    """Per-harness recording health, as `bag record status` reports it.

    Only a harness that has recorded at least one event can appear here -
    there is no name to key a zero row on for one that never has. That is
    exactly the failure `record status` exists to catch (an adapter bound to
    hook names its harness never emits records nothing and looks like a
    quiet day), so `services.events.render` turns an empty list of these
    into a visible "no events" line rather than an absent section.
    """

    harness: str
    events_24h: int
    last_event_at: datetime | None
    sessions_awaiting: int


@dataclass(slots=True)
class DuplicateGroup:
    """One suspected duplicate, as `bag record status` reports it.

    Only events with no harness id of their own are ever counted here -
    011's unique index already makes a duplicate impossible for the rest.
    This is a report and never a delete, which is what makes payload
    equality an acceptable signal: a false positive costs a line of output.
    """

    project: str
    harness: str
    session_id: str
    count: int


@dataclass(slots=True)
class DuplicateSet:
    """Live entries sharing one body, as `bag dedupe report` groups them.

    Not `DuplicateGroup`: that name is taken by duplicate raw *events* in
    `bag record status`, and the two are unrelated questions.

    Membership is transitive here, unlike `NearPair`, because identical
    checksums are an equivalence relation. That is the whole reason the
    exact tier can report groups and the near tier cannot.
    """

    entries: list[Entry]


@dataclass(slots=True)
class NearPair:
    """Two entries close enough in vector space to be worth a human look.

    A pair, never a group. Similarity is not transitive: A near B and B near
    C says nothing about A and C, so chaining pairs into groups would
    produce memberships nobody could defend.
    """

    a: Entry
    b: Entry
    similarity: float


@dataclass(slots=True)
class DedupeReport:
    """Everything `bag dedupe report` renders, fetched in one pass.

    `near_total` is the count above the threshold BEFORE truncation, so the
    renderer can say "showing 20 of 431" rather than quietly cutting the
    list. A report that names no boundary is the diagnostic that eventually
    lies confidently.

    `embedded` and `total` are the near tier's coverage. They are always
    rendered, because vectors are written only by `bag embed` and an empty
    near section with no coverage line reads like a clean bill of health.

    `near_suppressed` and `near_truncated` are kept apart on purpose. Both
    shrink the rendered list, and collapsing them into one "showing N of M"
    told the reader their output had been cut short when in fact the missing
    pairs were printed above as identical bodies. Two different facts about
    why a list is short need two different sentences.
    """

    exact: list[DuplicateSet]
    near: list[NearPair]
    near_total: int
    threshold: float
    model: str
    embedded: int
    total: int
    #: Pairs dropped because both members share an exact group.
    near_suppressed: int = 0
    #: Whether the store had more pairs above the threshold than it returned.
    near_truncated: bool = False


@dataclass(slots=True)
class ProvenanceRow:
    """One event behind an entry, as `bag events show` reports it.

    `present` is what lets the forensic lookup tell "we recorded where this
    came from and then deleted the raw" apart from "we never recorded
    anything" - see `Store.provenance`, the left join this is built from.
    """

    event_id: UUID
    session_id: str
    harness: str
    present: bool


class TranscriptTrigger(StrEnum):
    """Who started an import. See transcript_runs.trigger."""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass(slots=True)
class Transcript:
    """A stored session transcript, without its bytes.

    `content` is deliberately absent: a transcript is megabytes, listing them
    is common, and reading the bytes is a separate store call made only when
    something is about to parse them.
    """

    id: UUID
    owner_id: UUID
    project: str
    harness: str
    session_id: str
    #: None for a session's own transcript; the subagent's `agentId` for a
    #: `<session>/subagents/agent-<id>.jsonl`. `session_id` is the parent's
    #: in both cases.
    agent_id: str | None
    path: str
    bytes: int
    sha256: str
    first_seen: datetime
    last_read: datetime
    #: Whether the subagent's `agent-<id>.meta.json` is stored - never the
    #: bytes, for the reason `content` is absent. Always False for a
    #: session's own transcript, which has no sidecar.
    has_meta: bool
    #: Whether any `transcript_lines` row exists. Carried on the row so an
    #: import can tell a stranded derived half from a healthy one without a
    #: count query per file - see `services.transcripts._import_one`.
    has_lines: bool


@dataclass(slots=True)
class TranscriptLine:
    """One parsed JSONL line. Derived, and rebuildable from the source."""

    seq: int
    type: str | None
    uuid: str | None
    occurred_at: datetime | None
    raw: dict[str, Any]


@dataclass(slots=True)
class TranscriptPath:
    """A directory a project claims. Absolute, as the user gave it."""

    owner_id: UUID
    project: str
    path: str
    added_at: datetime


@dataclass(slots=True)
class TranscriptRun:
    """One import invocation. `finished_at` None means it died mid-run."""

    id: UUID
    owner_id: UUID
    project: str
    trigger: TranscriptTrigger
    started_at: datetime
    finished_at: datetime | None
    files_seen: int
    files_new: int
    files_appended: int
    files_rebuilt: int
    lines_written: int
    bytes_written: int
    #: Subagent sidecars stored. Counted apart from the file counters, which
    #: describe transcript I/O - a backfill run moves sidecars and nothing
    #: else, and must not read as a run that did nothing.
    metas_written: int
    anomalies: list[dict[str, Any]]
    failures: list[dict[str, Any]]


@dataclass(slots=True)
class SessionRef:
    """A session with events, as the idle trigger sees it.

    `extract_from` is the watermark: the newest `covers_through` of a done
    extract job for this session, or None when nothing has ever extracted
    it. Events at or before it have already produced whatever they were
    going to produce.
    """

    project: str
    harness: str
    session_id: str
    event_count: int
    last_event_at: datetime
    extract_from: datetime | None = None


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
