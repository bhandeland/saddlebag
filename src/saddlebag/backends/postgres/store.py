"""The Postgres Store implementation. Hand-written SQL, psycopg 3, no ORM."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from saddlebag.backends.postgres.sqltext import as_sql
from saddlebag.domain import (
    AccessRecord,
    AccessSummary,
    Collection,
    CollectionQuery,
    DuplicateGroup,
    DuplicateSet,
    Entry,
    EntryCounts,
    Event,
    EventKind,
    ExtractJob,
    HarnessStats,
    Hit,
    IngestDesignation,
    IngestRun,
    IngestTrigger,
    InjectionRecord,
    InjectionSummary,
    JobStatus,
    Kind,
    Match,
    MemoryDesignation,
    MemoryRun,
    MemoryTrigger,
    NearPair,
    Origin,
    PipelineCounts,
    Principal,
    PrincipalKind,
    Query,
    Scope,
    SessionRef,
    Transcript,
    TranscriptLine,
    TranscriptPath,
    TranscriptRun,
    TranscriptTrigger,
    new_id,
)
from saddlebag.store import NotOwner

ENTRY_FIELDS = [
    "id",
    "kind",
    "title",
    "body",
    "summary",
    "project",
    "scope",
    "owner_id",
    "tags",
    "links",
    "agent",
    "session_id",
    "origin",
    "superseded_by",
    "created_at",
    "updated_at",
]


def _one(cur: psycopg.Cursor[dict[str, Any]]) -> dict[str, Any]:
    """The single row a statement that cannot return zero rows returned.

    `fetchone()` is typed `dict | None` because most statements might match
    nothing, and most callers here check for that - `get_collection` returns
    None for an unknown slug on purpose. These sites are the other kind: a
    `count(*)`, a `returning` on a row this statement just wrote, a
    `pg_try_advisory_lock`. Saying so once beats a dozen scattered asserts,
    and it turns "None is not subscriptable" three frames downstream into a
    named failure at the point where the assumption actually lives.
    """
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("expected exactly one row, got none")
    return row


def entry_columns(alias: str = "") -> str:
    """Column list, optionally table-qualified for joins."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in ENTRY_FIELDS)


def _aliased_entry_columns(alias: str, prefix: str) -> str:
    """Entry columns renamed, for a query joining `entries` to itself.

    Two copies of the same table produce two `id` keys in one result row,
    and the second silently wins. Prefixing makes both readable.
    """
    return ", ".join(f"{alias}.{f} as {prefix}{f}" for f in ENTRY_FIELDS)


def _row_to_entry_prefixed(row: dict[str, Any], prefix: str) -> Entry:
    return _row_to_entry(
        {k[len(prefix) :]: v for k, v in row.items() if k.startswith(prefix)}
    )


MEMORY_RUN_FIELDS = [
    "id",
    "owner_id",
    "project",
    "trigger",
    "started_at",
    "finished_at",
    "adopted",
    "healed",
    "edited",
    "regenerated",
    "deleted",
    "unchanged",
    "renamed",
    "conflicts",
    "sidecars",
    "failures",
]


def memory_run_columns() -> str:
    return ", ".join(MEMORY_RUN_FIELDS)


