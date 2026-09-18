"""FastMCP frontend. Seven tools; every extra one is context the agent pays
for on every turn. Tool docstrings say WHEN to reach for each tool - that
text is the only thing steering agent behaviour."""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import UUID

# The installed mcp package is 2.x, where `FastMCP` was renamed to
# `MCPServer` and moved to `mcp.server.mcpserver` (mcp.server.fastmcp now
# raises ModuleNotFoundError on import, pointing at this rename). The class's
# tool-registration and `list_tools()` API is unchanged from v1, so nothing
# else in this module needed to change - only this import.
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from saddlebag.domain import AccessRecord, Kind, Origin, Query
from saddlebag.project import resolve_project
from saddlebag.services import kb, usage, write
from saddlebag.services.search import find
from saddlebag.session import open_session

mcp = MCPServer("saddlebag")

AGENT_NAME = "claude-code"

# Set once at launch by `configure`. Under stdio both stay at their defaults
# and every behaviour below is exactly what it was before HTTP existed.
_pinned_project: str | None = None
_http_mode = False


def configure(*, project: str | None = None, http: bool = False) -> None:
    """Record how this process was launched. Call before serving."""
    global _pinned_project, _http_mode
    _pinned_project = project
    _http_mode = http


def _default_project() -> str | None:
    """The project an agent is working in, from the server's own directory.

    Claude Code starts the MCP server in the session's directory, so this
    matches the knowledge base the SessionStart hook injects. Without it, an
    agent that omits `project` writes an entry with none - which the project's
    knowledge base will never surface, even though the write succeeded.

    Resolved from the git repository rather than the directory name, so a
    subdirectory or a worktree still files under the project it belongs to.

    Over HTTP the server is a host-side process whose working directory has
    nothing to do with the agent's, so the project is pinned at launch
    instead. An explicit `project` argument on a tool call still wins; this
    only supplies the default.
    """
    if _pinned_project is not None:
        return _pinned_project
    return resolve_project()


def _session_id() -> str | None:
    # Claude Code sets CLAUDE_CODE_SESSION_ID in the server's environment -
    # see usage.SESSION_ENV. The server is started once per Claude Code
    # process, so after /clear this is the id the process started with, not
    # the new one; still the right session far more often than null is.
    #
    # Over HTTP the environment is the launcher's, not the agent's, so the
    # variable is not merely absent but wrong. Same argument, stronger case.
    if _http_mode:
        return None
    return usage.session_id_from_env(os.environ)


def _invalid_kind_message(kind: str) -> str:
    valid = ", ".join(k.value for k in Kind)
    return f"invalid kind '{kind}'; expected one of: {valid}"


