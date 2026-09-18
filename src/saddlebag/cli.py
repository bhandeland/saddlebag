"""Typer frontend. Parses arguments, calls services, formats output.
No decisions about knowledge belong in this file."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Annotated, Any, NoReturn, Optional
from uuid import UUID

import psycopg
import typer

from saddlebag.backends.postgres.migrate import (
    applied_versions,
    migrate,
    pending_versions,
)
from saddlebag.config import load
from saddlebag.domain import (
    AccessRecord,
    CollectionQuery,
    Entry,
    Kind,
    Match,
    MemoryTrigger,
    Origin,
    Query,
    TranscriptTrigger,
)
from saddlebag.embed import EmbedderUnavailable, load_embedder
from saddlebag.importers import claude_mem
from saddlebag.project import repo_root, resolve_project, toplevel
from saddlebag.services import dedupe as dedupe_service
from saddlebag.services import import_, kb, usage, write
from saddlebag.services import ingest as ingest_service
from saddlebag.services import memory as memory_service
from saddlebag.services import transcripts as transcripts_service
from saddlebag.services.embed import backfill
from saddlebag.services.search import find
from saddlebag.session import ensure_database, open_session

app = typer.Typer(help="Knowledge and memory store for AI coding agents.")
db_app = typer.Typer(help="Database setup and status.")
kb_app = typer.Typer(help="Knowledge bases.")
app.add_typer(db_app, name="db")
app.add_typer(kb_app, name="kb")

# Hidden, not removed: `capture` is muscle memory and it is in people's shell
# history. A command that has moved should say where, once, rather than
# failing with a usage error that does not name the new spelling.
capture_app = typer.Typer(help="Deprecated - see `bag record` and `bag events`.")
app.add_typer(capture_app, name="capture", hidden=True)

record_app = typer.Typer(help="Record raw events from a harness.")
app.add_typer(record_app, name="record")

events_app = typer.Typer(help="Extraction and retention for recorded events.")
app.add_typer(events_app, name="events")

handoff_app = typer.Typer(help="Session handoffs.")
app.add_typer(handoff_app, name="handoff")

config_app = typer.Typer(help="saddlebag and agent settings.")
app.add_typer(config_app, name="config")

memory_app = typer.Typer(help="Claude Code's memory directory, from saddlebag.")
app.add_typer(memory_app, name="memory")

# Deliberately NOT subcommands of `ingest`: that is a bare command taking
# positional paths, and `bag ingest docs/specs` is documented, in muscle
# memory and in every handoff. A sub-app of the same name cannot coexist
# with it, and breaking the manual command to make room for the automatic
# one would be the wrong trade.
dedupe_app = typer.Typer(help="Find entries that say the same thing twice.")
app.add_typer(dedupe_app, name="dedupe")

reingest_app = typer.Typer(help="Automatic re-ingest of designated paths.")
app.add_typer(reingest_app, name="reingest")

import_app = typer.Typer(help="Import knowledge from another tool's store.")
app.add_typer(import_app, name="import")

transcripts_app = typer.Typer(help="Raw session transcripts, stored in saddlebag.")
app.add_typer(transcripts_app, name="transcripts")


def _default_project() -> str | None:
    """The repository's name, not the current directory's.

    See saddlebag.project - a subdirectory or a worktree used to file entries
    under its own directory name, silently, where nothing would find them.
    """
    return resolve_project()


def _unreachable(dsn: str) -> NoReturn:
    """Docker not running is this tool's expected failure mode; say so."""
    typer.echo(
        f"Cannot reach Postgres at {dsn}. Start it with `docker compose up -d` "
        "(and make sure Docker itself is running).",
        err=True,
    )
    raise typer.Exit(1)


@contextmanager
def _session(*, autocommit: bool = False):
    """open_session() with the one failure every command shares handled once."""
    cfg = load()
    try:
        with open_session(cfg, autocommit=autocommit) as s:
            yield s
    except psycopg.OperationalError:
        _unreachable(cfg.dsn)


@contextmanager
def _connect(dsn: str):
    """A bare connection, for the db commands that run before the schema
    exists: open_session() seeds the principal, which needs its table."""
    try:
        with psycopg.connect(dsn) as conn:
            yield conn
    except psycopg.OperationalError:
        _unreachable(dsn)


def _entry_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        typer.echo(f"'{value}' is not a valid entry id", err=True)
        raise typer.Exit(1)


def _job_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        typer.echo(f"'{value}' is not a valid job id", err=True)
        raise typer.Exit(1)


def _entry_dict(entry: Entry, snippet: str | None = None) -> dict[str, Any]:
    data = {
        "id": str(entry.id),
        "kind": str(entry.kind),
        "title": entry.title,
        # Always present, null when unset: a key that appears only sometimes
        # makes every consumer write a membership test.
        "summary": entry.summary,
        "project": entry.project,
        "tags": list(entry.tags),
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }
    if snippet is not None:
        data["snippet"] = snippet
    else:
        data["body"] = entry.body
    return data


def _body_from_editor(initial: str = "") -> str:
    """Compose a body in $EDITOR.

    A rule worth keeping is usually a paragraph, and shell quoting is a poor
    place to write prose. An empty result aborts: storing a blank entry because
    the editor was closed without writing is worse than doing nothing.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(initial)
        path = fh.name
    try:
        subprocess.call([editor, path])
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not text:
        typer.echo("Nothing written - not storing an empty entry.", err=True)
        raise typer.Exit(1)
    return text


def _resolve_project(project: str | None, is_global: bool) -> str | None:
    """Project defaults to the directory; --global opts out deliberately.

    Before this defaulted, forgetting --project stored an entry with no
    project - a silent orphan, since a project's knowledge base queries on it.
    The write succeeded and the entry simply never appeared.
    """
    if is_global and project is not None:
        typer.echo("Pass either --project or --global, not both", err=True)
        raise typer.Exit(1)
    if is_global:
        return None
    return project or _default_project()


def _require_project(project: str | None) -> str:
    """A project name, or a refusal - never None passed on to a service.

    `resolve_project` returns None when there is no name to be had, which
    in practice means the working directory is the filesystem root. The
    services below take `project: str`, so passing that None through wrote
    a row nothing could ever query on again. Rare is not the same as
    impossible, and a refusal here costs one line.
    """
    if project is None:
        typer.echo(
            "No project name for this directory - pass --project.",
            err=True,
        )
        raise typer.Exit(1)
    return project


def _read_body(body: str | None) -> str:
    if body == "-":
        return sys.stdin.read()
    if body is None:
        raise typer.BadParameter("--body is required (use '-' to read stdin)")
    return body


@app.command()
def whoami():
    """Show the active principal and database."""
    with _session() as s:
        typer.echo(f"{s.owner.handle}  ({s.owner.id})")
        typer.echo(s.config.dsn)


@app.command()
def remember(
    title: str,
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    summary: Annotated[Optional[str], typer.Option("--summary")] = None,
    edit: Annotated[bool, typer.Option("--edit")] = False,
    kind: Annotated[Kind, typer.Option("--kind")] = Kind.NOTE,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    is_global: Annotated[bool, typer.Option("--global")] = False,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
):
    """Store a memory, doc, or rule.

    The project defaults to this directory's name, which is what the knowledge
    base injected at session start queries on. Pass --global for knowledge that
    is not tied to one project.
    """
    resolved = _resolve_project(project, is_global)
    text = _body_from_editor() if edit else _read_body(body)
    with _session() as s:
        try:
            entry = write.remember(
                s.store,
                s.owner.id,
                title=title,
                body=text,
                kind=kind,
                summary=summary,
                project=resolved,
                tags=list(tag or []),
                origin=Origin.HUMAN,
            )
        except write.RuleNeedsSummary:
            typer.echo(
                "A rule needs --summary: it is the line every session sees, "
                "since the context block renders summaries rather than bodies.",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(entry.id)


@app.command()
def rule(
    title: str,
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    summary: Annotated[Optional[str], typer.Option("--summary")] = None,
    edit: Annotated[bool, typer.Option("--edit")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    is_global: Annotated[bool, typer.Option("--global")] = False,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
):
    """Write a convention for this project.

    Shorthand for `remember --kind rule`. Rules are the entries injected into
    every session and never truncated, so they are the ones worth making
    frictionless to write.
    """
    resolved = _resolve_project(project, is_global)
    text = _body_from_editor() if edit else _read_body(body)
    with _session() as s:
        try:
            entry = write.remember(
                s.store,
                s.owner.id,
                title=title,
                body=text,
                kind=Kind.RULE,
                summary=summary,
                project=resolved,
                tags=list(tag or []),
                origin=Origin.HUMAN,
            )
        except write.RuleNeedsSummary:
            typer.echo(
                "A rule needs --summary: it is the line every session sees, "
                "since the context block renders summaries rather than bodies.",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(entry.id)


@app.command()
def ingest(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories.")],
    archive: Annotated[bool, typer.Option("--archive")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    is_global: Annotated[bool, typer.Option("--global")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
):
    """Load markdown documents in as searchable, heading-sized entries.

    Re-ingesting is safe and cheap: unchanged sections are skipped entirely,
    edited ones supersede their previous version, and sections that have
    disappeared from the file are superseded by the document's anchor entry
    so nothing is left live and stale.

    --archive stores these documents under the 'archived' origin, which is
    excluded from default search results and reachable with
    `bag search --archived`. Use it for material that is history rather
    than reference - executed implementation plans, for instance.
    """
    resolved = _resolve_project(project, is_global)
    given = list(paths)
    root = None
    top = toplevel()
    if top is not None:
        # Inside a repository, identity is the repository-relative path -
        # the same one the automatic refresh computes - whatever directory
        # this was typed from. Outside one, it stays the path as typed.
        try:
            given = ingest_service.relative_to_root(given, top, cwd=Path.cwd())
        except ingest_service.BadDesignation as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        root = top
    with _session() as s:
        report = ingest_service.ingest_manual(
            s.store,
            s.owner.id,
            given,
            project=resolved,
            root=root,
            archive=archive,
            dry_run=dry_run,
        )
    prefix = "Would write: " if dry_run else ""
    typer.echo(
        f"{prefix}{report.created} new, {report.changed} changed, "
        f"{report.unchanged} unchanged, {report.swept} swept."
    )
    for path, existing, live in report.twins:
        # A question, not a failure - a moved file and a document ingested
        # under two identities look the same from here. Exit code unchanged.
        typer.echo(
            f"twin: {path} is new, but src:{existing} has {live} live "
            f"chunks - a moved file, or ingested from a different directory?",
            err=True,
        )
    for path, reason in report.failures:
        typer.echo(f"failed: {path}: {reason}", err=True)
    if report.failures:
        # Fail-loud, unlike every hook in this repo: a person typed this.
        raise typer.Exit(1)
    if not dry_run and (report.created or report.changed):
        # Not after a dry run: nothing was written, so there is nothing to
        # embed, and saying otherwise sends the user to a no-op.
        typer.echo("Run `bag embed` to give the new entries vectors.")


@app.command()
def search(
    query: str,
    kind: Annotated[Optional[list[Kind]], typer.Option("--kind")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    handoff: Annotated[bool, typer.Option("--handoff")] = False,
    archived: Annotated[bool, typer.Option("--archived")] = False,
):
    """Search stored knowledge.

    Three tiers, tried in order and never blended: exact full-text, then
    entries with related meaning (marked ~), then typo-tolerant matching
    (marked ?). --json reports which as "match".
    --handoff also searches session handoffs, which are excluded by default.
    --archived also searches archived document chunks, which are excluded
    by default.
    """
    with _session() as s:
        # No embedder is passed. The service builds one only if the semantic
        # tier is reached, and decides for itself that an unavailable one is
        # a None rather than an error - both of which are policy, and neither
        # of which a frontend should be restating.
        hits = find(
            s.store,
            s.owner.id,
            Query(
                text=query,
                kinds=list(kind or []),
                project=project,
                tags=list(tag or []),
                limit=limit,
            ),
            fuzzy_threshold=s.config.fuzzy_threshold,
            include_handoffs=handoff,
            include_archived=archived,
            semantic_threshold=s.config.semantic_threshold,
            embed_model=s.config.embed_model,
            source="cli",
            session_id=usage.session_id_from_env(os.environ),
            log_project=_default_project(),
        )
    if as_json:
        payload = []
        for h in hits:
            data = _entry_dict(h.entry, h.snippet)
            data["match"] = str(h.match)
            payload.append(data)
        typer.echo(json.dumps(payload, indent=2))
        return
    if not hits:
        typer.echo("No matches.")
        return
    # Say it once, up front: the caller should know how these were found
    # before reading any of them. Tiers never blend, so hits[0] speaks for
    # the whole result set.
    if hits[0].match is Match.SEMANTIC:
        typer.echo(
            f"No exact matches for {query!r}. Showing entries with related meaning:\n"
        )
    elif hits[0].match is Match.FUZZY:
        typer.echo(
            f"No exact or related matches for {query!r}. Showing similar spellings:\n"
        )
    for h in hits:
        marker = {Match.EXACT: "", Match.SEMANTIC: "~ ", Match.FUZZY: "? "}[h.match]
        typer.echo(f"{marker}{h.entry.id}  [{h.entry.kind}] {h.entry.title}")
        typer.echo(f"    {h.snippet}")


def _window(value: str) -> timedelta:
    """`7d`, `24h`, `30d`. Anything else is refused - a typo silently read as
    some default window would report numbers for a period nobody asked for."""
    match = re.fullmatch(r"(\d+)([dh])", value.strip())
    if not match or int(match.group(1)) == 0:
        raise typer.BadParameter("use a number followed by d or h, e.g. 7d or 24h")
    n = int(match.group(1))
    return timedelta(days=n) if match.group(2) == "d" else timedelta(hours=n)


@app.command()
def stats(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    window: Annotated[str, typer.Option("--window")] = "7d",
    recent: Annotated[int, typer.Option("--recent")] = 10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """How saddlebag is being used, and whether its pipelines keep up.

    Reads and session starts are counted from when usage logging began; a
    younger log says "since <date>" rather than claiming a quiet week. A
    section that could not be read prints "unavailable" and still exits 0.
    """
    from saddlebag.services import stats as stats_svc

    # Parsed before _session() opens anything: a bad window fails without a
    # database, which is what test_a_bad_window_is_refused relies on.
    period = _window(window)
    name = _require_project(project or _default_project())
    root = repo_root()
    with _session() as s:
        got = stats_svc.collect(
            s.store,
            s.owner.id,
            name,
            s.config,
            now=datetime.now(timezone.utc),
            window=period,
            recent=recent,
            current_root=root,
        )
    if as_json:
        typer.echo(json.dumps(stats_svc.to_dict(got), indent=2))
        return
    for line in stats_svc.render(got):
        typer.echo(line)


@app.command()
def embed(
    limit: Annotated[
        Optional[int], typer.Option("--limit", help="Stop after this many entries.")
    ] = None,
    batch: Annotated[int, typer.Option("--batch")] = 32,
):
    """Embed entries that have no vector for the configured model.

    Idempotent and safe to re-run - it does whatever is missing. Run it after
    writing entries, or from cron. Changing BAG_EMBED_MODEL makes every
    entry need embedding again; the old vectors stay until deleted.
    """
    # Built before the session is opened, deliberately: a cron command
    # should fail on a missing embedder without first paying for a Postgres
    # connection. That means this reads config via load() rather than the
    # session's s.config - the two are the same file, just read a moment
    # apart, which is fine for a value (embed_model) nothing else in this
    # command touches concurrently.
    try:
        embedder = load_embedder(load().embed_model)
    except EmbedderUnavailable as exc:
        # Loud, not fail-soft: embedding is this command's entire job, and a
        # silent success would leave search quietly missing a tier forever.
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    with _session() as s:
        if not s.store.try_advisory_lock("embed", s.owner.id):
            # A previous run is still going. Silence and exit 0 - a cron
            # command that mails the user about a working system is a cron
            # command they will turn off. The same rule as `events process`:
            # every command in this pipeline is expected to overlap itself.
            raise typer.Exit(0)
        result = backfill(
            s.store, s.owner.id, embedder, batch_size=batch, max_entries=limit
        )

    typer.echo(f"Embedded {result.embedded} entries with {result.model}.")
    if result.failed:
        typer.echo(f"{result.failed} failed - re-run to retry.", err=True)
        raise typer.Exit(1)


@app.command()
def get(
    entry_id: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Print one entry in full."""
    parsed = _entry_id(entry_id)
    with _session() as s:
        started = time.perf_counter()
        entry = s.store.get_entry(parsed, s.owner.id)
        usage.log_access(
            s.store,
            AccessRecord(
                owner_id=s.owner.id,
                source="cli",
                op="get",
                hits=0 if entry is None else 1,
                entry_ids=() if entry is None else (entry.id,),
                elapsed_ms=usage.elapsed_ms(started),
                project=_default_project(),
                session_id=usage.session_id_from_env(os.environ),
            ),
        )
    if entry is None:
        typer.echo(f"No entry {entry_id}", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(_entry_dict(entry), indent=2))
    else:
        typer.echo(f"# {entry.title}\n")
        # Only when there is one: a blockquote holding nothing reads as a
        # rendering bug rather than as an entry with no summary.
        if entry.summary:
            typer.echo(f"> {entry.summary}\n")
        typer.echo(entry.body)


