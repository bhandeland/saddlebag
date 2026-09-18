"""The portability seam. One implementation today (Postgres); a file backend
would implement this same protocol."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from saddlebag.domain import (
    AccessRecord,
    AccessSummary,
    Collection,
    DuplicateGroup,
    DuplicateSet,
    Entry,
    EntryCounts,
    Event,
    ExtractJob,
    HarnessStats,
    Hit,
    IngestDesignation,
    IngestRun,
    IngestTrigger,
    InjectionRecord,
    InjectionSummary,
    JobStatus,
    MemoryDesignation,
    MemoryRun,
    MemoryTrigger,
    NearPair,
    PipelineCounts,
    Principal,
    Query,
    SessionRef,
    Transcript,
    TranscriptLine,
    TranscriptPath,
    TranscriptRun,
    TranscriptTrigger,
)


class NotOwner(PermissionError):
    """A write targeted a row that belongs to a different principal.

    Ownership is enforced in the store, not left to callers: a backend that
    silently rewrote another principal's row would be an integrity hole no
    service-layer check could close.
    """


class Store(Protocol):
    # principals
    def ensure_principal(self, handle: str) -> Principal: ...
    def get_principal(self, handle: str) -> Principal | None: ...

    # entries
    def put_entry(self, entry: Entry) -> Entry: ...
    def get_entry(self, entry_id: UUID, owner_id: UUID) -> Entry | None: ...
    def set_superseded(
        self, old_id: UUID, new_entry_id: UUID, owner_id: UUID
    ) -> bool: ...
    def search(self, query: Query, owner_id: UUID) -> list[Hit]: ...
    def fuzzy_search(
        self, query: Query, owner_id: UUID, threshold: float
    ) -> list[Hit]: ...

    # vectors
    def exact_duplicate_groups(
        self, query: Query, owner_id: UUID
    ) -> list[DuplicateSet]: ...

    def near_duplicate_pairs(
        self,
        query: Query,
        owner_id: UUID,
        model: str,
        threshold: float,
        limit: int,
    ) -> tuple[list[NearPair], int]: ...

    def vector_coverage(
        self, query: Query, owner_id: UUID, model: str
    ) -> tuple[int, int]: ...

    def put_vector(
        self,
        entry_id: UUID,
        model: str,
        dim: int,
        vector: list[float],
        owner_id: UUID,
    ) -> None: ...
    def entries_missing_vectors(
        self, owner_id: UUID, model: str, limit: int
    ) -> list[Entry]: ...
    def semantic_search(
        self,
        query: Query,
        owner_id: UUID,
        vector: list[float],
        model: str,
        threshold: float,
    ) -> list[Hit]: ...

    # collections
    def put_collection(self, collection: Collection) -> Collection: ...
    def get_collection(self, slug: str, owner_id: UUID) -> Collection | None: ...
    def list_collections(self, owner_id: UUID) -> list[Collection]: ...
    #: False when the ownership guards matched nothing - the same contract
    #: as `set_superseded`, and callers must check it. Declaring this `None`
    #: hid the verdict from every reader of the interface, and `kb.pin`
    #: duly discarded it.
    def pin(
        self, collection_id: UUID, entry_id: UUID, position: int, owner_id: UUID
    ) -> bool: ...
    def pinned_entries(self, collection_id: UUID, owner_id: UUID) -> list[Entry]: ...

    # transcripts
    #: Upsert by (owner, harness, session, agent). Re-importing a file updates
    #: it rather than creating a twin - identity is the session and agent, not
    #: the path. `agent_id` None is the session's own transcript.
    def put_transcript(
        self,
        owner_id: UUID,
        project: str,
        harness: str,
        session_id: str,
        path: str,
        content: bytes,
        sha256: str,
        *,
        agent_id: str | None,
    ) -> Transcript: ...
    def get_transcript(
        self,
        owner_id: UUID,
        harness: str,
        session_id: str,
        *,
        agent_id: str | None,
    ) -> Transcript | None: ...
    #: The bytes, fetched deliberately and separately. `Transcript` does not
    #: carry them: listing is common and a transcript is megabytes.
    def transcript_content(
        self, transcript_id: UUID, owner_id: UUID
    ) -> bytes | None: ...
    #: False when the ownership guard matched nothing - the same contract as
    #: `pin` and `set_superseded`, and callers must check it.
    def append_transcript(
        self, transcript_id: UUID, owner_id: UUID, tail: bytes, sha256: str
    ) -> bool: ...
    #: Delete and rewrite every line. Safe by construction: the rows are
    #: derived, and nothing may store anything only in them.
    def replace_transcript_lines(
        self, transcript_id: UUID, lines: list[TranscriptLine]
    ) -> int: ...
    def add_transcript_lines(
        self, transcript_id: UUID, lines: list[TranscriptLine]
    ) -> int: ...
    #: The subagent's sidecar, fetched deliberately like `content`. None
    #: when nothing is stored or the row is not this owner's.
    def transcript_meta(self, transcript_id: UUID, owner_id: UUID) -> bytes | None: ...
    #: False when the ownership guard matched nothing - the same contract as
    #: `append_transcript`. Overwrites; whether to is the service's call.
    def set_transcript_meta(
        self, transcript_id: UUID, owner_id: UUID, meta: bytes
    ) -> bool: ...
    def transcript_line_count(self, transcript_id: UUID) -> int: ...
    def stored_transcripts(self, owner_id: UUID, project: str) -> list[Transcript]: ...
    #: Every project, deliberately: an import looks a file up by identity
    #: alone, and a session filed under another project is still that row.
    def transcripts_for_harness(
        self, owner_id: UUID, harness: str
    ) -> list[Transcript]: ...

    # transcript path claims
    #: Claim a directory for a project. Returns None on success and the
    #: conflicting project's name on refusal, rather than a bare bool: told
    #: only "taken", a user has no way to find out by what, and the real
    #: failure would surface much later as a session filed under the wrong
    #: project.
    def add_transcript_path(
        self, owner_id: UUID, project: str, path: str
    ) -> str | None: ...
    def remove_transcript_path(
        self, owner_id: UUID, project: str, path: str
    ) -> bool: ...
    #: Claims for one project, or every claim when project is None - the
    #: sweep `bag record status` needs to report on every claimed project.
    def transcript_paths(
        self, owner_id: UUID, project: str | None = None
    ) -> list[TranscriptPath]: ...

    # transcript runs
    #: Opens a row and returns it. Called before any file is read, so that a
    #: process which dies mid-run leaves a started, unfinished row behind -
    #: the same contract `start_ingest_run` and `start_memory_run` keep.
    def start_transcript_run(
        self, owner_id: UUID, project: str, trigger: TranscriptTrigger
    ) -> TranscriptRun: ...
    #: Records the outcome and returns the finished row. Raises NotOwner for
    #: a row that is not the caller's - ownership is enforced here, not by
    #: callers.
    def finish_transcript_run(
        self,
        run_id: UUID,
        owner_id: UUID,
        *,
        files_seen: int,
        files_new: int,
        files_appended: int,
        files_rebuilt: int,
        lines_written: int,
        bytes_written: int,
        metas_written: int,
        anomalies: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> TranscriptRun: ...
    #: The newest-started row for one project, finished or not.
    def latest_transcript_run(
        self, owner_id: UUID, project: str
    ) -> TranscriptRun | None: ...
    #: Distinct session ids recorded for a project. Read-only, and used only
    #: by `discover`, which intersects these with transcript filenames on
    #: disk to prove which directory belongs to which project - a
    #: transcript filename IS a session id.
    def event_session_ids(self, owner_id: UUID, project: str) -> list[str]: ...
    #: Every distinct (session id, project) pair recorded for an owner.
    #: `event_session_ids` asks the same question with the project already
    #: pinned, which cannot answer the import's: whether the project that
    #: CLAIMED a directory and the project the session was recorded under
    #: disagree. Pairs rather than a mapping because one session id can
    #: carry more than one project, and collapsing that here would be the
    #: guess the check exists to refuse.
    def event_session_projects(self, owner_id: UUID) -> list[tuple[str, str]]: ...

    # recording
    def set_record_enabled(
        self, owner_id: UUID, project: str, enabled: bool
    ) -> None: ...
    def record_enabled(self, owner_id: UUID, project: str) -> bool: ...
    def enabled_record_projects(self, owner_id: UUID) -> list[str]: ...
    #: The retired capture spool, counted once so it is visible rather than
    #: mysterious. See 010_retire_capture_jobs.sql.
    def pending_legacy_capture_jobs(self, owner_id: UUID) -> int: ...
    #: Per-harness recent volume for `bag record status`. Deliberately
    #: silent on backlog: `sessions_awaiting` comes back 0 here always, and
    #: `services.events.status` fills it in via `services.extraction`'s
    #: "given up" rule rather than the store re-deriving that policy.
    def event_stats(self, owner_id: UUID) -> list[HarnessStats]: ...
    #: Events with no harness id of their own that repeat within one
    #: session - the duplicate 011's unique index cannot reach. Read-only
    #: and advisory: nothing deletes on the strength of it.
    def duplicate_unkeyed_events(
        self, owner_id: UUID, limit: int = 20
    ) -> list[DuplicateGroup]: ...

    # memory export
    def set_memory_collection(
        self,
        owner_id: UUID,
        project: str,
        slug: str | None,
        working_dir: str | None = None,
    ) -> None: ...
    def memory_collection(self, owner_id: UUID, project: str) -> str | None: ...
    #: Every designation this owner has, for `sync --all`. Returns the
    #: recorded working directory too, which is the only thing that makes
    #: syncing a project other than the current one possible at all.
    def memory_designations(self, owner_id: UUID) -> list[MemoryDesignation]: ...

    # ingest designations
    #: `paths=None` clears the (project, archive) designation. Passing a
    #: list replaces it wholesale - a designation is the whole set, never
    #: something appended to.
    def start_memory_run(
        self, owner_id: UUID, project: str, trigger: MemoryTrigger
    ) -> MemoryRun: ...

    def finish_memory_run(
        self,
        run_id: UUID,
        owner_id: UUID,
        *,
        adopted: int,
        healed: int,
        edited: int,
        regenerated: int,
        deleted: int,
        unchanged: int,
        renamed: list[list[str]],
        conflicts: list[str],
        sidecars: list[str],
        failures: list[dict[str, Any]],
    ) -> None: ...

    def latest_memory_run(self, owner_id: UUID, project: str) -> MemoryRun | None: ...

    def set_ingest_paths(
        self,
        owner_id: UUID,
        project: str,
        paths: list[str] | None,
        archive: bool = False,
    ) -> None: ...
    #: Every designation for one project, or for every project when
    #: `project` is None - the latter is what a future `refresh --all`
    #: would read, and what `bag ingest status` lists today.
    def ingest_designations(
        self, owner_id: UUID, project: str | None = None
    ) -> list[IngestDesignation]: ...

    # ingest runs
    #: Opens a row and returns it. Called before any file is read, so that a
    #: process which dies mid-run leaves a started, unfinished row behind.
    def start_ingest_run(
        self,
        owner_id: UUID,
        project: str,
        trigger: IngestTrigger,
        archive: bool = False,
    ) -> IngestRun: ...
    #: Records the outcome. Raises NotOwner for a row that is not the
    #: caller's - ownership is enforced here, not by callers.
    def finish_ingest_run(
        self,
        run_id: UUID,
        owner_id: UUID,
        *,
        created: int,
        changed: int,
        unchanged: int,
        swept: int,
        embedded: int,
        failures: list[dict[str, Any]],
        twins: list[dict[str, Any]],
        embed_error: str | None,
    ) -> None: ...
    #: The newest-started row for one project, finished or not.
    def latest_ingest_run(self, owner_id: UUID, project: str) -> IngestRun | None: ...

    #: Live ingested/archived entries in a project that carry a `src:` tag
    #: and no `sec:` tag - one per ingested document. `search` cannot say
    #: "has a tag with this prefix and lacks one with that prefix", and
    #: pulling every chunk through it to filter in Python meets Query.limit
    #: on any project with a few hundred chunks. Newest first.
    def anchors(self, owner_id: UUID, project: str) -> list[Entry]: ...

    # events
    def put_event(self, event: Event) -> Event: ...
    def events_for_session(
        self,
        owner_id: UUID,
        project: str,
        harness: str,
        session_id: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[Event]: ...
    #: Deletes every event for one (owner, project, harness, session) -
    #: nothing wider. Exists for install verification's cleanup, which must
    #: not be able to touch anything outside the reserved project it wrote
    #: to even in principle; `prune_events` is an operator command whose
    #: contract is a time window over everything an owner has, and reaching
    #: for it to delete one known event was reaching for the wrong tool.
    def delete_session_events(
        self, owner_id: UUID, project: str, harness: str, session_id: str
    ) -> int: ...
    def link_entry_events(
        self, entry_id: UUID, events: list[Event], owner_id: UUID
    ) -> None: ...
    def provenance(
        self, entry_id: UUID, owner_id: UUID
    ) -> list[tuple[UUID, str, str, bool]]: ...
    #: `project` narrows the prune to one project; None means all of them.
    #: The gate that decides whether an event is ever recorded is per
    #: project, so the one that deletes it has to be too.
    def prune_events(
        self,
        owner_id: UUID,
        before: datetime,
        force: bool,
        project: str | None = None,
    ) -> tuple[int, int, int]: ...

    # extraction spool
    def sessions_awaiting_extraction(
        self, owner_id: UUID, idle_seconds: int, limit: int
    ) -> list[SessionRef]: ...
    def extract_job_for_session(
        self, owner_id: UUID, session: SessionRef
    ) -> ExtractJob | None: ...
    def claim_extract_job(self, owner_id: UUID, session: SessionRef) -> ExtractJob: ...
    def claim_extract_job_by_id(
        self, job_id: UUID, owner_id: UUID
    ) -> ExtractJob | None: ...
    def finish_extract_job(
        self,
        job_id: UUID,
        owner_id: UUID,
        status: JobStatus,
        error: str | None,
        entries_written: int,
        covers_through: datetime | None,
    ) -> None: ...
    def get_extract_job(self, job_id: UUID, owner_id: UUID) -> ExtractJob | None: ...
    def extract_job_counts(self, owner_id: UUID) -> dict[str, int]: ...
    def recent_failed_extract_jobs(
        self, owner_id: UUID, limit: int = 5
    ) -> list[ExtractJob]: ...
    def try_advisory_lock(self, name: str, owner_id: UUID) -> bool: ...

    # usage (026) - metadata only; see services/usage.py for the fail-soft
    # wrapper every caller goes through.
    def log_access(self, record: AccessRecord) -> None: ...
    def log_injection(self, record: InjectionRecord) -> None: ...

    # usage reads - see services/stats.py, the only consumer of these.
    def entry_counts(self, owner_id: UUID, project: str | None) -> EntryCounts: ...
    def access_summary(
        self, owner_id: UUID, since: datetime, project: str | None
    ) -> AccessSummary: ...
    def injection_summary(
        self, owner_id: UUID, since: datetime, project: str | None
    ) -> InjectionSummary: ...
    #: Newest `created_at` first, live and superseded alike, every origin -
    #: it is "what was written", not "what a context block would show".
    def recent_entries(self, owner_id: UUID, limit: int) -> list[Entry]: ...
    #: Runs are the newest across all projects for this owner.
    def pipeline_counts(self, owner_id: UUID, since: datetime) -> PipelineCounts: ...

    def set_statement_timeout(self, ms: int) -> None:
        """Bound the statements in the transaction this is called inside.

        Local to that transaction, not the connection: `bag stats` is read
        from a session-start hook with ten seconds for everything it does,
        and a section that cannot answer in time must become one
        "unavailable" line rather than cost the caller its other sections.
        Nothing else in this codebase wants a timeout, so it is set where it
        is needed and unset again when that transaction ends.
        """
        ...

    #: `Any`, not `None`: what the context manager yields is deliberately
    #: not part of this contract - callers use a bare `with` and never bind
    #: it - and naming psycopg's `Transaction` here would drag the driver
    #: into the one module that exists to keep it out.
    def transaction(self) -> AbstractContextManager[Any]:
        """Group statements that must commit or roll back together.

        Under an autocommit connection this opens a real transaction; inside
        an already-open transaction (the common case in tests, which run
        inside one rolled-back transaction per test) it is a savepoint -
        psycopg's `Connection.transaction()` nests either way. It exists
        because `write.supersede` is two statements - insert the
        replacement, then retire the old row - and `reingest run` runs
        autocommit so its run row survives a crash. Without this, a process
        killed between those two statements leaves two live entries sharing
        the same `(src:, sec:)` pair, and no later run can sweep the loser:
        it is not in `ingest_file`'s `existing` dict (keyed on slug, one
        winner per slug) so it is never superseded and never swept.
        """
        ...