@mcp.tool(name="remember")
def remember_tool(
    title: str,
    body: str,
    kind: str = "note",
    project: str | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Store something worth knowing later.

    Use when you learn a durable fact about this project, write down a
    convention that should be followed, or capture reference material. Do
    NOT use for transient details of the current task.

    project: omit it and this session's project is used, which is what the
    knowledge base injected at session start queries on. Only pass it to file
    something under a different project.

    kind: "note" (something learned), "doc" (reference material), or
    "rule" (a convention that must be followed - these are always injected
    into future sessions).

    summary: REQUIRED for kind "rule", optional for everything else. One
    line stating the rule itself, because the context block
    injected into every session renders this and not the body. Put the
    case for the rule - the incident, the reasoning - in body, where it
    stays one recall away.
    """
    try:
        parsed_kind = Kind(kind)
    except ValueError:
        return {"error": _invalid_kind_message(kind)}
    with open_session() as s:
        try:
            entry = write.remember(
                s.store,
                s.owner.id,
                title=title,
                body=body,
                summary=summary,
                kind=parsed_kind,
                project=project or _default_project(),
                tags=list(tags or []),
                agent=AGENT_NAME,
                session_id=_session_id(),
                origin=Origin.AGENT,
            )
        except write.RuleNeedsSummary:
            # An error dict, not a raise: a raise reaches the model as a
            # stack trace, and this is a correctable mistake it can retry.
            return {
                "error": "a rule needs a summary - one line stating the "
                "rule, which is what every session's context "
                "block renders instead of the body"
            }
        return {"id": str(entry.id), "title": entry.title}


@mcp.tool(name="recall")
def recall_tool(
    query: str,
    kind: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    include_handoffs: bool = False,
    include_archived: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Search stored knowledge before assuming something is unknown.

    Use at the start of work on an unfamiliar area, when the user refers to
    a past decision, or before re-deriving something. Returns snippets and
    ids; call get_entry for anything worth reading in full.

    kind: optional filter - "note", "doc", or "rule". Omit to search
    across all kinds.

    include_handoffs: session handoffs are excluded by default because a
    project accumulates many of them. Pass true when resuming a workstream
    and looking for where it was left.

    include_archived: archived document chunks - executed implementation
    plans - are excluded by default because they are three times the volume
    of the reasoning docs and mostly source code that now lives in the repo.
    Pass true when looking for how something was originally built.

    Every result carries "match", saying how it was found:
      "exact"    - the words are in the entry. Trust it.
      "semantic" - related in meaning, not in wording. Usually what you
                   meant, but read the entry in full via get_entry before
                   citing it.
      "fuzzy"    - a spelling-similarity guess made because nothing else
                   matched. Verify before relying on it at all.
    Tiers never mix: every result in one response has the same "match".
    """
    kinds: list[Kind] = []
    if kind is not None:
        try:
            kinds = [Kind(kind)]
        except ValueError:
            return {"error": _invalid_kind_message(kind)}
    with open_session() as s:
        # No embedder is passed. The service builds one only if the semantic
        # tier is reached, and memoises it - which matters more here than in
        # the CLI, because this process is long-lived and would otherwise
        # rebuild the ONNX session on every recall call.
        hits = find(
            s.store,
            s.owner.id,
            Query(
                text=query,
                kinds=kinds,
                project=project,
                tags=list(tags or []),
                limit=limit,
            ),
            fuzzy_threshold=s.config.fuzzy_threshold,
            include_handoffs=include_handoffs,
            include_archived=include_archived,
            semantic_threshold=s.config.semantic_threshold,
            embed_model=s.config.embed_model,
            source="mcp",
            session_id=_session_id(),
            log_project=_default_project(),
        )
        return [
            {
                "id": str(h.entry.id),
                "title": h.entry.title,
                "kind": str(h.entry.kind),
                "project": h.entry.project,
                "tags": list(h.entry.tags),
                "snippet": h.snippet,
                "match": str(h.match),
            }
            for h in hits
        ]


@mcp.tool(name="get_entry")
def get_entry_tool(entry_id: str) -> dict[str, Any]:
    """Read one entry in full, by id from a recall result.

    Use when a recall snippet looks relevant and you need the whole text.
    """
    with open_session() as s:
        started = time.perf_counter()
        try:
            parsed = UUID(entry_id)
        except ValueError:
            # Never reached the store, so there is nothing to log - an
            # invalid id is a malformed call, not a read.
            return {"error": f"'{entry_id}' is not a valid entry id"}
        entry = s.store.get_entry(parsed, s.owner.id)
        usage.log_access(
            s.store,
            AccessRecord(
                owner_id=s.owner.id,
                source="mcp",
                op="get",
                hits=0 if entry is None else 1,
                entry_ids=() if entry is None else (entry.id,),
                elapsed_ms=usage.elapsed_ms(started),
                project=_default_project(),
                session_id=_session_id(),
            ),
        )
        if entry is None:
            return {"error": f"no entry {entry_id}"}
        return {
            "id": str(entry.id),
            "title": entry.title,
            "kind": str(entry.kind),
            "body": entry.body,
            "project": entry.project,
            "tags": list(entry.tags),
        }


@mcp.tool(name="supersede")
def supersede_tool(
    entry_id: str, title: str, body: str, summary: str | None = None
) -> dict[str, Any]:
    """Replace knowledge that stopped being true.

    Use when you discover a stored entry is now wrong or out of date. The
    old entry is kept but stops appearing in searches. Prefer this over
    storing a contradicting second memory.

    summary: omit it and the old entry's summary carries onto the
    replacement, same as title and tags would. Pass it to correct a rule
    written before summaries were required - superseding it without one
    would otherwise fail (see the error this returns).
    """
    with open_session() as s:
        try:
            entry = write.supersede(
                s.store,
                s.owner.id,
                UUID(entry_id),
                title=title,
                body=body,
                summary=summary,
            )
        except write.EntryNotFound, ValueError:
            return {"error": f"no entry {entry_id}"}
        except write.RuleNeedsSummary:
            # An error dict, not a raise, same as remember_tool: this is a
            # correctable mistake, not a crash. This is the one rule this
            # entry predates a summary being required, and superseding it
            # needs one supplied since there is no old one to carry.
            return {
                "error": "this rule has no summary to carry onto the "
                "replacement - pass summary with the one line "
                "the context block should render"
            }
        return {"id": str(entry.id), "replaced": entry_id}


@mcp.tool(name="kb_context")
def kb_context_tool(slug: str, max_chars: int | None = None) -> str:
    """Render a knowledge base as a context block.

    Use when starting work that a knowledge base covers, to load its rules
    and accumulated knowledge at once instead of searching repeatedly.
    """
    with open_session() as s:
        try:
            collection = kb.get(s.store, s.owner.id, slug)
            entries = kb.resolve(s.store, s.owner.id, slug)
            return kb.render(collection, entries, max_chars or s.config.max_chars)
        except kb.CollectionNotFound:
            return f"No knowledge base '{slug}'. Call kb_list to see what exists."
        except kb.RulesExceedBudget as exc:
            return f"Knowledge base '{slug}' needs pruning: {exc}"


@mcp.tool(name="kb_list")
def kb_list_tool() -> list[dict[str, Any]]:
    """List available knowledge bases and what each covers."""
    with open_session() as s:
        return [
            {
                "slug": c.slug,
                "title": c.title,
                "description": c.description,
                "project": c.project,
            }
            for c in s.store.list_collections(s.owner.id)
        ]


@mcp.tool(name="kb_pin")
def kb_pin_tool(slug: str, entry_id: str) -> dict[str, Any]:
    """Curate an entry into a knowledge base so it is always included.

    Use when an entry is important enough that it should appear in the
    knowledge base regardless of search ranking.
    """
    with open_session() as s:
        try:
            kb.pin(s.store, s.owner.id, slug, UUID(entry_id))
        except kb.CollectionNotFound, ValueError:
            return {"error": f"could not pin {entry_id} to '{slug}'"}
        except kb.EntryNotFound:
            return {"error": f"no entry {entry_id}"}
        return {"pinned": entry_id, "slug": slug}


def http_run_kwargs(host: str, port: int) -> dict[str, Any]:
    """Keyword arguments for serving over streamable-HTTP.

    Separate from `serve_http` so the security-relevant parts can be asserted
    without binding a socket.
    """
    return {
        "host": host,
        "port": port,
        "stateless_http": True,
        # Host-header validation, which the SDK enables by default to stop DNS
        # rebinding. It is not access control and is not claimed as any - the
        # boundary is the network saddle puts the container on. But binding to
        # a gateway address means that address must be allowlisted or every
        # request is refused.
        "transport_security": TransportSecuritySettings(
            allowed_hosts=[f"{host}:{port}"],
            allowed_origins=[],
        ),
    }


def serve_http(host: str, port: int, project: str | None) -> None:
    """Serve over streamable-HTTP. Blocks until the server stops."""
    configure(project=project, http=True)
    mcp.run("streamable-http", **http_run_kwargs(host, port))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