@app.command()
def update(
    entry_id: str,
    title: Annotated[Optional[str], typer.Option("--title")] = None,
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    summary: Annotated[Optional[str], typer.Option("--summary")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    clear_project: Annotated[bool, typer.Option("--clear-project")] = False,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    clear_tags: Annotated[bool, typer.Option("--clear-tags")] = False,
):
    """Edit an entry in place (for typos - use supersede for corrections).

    Only the fields you pass change. --clear-project and --clear-tags empty a
    field, which passing nothing cannot express.

    --summary is how a rule written before summaries were required gets
    the line the context block renders, without superseding it.
    """
    parsed = _entry_id(entry_id)
    text = _read_body(body) if body is not None else None
    if clear_project and project is not None:
        typer.echo("Pass either --project or --clear-project, not both", err=True)
        raise typer.Exit(1)
    if clear_tags and tag:
        typer.echo("Pass either --tag or --clear-tags, not both", err=True)
        raise typer.Exit(1)

    new_project = write.CLEAR if clear_project else project
    new_tags = [] if clear_tags else (list(tag) if tag else None)

    with _session() as s:
        try:
            entry = write.update(
                s.store,
                s.owner.id,
                parsed,
                title=title,
                body=text,
                summary=summary,
                project=new_project,
                tags=new_tags,
            )
        except write.EntryNotFound:
            typer.echo(f"No entry {entry_id}", err=True)
            raise typer.Exit(1)
        except write.RuleNeedsSummary:
            typer.echo(
                "A rule needs --summary: it is the line every session sees, "
                "since the context block renders summaries rather than bodies.",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(entry.id)


@app.command()
def supersede(
    entry_id: str,
    title: Annotated[str, typer.Option("--title")],
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    summary: Annotated[Optional[str], typer.Option("--summary")] = None,
):
    """Replace knowledge that stopped being true. The old entry is kept.

    Omitting --summary carries the old entry's summary onto the replacement,
    the same way tags and project are carried. That is deliberate: making
    "omitted" mean "clear it" would silently empty the frontmatter
    description of any memory file whose entry was ever corrected.
    """
    parsed = _entry_id(entry_id)
    text = _read_body(body)
    with _session() as s:
        try:
            entry = write.supersede(
                s.store, s.owner.id, parsed, title=title, body=text, summary=summary
            )
        except write.EntryNotFound:
            typer.echo(f"No entry {entry_id}", err=True)
            raise typer.Exit(1)
        except write.RuleNeedsSummary:
            typer.echo(
                "This rule has no summary to carry onto the replacement - "
                "pass --summary: it is the line every session sees, since "
                "the context block renders summaries rather than bodies.",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(entry.id)


@kb_app.command("new")
def kb_new(
    slug: str,
    title: Annotated[str, typer.Option("--title")],
    description: Annotated[Optional[str], typer.Option("--description")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
):
    """Create a knowledge base."""
    with _session() as s:
        c = kb.create(
            s.store,
            s.owner.id,
            slug=slug,
            title=title,
            description=description,
            project=project,
            query=CollectionQuery(tags=list(tag or []), project=project),
        )
        typer.echo(c.slug)
        for advisory in kb.advisories(c):
            typer.echo(f"warning: {advisory}", err=True)


@kb_app.command("query")
def kb_query(
    slug: str,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    kind: Annotated[Optional[list[Kind]], typer.Option("--kind")] = None,
    clear: Annotated[bool, typer.Option("--clear")] = False,
):
    """Replace which entries a knowledge base selects automatically.

    A query is otherwise fixed at creation. Pinned entries are unaffected.
    --clear empties the query so the knowledge base holds only its pins.
    """
    if clear and (project or tag or kind):
        typer.echo("Pass either --clear or the filters, not both", err=True)
        raise typer.Exit(1)

    query = (
        CollectionQuery()
        if clear
        else CollectionQuery(
            tags=list(tag or []), kinds=list(kind or []), project=project
        )
    )
    with _session() as s:
        try:
            collection = kb.set_query(s.store, s.owner.id, slug, query)
        except kb.CollectionNotFound:
            typer.echo(f"No knowledge base '{slug}'", err=True)
            raise typer.Exit(1)
        for note in kb.advisories(collection):
            typer.echo(f"note: {note}", err=True)
        typer.echo(collection.slug)


@kb_app.command("list")
def kb_list():
    """List knowledge bases."""
    with _session() as s:
        for c in s.store.list_collections(s.owner.id):
            typer.echo(f"{c.slug}\t{c.title}")


@kb_app.command("pin")
def kb_pin(
    slug: str, entry_id: str, position: Annotated[int, typer.Option("--position")] = 0
):
    """Pin an entry into a knowledge base."""
    parsed = _entry_id(entry_id)
    with _session() as s:
        try:
            kb.pin(s.store, s.owner.id, slug, parsed, position)
        except kb.CollectionNotFound:
            typer.echo(f"No knowledge base '{slug}'", err=True)
            raise typer.Exit(1)
        except kb.EntryNotFound:
            typer.echo(f"No entry {entry_id}", err=True)
            raise typer.Exit(1)
        typer.echo("pinned")


@kb_app.command("show")
def kb_show(
    slug: str,
    max_chars: Annotated[Optional[int], typer.Option("--max-chars")] = None,
    full: Annotated[bool, typer.Option("--full")] = False,
):
    """Render a knowledge base as a context block."""
    with _session() as s:
        try:
            collection = kb.get(s.store, s.owner.id, slug)
            entries = kb.resolve(s.store, s.owner.id, slug)
        except kb.CollectionNotFound:
            typer.echo(f"No knowledge base '{slug}'", err=True)
            raise typer.Exit(1)
        budget = 10**9 if full else (max_chars or s.config.max_chars)
        try:
            typer.echo(kb.render(collection, entries, budget))
        except kb.RulesExceedBudget as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)


@kb_app.command("budget")
def kb_budget(
    slug: Annotated[Optional[str], typer.Argument()] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """What a knowledge base's rules cost against the context budget.

    The numeric counterpart to the advisory in `bag record status`, which
    is prose for a person and says nothing at all about a healthy
    knowledge base - correctly, since there is nothing to tell. Anything
    that *displays* the number continuously needs it in every state,
    including the healthy one it occupies almost all the time, so it reads
    this instead of parsing sentences that are usually absent.

    The slug defaults to the project the working directory resolves to -
    the same resolution `ClaudeCodeAdapter.identity` hands the SessionStart
    hook - so the number reported here governs the block this session
    actually received, rather than some other knowledge base's.
    """
    with _session() as s:
        name = slug or resolve_project()
        if not name:
            typer.echo("No project here, and no slug given", err=True)
            raise typer.Exit(1)
        try:
            got = kb.budget(s.store, s.owner.id, name, s.config.max_chars)
        except kb.CollectionNotFound:
            typer.echo(f"No knowledge base '{name}'", err=True)
            raise typer.Exit(1)

    if as_json:
        typer.echo(json.dumps(kb.budget_to_dict(got), indent=2))
        return
    typer.echo(f"{got.slug} {got.used}/{got.budget} ({got.fraction:.0%}) {got.state}")


@db_app.command("up")
def db_up():
    """Create the database, apply migrations, and seed the principal."""
    from saddlebag.backends.postgres.store import PostgresStore

    cfg = load()
    try:
        created = ensure_database(cfg.dsn)
    except psycopg.OperationalError:
        _unreachable(cfg.dsn)
    if created:
        typer.echo(f"Created database at {cfg.dsn}")

    with _connect(cfg.dsn) as conn:
        applied = migrate(conn)
        conn.commit()
        owner = PostgresStore(conn).ensure_principal(cfg.user_handle)
        conn.commit()

    typer.echo(f"Applied: {', '.join(applied) if applied else 'nothing pending'}")
    typer.echo(f"Principal: {owner.handle} ({owner.id})")


@db_app.command("migrate")
def db_migrate():
    """Apply pending migrations."""
    cfg = load()
    with _connect(cfg.dsn) as conn:
        applied = migrate(conn)
        conn.commit()
    typer.echo(", ".join(applied) if applied else "nothing pending")


@db_app.command("status")
def db_status():
    """Show connectivity and migration state."""
    cfg = load()
    with _connect(cfg.dsn) as conn:
        typer.echo(f"Connected: {cfg.dsn}")
        typer.echo(f"Applied:   {', '.join(applied_versions(conn)) or 'none'}")
        typer.echo(f"Pending:   {', '.join(pending_versions(conn)) or 'none'}")


@app.command()
def serve(
    http: Annotated[bool, typer.Option("--http")] = False,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 9100,
    project: Annotated[str | None, typer.Option("--project")] = None,
):
    """Run the MCP server.

    Stdio by default, which is what an agent on this machine launches.
    `--http` serves streamable-HTTP instead, for an agent that cannot start a
    local process - a container, or a remote host. Over HTTP the working
    directory is the server's, not the agent's, so pass `--project` or writes
    file under the wrong project and never surface again.
    """
    from saddlebag import mcp_server

    if http:
        mcp_server.serve_http(host, port, project)
    else:
        mcp_server.main()


@app.command()
def install(
    agent: Annotated[str, typer.Argument()] = "claude-code",
    scope: Annotated[str, typer.Option("--scope")] = "user",
):
    """Install saddlebag into an agent (MCP server, hook, and skill)."""
    from saddlebag.agents.base import UnsupportedScope
    from saddlebag.agents.registry import UnknownAgent
    from saddlebag.agents.registry import get as get_adapter

    try:
        adapter = get_adapter(agent)()
    except UnknownAgent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    try:
        report = adapter.install(scope=scope)
    except UnsupportedScope as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    for action in report.actions:
        typer.echo(f"  {action}")
    for warning in report.warnings:
        typer.echo(f"  warning: {warning}", err=True)
    typer.echo(f"\nInstalled saddlebag for {report.agent}.")
    for note in report.notes:
        typer.echo(note)


@app.command()
def verify(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Prove an agent's install actually records events, without reinstalling.

    The same live round-trip `install` runs as its own last step - record,
    read back, delete - so a user who wants to re-check after fixing the
    database, or just before trusting the pipeline, does not have to run the
    whole install again to find out.
    """
    from saddlebag.agents.registry import UnknownAgent
    from saddlebag.agents.registry import get as get_adapter

    try:
        adapter = get_adapter(agent)()
    except UnknownAgent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    verify_fn = getattr(adapter, "verify", None)
    if verify_fn is None:
        typer.echo(f"{agent} has no install verification.", err=True)
        raise typer.Exit(1)

    report = verify_fn(env=dict(os.environ), home=Path.home())
    for action in report.actions:
        typer.echo(f"  {action}")
    for warning in report.warnings:
        typer.echo(f"  warning: {warning}", err=True)
    if report.warnings:
        # verify() itself never raises - a failure is a warning on the
        # report, because install() calling it must never die mid-install.
        # But this command is typed by a human asking "does this actually
        # work?", and a report full of warnings that still exits 0 answers
        # that question wrong.
        raise typer.Exit(1)
    typer.echo(f"\nVerified {report.agent}.")


@app.command("doctor")
def doctor(
    agent: Annotated[Optional[str], typer.Argument()] = None,
    # No default scope. `--scope` unset means "look everywhere this adapter
    # can be installed" - the service sweeps - because defaulting to user
    # scope made this command report cursor "not installed" on a machine
    # where cursor was installed at project scope and recording events. An
    # unexamined scope must never produce a confident answer about it.
    scope: Annotated[Optional[str], typer.Option("--scope")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Check that each harness's config registers the hooks saddlebag installs.

    The gap this closes: hooks are fail-soft, so a harness that was never
    told to call saddlebag looks exactly like one with nothing to say. `saddlebag
    verify` proves saddlebag records when called; this asks whether the harness
    will ever call it. Neither answers the other's question.

    Opens no database connection - a diagnostic that needs the system
    healthy is no use when it is not.
    """
    from saddlebag.agents import registry
    from saddlebag.services import doctor as doctor_service

    adapters = registry.discover()
    if agent is not None:
        if agent not in adapters:
            known = ", ".join(sorted(adapters)) or "none"
            typer.echo(f"unknown agent '{agent}'. Available: {known}")
            raise typer.Exit(1)
        adapters = {agent: adapters[agent]}

    reports = doctor_service.check(
        adapters, scope=scope, home=Path.home(), env=dict(os.environ)
    )
    if as_json:
        typer.echo(json.dumps(doctor_service.to_dict(reports), indent=2))
    else:
        typer.echo(doctor_service.render(reports))
    raise typer.Exit(1 if doctor_service.failed(reports) else 0)


def _message(exc: Exception) -> str:
    """An exception's message, without KeyError's repr quotes.

    UnknownSetting subclasses KeyError, and KeyError stringifies as the repr
    of its argument, so str() would render a sentence wrapped in quotes.
    Reaching for args[0] is exact; stripping quote characters off both ends
    of the rendered string would also mangle a message that legitimately
    ends in a quoted key name.
    """
    return exc.args[0] if isinstance(exc, KeyError) else str(exc)


def _config_targets(agent: str):
    """Resolve --agent to an adapter and ask the service where its files are.

    Everything this function decides is a parsing decision: the name of the
    agent, and what to print when there is no such agent. Which file that
    agent's environment block lives in, and what a missing capability means,
    are the service's calls - hard-wiring one adapter's resolver here is how
    `--agent codex` would end up writing into ~/.claude/settings.json.
    """
    import os
    from pathlib import Path

    from saddlebag.agents.registry import UnknownAgent
    from saddlebag.agents.registry import get as get_adapter
    from saddlebag.services import settings as svc

    env = os.environ
    try:
        adapter = get_adapter(agent)()
    except UnknownAgent as exc:
        # registry.get already lists what is registered, so echo it as-is.
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    targets = svc.resolve_targets(adapter, Path.home(), env)
    return env, targets.saddlebag_path, targets.agent_path, targets.table


@config_app.command("set")
def config_set(
    key: str,
    value: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Set one setting, in saddlebag's config or the agent's environment."""
    from saddlebag.services import settings as svc

    env, saddlebag_path, agent_path, table = _config_targets(agent)
    try:
        target, var = svc.route(key, table)
        resolved = svc.coerce(var, value, target)
    except (svc.UnknownSetting, svc.NotSettable, svc.InvalidValue) as exc:
        typer.echo(_message(exc), err=True)
        raise typer.Exit(1)

    if target is svc.Target.SADDLEBAG:
        backup = svc.write_saddlebag(saddlebag_path, var.name, resolved)
        where = saddlebag_path
    else:
        # resolve_targets returns agent_path=None only alongside an empty
        # table, and `route` raises on an empty table before it can answer
        # Target.AGENT - so getting here proves there is a path.
        assert agent_path is not None
        backup = svc.write_agent(agent_path, var.name, resolved)
        where = agent_path

    typer.echo(f"{var.name} = {resolved!r} in {where}")
    # Rewriting either file loses whatever was not a setting - comments and
    # formatting in config.toml, key order in settings.json. The copy is
    # taken automatically, so the only thing left to get wrong is not saying
    # where it went; a timestamped .bak name is not one anybody would guess.
    if backup is not None:
        typer.echo(f"Backed up to {backup}")
    warning = svc.shadow_warning(target, var.name, env)
    if warning:
        typer.echo(warning, err=True)


@config_app.command("unset")
def config_unset(
    key: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Remove one setting, restoring its default."""
    from saddlebag.services import settings as svc

    _, saddlebag_path, agent_path, table = _config_targets(agent)
    try:
        target, var = svc.route(key, table)
    except (svc.UnknownSetting, svc.NotSettable) as exc:
        typer.echo(_message(exc), err=True)
        raise typer.Exit(1)

    if target is svc.Target.SADDLEBAG:
        backup = svc.write_saddlebag(saddlebag_path, var.name, None)
    else:
        # resolve_targets returns agent_path=None only alongside an empty
        # table, and `route` raises on an empty table before it can answer
        # Target.AGENT - so getting here proves there is a path.
        assert agent_path is not None
        backup = svc.write_agent(agent_path, var.name, None)
    typer.echo(f"Unset {var.name}.")
    if backup is not None:
        typer.echo(f"Backed up to {backup}")


@config_app.command("get")
def config_get(
    key: str,
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Print one setting's effective value and where it came from."""
    from saddlebag.services import settings as svc

    env, saddlebag_path, agent_path, table = _config_targets(agent)
    try:
        _, var = svc.route(key, table)
    except (svc.UnknownSetting, svc.NotSettable) as exc:
        typer.echo(_message(exc), err=True)
        raise typer.Exit(1)

    rows = svc.list_settings(saddlebag_path, agent_path, table, env)
    row = next(r for r in rows if r.key == var.name)
    typer.echo(f"{row.value if row.value is not None else '(unset)'}\t{row.source}")


@config_app.command("list")
def config_list(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Show every settable key, its value, and where that value came from."""
    from saddlebag.services import settings as svc

    env, saddlebag_path, agent_path, table = _config_targets(agent)
    if not table:
        typer.echo(f"{agent} has no settable environment variables.")
    for row in svc.list_settings(saddlebag_path, agent_path, table, env):
        value = row.value if row.value is not None else "(unset)"
        typer.echo(f"{row.key}\t{value}\t{row.source}\t{row.var.help}")
        if row.var.note:
            typer.echo(f"\t{row.var.note}")


hook_app = typer.Typer(help="Agent hook entry points (not for interactive use).")
app.add_typer(hook_app, name="hook")


@hook_app.command("session-start")
def hook_session_start():
    """Print the project's knowledge base. Always exits 0."""
    from saddlebag.agents.claude_code.hook import main as hook_main

    raise typer.Exit(hook_main())


@hook_app.command("session-end")
def hook_session_end():
    """Record this SessionEnd payload as an event. Always exits 0.

    The command name is kept from before the idle trigger existed - an
    already-installed settings.json names it, and a hook command that no
    longer exists is an error on every session close. It now does exactly
    what `bag hook record-event` does: extraction runs on an idle timer,
    so this just shortens the wait rather than being required for it.
    """
    from saddlebag.agents.claude_code.hook import main_record_event

    raise typer.Exit(main_record_event())


@hook_app.command("record-event")
def hook_record_event():
    """Record this PostToolUse or SessionEnd payload as an event. Always exits 0."""
    from saddlebag.agents.claude_code.hook import main_record_event

    raise typer.Exit(main_record_event())


@hook_app.command("session-size")
def hook_session_size():
    """Warn when a session has grown long enough to hand off. Always exits 0."""
    from saddlebag.agents.claude_code.hook import main_session_size

    raise typer.Exit(main_session_size())


@hook_app.command("context")
def hook_context(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
):
    """Print the knowledge base block for the session on stdin.

    The harness-neutral half of what SessionStart does for Claude Code. A
    harness with no session-start hook - opencode, Cursor - calls this
    instead, passing whatever payload it has; the adapter's identity()
    turns that into a project.

    Fail-soft like every hook: exits 0 unconditionally, and prints the
    block and nothing else to stdout. Reasons go to stderr under
    BAG_HOOK_DEBUG.
    """
    from saddlebag import hookio
    from saddlebag.agents import registry
    from saddlebag.hookio import debug
    from saddlebag.services import context

    env = dict(os.environ)

    try:
        stdin_text = sys.stdin.read()
        try:
            payload: dict[str, Any] = (
                json.loads(stdin_text) if stdin_text.strip() else {}
            )
        except json.JSONDecodeError, AttributeError:
            debug(env, "stdin was not valid JSON")
            raise typer.Exit(0)

        try:
            adapter = registry.get(agent)()
        except registry.UnknownAgent as exc:
            debug(env, str(exc))
            raise typer.Exit(0)

        # identity() is a required Protocol method, unlike event() - but a
        # third-party adapter whose implementation raises must still only
        # cost the block, not the hook, so it is caught exactly like the
        # optional capabilities in services/settings.py are.
        try:
            identity = adapter.identity(env, payload)
        except Exception as exc:
            debug(
                env,
                f"{agent} adapter's identity() raised {type(exc).__name__}: {exc}",
            )
            raise typer.Exit(0)

        if not identity.project:
            debug(env, "the hook payload carried no project")
            raise typer.Exit(0)

        cfg = load()
        with open_session(cfg) as s:
            rendered = context.block(
                s.store,
                s.owner.id,
                identity.project,
                cfg.max_chars,
                note=lambda reason: debug(env, reason),
                owner_handle=s.owner.handle,
                log=context.InjectionLog(agent, identity.session_id),
            )

        # Delivery is the adapter's business, not the frontend's. An
        # adapter with no inject() is one whose harness reads stdout, which
        # is the default and not a degradation - see the Protocol comment
        # in agents/base.py. A capability that raises degrades to the
        # stdout path and a debug line, never to a broken hook.
        inject = getattr(adapter, "inject", None)
        if inject is not None:
            try:
                written = inject(rendered, payload, note=partial(debug, env))
            except Exception as exc:
                debug(
                    env,
                    f"{agent} adapter's inject() raised {type(exc).__name__}: {exc}",
                )
            else:
                if written:
                    debug(env, f"wrote the context block to {written}")
                else:
                    debug(env, "the adapter wrote no context block")
                raise typer.Exit(0)

        typer.echo(rendered, nl=False)
    except typer.Exit:
        raise
    except Exception as exc:
        debug(env, f"{type(exc).__name__}: {exc}")
    finally:
        # Extraction is triggered here for every harness that has no
        # session-start hook of its own - opencode and Cursor both call this
        # command once per session, which makes it the one trigger all three
        # harnesses share. Claude Code does the same from SessionStart.
        #
        # In `finally`, so it runs on every path above, including the early
        # returns for unusable stdin, an unknown agent, and an unresolvable
        # project. The backlog is global: `bag events process` works off
        # every extractable session for the owner, so whether THIS payload
        # produced a block says nothing about whether extraction has work.
        #
        # A harness with no `claude` on PATH will fail these jobs rather than
        # silently skip them - the attempt cap stops the retries and
        # `bag record status` shows the reason, which beats a probe here
        # that guesses wrong about where the extractor lives.
        hookio.spawn_process(env)
        # And the re-ingest, for the same reason and from the same two
        # places: a designated project's docs go stale otherwise, and
        # nothing but a session start reliably happens.
        hookio.spawn_ingest(env)
        # And the memory sync, third and last, from the same two places.
        # A designated project's memory directory has a second writer that
        # cannot be told to stop, so it drifts exactly the way designated
        # ingest paths did. An undesignated project - the common case -
        # exits 0 having done nothing.
        hookio.spawn_memory(env)
        # And the transcript refresh, fourth and last, from the same two
        # places. It is bounded before it reads, so a session start never
        # pays for the backfill a project's first `bag transcripts import`
        # would - a project with no claimed transcript directory does
        # nothing, same as the other three when their designation is absent.
        hookio.spawn_transcripts(env)

    raise typer.Exit(0)


@capture_app.command("enable", hidden=True)
def capture_enable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Deprecated: use `bag record enable`."""
    # stderr: a script or crontab still calling the old name should not gain
    # a permanent line of noise on stdout, once per run, forever.
    typer.echo("`capture enable` is renamed to `bag record enable`.", err=True)
    from saddlebag.services import record

    name = _require_project(project or _default_project())
    with _session() as s:
        record.enable(s.store, s.owner.id, name)
        model = s.config.extract_model
    typer.echo(f"capture enabled for '{name}'")
    # State the cost at the moment the tradeoff is actionable. Measured on a
    # real session: roughly 20 cents per extraction on sonnet, a third of
    # that on haiku - which returned noticeably worse judgement about what was
    # worth keeping. This deprecated command keeps naming the deprecated
    # variable - `bag record enable` is where the current name is taught.
    typer.echo(
        f"extraction runs `claude -p --model {model}` once per session, "
        f"roughly $0.10-0.25 each.\n"
        f"change it with BAG_CAPTURE_MODEL (e.g. haiku for less, "
        f"opus for more)."
    )


@capture_app.command("disable", hidden=True)
def capture_disable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Deprecated: use `bag record disable`."""
    typer.echo("`capture disable` is renamed to `bag record disable`.", err=True)
    from saddlebag.services import record

    name = _require_project(project or _default_project())
    with _session() as s:
        record.disable(s.store, s.owner.id, name)
    typer.echo(f"capture disabled for '{name}'")


@capture_app.command("status", hidden=True)
def capture_status(
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Deprecated: use `bag record status`."""
    # stderr: --json below must stay parseable on its own, and this is the
    # same reasoning as the other three aliases besides.
    typer.echo("`capture status` is renamed to `bag record status`.", err=True)
    with _session() as s:
        counts = s.store.extract_job_counts(s.owner.id)
        failures = s.store.recent_failed_extract_jobs(s.owner.id)
        model = s.config.extract_model
        projects = s.store.enabled_record_projects(s.owner.id)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "model": model,
                    "enabled_projects": projects,
                    "counts": counts,
                    "failures": [
                        {"id": str(f.id), "project": f.project, "error": f.error}
                        for f in failures
                    ],
                },
                indent=2,
            )
        )
        return

    typer.echo(f"Recording enabled for: {', '.join(projects) or 'no projects'}")
    typer.echo(f"Extraction model: {model}")
    if counts:
        typer.echo("Jobs: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        typer.echo("Jobs: none yet")
    for f in failures:
        typer.echo(f"  failed {f.id} [{f.project}]: {f.error}")


@capture_app.command("drain", hidden=True)
def capture_drain(
    limit: Annotated[int, typer.Option("--limit")] = 10,
    job: Annotated[Optional[str], typer.Option("--job")] = None,
):
    """Deprecated: use `bag events process`."""
    # stderr - see capture_enable's comment: `capture drain` is exactly the
    # kind of command an old crontab still calls, unattended, forever.
    typer.echo("`capture drain` is renamed to `bag events process`.", err=True)
    events_process(limit=limit, job=job)


@events_app.command("process")
def events_process(
    limit: Annotated[int, typer.Option("--limit")] = 10,
    job: Annotated[Optional[str], typer.Option("--job")] = None,
):
    """Extract entries from sessions that have gone quiet.

    A session is extracted once it has been idle for BAG_IDLE_MINUTES.
    Safe to run from cron: overlapping runs are held off by an advisory
    lock, and a run that finds nothing says so and exits 0.

    `--job ID` retries exactly that job, however many times it has already
    failed. It is the only way back for a job that hit the attempt cap.
    """
    from saddlebag.extract.claude_cli import ClaudeCliExtractor
    from saddlebag.services import extraction

    job_id = _job_id(job) if job is not None else None

    # Autocommit, unlike every other command: the run records its own
    # progress as it goes, and it spends minutes at a time inside `claude`.
    # One transaction for the batch would both discard already-succeeded work
    # on a database error and hold row locks across those minutes.
    with _session(autocommit=True) as s:
        if not s.store.try_advisory_lock("events-process", s.owner.id):
            # A previous run is still going. Silence and exit 0 - a cron
            # command that mails the user about a working system is a cron
            # command they will turn off.
            raise typer.Exit(0)
        extractor = ClaudeCliExtractor(model=s.config.extract_model)
        if job_id is not None:
            try:
                report = extraction.process_job(s.store, s.owner.id, job_id, extractor)
            except extraction.ExtractJobNotFound as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1)
        else:
            report = extraction.process(
                s.store,
                s.owner.id,
                extractor,
                idle_seconds=s.config.idle_minutes * 60,
                limit=limit,
            )
    typer.echo(
        f"claimed {report.claimed}, succeeded {report.succeeded}, "
        f"failed {report.failed}, entries written {report.entries_written}"
    )


@events_app.command("prune")
def events_prune(
    before: Annotated[
        Optional[str], typer.Option("--before", help="e.g. 30d, 12h, 90m")
    ] = None,
    project: Annotated[
        Optional[str],
        typer.Option("--project", help="only this project (default: all)"),
    ] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Delete raw events older than a window, if they have been extracted.

    There is no default window: `--before` must always be something the
    user typed, so a configured retention number can never quietly delete
    history. An event that has not been extracted yet is never deleted
    unless `--force` says so - for a session whose extraction is never
    going to finish.

    `--project` narrows the run to one project. Recording is opt-in per
    project, so the delete reaches the same granularity - otherwise the
    only way to drop one project's events is to delete every project's.
    It does NOT default to the current directory the way the writing
    commands do: every other `--project` here narrows a read or files a
    new row, and this one deletes, so the scope has to be typed.
    """
    from saddlebag.services import events

    if before is None:
        typer.echo(
            "--before is required (e.g. --before 30d) - there is no default "
            "retention window.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        window = events.parse_window(before)
    except events.BadWindow as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    with _session() as s:
        cutoff = datetime.now(timezone.utc) - window
        try:
            report = events.prune(
                s.store, s.owner.id, before=cutoff, force=force, project=project
            )
        except events.PruneRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "deleted": report.deleted,
                    "kept_unextracted": report.kept_unextracted,
                    "dangling": report.dangling,
                    "project": project,
                },
                indent=2,
            )
        )
        return
    # The dangling count prints even when it is zero - its absence would be
    # indistinguishable from a prune that never reported it at all.
    # The scope is named on every run, including the unscoped one. A
    # destructive command that says only what it deleted leaves the user to
    # guess whether it hit one project or all of them.
    scope = f"project '{project}'" if project else "all projects"
    typer.echo(
        f"deleted {report.deleted} events from {scope}, kept "
        f"{report.kept_unextracted} unextracted, left {report.dangling} "
        f"provenance rows dangling"
    )


@events_app.command("show")
def events_show(
    entry_id: Annotated[str, typer.Argument()],
):
    """Show the raw events an entry was extracted from - the forensic lookup.

    No provenance at all is an ordinary answer for a hand-written entry, not
    an error. A pruned event is reported as "event pruned", never "not
    found" - those mean different things and look identical to a user who
    is only told one of them.
    """
    from saddlebag.services import events

    eid = _entry_id(entry_id)
    with _session() as s:
        rows = events.forensics(s.store, s.owner.id, eid)
    typer.echo(events.render_provenance(rows))


@record_app.command("status")
def record_status(
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Show what's been recorded, per harness, and what's stuck.

    This is the fail-loud half of a fail-soft pipeline: hooks and the idle
    trigger never raise, so this is the one place a harness that has quietly
    recorded nothing - wrong hook names, a broken adapter, opt-in never
    turned on - becomes visible on demand rather than never.
    """
    from saddlebag.agents import registry
    from saddlebag.services import doctor as doctor_service
    from saddlebag.services import events

    # Wrapped: an unreadable config file must not take down a status
    # command that is otherwise about the database. doctor.check already
    # degrades per adapter; this covers the registry call itself.
    #
    # No scope is passed, so this sweeps every scope - the same question
    # `bag doctor` with no arguments asks. It has to: the harness this
    # advisory exists for is the one that has recorded nothing ever, and on
    # the machine that motivated the feature that harness is Cursor at
    # project scope, which a user-scope-only check cannot see at all.
    try:
        advisories = doctor_service.advisories(
            doctor_service.check(registry.discover(), env=dict(os.environ))
        )
    except Exception:
        advisories: list[str] = []

    with _session() as s:
        # Wrapped like the doctor call above, and for the same reason: an
        # ingest status that cannot be computed must not take down the
        # events status it decorates.
        try:
            ingest_advisories = ingest_service.advisories(
                s.store,
                s.owner.id,
                current_project=resolve_project(),
                root=repo_root(),
            )
        except Exception:
            ingest_advisories: list[str] = []

        # Wrapped like the doctor and ingest calls above, and for the same
        # reason: a memory advisory that cannot be computed must not take
        # down the events status it decorates.
        try:
            memory_advisories = memory_service.advisories(s.store, s.owner.id)
        except Exception:
            memory_advisories: list[str] = []

        # Wrapped like the three calls above, and for the same reason. The
        # judgement - which knowledge bases are over or near the budget,
        # and what "near" means - lives in `kb.budget_advisories`; this
        # only hands it the budget the injecting hooks would have used.
        try:
            kb_advisories = kb.budget_advisories(
                s.store, s.owner.id, s.config.max_chars
            )
        except Exception:
            kb_advisories: list[str] = []

        # Wrapped like the four calls above, and for the same reason: a
        # transcript status that cannot be computed must not take down the
        # events status it decorates.
        try:
            transcript_advisories = transcripts_service.advisories(s.store, s.owner.id)
        except Exception:
            transcript_advisories: list[str] = []

        report = events.status(
            s.store,
            s.owner.id,
            idle_seconds=s.config.idle_minutes * 60,
            hook_advisories=advisories,
            ingest_advisories=ingest_advisories,
            memory_advisories=memory_advisories,
            kb_advisories=kb_advisories,
            transcript_advisories=transcript_advisories,
        )

    if as_json:
        typer.echo(json.dumps(events.to_dict(report), indent=2))
        return
    typer.echo(events.render(report))


@record_app.command("enable")
def record_enable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Turn on event recording for a project (defaults to this directory)."""
    from saddlebag.services import record

    name = _require_project(project or _default_project())
    with _session() as s:
        record.enable(s.store, s.owner.id, name)
        model = s.config.extract_model
    typer.echo(f"recording enabled for '{name}'")
    # Same cost note as capture's, and for the same reason: state the
    # tradeoff at the moment it is actionable.
    typer.echo(
        f"extraction runs `claude -p --model {model}` once per session, "
        f"roughly $0.10-0.25 each.\n"
        f"change it with BAG_EXTRACT_MODEL (e.g. haiku for less, "
        f"opus for more)."
    )


@record_app.command("disable")
def record_disable(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Turn off event recording for a project."""
    from saddlebag.services import record

    name = _require_project(project or _default_project())
    with _session() as s:
        record.disable(s.store, s.owner.id, name)
    typer.echo(f"recording disabled for '{name}'")


@record_app.command("event")
def record_event(
    agent: Annotated[str, typer.Option("--agent")] = "claude-code",
    strict: Annotated[bool, typer.Option("--strict")] = False,
):
    """Record one harness event read from stdin.

    A hook entry point in everything but name: it runs once per tool call,
    so it exits 0 unconditionally and prints nothing to stdout. Every early
    return explains itself through the same BAG_HOOK_DEBUG channel the
    Claude Code hooks use - see saddlebag/hookio.py.

    The one loud case is unreadable stdin, and only when a human is
    plausibly the one who typed the command: interactively, or with
    --strict. From a hook, even that stays silent.
    """
    from saddlebag.agents import registry
    from saddlebag.hookio import debug
    from saddlebag.services import record

    env = dict(os.environ)
    loud = strict or sys.stdin.isatty()
    stdin_text = sys.stdin.read()

    try:
        payload: dict[str, Any] = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError, AttributeError:
        debug(env, "stdin was not valid JSON")
        if loud:
            typer.echo("stdin was not valid JSON", err=True)
            raise typer.Exit(1)
        raise typer.Exit(0)

    try:
        adapter = registry.get(agent)()
    except registry.UnknownAgent as exc:
        debug(env, str(exc))
        raise typer.Exit(0)

    # event() is an optional capability, probed exactly like
    # env_settings()/settings_path() in services/settings.py: an adapter
    # that lacks it, or whose implementation raises, must never be why
    # recording stops - it just cannot record, which is what "None" and a
    # caught exception both mean here.
    event_of = getattr(adapter, "event", None)
    if event_of is None:
        debug(env, f"agent '{agent}' does not support recording events")
        raise typer.Exit(0)

    try:
        harness_event = event_of(env, payload)
    except Exception as exc:
        debug(
            env,
            f"{agent} adapter's event() raised {type(exc).__name__}: {exc}",
        )
        raise typer.Exit(0)

    if harness_event is None:
        debug(env, "payload was not an event worth recording")
        raise typer.Exit(0)

    try:
        cfg = load()
        with open_session(cfg) as s:
            result = record.record(s.store, s.owner.id, harness_event, adapter.name)
    except Exception as exc:
        debug(env, f"{type(exc).__name__}: {exc}")
        raise typer.Exit(0)

    if result is None:
        debug(
            env,
            f"recording is not enabled for project "
            f"{harness_event.project!r}. Enable it with `bag record "
            f"enable --project {harness_event.project}`.",
        )
    raise typer.Exit(0)


@handoff_app.command("write")
def handoff_write(
    body: Annotated[Optional[str], typer.Option("--body")] = None,
    edit: Annotated[bool, typer.Option("--edit")] = False,
    topic: Annotated[Optional[str], typer.Option("--topic")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Store this session's state so it can be resumed after /clear.

    The body is four sections - Done, In flight, Next steps, Gotchas. Writing
    a handoff supersedes the previous one for the same topic, so a project
    only ever has one live handoff per workstream.
    """
    from saddlebag.services import handoff as handoff_svc

    name = _require_project(project or _default_project())
    text = _body_from_editor(handoff_svc.BLANK_BODY) if edit else _read_body(body)
    with _session() as s:
        try:
            entry, superseded = handoff_svc.write(
                s.store,
                s.owner.id,
                project=name,
                topic=topic,
                body=text,
            )
        except (handoff_svc.NoProject, ValueError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
    typer.echo(entry.id)
    if superseded is not None:
        typer.echo(f"superseded {superseded.id}")
    typer.echo(f"resume with: /clear, then bag-prime {handoff_svc.topic_of(entry)}")


@handoff_app.command("latest")
def handoff_latest(
    topic: Annotated[Optional[str], typer.Option("--topic")] = None,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Print the newest live handoff for this project."""
    from saddlebag.services import handoff as handoff_svc

    name = _require_project(project or _default_project())
    if not name:
        # No project to query at all - a printed one-liner, not a reason to
        # open a database connection (and, if Postgres is down, an error).
        typer.echo("No handoff stored for this directory.")
        return
    with _session() as s:
        started = time.perf_counter()
        try:
            entry = handoff_svc.latest(s.store, s.owner.id, project=name, topic=topic)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        usage.log_access(
            s.store,
            AccessRecord(
                owner_id=s.owner.id,
                source="cli",
                op="handoff",
                hits=0 if entry is None else 1,
                entry_ids=() if entry is None else (entry.id,),
                elapsed_ms=usage.elapsed_ms(started),
                project=name,
                session_id=usage.session_id_from_env(os.environ),
            ),
        )
    if entry is None:
        # An ordinary state, not an error: most projects have never been
        # handed off, and prime asks about them anyway.
        typer.echo(f"No handoff stored for '{name or 'this directory'}'.")
        return
    if as_json:
        typer.echo(json.dumps(_entry_dict(entry), indent=2))
        return
    typer.echo(f"{entry.title}  ({entry.id})")
    typer.echo("")
    typer.echo(entry.body)


def _memory_dir(cwd: Path) -> Path | None:
    """The claude-code adapter's answer, or None.

    Probed like every optional adapter capability (`registry.get` returns
    the class; a broken or absent capability degrades rather than crashing
    a command a person just typed).
    """
    from saddlebag.agents import registry

    try:
        adapter = registry.get("claude-code")()
    except registry.UnknownAgent as exc:
        typer.echo(str(exc), err=True)
        return None
    fn = getattr(adapter, "memory_dir", None)
    if fn is None:
        return None
    try:
        return fn(cwd)
    except Exception as exc:  # noqa: BLE001 - warn and degrade, never crash
        typer.echo(f"claude-code: memory_dir failed: {exc}", err=True)
        return None


@memory_app.command("designate")
def memory_designate(
    slug: Annotated[Optional[str], typer.Argument()] = None,
    clear: Annotated[bool, typer.Option("--none")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Point this project's memory export at an existing collection."""
    if (slug is None) == (not clear):
        # Exactly one of the two, because `None if clear else slug` would
        # otherwise read a bare `bag memory designate` as "clear it" - a
        # user who typed it to see the current designation would have
        # destroyed it and been told so in the past tense.
        raise typer.BadParameter(
            "pass a collection slug, or --none to clear the designation"
        )
    resolved = _resolve_project(project, False)
    if not clear and resolved != _default_project():
        # The designation now records this directory, and `sync --all` syncs
        # the memory directory that *this* cwd maps to. Designating some
        # other project from here would record a working directory that has
        # nothing to do with it, and the mismatch would only surface later,
        # as a sync writing to the wrong place. Fail here instead.
        typer.echo(
            f"Refusing: --project names '{resolved}' but this directory is "
            f"'{_default_project()}'. The designation records the working "
            f"directory it was made from, because Claude Code's memory "
            f"directory is keyed on it - so designate from the project's "
            f"own directory.",
            err=True,
        )
        raise typer.Exit(1)
    with _session() as s:
        try:
            memory_service.designate(
                s.store,
                s.owner.id,
                resolved,
                None if clear else slug,
                working_dir=None if clear else str(Path.cwd().resolve()),
            )
        except memory_service.NoProject:
            typer.echo(
                "No project here: saddlebag resolves one from the git repository "
                "root, and this directory is not in a repository. Pass "
                "--project <name> to designate one explicitly.",
                err=True,
            )
            raise typer.Exit(1)
        except kb.CollectionNotFound as exc:
            typer.echo(
                f"No collection {exc}. Create it with `bag kb create` "
                f"first - designating cannot create one, because a "
                f"collection with an empty query matches nothing forever.",
                err=True,
            )
            raise typer.Exit(1)
    typer.echo("Cleared." if clear else f"{resolved} -> {slug}")


def _memory_sync_all(dry_run: bool) -> None:
    """Every designated project, one line each.

    Prints a line per project rather than a total, because the failure this
    command exists to make visible is one project quietly not syncing - and
    a total of "40 unchanged" hides that as well as a silent skip would.
    """
    # autocommit, like `bag reingest run` and `bag events process`: the
    # started run row has to be committed before any file is read, or a
    # crash rolls it back and "crashed" becomes indistinguishable from
    # "never ran". It also stops sync's two halves disagreeing - files are
    # written to disk as the sync goes, so a single transaction only ever
    # rolled the store back and left the disk moved.
    with _session(autocommit=True) as s:
        outcomes = memory_service.sync_all(
            s.store,
            s.owner.id,
            resolve_directory=_memory_dir,
            dry_run=dry_run,
        )
    if not outcomes:
        typer.echo(
            "No project has a memory collection. "
            "Run `bag memory designate <slug>` in one first."
        )
        return
    width = max(len(o.project) for o in outcomes)
    problems = 0
    for o in outcomes:
        if o.skipped is not None:
            problems += 1
            typer.echo(f"{o.project:<{width}}  skipped: {o.skipped}", err=True)
            continue
        # Every outcome carries either a skip reason or a report, and the
        # `continue` above took the skips - see sync_all, which is where
        # that invariant is created.
        r = o.report
        assert r is not None
        typer.echo(
            f"{o.project:<{width}}  {r.adopted} adopted, {r.edited} edited, "
            f"{r.regenerated} regenerated, {r.healed} healed, "
            f"{r.deleted} deleted, {len(r.renamed)} renamed, "
            f"{r.unchanged} unchanged"
        )
        for old, new in r.renamed:
            typer.echo(f"{o.project:<{width}}  renamed: {old} -> {new}")
        for name in r.conflicts:
            problems += 1
            typer.echo(
                f"{o.project:<{width}}  conflict: {name} changed on both "
                f"sides; see {o.directory}",
                err=True,
            )
        for name, reason in r.failures:
            problems += 1
            typer.echo(
                f"{o.project:<{width}}  failed: {name}: {reason}",
                err=True,
            )
    if problems:
        # Same fail-loud contract as a single sync, and for the same reason:
        # a person typed this, and a skip is a definite statement that a
        # directory was not synced - not an "I could not tell".
        raise typer.Exit(1)


@memory_app.command("sync")
def memory_sync(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    every: Annotated[bool, typer.Option("--all")] = False,
):
    """Adopt what Claude wrote, then regenerate the directory from saddlebag."""
    if every:
        if project is not None:
            raise typer.BadParameter("pass --project or --all, not both")
        _memory_sync_all(dry_run)
        return
    resolved = _require_project(_resolve_project(project, False))
    directory = _memory_dir(Path.cwd())
    if directory is None:
        typer.echo("claude-code has no memory directory here.", err=True)
        raise typer.Exit(1)
    # autocommit, like `bag reingest run` and `bag events process`: the
    # started run row has to be committed before any file is read, or a
    # crash rolls it back and "crashed" becomes indistinguishable from
    # "never ran". It also stops sync's two halves disagreeing - files are
    # written to disk as the sync goes, so a single transaction only ever
    # rolled the store back and left the disk moved.
    with _session(autocommit=True) as s:
        try:
            report = memory_service.sync(
                s.store,
                s.owner.id,
                project=resolved,
                directory=directory,
                dry_run=dry_run,
            )
        except memory_service.NotDesignated:
            typer.echo(
                f"{resolved} has no memory collection. "
                f"Run `bag memory designate <slug>` first.",
                err=True,
            )
            raise typer.Exit(1)
        except memory_service.CollectionTooLarge as exc:
            typer.echo(
                f"Collection '{exc.slug}' resolves to {exc.limit} entries, "
                f"the limit saddlebag reads a collection at. Past it, an entry "
                f"saddlebag cannot see is indistinguishable from one that left "
                f"the collection, and its file would be deleted - so nothing "
                f"was written. Narrow the collection's query.",
                err=True,
            )
            raise typer.Exit(1)
    prefix = "Would write: " if dry_run else ""
    typer.echo(
        f"{prefix}{report.adopted} adopted, {report.edited} edited, "
        f"{report.regenerated} regenerated, {report.healed} healed, "
        f"{report.deleted} deleted, {len(report.renamed)} renamed, "
        f"{report.unchanged} unchanged."
    )
    for old, new in report.renamed:
        # Named rather than only counted. A rename re-tags an entry, which is
        # a write the user did not ask for by name, and the pair is also how
        # they would spot saddlebag following a rename they did not intend.
        typer.echo(f"{prefix}renamed: {old} -> {new}")
    for name in report.conflicts:
        # Only a conflict that actually wrote a sidecar names one. A dry run
        # writes none, and neither does a file edited for an entry that left
        # the collection, so pointing at the file unconditionally would send
        # the user looking for something that is not there.
        if name in report.sidecars:
            typer.echo(
                f"conflict: {name} changed on both sides; saddlebag's version is "
                f"in {name}{memory_service.CONFLICT_SUFFIX} - yours to "
                f"resolve and delete, nothing here does it for you.",
                err=True,
            )
        else:
            typer.echo(
                f"conflict: {name} was left alone and no "
                f"{memory_service.CONFLICT_SUFFIX} file was written for it - "
                f"a dry run writes none, and neither does a file whose entry "
                f"has left the collection.",
                err=True,
            )
    for name, reason in report.failures:
        typer.echo(f"failed: {name}: {reason}", err=True)
    if report.conflicts or report.failures:
        # Fail-loud: a person typed this, and a conflict is a definite
        # statement that work was not done - not an "I could not tell".
        raise typer.Exit(1)


@memory_app.command("refresh")
def memory_refresh():
    """Sync this project's memory directory. Spawned, not typed.

    The silent half of `bag memory sync`, and the exact analogue of
    `bag reingest run`: started detached by a session start, so it exits 0
    on every path, prints nothing to stdout, and explains itself only to
    stderr behind BAG_HOOK_DEBUG. An undesignated project does nothing,
    which is the common case.

    `sync` keeps its own contract - loud, and non-zero on a conflict -
    because a person typed it. Here a conflict writes its sidecar and says
    nothing; `bag memory status` and the advisory line in `bag record
    status` are what surface it afterwards, which is what that layer was
    built for.
    """
    from saddlebag import hookio

    env = dict(os.environ)
    try:
        _memory_refresh_once(env)
    except BaseException as exc:
        # BaseException, not Exception, and the reason is narrower than it
        # looks. `typer.Exit` is a RuntimeError, so `except Exception`
        # already swallows the `typer.Exit(1)` `_session` raises for an
        # unreachable database - measured, not assumed. What BaseException
        # adds is everything else that is not an Exception: a real
        # SystemExit from any library that calls sys.exit(), and a
        # KeyboardInterrupt. A detached command nobody is watching has no
        # path on which a non-zero exit helps anyone, so it catches the lot.
        hookio.debug(env, f"{type(exc).__name__}: {exc}")
    raise typer.Exit(0)


def _memory_refresh_once(env: dict[str, str]) -> None:
    """The work `memory refresh` wraps in silence. Free to raise."""
    from saddlebag import hookio

    resolved = resolve_project()
    if resolved is None:
        hookio.debug(env, "no project to sync")
        return
    directory = _memory_dir(Path.cwd())
    if directory is None:
        hookio.debug(env, "claude-code has no memory directory here")
        return
    # autocommit, exactly as `bag memory sync` does it: the started run
    # row has to be committed before any file is read, or a crash rolls it
    # back and "crashed" becomes indistinguishable from "never ran".
    with _session(autocommit=True) as s:
        try:
            report = memory_service.sync(
                s.store,
                s.owner.id,
                project=resolved,
                directory=directory,
                trigger=MemoryTrigger.AUTO,
            )
        except memory_service.NotDesignated:
            # The opt-in gate, and the common case - not a failure. Nothing
            # is recorded, because nothing ran.
            hookio.debug(env, f"{resolved} has no memory collection")
            return
    hookio.debug(
        env,
        f"memory sync: {report.adopted} adopted, {report.edited} edited, "
        f"{report.regenerated} regenerated, {report.deleted} deleted, "
        f"{report.unchanged} unchanged",
    )
    for name in report.conflicts:
        hookio.debug(env, f"memory sync conflict: {name}")
    for name, reason in report.failures:
        hookio.debug(env, f"memory sync failed: {name}: {reason}")


@memory_app.command("status")
def memory_status(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """What is designated, what is on disk, and what overlaps the KB."""
    resolved = _require_project(_resolve_project(project, False))
    directory = _memory_dir(Path.cwd())
    with _session() as s:
        st = memory_service.status(
            s.store,
            s.owner.id,
            project=resolved,
            directory=directory,
            kb_slug=resolved,
        )
    if as_json:
        # Before the not-designated early return below: --json keeps one
        # shape for every state, so an undesignated project is a null
        # `collection`, not a different document.
        typer.echo(json.dumps(memory_service.status_to_dict(st), indent=2))
        return
    if st.collection is None:
        typer.echo(f"{resolved}: not designated.")
        return
    typer.echo(f"{resolved}: {st.collection} -> {st.directory}")
    typer.echo(f"  {st.entries} entries, {st.files} files, {st.stale} stale")
    # What last happened, beside what is true now. Both are wanted: the
    # counts above describe the directory, this describes the run.
    typer.echo(memory_service.render_run(st.run))
    if st.conflicts:
        typer.echo(
            f"  {st.conflicts} conflict sidecar(s) on disk from a past "
            f"sync - yours to resolve and delete, saddlebag never does."
        )
    if st.overlap:
        typer.echo(
            f"  {st.overlap} entries ({st.overlap_bytes} bytes) are also in "
            f"the '{resolved}' knowledge base, so they load twice per session"
        )


if __name__ == "__main__":
    app()


@reingest_app.command("designate")
def reingest_designate(
    paths: Annotated[
        Optional[list[str]],
        typer.Argument(help="Repo-relative paths. Omit with --clear."),
    ] = None,
    archive: Annotated[bool, typer.Option("--archive")] = False,
    clear: Annotated[bool, typer.Option("--clear")] = False,
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Record which paths this project re-ingests automatically.

    Opt-in, per project, and it holds a value rather than a boolean - the
    question is "which paths", not "on or off". A project with no
    designation re-ingests nothing, which is what keeps this from running
    for anyone who did not ask.

    --archive designates the archive half separately, because the refresh
    is genuinely two invocations with different origins. The two are stored
    and cleared independently.
    """
    resolved = _resolve_project(project, False)
    if resolved is None:
        typer.echo(
            "No project to designate. Run this inside a repository or pass --project.",
            err=True,
        )
        raise typer.Exit(1)
    if clear and paths:
        typer.echo("Pass either paths or --clear, not both", err=True)
        raise typer.Exit(1)
    with _session() as s:
        try:
            ingest_service.designate(
                s.store,
                s.owner.id,
                resolved,
                None if clear else list(paths or []),
                archive=archive,
            )
        except ingest_service.BadDesignation as exc:
            # Fail-loud: a person typed this, and the whole point of
            # validating here is that there is someone to tell.
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
    half = "archive" if archive else "default"
    if clear:
        typer.echo(f"Cleared the {half} re-ingest designation for {resolved}.")
        return
    typer.echo(f"{resolved} ({half}) re-ingests: {', '.join(paths or [])}")


@reingest_app.command("status")
def reingest_status(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Show what this project re-ingests automatically, and how the last
    run went.

    Designated paths are checked on disk only for the project this
    directory resolves to - it is the only one with a known root - and the
    report says so for any other, rather than letting silence read as
    "all present".
    """
    resolved = _resolve_project(project, False)
    current = resolve_project()
    with _session() as s:
        found = ingest_service.status(
            s.store,
            s.owner.id,
            resolved,
            current_project=current,
            root=repo_root(),
        )
    if as_json:
        typer.echo(json.dumps(ingest_service.status_to_dict(found), indent=2))
        return
    typer.echo(ingest_service.render_status(found, resolved))


@reingest_app.command("run")
def reingest_run():
    """Re-ingest this project's designated paths. Spawned, not typed.

    Fail-soft in the strongest sense this repo has: it is started detached
    by a session start, so it exits 0 on every path, prints nothing to
    stdout, and explains itself only to stderr behind BAG_HOOK_DEBUG.
    An undesignated project does nothing, which is the common case.

    The manual commands keep their own contracts: `bag ingest` and
    `bag embed` are still loud, because someone asked for those.
    """
    from saddlebag import hookio

    env = dict(os.environ)
    try:
        _reingest_once(env)
    except BaseException as exc:
        # BaseException, not Exception. The original reason recorded here
        # was wrong: `typer.Exit` is a RuntimeError, so the `typer.Exit(1)`
        # `_session` raises for an unreachable database is caught by
        # `except Exception` too. What BaseException actually buys is a real
        # SystemExit - from any library that calls sys.exit() - and a
        # KeyboardInterrupt. A detached command nobody is watching has no
        # path on which a non-zero exit helps anyone, so it catches the lot.
        # Everything below this line is best-effort by contract.
        hookio.debug(env, f"{type(exc).__name__}: {exc}")
    raise typer.Exit(0)


def _reingest_once(env: dict[str, str]) -> None:
    """The work `reingest run` wraps in silence. Free to raise."""
    from saddlebag import hookio

    resolved = resolve_project()
    if resolved is None:
        hookio.debug(env, "no project to re-ingest")
        return
    # autocommit, like `bag events process`: this is long-running work
    # that records its own progress. The run row is started before any file
    # is read, and it has to be COMMITTED then, or a process that dies
    # mid-run rolls its own "I started" back and looks like it never ran.
    with _session(autocommit=True) as s:
        result = ingest_service.refresh(
            s.store,
            s.owner.id,
            resolved,
            repo_root(),
            embed_model=load().embed_model,
            load_embedder=lambda: load_embedder(load().embed_model),
        )
    hookio.debug(
        env,
        f"re-ingest: {result.report.created} new, "
        f"{result.report.changed} changed, {result.embedded} embedded",
    )
    for path, reason in result.report.failures:
        hookio.debug(env, f"re-ingest failed: {path}: {reason}")
    if result.embed_error:
        hookio.debug(env, f"re-ingest embed skipped: {result.embed_error}")


@import_app.command("claude-mem")
def import_claude_mem(
    path: Annotated[str, typer.Argument(help="Path to claude-mem's sqlite file.")],
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
):
    """Load a claude-mem database in as entries with origin='imported'.

    Re-runnable: an entry whose `cmem:<id>` tag is already present and whose
    body is unchanged is skipped without a write. There is no orphan sweep -
    a row deleted from claude-mem never deletes anything here, because the
    source is being decommissioned and this is a migration, not a sync.
    """
    source = Path(path)
    # read() runs before _session() opens a connection, deliberately: a
    # file that is not a database should be refused without ever touching
    # Postgres, the same ordering `bag ingest` uses for an unreadable path.
    try:
        result = claude_mem.read(source)
    except claude_mem.UnreadableSource as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    try:
        with _session() as s:
            report = import_.run(
                s.store,
                s.owner.id,
                result.records,
                namespace=claude_mem.NAMESPACE,
                project=project,
                dry_run=dry_run,
                skipped=result.skipped,
            )
    except import_.ImportPreconditionFailed as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if dry_run:
        typer.echo("Dry run - nothing was written.")
    # Two lists of the same "name: count" shape, printed back to back, are
    # indistinguishable without a heading - a claude-mem project genuinely
    # could be named something kind-shaped. "Projects" names what the
    # entries are filed under AFTER this run (see the by_project docstring
    # in import_.py) - not what --project asked for, which a second run
    # over already-imported rows cannot change.
    typer.echo("Projects:")
    for name, count in sorted(report.by_project.items()):
        typer.echo(f"  {name}: {count}")
    typer.echo("Kinds:")
    for name, count in sorted(report.by_kind.items()):
        typer.echo(f"  {name}: {count}")
    typer.echo(
        f"{report.created} created, {report.updated} updated, "
        f"{report.unchanged} unchanged"
    )
    # A row that could not be mapped is not a silent loss: it is named here,
    # exactly as it was named in the reader, so a person can go decide by
    # hand whether it mattered. This is the whole reason `skipped` exists.
    if report.skipped:
        typer.echo(f"{len(report.skipped)} skipped:")
        for line in report.skipped:
            typer.echo(f"  {line}")
    if report.created or report.updated:
        typer.echo("Run `bag embed` to give the new entries vectors.")


@dedupe_app.command("report")
def dedupe_report(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    kind: Annotated[Optional[list[Kind]], typer.Option("--kind")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    threshold: Annotated[
        float, typer.Option("--threshold")
    ] = dedupe_service.DEFAULT_THRESHOLD,
    limit: Annotated[int, typer.Option("--limit")] = dedupe_service.DEFAULT_PAIR_LIMIT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Report entries that duplicate each other. Read-only.

    Two tiers, both always run and never blended: identical bodies first,
    then entries close in meaning, each pair with its similarity. Nothing is
    merged - every group prints a `bag dedupe resolve` line to run, edit
    or ignore.

    --limit bounds the near tier only; identical bodies are never truncated.
    """
    with _session() as s:
        report = dedupe_service.report(
            s.store,
            s.owner.id,
            Query(
                project=project,
                kinds=list(kind or []),
                tags=list(tag or []),
                limit=limit,
            ),
            model=s.config.embed_model,
            threshold=threshold,
            limit=limit,
        )
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "exact": [
                        [_entry_dict(e) for e in g.entries] for g in report.exact
                    ],
                    "near": [
                        {
                            "similarity": p.similarity,
                            "entries": [_entry_dict(p.a), _entry_dict(p.b)],
                        }
                        for p in report.near
                    ],
                    "near_total": report.near_total,
                    "near_suppressed": report.near_suppressed,
                    "near_truncated": report.near_truncated,
                    # Explicit rather than inferred from an empty "near": a machine
                    # reader must be able to tell "none found" from "never ran", for
                    # the same reason the human rendering says so in words.
                    "near_checked": report.embedded > 0,
                    "threshold": report.threshold,
                    "model": report.model,
                    "coverage": {"embedded": report.embedded, "total": report.total},
                },
                indent=2,
                default=str,
            )
        )
        return
    typer.echo(dedupe_service.render(report))


@dedupe_app.command("resolve")
def dedupe_resolve(
    drop_id: str,
    keep: Annotated[str, typer.Option("--keep")],
):
    """Point one existing entry at another that says the same thing.

    Unlike `supersede`, no new entry is written: both already exist, and the
    dropped one is marked as superseded by the kept one. Fail-loud - a
    person asked for this.
    """
    with _session() as s:
        try:
            dropped, kept = dedupe_service.resolve(
                s.store, s.owner.id, UUID(drop_id), UUID(keep)
            )
        except (dedupe_service.CannotResolve, ValueError) as exc:
            typer.echo(f"Cannot resolve: {exc}")
            raise typer.Exit(1)
    typer.echo(f"{dropped.id} ({dropped.title})")
    typer.echo(f"  superseded by {kept.id} ({kept.title})")


def _count(n: int, noun: str) -> str:
    """ "1 subagent file", "2 subagent files". Every noun it is given takes
    a plain -s, which is why this is not a general pluraliser."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


@transcripts_app.command("discover")
def transcripts_discover(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Propose directories that hold this project's sessions. Writes nothing.

    Ownership is proven per session id - a transcript's filename intersected
    with what `bag record event` already recorded for this project - never
    guessed from a directory's name. This only proposes; `bag transcripts
    designate` is the one write, and a human decides.
    """
    resolved = _require_project(_resolve_project(project, False))
    # The service owns where this harness keeps its transcripts, honouring
    # CLAUDE_CONFIG_DIR - a frontend that builds the path itself is a
    # frontend deciding, and the hand-built copy that used to sit here found
    # nothing at all for anyone who moves that directory.
    root = transcripts_service.transcript_root()
    with _session() as s:
        found = transcripts_service.discover(s.store, s.owner.id, resolved, root)
    if not found:
        typer.echo(
            f"No directory under {root} has sessions this project recorded events for."
        )
        return
    for c in found:
        claimed = f" - claimed by {c.claimed_by}" if c.claimed_by else ""
        extra = f", plus {_count(c.subagents, 'subagent file')}" if c.subagents else ""
        typer.echo(
            f"{c.path}  {c.matched} of {_count(c.total, 'session')} match"
            f"{extra}{claimed}"
        )


@transcripts_app.command("designate")
def transcripts_designate(
    directory: Annotated[
        str, typer.Argument(help="A directory of .jsonl transcripts.")
    ],
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Claim a transcript directory for this project. Fail-loud.

    Claiming records a claim; it does not read a single file. The count
    printed here is what `bag transcripts import` will have to read to
    back it up - saying so is the whole point, because claiming without it
    would silently commit someone to a read that has been 179MB on this
    project's own history.
    """
    resolved = _require_project(_resolve_project(project, False))
    with _session() as s:
        try:
            absolute = transcripts_service.designate(
                s.store, s.owner.id, resolved, Path(directory)
            )
        except transcripts_service.PathRefused as exc:
            # Fail-loud, and the message is the deliverable: the conflicting-
            # project case names the project already holding the directory,
            # which is why the service returns a name rather than a bool.
            # The CLI does not reformat, re-derive or swallow it.
            typer.echo(str(exc))
            raise typer.Exit(1)
    # Through the service's layout function, not a glob: a hand-written
    # `*.jsonl` here is exactly the blind spot that under-reported what a
    # claim brings in.
    files = transcripts_service.transcript_files(Path(absolute))
    subagents = sum(1 for f in files if f.agent_id is not None)
    typer.echo(
        f"{resolved} claims {absolute} ({_count(len(files) - subagents, 'session')}, "
        f"{_count(subagents, 'subagent file')}). Nothing has been imported yet - run "
        f"`bag transcripts import` to read them."
    )


@transcripts_app.command("undesignate")
def transcripts_undesignate(
    directory: Annotated[str, typer.Argument(help="A claimed directory to release.")],
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Release a claimed transcript directory. Transcripts already imported
    are NOT deleted.

    The counterpart `designate` needs: a directory belongs to one project,
    so `designate` refuses one another project already holds, and without
    this the only way out of a claim made under the wrong project is
    hand-written SQL. `bag transcripts import` reports a session whose
    events were recorded under a different project as an anomaly - this is
    how a user acts on that report.

    Releasing stops future reading and nothing else. Destroying stored
    sessions is a separate, explicit act and there is no command for it, so
    releasing is safe - the echo says so, because someone who has just been
    told their transcripts are filed under the wrong project needs to know
    that before they type this.
    """
    resolved = _require_project(_resolve_project(project, False))
    with _session() as s:
        released = transcripts_service.undesignate(
            s.store, s.owner.id, resolved, Path(directory)
        )
    if not released:
        # Fail-loud: a person typed this, and "that project does not claim
        # that directory" is a definite statement, not an "I could not
        # tell". The likeliest cause is the claim being held by another
        # project, which is exactly the case this command exists for.
        typer.echo(
            f"{resolved} does not claim {Path(directory).resolve()} - "
            f"`bag transcripts status` names what it does claim."
        )
        raise typer.Exit(1)
    typer.echo(
        f"{resolved} released {Path(directory).resolve()}. Transcripts "
        f"already imported are kept - this stops future reading only."
    )


@transcripts_app.command("status")
def transcripts_status(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """What is claimed, what is on disk now, and how the last import went.

    `run` answers what last HAPPENED; the claimed-directory, backlog and
    irrecoverable lines answer the state NOW - a reader must not have to
    infer one from the other. `root` comes from
    `transcripts.transcript_root()`, the one place that resolves this
    harness's transcript directory, so status sees exactly what `discover`
    and `import` would - including when CLAUDE_CONFIG_DIR has moved it.
    """
    resolved = _require_project(_resolve_project(project, False))
    root = transcripts_service.transcript_root()
    with _session() as s:
        got = transcripts_service.status(s.store, s.owner.id, resolved, root)
    if as_json:
        # Before the text branches below: one object in every state, never
        # a shorter document, so a consumer checks a key for null rather
        # than branching on which keys arrived - the same rule `bag memory
        # status --json` follows.
        typer.echo(json.dumps(transcripts_service.status_to_dict(got), indent=2))
        return

    typer.echo(f"{resolved}:")
    # Four distinct spellings, following `memory.render_run` and `bag
    # reingest status`: never synced, clean, did not finish, and finished
    # with something wrong. Every spelling that has a run names its
    # TRIGGER, because a spawned `refresh` and a typed `import` leave
    # identical rows and a reader must be able to tell them apart.
    run = got.run
    if run is None:
        typer.echo("  never imported")
    elif run.finished_at is None:
        typer.echo(f"  last import ({run.trigger}) did not finish")
    elif run.failures or run.anomalies:
        trouble = []
        if run.failures:
            trouble.append(f"{len(run.failures)} failure(s)")
        if run.anomalies:
            trouble.append(f"{len(run.anomalies)} anomaly(ies)")
        typer.echo(
            f"  last import ({run.trigger}) at "
            f"{run.started_at.astimezone().strftime('%Y-%m-%d %H:%M')}: "
            f"{', '.join(trouble)}"
        )
    else:
        typer.echo(
            f"  last import ({run.trigger}) at "
            f"{run.started_at.astimezone().strftime('%Y-%m-%d %H:%M')}: clean"
        )

    if not got.paths:
        typer.echo("  no directory claimed")
    for p in got.paths:
        state = (
            f"{_count(p.on_disk, 'session')}, {_count(p.subagents, 'subagent file')}"
            if p.present
            else "missing"
        )
        typer.echo(f"  {p.path}: {state}")
    typer.echo(
        f"  backlog: {_count(got.backlog, 'session')}, "
        f"{_count(got.subagent_backlog, 'subagent file')}, "
        f"{_count(got.meta_backlog, 'sidecar')}"
    )
    typer.echo(f"  irrecoverable: {got.irrecoverable}")


@transcripts_app.command("import")
def transcripts_import(
    project: Annotated[Optional[str], typer.Option("--project")] = None,
):
    """Import every directory claimed for this project. Loud, unbounded.

    The typed, unbounded half of `bag transcripts refresh` - a person asked
    for this one, so it passes no cap and reads everything a claimed
    directory holds. The first import of a claimed directory can be 179MB
    across many files.
    """
    resolved = _require_project(_resolve_project(project, False))
    # autocommit, exactly as `bag memory sync` and `bag reingest run`: the
    # started run row has to be committed before any file is read, or a
    # crash rolls it back and "crashed" becomes indistinguishable from
    # "never ran".
    with _session(autocommit=True) as s:
        report = transcripts_service.run(
            s.store, s.owner.id, resolved, trigger=TranscriptTrigger.MANUAL
        )
    typer.echo(
        f"{report.files_seen} seen, {report.files_new} new, "
        f"{report.files_appended} appended, {report.files_rebuilt} rebuilt, "
        f"{report.lines_written} lines, {report.bytes_written} bytes, "
        f"{_count(report.metas_written, 'sidecar')}"
    )
    for a in report.anomalies:
        # Two shapes now, told apart by `reason` rather than by which keys
        # arrived - the service tags every entry it appends, so a third kind
        # cannot quietly render as one of these two.
        if a.get("reason") == transcripts_service.PROJECT_CONFLICT:
            typer.echo(
                f"anomaly: {a['path']} is claimed by '{a['claiming']}' but its "
                f"events were recorded under "
                f"{', '.join(repr(r) for r in a['recorded'])} - it was stored "
                f"anyway, under the project it was first filed as",
                err=True,
            )
        else:
            typer.echo(
                f"anomaly: {a['path']} shrank on disk (stored {a['stored']}, "
                f"on disk {a['on_disk']}) - the stored copy is kept",
                err=True,
            )
    for f in report.failures:
        typer.echo(f"failed: {f['path']}: {f['reason']}", err=True)
    if report.failures:
        # Fail-loud: a person typed this, and a failure is a definite
        # statement that something was not imported - not an "I could not
        # tell".
        raise typer.Exit(1)


@transcripts_app.command("refresh")
def transcripts_refresh():
    """Import this project's transcripts. Spawned, not typed.

    The silent half of `bag transcripts import`, and the exact analogue of
    `bag memory refresh` and `bag reingest run`: started detached by a
    session start, so it exits 0 on every path, prints nothing to stdout,
    and explains itself only to stderr behind BAG_HOOK_DEBUG. A project
    with no claimed directory does nothing, which is the common case.

    Bounded at REFRESH_FILE_CAP files, enforced before reading: the first
    import of a claimed directory is 179MB, and a session start must never
    pay for it. `bag transcripts import` is the unbounded half, and a person
    asked for that one.
    """
    from saddlebag import hookio

    env = dict(os.environ)
    try:
        _transcripts_refresh_once(env)
    except BaseException as exc:
        # BaseException, not Exception, and the reason is narrower than it
        # looks. `typer.Exit` is a RuntimeError, so `except Exception`
        # already swallows the `typer.Exit(1)` `_session` raises for an
        # unreachable database - measured, not assumed. What BaseException
        # adds is a real SystemExit from any library that calls sys.exit(),
        # and a KeyboardInterrupt. A detached command nobody is watching has
        # no path on which a non-zero exit helps anyone, so it catches the
        # lot.
        hookio.debug(env, f"{type(exc).__name__}: {exc}")
    raise typer.Exit(0)


def _transcripts_refresh_once(env: dict[str, str]) -> None:
    """The work `transcripts refresh` wraps in silence. Free to raise."""
    from saddlebag import hookio

    resolved = resolve_project()
    if resolved is None:
        hookio.debug(env, "no project to import transcripts for")
        return
    # autocommit, exactly as `bag transcripts import` does it: the started
    # run row has to be committed before any file is read, or a crash rolls
    # it back and "crashed" becomes indistinguishable from "never ran".
    with _session(autocommit=True) as s:
        report = transcripts_service.run(
            s.store,
            s.owner.id,
            resolved,
            trigger=TranscriptTrigger.AUTO,
            cap=transcripts_service.REFRESH_FILE_CAP,
        )
    hookio.debug(
        env,
        f"transcripts import: {report.files_seen} seen, "
        f"{report.files_new} new, {report.files_appended} appended, "
        f"{report.files_rebuilt} rebuilt",
    )
    for a in report.anomalies:
        hookio.debug(env, f"transcripts import anomaly: {a['path']}")
    for f in report.failures:
        hookio.debug(env, f"transcripts import failed: {f['path']}: {f['reason']}")