def _row_to_memory_run(row: dict[str, Any]) -> MemoryRun:
    return MemoryRun(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        trigger=MemoryTrigger(row["trigger"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        adopted=row["adopted"],
        healed=row["healed"],
        edited=row["edited"],
        regenerated=row["regenerated"],
        deleted=row["deleted"],
        unchanged=row["unchanged"],
        renamed=list(row["renamed"] or []),
        conflicts=list(row["conflicts"] or []),
        sidecars=list(row["sidecars"] or []),
        failures=list(row["failures"] or []),
    )


def _entry_filters(query: Query, owner_id: UUID) -> tuple[list[str], dict[str, Any]]:
    """The filters every entry read applies, built once for all search tiers.

    Extracted because there are now three tiers running the same predicates
    against the same table. Duplicated, they drift: a filter accidentally
    dropped from one tier is invisible until that tier happens to answer, and
    the owner check is among them. One builder means one place to be wrong.

    Returns clauses joined by the caller with " and ", plus the params they
    reference. Text matching is NOT included - that is what differs between
    tiers and is the caller's business.
    """
    params: dict[str, Any] = {"owner_id": owner_id, "limit": query.limit}
    where = ["e.owner_id = %(owner_id)s"]

    if not query.include_superseded:
        where.append("e.superseded_by is null")
    if query.kinds:
        where.append("e.kind = any(%(kinds)s::entry_kind[])")
        params["kinds"] = [str(k) for k in query.kinds]
    if query.project is not None:
        where.append("e.project = %(project)s")
        params["project"] = query.project
    if query.tags:
        where.append("e.tags && %(tags)s")
        params["tags"] = list(query.tags)
    if query.origins:
        where.append("e.origin = any(%(origins)s::entry_origin[])")
        params["origins"] = [str(o) for o in query.origins]
    if query.since is not None:
        where.append("e.created_at >= %(since)s")
        params["since"] = query.since

    return where, params


def _vector_literal(vector: list[float]) -> str:
    """pgvector's text input format.

    Passed as a string and cast in SQL rather than adding the pgvector-python
    adapter: one more dependency for one type, when the literal form is
    stable, documented, and two lines. Revisit if vectors ever need reading
    back into Python, which no current caller does.
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _row_to_entry(row: dict[str, Any]) -> Entry:
    return Entry(
        id=row["id"],
        kind=Kind(row["kind"]),
        title=row["title"],
        body=row["body"],
        owner_id=row["owner_id"],
        summary=row["summary"],
        project=row["project"],
        scope=Scope(row["scope"]),
        tags=list(row["tags"] or []),
        links=[UUID(str(x)) for x in (row["links"] or [])],
        agent=row["agent"],
        session_id=row["session_id"],
        origin=Origin(row["origin"]),
        superseded_by=row["superseded_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_collection(row: dict[str, Any]) -> Collection:
    query = row["query"]
    if isinstance(query, str):
        query = json.loads(query)
    return Collection(
        id=row["id"],
        slug=row["slug"],
        title=row["title"],
        owner_id=row["owner_id"],
        description=row["description"],
        project=row["project"],
        scope=Scope(row["scope"]),
        query=CollectionQuery.from_dict(query),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


EXTRACT_JOB_FIELDS = [
    "id",
    "owner_id",
    "project",
    "harness",
    "session_id",
    "covers_through",
    "status",
    "attempts",
    "error",
    "entries_written",
    "created_at",
    "updated_at",
]


def extract_job_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in EXTRACT_JOB_FIELDS)


def _row_to_extract_job(row: dict[str, Any]) -> ExtractJob:
    return ExtractJob(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        harness=row["harness"],
        session_id=row["session_id"],
        covers_through=row["covers_through"],
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        error=row["error"],
        entries_written=row["entries_written"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


INGEST_RUN_FIELDS = [
    "id",
    "owner_id",
    "project",
    "trigger",
    "archive",
    "started_at",
    "finished_at",
    "created",
    "changed",
    "unchanged",
    "swept",
    "embedded",
    "failures",
    "twins",
    "embed_error",
]


def ingest_run_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in INGEST_RUN_FIELDS)


def _row_to_ingest_run(row: dict[str, Any]) -> IngestRun:
    return IngestRun(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        trigger=IngestTrigger(row["trigger"]),
        archive=row["archive"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created=row["created"],
        changed=row["changed"],
        unchanged=row["unchanged"],
        swept=row["swept"],
        embedded=row["embedded"],
        failures=list(row["failures"]),
        twins=list(row["twins"]),
        embed_error=row["embed_error"],
    )


def transcript_columns(alias: str = "t") -> str:
    """The Transcript fields, in dataclass order. `content` is NOT here.

    A module constant so `as_sql` interpolation stays an interpolation of our
    own text, and so that listing transcripts can never accidentally select
    megabytes of bytea.
    """
    cols = (
        "id",
        "owner_id",
        "project",
        "harness",
        "session_id",
        "agent_id",
        "path",
        "bytes",
        "sha256",
        "first_seen",
        "last_read",
    )
    # `has_meta` is computed, not a column: the sidecar's bytes stay out of
    # every listing for the same reason `content` does. `has_lines` is an
    # `exists`, not a count: it stops at the first row of the
    # (transcript_id, seq) key, and presence is the only question asked.
    return ", ".join(f"{alias}.{c}" for c in cols) + (
        f", {alias}.meta is not null as has_meta"
        f", exists (select 1 from transcript_lines l"
        f" where l.transcript_id = {alias}.id) as has_lines"
    )


def _row_to_transcript(row: dict[str, Any]) -> Transcript:
    return Transcript(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        harness=row["harness"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        path=row["path"],
        bytes=row["bytes"],
        sha256=row["sha256"],
        first_seen=row["first_seen"],
        last_read=row["last_read"],
        has_meta=row["has_meta"],
        has_lines=row["has_lines"],
    )


def _row_to_transcript_path(row: dict[str, Any]) -> TranscriptPath:
    return TranscriptPath(
        owner_id=row["owner_id"],
        project=row["project"],
        path=row["path"],
        added_at=row["added_at"],
    )


TRANSCRIPT_RUN_FIELDS = [
    "id",
    "owner_id",
    "project",
    "trigger",
    "started_at",
    "finished_at",
    "files_seen",
    "files_new",
    "files_appended",
    "files_rebuilt",
    "lines_written",
    "bytes_written",
    "metas_written",
    "anomalies",
    "failures",
]


def transcript_run_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{f}" for f in TRANSCRIPT_RUN_FIELDS)


def _row_to_transcript_run(row: dict[str, Any]) -> TranscriptRun:
    return TranscriptRun(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        trigger=TranscriptTrigger(row["trigger"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        files_seen=row["files_seen"],
        files_new=row["files_new"],
        files_appended=row["files_appended"],
        files_rebuilt=row["files_rebuilt"],
        lines_written=row["lines_written"],
        bytes_written=row["bytes_written"],
        metas_written=row["metas_written"],
        anomalies=list(row["anomalies"]),
        failures=list(row["failures"]),
    )


def _row_to_session_ref(row: dict[str, Any]) -> SessionRef:
    return SessionRef(
        project=row["project"],
        harness=row["harness"],
        session_id=row["session_id"],
        event_count=row["event_count"],
        last_event_at=row["last_event_at"],
        extract_from=row["extract_from"],
    )


def _row_to_harness_stats(row: dict[str, Any]) -> HarnessStats:
    return HarnessStats(
        harness=row["harness"],
        events_24h=row["events_24h"],
        last_event_at=row["last_event_at"],
        sessions_awaiting=row["sessions_awaiting"],
    )


def _row_to_event(row: dict[str, Any]) -> Event:
    return Event(
        id=row["id"],
        owner_id=row["owner_id"],
        project=row["project"],
        harness=row["harness"],
        session_id=row["session_id"],
        kind=EventKind(row["kind"]),
        tool=row["tool"],
        payload=row["payload"],
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
    )


class PostgresStore:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def _cur(self):
        return self._conn.cursor(row_factory=dict_row)

    # ---------------- principals ----------------

    def ensure_principal(self, handle: str) -> Principal:
        existing = self.get_principal(handle)
        if existing is not None:
            return existing
        with self._cur() as cur:
            cur.execute(
                """
                insert into principals (id, handle) values (%s, %s)
                on conflict (handle) do nothing
                """,
                (new_id(), handle),
            )
        # A concurrent caller may have won the race to create this handle;
        # re-select rather than trust the insert to have landed our row.
        created = self.get_principal(handle)
        assert created is not None
        return created

    def get_principal(self, handle: str) -> Principal | None:
        with self._cur() as cur:
            cur.execute(
                "select id, handle, display_name, kind, created_at "
                "from principals where handle = %s",
                (handle,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Principal(
            id=row["id"],
            handle=row["handle"],
            display_name=row["display_name"],
            kind=PrincipalKind(row["kind"]),
            created_at=row["created_at"],
        )

    # ---------------- entries ----------------

    def put_entry(self, entry: Entry) -> Entry:
        with self._cur() as cur:
            # Read the pre-write text so we can tell, after the upsert, whether
            # this write is the kind that invalidates a vector. A row that does
            # not exist yet counts as changed but has nothing to delete, so
            # text_changed stays True and the delete below is a no-op.
            cur.execute(
                "select title, body from entries where id = %s and owner_id = %s",
                (entry.id, entry.owner_id),
            )
            previous = cur.fetchone()
            text_changed = (
                previous is None
                or previous["title"] != entry.title
                or previous["body"] != entry.body
            )
            cur.execute(
                as_sql(f"""
                insert into entries (
                  id, kind, title, body, summary, project, scope, owner_id,
                  tags, links, agent, session_id, origin, superseded_by
                ) values (
                  %(id)s, %(kind)s, %(title)s, %(body)s, %(summary)s,
                  %(project)s, %(scope)s, %(owner_id)s, %(tags)s, %(links)s,
                  %(agent)s, %(session_id)s, %(origin)s, %(superseded_by)s
                )
                on conflict (id) do update set
                  kind = excluded.kind, title = excluded.title,
                  body = excluded.body, summary = excluded.summary,
                  project = excluded.project,
                  scope = excluded.scope, tags = excluded.tags,
                  links = excluded.links, agent = excluded.agent,
                  session_id = excluded.session_id, origin = excluded.origin,
                  superseded_by = excluded.superseded_by,
                  updated_at = clock_timestamp()
                where entries.owner_id = %(owner_id)s
                returning {entry_columns()}
                """),
                {
                    "id": entry.id,
                    "kind": str(entry.kind),
                    "title": entry.title,
                    "body": entry.body,
                    "summary": entry.summary,
                    "project": entry.project,
                    "scope": str(entry.scope),
                    "owner_id": entry.owner_id,
                    "tags": list(entry.tags),
                    "links": [str(x) for x in entry.links],
                    "agent": entry.agent,
                    "session_id": entry.session_id,
                    "origin": str(entry.origin),
                    "superseded_by": entry.superseded_by,
                },
            )
            row = cur.fetchone()
            if row is None:
                # The id exists but belongs to someone else, so the ON CONFLICT
                # update matched no row. Never silently drop the write.
                raise NotOwner(
                    f"entry {entry.id} exists and is owned by another principal"
                )
            # Derived data must never outlive the text it was derived from.
            # An edited entry whose vector survives stays findable by its OLD
            # wording while returning its NEW body, and nothing downstream can
            # warn about it: the semantic tier's marker says "semantic", which
            # is true - the vector really is near the query. Deleting here
            # makes entries_missing_vectors offer the row again, so `saddlebag
            # embed` repairs it on its next run.
            #
            # Only a text change counts. Linking, tagging and superseding all
            # go through put_entry too, and re-embedding on those would give
            # `bag embed` a backlog that never empties.
            if text_changed:
                cur.execute(
                    "delete from entry_vectors where entry_id = %s",
                    (entry.id,),
                )
        return _row_to_entry(row)

    def get_entry(self, entry_id: UUID, owner_id: UUID) -> Entry | None:
        with self._cur() as cur:
            cur.execute(
                as_sql(
                    f"select {entry_columns()} from entries "
                    "where id = %s and owner_id = %s"
                ),
                (entry_id, owner_id),
            )
            row = cur.fetchone()
        return _row_to_entry(row) if row else None

    def set_superseded(self, old_id: UUID, new_entry_id: UUID, owner_id: UUID) -> bool:
        with self._cur() as cur:
            cur.execute(
                """
                update entries
                   set superseded_by = %(new_entry_id)s, updated_at = clock_timestamp()
                 where id = %(old_id)s
                   and owner_id = %(owner_id)s
                   and exists (
                         select 1 from entries
                          where id = %(new_entry_id)s and owner_id = %(owner_id)s
                       )
                """,
                {"new_entry_id": new_entry_id, "old_id": old_id, "owner_id": owner_id},
            )
            return cur.rowcount == 1

    def search(self, query: Query, owner_id: UUID) -> list[Hit]:
        """Ranked search. Owner and superseded filters are always applied."""
        text = (query.text or "").strip()
        where, params = _entry_filters(query, owner_id)

        if text:
            # websearch_to_tsquery accepts what people and agents actually
            # type, and never raises a syntax error on odd input.
            params["text"] = text
            where.append("e.search @@ q")
            # Empty StartSel/StopSel: the snippet feeds --json output and
            # agent context, where ts_headline's default <b> tags are markup
            # nobody renders and tokens everybody pays for.
            sql = f"""
                select {entry_columns("e")},
                       ts_rank_cd(e.search, q) as rank,
                       ts_headline('english', e.body, q,
                                   'MaxWords=32,MinWords=8,ShortWord=2,'
                                   'StartSel="",StopSel=""') as snippet
                from entries e,
                     websearch_to_tsquery('english', %(text)s) q
                where {" and ".join(where)}
                order by rank desc, e.created_at desc
                limit %(limit)s
            """
        else:
            sql = f"""
                select {entry_columns("e")},
                       0::float4 as rank,
                       left(e.body, 200) as snippet
                from entries e
                where {" and ".join(where)}
                order by e.created_at desc
                limit %(limit)s
            """

        with self._cur() as cur:
            cur.execute(as_sql(sql), params)
            rows = cur.fetchall()
        return [
            Hit(
                entry=_row_to_entry(r),
                rank=float(r["rank"]),
                snippet=r["snippet"],
                match=Match.EXACT,
            )
            for r in rows
        ]

    def fuzzy_search(self, query: Query, owner_id: UUID, threshold: float) -> list[Hit]:
        """Typo-tolerant search, for when exact search found nothing.

        Two operators, because they behave differently on the two columns:
        `similarity` compares whole strings, which works on a short title but
        dissolves into noise on a long body; `word_similarity` compares the
        query against the best-matching word sequence, which is what makes a
        misspelled word inside a body findable. The score is the better of the
        two, so an entry can match on either.
        """
        text = (query.text or "").strip()
        if not text:
            return []

        where, params = _entry_filters(query, owner_id)
        params["text"] = text
        params["threshold"] = threshold

        score = (
            "greatest(similarity(e.title, %(text)s), word_similarity(%(text)s, e.body))"
        )
        where.append(f"{score} >= %(threshold)s")

        sql = f"""
            select {entry_columns("e")},
                   {score} as rank,
                   left(e.body, 200) as snippet
            from entries e
            where {" and ".join(where)}
            order by rank desc, e.created_at desc
            limit %(limit)s
        """
        with self._cur() as cur:
            cur.execute(as_sql(sql), params)
            rows = cur.fetchall()
        return [
            Hit(
                entry=_row_to_entry(r),
                rank=float(r["rank"]),
                snippet=r["snippet"],
                match=Match.FUZZY,
            )
            for r in rows
        ]

    def put_vector(
        self,
        entry_id: UUID,
        model: str,
        dim: int,
        vector: list[float],
        owner_id: UUID,
    ) -> None:
        """Insert or replace one entry's vector for one model.

        Ownership is checked here, inside the store, like every other write:
        a caller that could write vectors for another principal's entries
        would be an integrity hole no service check could close.
        """
        with self._cur() as cur:
            cur.execute(
                "select 1 from entries where id = %s and owner_id = %s",
                (entry_id, owner_id),
            )
            if cur.fetchone() is None:
                raise NotOwner(f"entry {entry_id} does not belong to {owner_id}")
            cur.execute(
                """
                insert into entry_vectors (entry_id, model, dim, vector)
                values (%(entry_id)s, %(model)s, %(dim)s, %(vector)s::vector)
                on conflict (entry_id, model) do update
                   set vector = excluded.vector,
                       dim = excluded.dim,
                       created_at = clock_timestamp()
                """,
                {
                    "entry_id": entry_id,
                    "model": model,
                    "dim": dim,
                    "vector": _vector_literal(vector),
                },
            )

    def entries_missing_vectors(
        self, owner_id: UUID, model: str, limit: int
    ) -> list[Entry]:
        """Entries with no vector for this model, oldest first.

        Oldest first so a long backlog makes steady, resumable progress
        rather than re-visiting the same recent rows on every run.

        Superseded entries are skipped: they are invisible to every search
        tier, so embedding them is work whose result nothing can return.
        """
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {entry_columns("e")}
                from entries e
                left join entry_vectors v
                       on v.entry_id = e.id and v.model = %(model)s
                where e.owner_id = %(owner_id)s
                  and e.superseded_by is null
                  and v.entry_id is null
                order by e.created_at asc
                limit %(limit)s
                """),
                {"owner_id": owner_id, "model": model, "limit": limit},
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def semantic_search(
        self,
        query: Query,
        owner_id: UUID,
        vector: list[float],
        model: str,
        threshold: float,
    ) -> list[Hit]:
        """Nearest neighbours by cosine similarity, above a floor.

        `<=>` is pgvector's cosine DISTANCE, so similarity is 1 - distance.
        Reported as similarity because that is the direction every other tier
        ranks in, and a mixed convention across tiers is how a comparison
        silently inverts.

        Exact search, no index - see 006_vectors.sql for why, and for when
        that stops being the right answer.

        The join is inner: an entry with no vector for this model is
        invisible here and reachable by the other two tiers. That is the
        correct degradation - an un-embedded entry is not lost, only less
        findable, and `bag embed` fixes it.
        """
        where, params = _entry_filters(query, owner_id)
        params["vector"] = _vector_literal(vector)
        params["model"] = model
        params["threshold"] = threshold

        similarity = "1 - (v.vector <=> %(vector)s::vector)"
        where.append("v.model = %(model)s")
        where.append(f"{similarity} >= %(threshold)s")

        sql = f"""
            select {entry_columns("e")},
                   {similarity} as rank,
                   left(e.body, 200) as snippet
            from entries e
            join entry_vectors v on v.entry_id = e.id
            where {" and ".join(where)}
            order by rank desc, e.created_at desc
            limit %(limit)s
        """
        with self._cur() as cur:
            cur.execute(as_sql(sql), params)
            rows = cur.fetchall()
        return [
            Hit(
                entry=_row_to_entry(r),
                rank=float(r["rank"]),
                snippet=r["snippet"],
                match=Match.SEMANTIC,
            )
            for r in rows
        ]

    def exact_duplicate_groups(
        self, query: Query, owner_id: UUID
    ) -> list[DuplicateSet]:
        """Live entries sharing a body, grouped.

        The checksum is over the body ALONE and trimmed. Body alone because
        two entries holding one fact under different titles are duplicates,
        and the extractor's title is never the one a human would have
        chosen. This is deliberately the opposite of `ingest`, which
        compares title and body - that comparison asks whether a chunk needs
        re-indexing, and there the title is half the embedding text.

        `btrim` because a hand-written memory file and a saddlebag-generated one
        can differ by a trailing newline, and that is not a different fact.
        The character set is spelled out: one-argument `btrim` strips spaces
        ONLY, so the newline case - the one this exists for - would have
        sailed straight past it.

        Nothing looser: normalising interior whitespace would start merging
        entries whose formatting genuinely differs, which is the near tier's
        job, with a score attached.

        A window function rather than a `group by` subquery so that
        `_entry_filters` is applied exactly once - a second copy under a
        second alias is how a filter silently drifts out of one path.

        No `limit`. The groups are a finite, cheap fact about the store, and
        a truncated list of identical bodies would hide the easiest half of
        this report's own answer.
        """
        where, params = _entry_filters(query, owner_id)
        sql = f"""
            select * from (
                select {entry_columns("e")},
                       md5(btrim(e.body, E' \\t\\n\\r')) as body_key,
                       count(*) over (
                           partition by md5(btrim(e.body, E' \\t\\n\\r'))
                       ) as n
                from entries e
                where {" and ".join(where)}
            ) s
            where s.n > 1
            order by s.body_key, s.created_at asc
        """
        with self._cur() as cur:
            cur.execute(as_sql(sql), params)
            rows = cur.fetchall()

        groups: dict[str, list[Entry]] = {}
        for r in rows:
            groups.setdefault(r["body_key"], []).append(_row_to_entry(r))
        return [DuplicateSet(entries=members) for members in groups.values()]

    def near_duplicate_pairs(
        self,
        query: Query,
        owner_id: UUID,
        model: str,
        threshold: float,
        limit: int,
    ) -> tuple[list[NearPair], int]:
        """Entry pairs above a cosine-similarity floor, and how many there are.

        `b.id > a.id` so each pair is computed and reported once rather than
        twice in both orders.

        The join is inner on both sides, so an entry with no vector for this
        model is invisible here - the same correct degradation
        `semantic_search` documents. `vector_coverage` is what tells the
        caller how much of the population that silently excluded.

        The count is taken over the whole matching set, not the returned
        page, because a report that truncates without saying so is the
        diagnostic that eventually lies confidently.

        Exact, no index, like `semantic_search` - see 006_vectors.sql.
        """
        where, params = _entry_filters(query, owner_id)
        # _entry_filters writes its clauses against the alias `e`. This query
        # has two entry aliases, so the same predicate is applied to both -
        # rebuilt by substitution rather than by a second hand-written copy,
        # which is how a filter drifts out of one side unnoticed.
        a_where = [c.replace("e.", "a.") for c in where]
        b_where = [c.replace("e.", "b.") for c in where]
        params["model"] = model
        params["threshold"] = threshold
        params["pair_limit"] = limit

        similarity = "1 - (va.vector <=> vb.vector)"
        joins = f"""
            from entries a
            join entry_vectors va on va.entry_id = a.id
                                 and va.model = %(model)s
            join entries b on b.id > a.id
            join entry_vectors vb on vb.entry_id = b.id
                                 and vb.model = %(model)s
            where {" and ".join(a_where + b_where)}
              and {similarity} >= %(threshold)s
        """
        with self._cur() as cur:
            cur.execute(as_sql(f"select count(*) as n {joins}"), params)
            total = int(_one(cur)["n"])
            if total == 0:
                return [], 0
            cur.execute(
                as_sql(f"""
                select {_aliased_entry_columns("a", "a_")},
                       {_aliased_entry_columns("b", "b_")},
                       {similarity} as similarity
                {joins}
                order by similarity desc, a.id, b.id
                limit %(pair_limit)s
                """),
                params,
            )
            rows = cur.fetchall()
        return [
            NearPair(
                a=_row_to_entry_prefixed(r, "a_"),
                b=_row_to_entry_prefixed(r, "b_"),
                similarity=float(r["similarity"]),
            )
            for r in rows
        ], total

    def vector_coverage(
        self, query: Query, owner_id: UUID, model: str
    ) -> tuple[int, int]:
        """How many of the population carry a vector for this model.

        A separate query rather than a count inside the pair join: an entry
        with no vector is not in that join at all, so the join can never
        report the entries it is missing.
        """
        where, params = _entry_filters(query, owner_id)
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select count(v.entry_id) as embedded, count(*) as total
                from entries e
                left join entry_vectors v
                       on v.entry_id = e.id and v.model = %(model)s
                where {" and ".join(where)}
                """),
                {**params, "model": model},
            )
            row = _one(cur)
        return int(row["embedded"]), int(row["total"])

    # ---------------- collections ----------------

    def put_collection(self, collection: Collection) -> Collection:
        with self._cur() as cur:
            cur.execute(
                """
                insert into collections (
                  id, slug, title, description, project, scope, owner_id, query
                ) values (
                  %(id)s, %(slug)s, %(title)s, %(description)s, %(project)s,
                  %(scope)s, %(owner_id)s, %(query)s
                )
                on conflict (owner_id, slug) do update set
                  title = excluded.title, description = excluded.description,
                  project = excluded.project, scope = excluded.scope,
                  query = excluded.query, updated_at = now()
                returning id, slug, title, description, project, scope,
                          owner_id, query, created_at, updated_at
                """,
                {
                    "id": collection.id,
                    "slug": collection.slug,
                    "title": collection.title,
                    "description": collection.description,
                    "project": collection.project,
                    "scope": str(collection.scope),
                    "owner_id": collection.owner_id,
                    "query": json.dumps(collection.query.to_dict()),
                },
            )
            return _row_to_collection(_one(cur))

    def get_collection(self, slug: str, owner_id: UUID) -> Collection | None:
        with self._cur() as cur:
            cur.execute(
                "select id, slug, title, description, project, scope, owner_id,"
                " query, created_at, updated_at from collections "
                "where slug = %s and owner_id = %s",
                (slug, owner_id),
            )
            row = cur.fetchone()
        return _row_to_collection(row) if row else None

    def list_collections(self, owner_id: UUID) -> list[Collection]:
        with self._cur() as cur:
            cur.execute(
                "select id, slug, title, description, project, scope, owner_id,"
                " query, created_at, updated_at from collections "
                "where owner_id = %s order by slug",
                (owner_id,),
            )
            return [_row_to_collection(r) for r in cur.fetchall()]

    def pin(
        self, collection_id: UUID, entry_id: UUID, position: int, owner_id: UUID
    ) -> bool:
        """Pin an entry into a collection. Both must belong to owner_id.

        Returns False when the ownership guards match nothing, rather than
        writing nothing silently — the same contract as set_superseded.
        """
        with self._cur() as cur:
            cur.execute(
                """
                insert into collection_members (collection_id, entry_id, position)
                select %(collection_id)s, %(entry_id)s, %(position)s
                 where exists (select 1 from collections
                                where id = %(collection_id)s
                                  and owner_id = %(owner_id)s)
                   and exists (select 1 from entries
                                where id = %(entry_id)s
                                  and owner_id = %(owner_id)s)
                on conflict (collection_id, entry_id)
                  do update set position = excluded.position
                """,
                {
                    "collection_id": collection_id,
                    "entry_id": entry_id,
                    "position": position,
                    "owner_id": owner_id,
                },
            )
            return cur.rowcount == 1

    def pinned_entries(self, collection_id: UUID, owner_id: UUID) -> list[Entry]:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {entry_columns("e")}
                from collection_members m
                join entries e on e.id = m.entry_id
                where m.collection_id = %s and e.owner_id = %s
                order by m.position, e.created_at
                """),
                (collection_id, owner_id),
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    # ---------------- transcripts ----------------

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
    ) -> Transcript:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                insert into transcripts
                  (id, owner_id, project, harness, session_id, agent_id, path,
                   content, bytes, sha256)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                -- By constraint name, not by column list: the target has a
                -- nullable column, and naming the constraint says exactly
                -- which uniqueness rule this upsert rides on.
                on conflict on constraint transcripts_identity do update
                  set content = excluded.content,
                      bytes = excluded.bytes,
                      sha256 = excluded.sha256,
                      path = excluded.path,
                      -- `project` is deliberately NOT updated. A transcript
                      -- keeps the project it was first filed under. Two
                      -- projects can legitimately claim directories holding
                      -- the same session id if a working directory moved,
                      -- and overwriting here re-homed the transcript
                      -- silently: every count on both sides is
                      -- project-scoped, so one project's numbers quietly
                      -- dropped and the other's quietly rose with nothing
                      -- recorded anywhere. The import reports the
                      -- disagreement as a project-conflict anomaly instead
                      -- and stores the bytes regardless - reported and
                      -- stable, rather than moved and invisible.
                      last_read = clock_timestamp()
                returning {transcript_columns("transcripts")}
                """),
                (
                    new_id(),
                    owner_id,
                    project,
                    harness,
                    session_id,
                    agent_id,
                    path,
                    content,
                    len(content),
                    sha256,
                ),
            )
            return _row_to_transcript(_one(cur))

    def get_transcript(
        self,
        owner_id: UUID,
        harness: str,
        session_id: str,
        *,
        agent_id: str | None,
    ) -> Transcript | None:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {transcript_columns("t")} from transcripts t
                 where t.owner_id = %s and t.harness = %s and t.session_id = %s
                   -- `=` never matches NULL, and NULL is how a session's own
                   -- transcript is spelled.
                   and t.agent_id is not distinct from %s
                """),
                (owner_id, harness, session_id, agent_id),
            )
            row = cur.fetchone()
            return _row_to_transcript(row) if row else None

    def transcript_content(self, transcript_id: UUID, owner_id: UUID) -> bytes | None:
        with self._cur() as cur:
            cur.execute(
                "select content from transcripts where id = %s and owner_id = %s",
                (transcript_id, owner_id),
            )
            row = cur.fetchone()
            return bytes(row["content"]) if row else None

    def transcript_meta(self, transcript_id: UUID, owner_id: UUID) -> bytes | None:
        with self._cur() as cur:
            cur.execute(
                "select meta from transcripts where id = %s and owner_id = %s",
                (transcript_id, owner_id),
            )
            row = cur.fetchone()
            return bytes(row["meta"]) if row and row["meta"] is not None else None

    def set_transcript_meta(
        self, transcript_id: UUID, owner_id: UUID, meta: bytes
    ) -> bool:
        with self._cur() as cur:
            cur.execute(
                "update transcripts set meta = %s where id = %s and owner_id = %s",
                (meta, transcript_id, owner_id),
            )
            return cur.rowcount == 1

    def append_transcript(
        self, transcript_id: UUID, owner_id: UUID, tail: bytes, sha256: str
    ) -> bool:
        """Append bytes in place, in one statement.

        `content || %s` rather than read-modify-write: the bytes never travel
        to Python and back, which for a 22MB transcript is the difference
        between a cheap session-start refresh and an expensive one.
        """
        with self._cur() as cur:
            cur.execute(
                """
                update transcripts
                   set content = content || %s,
                       bytes = bytes + %s,
                       sha256 = %s,
                       last_read = clock_timestamp()
                 where id = %s and owner_id = %s
                """,
                (tail, len(tail), sha256, transcript_id, owner_id),
            )
            return cur.rowcount == 1

    def replace_transcript_lines(
        self, transcript_id: UUID, lines: list[TranscriptLine]
    ) -> int:
        with self._cur() as cur:
            cur.execute(
                "delete from transcript_lines where transcript_id = %s",
                (transcript_id,),
            )
        return self.add_transcript_lines(transcript_id, lines)

    def add_transcript_lines(
        self, transcript_id: UUID, lines: list[TranscriptLine]
    ) -> int:
        if not lines:
            return 0
        with self._cur() as cur:
            cur.executemany(
                """
                insert into transcript_lines
                  (transcript_id, seq, type, uuid, occurred_at, raw)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (transcript_id, seq) do nothing
                """,
                [
                    (
                        transcript_id,
                        line.seq,
                        line.type,
                        line.uuid,
                        line.occurred_at,
                        Jsonb(line.raw),
                    )
                    for line in lines
                ],
            )
        return len(lines)

    def transcript_line_count(self, transcript_id: UUID) -> int:
        with self._cur() as cur:
            cur.execute(
                "select count(*) as count from transcript_lines"
                " where transcript_id = %s",
                (transcript_id,),
            )
            return int(_one(cur)["count"])

    def transcripts_for_harness(self, owner_id: UUID, harness: str) -> list[Transcript]:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {transcript_columns("t")} from transcripts t
                 where t.owner_id = %s and t.harness = %s
                """),
                (owner_id, harness),
            )
            return [_row_to_transcript(r) for r in cur.fetchall()]

    def stored_transcripts(self, owner_id: UUID, project: str) -> list[Transcript]:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {transcript_columns("t")} from transcripts t
                 where t.owner_id = %s and t.project = %s
                 order by t.first_seen
                """),
                (owner_id, project),
            )
            return [_row_to_transcript(r) for r in cur.fetchall()]

    # ---------------- transcript path claims ----------------

    def add_transcript_path(
        self, owner_id: UUID, project: str, path: str
    ) -> str | None:
        """Claim a directory, or name the project that already holds it.

        Returns None on success and the conflicting project's name on
        refusal, rather than a bare bool: told only "taken", a user has no
        way to find out by what, and the real failure would surface much
        later as a session filed under the wrong project.

        This is an upsert, not a check-then-insert: a SELECT followed by a
        separate INSERT leaves a race window where two concurrent claims of
        the same path both see no row and both attempt to insert, so the
        second crashes on `transcript_paths_one_owner_idx` instead of
        returning the conflicting project name that is this method's entire
        contract. The upsert collapses both statements into one round trip
        with no window between them.

        `do update set project = transcript_paths.project` is a deliberate
        no-op write - it changes nothing - whose only purpose is to make
        `returning` hand back the *existing* row's project on a conflict.
        `do nothing` returns no row at all on conflict, which would force
        exactly the second round trip (a follow-up SELECT) this upsert
        exists to eliminate.
        """
        with self._cur() as cur:
            cur.execute(
                """
                insert into transcript_paths (owner_id, project, path)
                values (%s, %s, %s)
                on conflict (owner_id, path)
                  do update set project = transcript_paths.project
                returning project
                """,
                (owner_id, project, path),
            )
            # This module's cursor uses `dict_row`, so rows are read by
            # column name, never by position.
            holder = str(_one(cur)["project"])
            return None if holder == project else holder

    def remove_transcript_path(self, owner_id: UUID, project: str, path: str) -> bool:
        with self._cur() as cur:
            cur.execute(
                "delete from transcript_paths"
                " where owner_id = %s and project = %s and path = %s",
                (owner_id, project, path),
            )
            return cur.rowcount == 1

    def transcript_paths(
        self, owner_id: UUID, project: str | None = None
    ) -> list[TranscriptPath]:
        """Claims for one project, or every claim when project is None.

        The sweep is what `bag record status` needs: it reports one advisory
        line per unhealthy claimed project, and unlike ingest's status it can
        answer for every project because the claim stores an absolute path
        and needs no recorded working directory to resolve.
        """
        where = "owner_id = %s" + ("" if project is None else " and project = %s")
        params: tuple[Any, ...] = (
            (owner_id,) if project is None else (owner_id, project)
        )
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select owner_id, project, path, added_at from transcript_paths
                 where {where}
                 order by project, path
                """),
                params,
            )
            return [_row_to_transcript_path(r) for r in cur.fetchall()]

    # ---------------- transcript runs ----------------

    def start_transcript_run(
        self, owner_id: UUID, project: str, trigger: TranscriptTrigger
    ) -> TranscriptRun:
        run_id = new_id()
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                insert into transcript_runs (id, owner_id, project, trigger)
                values (%s, %s, %s, %s)
                returning {transcript_run_columns()}
                """),
                (run_id, owner_id, project, str(trigger)),
            )
            return _row_to_transcript_run(_one(cur))

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
    ) -> TranscriptRun:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                update transcript_runs
                   set finished_at = clock_timestamp(),
                       files_seen = %s, files_new = %s, files_appended = %s,
                       files_rebuilt = %s, lines_written = %s,
                       bytes_written = %s, metas_written = %s,
                       anomalies = %s, failures = %s
                 where id = %s and owner_id = %s
                returning {transcript_run_columns()}
                """),
                (
                    files_seen,
                    files_new,
                    files_appended,
                    files_rebuilt,
                    lines_written,
                    bytes_written,
                    metas_written,
                    Jsonb(anomalies),
                    Jsonb(failures),
                    run_id,
                    owner_id,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise NotOwner(f"transcript run {run_id} is not owned by {owner_id}")
            return _row_to_transcript_run(row)

    def latest_transcript_run(
        self, owner_id: UUID, project: str
    ) -> TranscriptRun | None:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {transcript_run_columns()} from transcript_runs
                 where owner_id = %s and project = %s
                 order by started_at desc
                 limit 1
                """),
                (owner_id, project),
            )
            row = cur.fetchone()
        return _row_to_transcript_run(row) if row else None

    def event_session_ids(self, owner_id: UUID, project: str) -> list[str]:
        """Distinct session ids recorded for a project.

        Read-only and used only by `discover`, which intersects these with
        transcript filenames to prove which directory belongs to which
        project.
        """
        with self._cur() as cur:
            cur.execute(
                "select distinct session_id from events"
                " where owner_id = %s and project = %s",
                (owner_id, project),
            )
            return [str(r["session_id"]) for r in cur.fetchall()]

    def event_session_projects(self, owner_id: UUID) -> list[tuple[str, str]]:
        """Distinct (session id, project) pairs recorded for an owner.

        Read-only, and built once per import run rather than once per file:
        a claimed directory holds hundreds of transcripts and this is one
        query for all of them.
        """
        with self._cur() as cur:
            cur.execute(
                "select distinct session_id, project from events where owner_id = %s",
                (owner_id,),
            )
            return [(str(r["session_id"]), str(r["project"])) for r in cur.fetchall()]

    # ---------------- recording ----------------

    def set_record_enabled(self, owner_id: UUID, project: str, enabled: bool) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                insert into record_settings (owner_id, project, enabled)
                values (%s, %s, %s)
                on conflict (owner_id, project)
                  do update set enabled = excluded.enabled
                """,
                (owner_id, project, enabled),
            )

    def record_enabled(self, owner_id: UUID, project: str) -> bool:
        with self._cur() as cur:
            cur.execute(
                "select enabled from record_settings "
                "where owner_id = %s and project = %s",
                (owner_id, project),
            )
            row = cur.fetchone()
        return bool(row["enabled"]) if row else False

    def set_memory_collection(
        self,
        owner_id: UUID,
        project: str,
        slug: str | None,
        working_dir: str | None = None,
    ) -> None:
        with self._cur() as cur:
            if slug is None:
                cur.execute(
                    "delete from memory_settings where owner_id = %s and project = %s",
                    (owner_id, project),
                )
                return
            cur.execute(
                """
                insert into memory_settings
                    (owner_id, project, collection_slug, working_dir)
                values (%s, %s, %s, %s)
                on conflict (owner_id, project)
                  do update set collection_slug = excluded.collection_slug,
                                working_dir = excluded.working_dir,
                                updated_at = clock_timestamp()
                """,
                (owner_id, project, slug, working_dir),
            )

    def memory_designations(self, owner_id: UUID) -> list[MemoryDesignation]:
        with self._cur() as cur:
            cur.execute(
                "select project, collection_slug, working_dir "
                "from memory_settings where owner_id = %s order by project",
                (owner_id,),
            )
            rows = cur.fetchall()
        return [
            MemoryDesignation(
                project=r["project"],
                collection=r["collection_slug"],
                working_dir=r["working_dir"],
            )
            for r in rows
        ]

    def memory_collection(self, owner_id: UUID, project: str) -> str | None:
        with self._cur() as cur:
            cur.execute(
                "select collection_slug from memory_settings "
                "where owner_id = %s and project = %s",
                (owner_id, project),
            )
            row = cur.fetchone()
        return row["collection_slug"] if row else None

    def set_ingest_paths(
        self,
        owner_id: UUID,
        project: str,
        paths: list[str] | None,
        archive: bool = False,
    ) -> None:
        with self._cur() as cur:
            if paths is None:
                cur.execute(
                    "delete from ingest_settings "
                    "where owner_id = %s and project = %s and archive = %s",
                    (owner_id, project, archive),
                )
                return
            cur.execute(
                """
                insert into ingest_settings
                    (owner_id, project, archive, paths)
                values (%s, %s, %s, %s)
                on conflict (owner_id, project, archive)
                  do update set paths = excluded.paths,
                                updated_at = clock_timestamp()
                """,
                (owner_id, project, archive, list(paths)),
            )

    def ingest_designations(
        self, owner_id: UUID, project: str | None = None
    ) -> list[IngestDesignation]:
        with self._cur() as cur:
            cur.execute(
                "select project, archive, paths from ingest_settings "
                "where owner_id = %s and (%s::text is null or project = %s) "
                "order by project, archive",
                (owner_id, project, project),
            )
            rows = cur.fetchall()
        return [
            IngestDesignation(
                project=r["project"],
                paths=tuple(r["paths"]),
                archive=r["archive"],
            )
            for r in rows
        ]

    # ---------------- ingest runs ----------------

    def start_ingest_run(
        self,
        owner_id: UUID,
        project: str,
        trigger: IngestTrigger,
        archive: bool = False,
    ) -> IngestRun:
        run_id = new_id()
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                insert into ingest_runs (id, owner_id, project, trigger, archive)
                values (%s, %s, %s, %s, %s)
                returning {ingest_run_columns()}
                """),
                (run_id, owner_id, project, str(trigger), archive),
            )
            return _row_to_ingest_run(_one(cur))

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
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                update ingest_runs
                   set finished_at = clock_timestamp(),
                       created = %s, changed = %s, unchanged = %s,
                       swept = %s, embedded = %s,
                       failures = %s::jsonb, twins = %s::jsonb,
                       embed_error = %s
                 where id = %s and owner_id = %s
                """,
                (
                    created,
                    changed,
                    unchanged,
                    swept,
                    embedded,
                    json.dumps(failures),
                    json.dumps(twins),
                    embed_error,
                    run_id,
                    owner_id,
                ),
            )
            if cur.rowcount == 0:
                raise NotOwner(f"ingest run {run_id} is not owned by {owner_id}")

    def latest_ingest_run(self, owner_id: UUID, project: str) -> IngestRun | None:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {ingest_run_columns()} from ingest_runs
                 where owner_id = %s and project = %s
                 order by started_at desc
                 limit 1
                """),
                (owner_id, project),
            )
            row = cur.fetchone()
        return _row_to_ingest_run(row) if row else None

    def start_memory_run(
        self, owner_id: UUID, project: str, trigger: MemoryTrigger
    ) -> MemoryRun:
        """The row that exists before any file is read.

        Committed by the caller's autocommit session, which is what makes a
        `finished_at` of null mean "the process died" rather than "the
        transaction rolled back".
        """
        run_id = new_id()
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                insert into memory_runs (id, owner_id, project, trigger)
                values (%s, %s, %s, %s)
                returning {memory_run_columns()}
                """),
                (run_id, owner_id, project, str(trigger)),
            )
            return _row_to_memory_run(_one(cur))

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
    ) -> None:
        with self._cur() as cur:
            cur.execute(
                """
                update memory_runs
                   set finished_at = clock_timestamp(),
                       adopted = %s, healed = %s, edited = %s,
                       regenerated = %s, deleted = %s, unchanged = %s,
                       renamed = %s::jsonb, conflicts = %s::jsonb,
                       sidecars = %s::jsonb, failures = %s::jsonb
                 where id = %s and owner_id = %s
                """,
                (
                    adopted,
                    healed,
                    edited,
                    regenerated,
                    deleted,
                    unchanged,
                    json.dumps(renamed),
                    json.dumps(conflicts),
                    json.dumps(sidecars),
                    json.dumps(failures),
                    run_id,
                    owner_id,
                ),
            )
            if cur.rowcount == 0:
                raise NotOwner(f"memory run {run_id} is not owned by {owner_id}")

    def latest_memory_run(self, owner_id: UUID, project: str) -> MemoryRun | None:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {memory_run_columns()} from memory_runs
                 where owner_id = %s and project = %s
                 order by started_at desc
                 limit 1
                """),
                (owner_id, project),
            )
            row = cur.fetchone()
        return _row_to_memory_run(row) if row else None

    def anchors(self, owner_id: UUID, project: str) -> list[Entry]:
        # `%%` because this statement takes positional parameters, so a
        # literal percent has to be doubled for psycopg's formatter.
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {entry_columns("e")} from entries e
                 where e.owner_id = %s
                   and e.project = %s
                   and e.superseded_by is null
                   and e.origin in ('ingested', 'archived')
                   and exists (select 1 from unnest(e.tags) t where t like 'src:%%')
                   and not exists (select 1 from unnest(e.tags) t where t like 'sec:%%')
                 order by e.created_at desc
                """),
                (owner_id, project),
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def pending_legacy_capture_jobs(self, owner_id: UUID) -> int:
        """How many rows the retired capture spool still holds as pending.

        The only query left that touches `capture_jobs_legacy` (see
        010_retire_capture_jobs.sql). It exists so a user who opted into the
        old pipeline is told once that something is sitting there, rather
        than discovering a table nothing reads years later.
        """
        with self._cur() as cur:
            cur.execute(
                "select count(*) as n from capture_jobs_legacy "
                "where owner_id = %s and status = 'pending'",
                (owner_id,),
            )
            return int(_one(cur)["n"])

    def enabled_record_projects(self, owner_id: UUID) -> list[str]:
        """Projects with recording switched on, for the status commands."""
        with self._cur() as cur:
            cur.execute(
                "select project from record_settings "
                "where owner_id = %s and enabled order by project",
                (owner_id,),
            )
            return [r["project"] for r in cur.fetchall()]

    def event_stats(self, owner_id: UUID) -> list[HarnessStats]:
        """Per-harness recent volume for `bag record status`.

        Only harnesses with at least one recorded event ever appear in the
        result - there is no harness name to key a zero row on for one that
        has recorded nothing, which is exactly the failure this command
        exists to catch. `services.events.render` turns an empty list into a
        visible "no events" line rather than an absent section.

        `sessions_awaiting` is deliberately not computed here: that count
        depends on the "given up" rule, which is a policy decision that
        belongs in `services.extraction` (`_gave_up`/`awaiting_sessions`),
        not duplicated into SQL. `services.events.status` fills it in after
        this call by tallying `extraction.awaiting_sessions` per harness -
        see the comment there for why, and the task-8 review this answers.
        """
        with self._cur() as cur:
            cur.execute(
                """
                select harness,
                       count(*) filter (
                         where occurred_at
                                 >= clock_timestamp() - interval '24 hours'
                       ) as events_24h,
                       max(occurred_at) as last_event_at,
                       0 as sessions_awaiting
                  from events
                 where owner_id = %(owner_id)s
                 group by harness
                 order by harness
                """,
                {"owner_id": owner_id},
            )
            return [_row_to_harness_stats(r) for r in cur.fetchall()]

    # ---------------- events ----------------

    def duplicate_unkeyed_events(
        self, owner_id: UUID, limit: int = 20
    ) -> list[DuplicateGroup]:
        """Repeated events that 011's unique index cannot see.

        Scoped to `event_key is null` on purpose. For a keyed event a
        duplicate is already impossible, and asking the same question of
        those rows could only produce false alarms: two tool calls with the
        same payload in one session is ordinary, and it is only the
        harness's own id that says otherwise.

        Payload equality is the signal here, which would be the wrong rule
        for a constraint and is the right one for a report: the worst a
        false positive can do is print a line.

        Tool calls are excluded even when unkeyed. Both harnesses that
        record them stamp a tool_use_id, so an unkeyed one is already
        unusual - and running the same command twice in a session is
        completely ordinary, which would make this fire on healthy data.
        A report nobody can trust is one nobody reads. What is left is
        exactly the two shapes with no id to key on and no reason to
        repeat: claude-code's SessionEnd and opencode's message.
        """
        with self._cur() as cur:
            cur.execute(
                """
                select project, harness, session_id, count(*) as n
                  from events
                 where owner_id = %(owner_id)s
                   and event_key is null
                   and kind <> 'tool_call'
                 group by project, harness, session_id, kind, payload
                having count(*) > 1
                 order by count(*) desc, harness, session_id
                 limit %(limit)s
                """,
                {"owner_id": owner_id, "limit": limit},
            )
            return [
                DuplicateGroup(
                    project=r["project"],
                    harness=r["harness"],
                    session_id=r["session_id"],
                    count=r["n"],
                )
                for r in cur.fetchall()
            ]

    def put_event(self, event: Event) -> Event:
        """One INSERT. This is the hot path - it runs per tool call."""
        with self._cur() as cur:
            cur.execute(
                """
                insert into events (
                  id, owner_id, project, harness, session_id,
                  kind, tool, payload, occurred_at
                ) values (
                  %(id)s, %(owner_id)s, %(project)s, %(harness)s,
                  %(session_id)s, %(kind)s::event_kind, %(tool)s,
                  %(payload)s::jsonb, %(occurred_at)s
                )
                -- A duplicate is dropped, not raised. The caller is a
                -- fail-soft hook doing one INSERT; an exception here is how
                -- a twice-registered hook turns into a broken session.
                --
                -- `do update` setting payload to what it already is, rather
                -- than `do nothing`: a no-op write that still RETURNS the
                -- surviving row. `do nothing` returns nothing, and finding
                -- that row afterwards would mean a second copy of 011's
                -- event_key expression here, free to drift from it.
                -- First write wins; nothing about the stored row changes.
                on conflict (owner_id, project, harness, session_id, event_key)
                  where event_key is not null
                  do update set payload = events.payload
                returning id, recorded_at
                """,
                {
                    "id": event.id,
                    "owner_id": event.owner_id,
                    "project": event.project,
                    "harness": event.harness,
                    "session_id": event.session_id,
                    "kind": str(event.kind),
                    "tool": event.tool,
                    "payload": json.dumps(event.payload),
                    # A NOT NULL column with no default is how a fail-soft hook
                    # turns into a lost event, so we default here rather than
                    # trust every caller to have set occurred_at.
                    "occurred_at": event.occurred_at or datetime.now(timezone.utc),
                },
            )
            # The row actually stored, which on a duplicate is the earlier
            # one - so every caller gets a usable Event either way, and the
            # id it carries is the id that is really in the table.
            row = _one(cur)
            event.id = row["id"]
            event.recorded_at = row["recorded_at"]
        return event

    def events_for_session(
        self,
        owner_id: UUID,
        project: str,
        harness: str,
        session_id: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[Event]:
        with self._cur() as cur:
            cur.execute(
                """
                select id, owner_id, project, harness, session_id, kind,
                       tool, payload, occurred_at, recorded_at
                  from events
                 where owner_id = %(owner_id)s and project = %(project)s
                   and harness = %(harness)s and session_id = %(session_id)s
                   and (%(since)s::timestamptz is null or occurred_at > %(since)s)
                 order by occurred_at
                 limit %(limit)s
                """,
                {
                    "owner_id": owner_id,
                    "project": project,
                    "harness": harness,
                    "session_id": session_id,
                    "since": since,
                    "limit": limit,
                },
            )
            return [_row_to_event(r) for r in cur.fetchall()]

    def delete_session_events(
        self, owner_id: UUID, project: str, harness: str, session_id: str
    ) -> int:
        """Delete every event for one session. Scoped by all four keys, not
        just owner_id and a time window - see the Protocol docstring for why
        `prune_events` is the wrong tool for this."""
        with self._cur() as cur:
            cur.execute(
                """
                delete from events
                 where owner_id = %(owner_id)s and project = %(project)s
                   and harness = %(harness)s and session_id = %(session_id)s
                """,
                {
                    "owner_id": owner_id,
                    "project": project,
                    "harness": harness,
                    "session_id": session_id,
                },
            )
            return cur.rowcount

    def link_entry_events(
        self, entry_id: UUID, events: list[Event], owner_id: UUID
    ) -> None:
        # Ownership is checked here, like every other write: the entry must
        # belong to this principal before we record anything about where it
        # came from.
        with self._cur() as cur:
            cur.execute(
                "select 1 from entries where id = %s and owner_id = %s",
                (entry_id, owner_id),
            )
            if cur.fetchone() is None:
                raise NotOwner(f"entry {entry_id} does not belong to {owner_id}")
            for event in events:
                cur.execute(
                    """
                    insert into entry_events (entry_id, event_id, session_id, harness)
                    values (%s, %s, %s, %s)
                    -- Re-running an extraction must not fail on provenance
                    -- it already wrote.
                    on conflict (entry_id, event_id) do nothing
                    """,
                    (entry_id, event.id, event.session_id, event.harness),
                )

    def provenance(
        self, entry_id: UUID, owner_id: UUID
    ) -> list[tuple[UUID, str, str, bool]]:
        """The forensic lookup, and the only query allowed to follow event_id.

        A left join, never an inner one: a pruned event must come back as a
        row with `present = False`, because "we recorded where this came
        from and then deleted the raw" and "we never recorded anything" are
        different answers and the user needs to be able to tell them apart.
        """
        with self._cur() as cur:
            cur.execute(
                """
                select ee.event_id, ee.session_id, ee.harness,
                       (ev.id is not null) as present
                  from entry_events ee
                  join entries e on e.id = ee.entry_id
             left join events ev on ev.id = ee.event_id
                 where ee.entry_id = %s and e.owner_id = %s
                 order by ee.event_id
                """,
                (entry_id, owner_id),
            )
            return [
                (r["event_id"], r["session_id"], r["harness"], r["present"])
                for r in cur.fetchall()
            ]

    def prune_events(
        self,
        owner_id: UUID,
        before: datetime,
        force: bool,
        project: str | None = None,
    ) -> tuple[int, int, int]:
        """Delete raw events older than `before`, and say what that cost.

        "Extracted" uses the same watermark rule as
        `sessions_awaiting_extraction` - the newest `covers_through` recorded
        for a session, whatever its job's current status - so prune and
        process can never disagree about what has already been extracted.
        `--force` (the `force` argument) drops that condition entirely
        rather than widening it: an unextracted
        event is raw that produced nothing, and losing it is the outcome the
        whole pipeline exists to prevent, so overriding that is a deliberate
        act, not a wider window.

        `project` narrows every part of this - the delete AND the
        `kept_unextracted` count - to one project. It has to narrow both:
        counting the whole window while deleting one project's slice would
        refuse a prune because of raw belonging to a project the user never
        named, with nothing in the message to say so. None means every
        project, which is the behaviour that shipped first and stays the
        default; the flag only ever narrows.

        The dangling count is taken from `entry_events` before the delete
        runs, in the same statement - after the delete the rows are already
        dangling and counting them then would just be re-deriving what this
        statement did. `kept_unextracted` is the other side of the refusal:
        how many events in the window survived only because they had not
        been extracted yet.
        """
        with self._cur() as cur:
            cur.execute(
                """
                with watermarks as (
                  -- Keyed on covers_through, never on status: extract_jobs
                  -- holds one row per session, so a job that succeeded and
                  -- later failed leaves the row FAILED even though its mark
                  -- still stands. covers_through is set only by a successful
                  -- finish and preserved on every failure path, so it is a
                  -- precise record of "extracted through here"; status only
                  -- records how the last run ended. Filtering on status would
                  -- make already-extracted events read as unextracted and
                  -- permanently overcount kept_unextracted.
                  select project, harness, session_id, max(covers_through) as mark
                    from extract_jobs
                   where owner_id = %(owner_id)s and covers_through is not null
                   group by project, harness, session_id
                ), scoped as (
                  select e.id,
                         (w.mark is not null and e.occurred_at <= w.mark)
                           as extracted
                    from events e
                    left join watermarks w
                           on w.project = e.project and w.harness = e.harness
                          and w.session_id = e.session_id
                   where e.owner_id = %(owner_id)s
                     and e.occurred_at < %(before)s
                     -- One `scoped` CTE feeds both the delete and the
                     -- kept_unextracted count, so filtering here is what
                     -- keeps the two from disagreeing about the window.
                     and (%(project)s::text is null or e.project = %(project)s)
                ), candidates as (
                  select id from scoped where %(force)s or extracted
                ), dangling as (
                  select count(*) as n from entry_events
                   where event_id in (select id from candidates)
                ), deleted as (
                  delete from events where id in (select id from candidates)
                  returning id
                )
                select (select count(*) from deleted) as deleted,
                       (select n from dangling) as dangling,
                       -- Under --force nothing is "kept" for lack of
                       -- extraction - it was deleted along with everything
                       -- else in the window, so this is unconditionally 0
                       -- rather than a count of rows that no longer exist.
                       (case when %(force)s then 0
                             else (select count(*) from scoped
                                    where not extracted) end)
                         as kept_unextracted
                """,
                {
                    "owner_id": owner_id,
                    "before": before,
                    "force": force,
                    "project": project,
                },
            )
            row = _one(cur)
            return (row["deleted"], row["dangling"], row["kept_unextracted"])

    # ---------------- extraction spool ----------------

    def get_extract_job(self, job_id: UUID, owner_id: UUID) -> ExtractJob | None:
        with self._cur() as cur:
            cur.execute(
                as_sql(
                    f"select {extract_job_columns()} from extract_jobs "
                    "where id = %s and owner_id = %s"
                ),
                (job_id, owner_id),
            )
            row = cur.fetchone()
        return _row_to_extract_job(row) if row else None

    def extract_job_counts(self, owner_id: UUID) -> dict[str, int]:
        with self._cur() as cur:
            cur.execute(
                "select status, count(*) as n from extract_jobs "
                "where owner_id = %s group by status",
                (owner_id,),
            )
            return {str(r["status"]): r["n"] for r in cur.fetchall()}

    def recent_failed_extract_jobs(
        self, owner_id: UUID, limit: int = 5
    ) -> list[ExtractJob]:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {extract_job_columns()} from extract_jobs
                 where owner_id = %s and status = 'failed'
                 order by updated_at desc
                 limit %s
                """),
                (owner_id, limit),
            )
            return [_row_to_extract_job(r) for r in cur.fetchall()]

    def finish_extract_job(
        self,
        job_id: UUID,
        owner_id: UUID,
        status: JobStatus,
        error: str | None,
        entries_written: int,
        covers_through: datetime | None,
    ) -> None:
        """Record a job's outcome, and on success clear its retry budget.

        `attempts` counts *consecutive* failures, which is what MAX_ATTEMPTS
        and every docstring around it already claim it means - so a run that
        succeeds resets it to zero. The old capture spool needed no such
        reset: a job was keyed on a transcript path and claimed exactly once,
        so every increment really was a failed try. Discovery-based claiming
        changed that. `claim_extract_job` upserts on the session key, and the
        SAME row is legitimately re-claimed every time the session produces
        new outstanding events - a resumed session (Claude Code keeps its
        session_id across --continue/--resume), or a long one worked over
        successive runs by MAX_EVENTS_PER_JOB. Without the reset, a session
        extracted cleanly more than MAX_ATTEMPTS times dies permanently with
        "gave up after N attempts", a failure that never happened.

        The reset belongs here and not in `claim_extract_job`: claiming stays
        a pure claim, and "a successful run clears the retry budget" sits with
        the rest of the outcome recording, where the next reader will find it.
        """
        with self._cur() as cur:
            cur.execute(
                """
                update extract_jobs
                   set status = %(status)s::job_status, error = %(error)s,
                       entries_written = %(written)s,
                       covers_through = %(covers_through)s,
                       -- Explicitly cast on both uses: the same parameter is
                       -- assigned to a job_status column and compared to a
                       -- text literal, and Postgres refuses to deduce one
                       -- type for both.
                       attempts = case when %(status)s::text = 'done' then 0
                                       else attempts end,
                       updated_at = clock_timestamp()
                 where id = %(id)s and owner_id = %(owner_id)s
                """,
                {
                    "status": str(status),
                    "error": error,
                    "written": entries_written,
                    "covers_through": covers_through,
                    "id": job_id,
                    "owner_id": owner_id,
                },
            )

    def try_advisory_lock(self, name: str, owner_id: UUID) -> bool:
        # Session-level, not transaction-level: `events process` runs with
        # autocommit on, so a transaction-scoped lock would be released at
        # the first commit - which is the first job it finishes, exactly
        # when a second run must still be kept out. The lock dies with the
        # connection, which is the process ending, which is what we want.
        #
        # Postgres's advisory lock functions come in a one-bigint and a
        # two-int form; the two-int form is used here so the command name
        # and the owner can be hashed independently instead of combined into
        # one 64-bit value, which would need care to avoid collisions between
        # different (name, owner) pairs landing on the same bigint.
        with self._cur() as cur:
            cur.execute(
                "select pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
                (name, str(owner_id)),
            )
            return bool(_one(cur)["pg_try_advisory_lock"])

    # ---------------- usage ----------------

    def log_access(self, record: AccessRecord) -> None:
        with self._cur() as cur:
            cur.execute(
                "insert into access_log (id, owner_id, project, session_id, "
                "source, op, query_len, tier, hits, entry_ids, elapsed_ms) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    new_id(),
                    record.owner_id,
                    record.project,
                    record.session_id,
                    record.source,
                    record.op,
                    record.query_len,
                    record.tier,
                    record.hits,
                    list(record.entry_ids),
                    record.elapsed_ms,
                ),
            )

    def log_injection(self, record: InjectionRecord) -> None:
        with self._cur() as cur:
            cur.execute(
                "insert into injection_log (id, owner_id, project, harness, "
                "session_id, found, rules, notes, chars, tokens_est, "
                "budget_chars, entry_ids) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    new_id(),
                    record.owner_id,
                    record.project,
                    record.harness,
                    record.session_id,
                    record.found,
                    record.rules,
                    record.notes,
                    record.chars,
                    record.tokens_est,
                    record.budget_chars,
                    list(record.entry_ids),
                ),
            )

    def entry_counts(self, owner_id: UUID, project: str | None) -> EntryCounts:
        with self._cur() as cur:
            cur.execute(
                """
                select
                  count(*) filter (where superseded_by is null) as live,
                  count(*) filter (where superseded_by is null
                                   and project = %(project)s) as project_live,
                  count(*) filter (where superseded_by is not null) as superseded,
                  count(distinct project) filter (where superseded_by is null)
                    as projects,
                  (select count(*) from collections where owner_id = %(owner)s)
                    as collections
                from entries where owner_id = %(owner)s
                """,
                {"owner": owner_id, "project": project},
            )
            head = _one(cur)
            cur.execute(
                "select kind::text as k, count(*) as n from entries "
                "where owner_id = %s and superseded_by is null group by 1",
                (owner_id,),
            )
            kinds = {r["k"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute(
                "select origin::text as k, count(*) as n from entries "
                "where owner_id = %s and superseded_by is null group by 1",
                (owner_id,),
            )
            origins = {r["k"]: int(r["n"]) for r in cur.fetchall()}
        return EntryCounts(
            live=int(head["live"]),
            project_live=int(head["project_live"]),
            by_kind=kinds,
            by_origin=origins,
            superseded=int(head["superseded"]),
            collections=int(head["collections"]),
            projects=int(head["projects"]),
        )

    def access_summary(
        self, owner_id: UUID, since: datetime, project: str | None
    ) -> AccessSummary:
        # `project is null` in the parameter means "every project". Written as
        # one predicate rather than two SQL strings so the owner filter exists
        # exactly once.
        owned = (
            "owner_id = %(owner)s "
            "and (%(project)s::text is null or project = %(project)s)"
        )
        scope = f"{owned} and at >= %(since)s"
        args = {"owner": owner_id, "since": since, "project": project}
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select
                  count(*) as reads,
                  count(*) filter (where op = 'search') as searches,
                  count(*) filter (where op = 'search' and hits > 0) as search_hits,
                  percentile_cont(0.5) within group (order by elapsed_ms)
                    filter (where op = 'search') as p50,
                  count(distinct session_id) as sessions,
                  -- `owned`, not `scope`: "since" is about the whole log,
                  -- not the window. It must carry the project predicate
                  -- though, or a first read in a project that started
                  -- yesterday reads as `7d: 0 reads` on an old install -
                  -- the very "a fresh install looks like nobody recalls
                  -- anything" failure the `since` spelling exists to stop.
                  (select min(at) from access_log where {owned}) as first_at
                from access_log where {scope}
                """),
                args,
            )
            head = _one(cur)
            by: dict[str, dict[str, int]] = {}
            # `column` is interpolated from this literal tuple, not from
            # anything derived - a module constant in spirit, the same
            # exemption `as_sql`'s own docstring describes.
            for column in ("source", "op", "tier"):
                cur.execute(
                    as_sql(f"""
                    select {column} as k, count(*) as n from access_log
                     where {scope} and {column} is not null group by 1
                    """),
                    args,
                )
                by[column] = {r["k"]: int(r["n"]) for r in cur.fetchall()}
        return AccessSummary(
            first_at=head["first_at"],
            reads=int(head["reads"]),
            by_source=by["source"],
            by_op=by["op"],
            searches=int(head["searches"]),
            search_hits=int(head["search_hits"]),
            tiers=by["tier"],
            p50_ms=None if head["p50"] is None else round(head["p50"]),
            sessions=int(head["sessions"]),
        )

    def injection_summary(
        self, owner_id: UUID, since: datetime, project: str | None
    ) -> InjectionSummary:
        owned = (
            "owner_id = %(owner)s "
            "and (%(project)s::text is null or project = %(project)s)"
        )
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                with inj as (
                  select * from injection_log
                   where {owned} and at >= %(since)s
                ),
                -- One row per SESSION, not per injection row. Claude Code
                -- fires SessionStart on startup, resume, clear and compact,
                -- so a single session logs several rows and `count(*)` over
                -- `inj` would report several sessions where there was one.
                sess as (
                  select session_id, min(at) as at from inj
                   where session_id is not null group by 1
                ),
                -- Deduped per (session, entry) on the earliest injection,
                -- for the same reason: a session injected three times
                -- would otherwise count its rule ids three times in
                -- `injected` while `opened` counts each once, dragging
                -- follow-through toward zero by how often the user
                -- compacted.
                ids as (
                  select i.session_id, min(i.at) as at, u.id as entry_id
                    from inj i, unnest(i.entry_ids) as u(id)
                   where i.session_id is not null
                   group by i.session_id, u.id
                )
                select
                  (select count(*) from sess) as sessions,
                  -- The two halves of the "N/M sessions" ratio, both drawn
                  -- from `sess` so they describe one population: sessions
                  -- that were injected, and those of them that then read.
                  -- A session that read without an injection row - every
                  -- cursor and opencode session today, since the CLI reads
                  -- its session id from a Claude Code variable - is outside
                  -- the ratio entirely rather than inflating one side of
                  -- it. Not project-scoped on the access side: the
                  -- population is already pinned by `sess`, and a session
                  -- that read from a subdirectory filed under another
                  -- project still followed through.
                  (select count(*) from sess s
                    where exists (
                      select 1 from access_log a
                       where a.owner_id = %(owner)s
                         and a.session_id = s.session_id
                         and a.at >= s.at)) as sessions_read,
                  (select avg(rules) from inj) as mean_rules,
                  (select avg(notes) from inj) as mean_notes,
                  (select avg(tokens_est) from inj) as mean_tokens,
                  (select count(*) from ids) as injected,
                  -- A `search` row whose hits include the injected id also
                  -- counts as "opened": a recall that surfaces the entry
                  -- again is the same follow-through as a direct `get`.
                  (select count(*) from ids
                    where exists (
                      select 1 from access_log a
                       where a.owner_id = %(owner)s
                         and a.session_id = ids.session_id
                         and a.at >= ids.at
                         and ids.entry_id = any(a.entry_ids))) as opened,
                  -- `owned`, not the window: "since" is about the whole
                  -- log. It carries the project predicate for the reason
                  -- `access_summary` gives - an owner-wide `min(at)` makes
                  -- a project logged for the first time yesterday read as
                  -- a silent week on an old install.
                  (select min(at) from injection_log where {owned}) as first_at
                """),
                {"owner": owner_id, "since": since, "project": project},
            )
            row = _one(cur)
        return InjectionSummary(
            first_at=row["first_at"],
            sessions=int(row["sessions"]),
            sessions_read=int(row["sessions_read"]),
            mean_rules=float(row["mean_rules"] or 0),
            mean_notes=float(row["mean_notes"] or 0),
            mean_tokens=float(row["mean_tokens"] or 0),
            injected=int(row["injected"]),
            opened=int(row["opened"]),
        )

    def recent_entries(self, owner_id: UUID, limit: int) -> list[Entry]:
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {entry_columns()} from entries
                 where owner_id = %s
                 order by created_at desc, id desc
                 limit %s
                """),
                (owner_id, limit),
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def pipeline_counts(self, owner_id: UUID, since: datetime) -> PipelineCounts:
        with self._cur() as cur:
            cur.execute(
                """
                select
                  count(*) filter (where agent_id is null) as sessions,
                  count(*) filter (where agent_id is not null) as subagents
                from transcripts where owner_id = %s
                """,
                (owner_id,),
            )
            t = _one(cur)
            cur.execute(
                "select count(*) as n from entries where owner_id = %s "
                "and origin = 'extracted' and created_at >= %s",
                (owner_id, since),
            )
            extracted = int(_one(cur)["n"])
            cur.execute(
                as_sql(f"""
                select {transcript_run_columns()} from transcript_runs
                 where owner_id = %s order by started_at desc limit 1
                """),
                (owner_id,),
            )
            tr = cur.fetchone()
            cur.execute(
                as_sql(f"""
                select {memory_run_columns()} from memory_runs
                 where owner_id = %s order by started_at desc limit 1
                """),
                (owner_id,),
            )
            mr = cur.fetchone()
            cur.execute(
                as_sql(f"""
                select {ingest_run_columns()} from ingest_runs
                 where owner_id = %s order by started_at desc limit 1
                """),
                (owner_id,),
            )
            ir = cur.fetchone()
        return PipelineCounts(
            transcript_sessions=int(t["sessions"]),
            transcript_subagents=int(t["subagents"]),
            last_transcript_run=_row_to_transcript_run(tr) if tr else None,
            last_memory_run=_row_to_memory_run(mr) if mr else None,
            last_ingest_run=_row_to_ingest_run(ir) if ir else None,
            extracted_since=extracted,
        )

    def set_statement_timeout(self, ms: int) -> None:
        # `set local` takes no bind parameter - Postgres parses the value
        # at parse time, so psycopg cannot send it as one - which is why
        # this is the rare interpolation. `int()` is what makes it safe:
        # the only thing that can reach the query text is a decimal
        # integer, whatever the caller passed. `as_sql` is called
        # deliberately, as its docstring asks, so the exemption is
        # greppable.
        with self._cur() as cur:
            cur.execute(as_sql(f"set local statement_timeout = {int(ms)}"))

    def transaction(self) -> AbstractContextManager[Any]:
        # psycopg's own transaction() already does exactly what the
        # Protocol promises: a real transaction under autocommit, a
        # savepoint inside one already open. No wrapping needed.
        return self._conn.transaction()

    def extract_job_for_session(
        self, owner_id: UUID, session: SessionRef
    ) -> ExtractJob | None:
        """The job this session already has, if any. Reads nothing else.

        Deliberately status-blind: what to do with a job that has given up
        is a policy question, and policy lives in services/. This just says
        what the row is.
        """
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                select {extract_job_columns()} from extract_jobs
                 where owner_id = %s and project = %s and harness = %s
                   and session_id = %s
                """),
                (owner_id, session.project, session.harness, session.session_id),
            )
            row = cur.fetchone()
        return _row_to_extract_job(row) if row else None

    def claim_extract_job(self, owner_id: UUID, session: SessionRef) -> ExtractJob:
        """Upsert on the session key, so a retried session reuses its row
        and its attempt count instead of accumulating one row per attempt."""
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                insert into extract_jobs (
                  id, owner_id, project, harness, session_id, status, attempts
                ) values (%(id)s, %(owner_id)s, %(project)s, %(harness)s,
                          %(session_id)s, 'running', 1)
                on conflict (owner_id, project, harness, session_id)
                  do update set status = 'running',
                                attempts = extract_jobs.attempts + 1,
                                updated_at = clock_timestamp()
                returning {extract_job_columns()}
                """),
                {
                    "id": new_id(),
                    "owner_id": owner_id,
                    "project": session.project,
                    "harness": session.harness,
                    "session_id": session.session_id,
                },
            )
            return _row_to_extract_job(_one(cur))

    def claim_extract_job_by_id(
        self, job_id: UUID, owner_id: UUID
    ) -> ExtractJob | None:
        """Claim one named job whatever its status, for `process --job ID`.

        Unlike claim_extract_job this ignores status entirely: retrying a
        job that already gave up is the whole point of the flag. SKIP LOCKED
        still keeps a concurrent run from taking the same row.
        """
        with self._cur() as cur:
            cur.execute(
                as_sql(f"""
                with claimed as (
                  select id from extract_jobs
                   where id = %(id)s and owner_id = %(owner_id)s
                   for update skip locked
                )
                update extract_jobs j
                   set status = 'running',
                       attempts = j.attempts + 1,
                       updated_at = clock_timestamp()
                  from claimed
                 where j.id = claimed.id
                returning {extract_job_columns("j")}
                """),
                {"id": job_id, "owner_id": owner_id},
            )
            row = cur.fetchone()
        return _row_to_extract_job(row) if row else None

    def sessions_awaiting_extraction(
        self, owner_id: UUID, idle_seconds: int, limit: int
    ) -> list[SessionRef]:
        """Sessions with events past their watermark, quiet long enough.

        The `watermarks` CTE gives the newest covers_through per session -
        the newest, not any, because a session can be extracted more than
        once across its life and only the latest watermark matters. It keys
        on covers_through rather than on job status; see the CTE's comment.
        Events at or before that mark already produced whatever they were
        going to produce; event_count and the idle check
        both look only at what is left after it, which is what makes
        event_count mean "work outstanding" rather than "events that exist".
        """
        with self._cur() as cur:
            cur.execute(
                """
                with watermarks as (
                  -- Keyed on covers_through, never on status: one row per
                  -- session means a job that succeeded and later failed
                  -- leaves the row FAILED with its mark intact. covers_through
                  -- is written only by a successful finish and preserved on
                  -- every failure path, so it says "extracted through here"
                  -- where status only says how the last run ended. On status
                  -- this session would report every event, extracted ones
                  -- included, as outstanding forever.
                  select project, harness, session_id, max(covers_through) as mark
                    from extract_jobs
                   where owner_id = %(owner_id)s and covers_through is not null
                   group by project, harness, session_id
                )
                select e.project, e.harness, e.session_id,
                       count(*) as event_count,
                       max(e.occurred_at) as last_event_at,
                       w.mark as extract_from
                  from events e
                  left join watermarks w
                         on w.project = e.project
                        and w.harness = e.harness
                        and w.session_id = e.session_id
                 where e.owner_id = %(owner_id)s
                   and (w.mark is null or e.occurred_at > w.mark)
                 group by e.project, e.harness, e.session_id, w.mark
                having max(e.occurred_at)
                         < clock_timestamp() - make_interval(secs => %(idle)s)
                 order by max(e.occurred_at)
                 limit %(limit)s
                """,
                {"owner_id": owner_id, "idle": idle_seconds, "limit": limit},
            )
            return [_row_to_session_ref(r) for r in cur.fetchall()]
