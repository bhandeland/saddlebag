"""Query hygiene lives here so every frontend gets it for free."""

from __future__ import annotations

import time
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Any, Final, Protocol, final
from uuid import UUID

from saddlebag.config import DEFAULT_FUZZY_THRESHOLD, DEFAULT_SEMANTIC_THRESHOLD
from saddlebag.domain import AccessRecord, Hit, Origin, Query
from saddlebag.embed import (
    DEFAULT_EMBED_MODEL,
    Embedder,
    EmbedderUnavailable,
    load_embedder,
    query_text,
)
from saddlebag.services import usage

MAX_LIMIT = 200


class SearchStore(Protocol):
    """The three store methods `find` actually reaches, and nothing else.

    `Store` is the portability seam and is deliberately wide; `find` uses
    three of its methods. Naming that subset here means the signature states
    the real dependency rather than the widest type that happens to satisfy
    it, and a test double covering the three tiers type-checks as itself
    instead of needing a cast that would let it drift from the protocol
    unnoticed. `PostgresStore` satisfies this structurally, so no caller
    changes.
    """

    def search(self, query: Query, owner_id: UUID) -> list[Hit]: ...
    def semantic_search(
        self,
        query: Query,
        owner_id: UUID,
        vector: list[float],
        model: str,
        threshold: float,
    ) -> list[Hit]: ...
    def fuzzy_search(
        self, query: Query, owner_id: UUID, threshold: float
    ) -> list[Hit]: ...
    def log_access(self, record: AccessRecord) -> None: ...
    def transaction(self) -> AbstractContextManager[Any]: ...


#: What a search returns when the caller did not ask for specific origins.
#: Handoffs are excluded: a project hands off dozens of times and every one of
#: them would otherwise sit on top of the results. Archived document chunks
#: are excluded for the neighbouring reason - they are the minority by count
#: (111 chunks against 211 at the time of writing) but three times the volume,
#: and what they contain is executed plan steps and source code that now lives
#: in src/.
#:
#: This list must gain any future origin, or that origin silently vanishes
#: from search. The alternative - an `exclude_origins` field on Query - avoids
#: that at the cost of a second overlapping filter in the store's SQL for one
#: caller. Chosen deliberately; if a sixth origin appears, look here.
DEFAULT_ORIGINS = [
    Origin.HUMAN,
    Origin.AGENT,
    Origin.EXTRACTED,
    Origin.INGESTED,
    Origin.IMPORTED,
]


@final
class _Unspecified:
    """The type of `_UNSPECIFIED`, and the only reason it is a class.

    A bare `object()` cannot be spelled in an annotation, so the sentinel
    had to be suppressed into the signature with a `type: ignore` - which
    also hid the fact that `find` then passed it on to `_semantic`, whose
    parameter said `Embedder | None` and never saw the third state. A named
    type makes the three states declarable, so the checker narrows them
    instead of being told to look away.
    """

    __slots__ = ()


#: The default value of `find(embedder=...)`, and not the same thing as None.
#:
#: None is a caller saying "there is no embedder, skip the semantic tier".
#: This sentinel is a caller saying nothing at all, which is every frontend,
#: and means "build the shared one if and when the semantic tier is reached".
#: Collapsing the two would make an explicit `embedder=None` silently grow an
#: embedder - including in the tests that pass it to assert the two-tier
#: degradation, where it would cost a 130MB model download.
_UNSPECIFIED: Final = _Unspecified()

#: Embedders memoised by model name.
#:
#: Constructing a LocalEmbedder imports fastembed, builds an ONNX session and
#: runs an inference call to probe the dimension - a fifth of a second warm,
#: and a ~130MB download cold. The MCP server is a long-lived process that
#: would otherwise pay that on every single recall call.
#:
#: A None value is cached too: an embedder that is unavailable stays
#: unavailable for the life of the process, and re-attempting the import on
#: every search would repay the failure without ever changing the answer.
#:
#: Tests never populate this - they pass an embedder (or None) explicitly, and
#: the sentinel above is what keeps those two paths from touching this cache.
_EMBEDDERS: dict[str, Embedder | None] = {}


def shared_embedder(model_name: str = DEFAULT_EMBED_MODEL) -> Embedder | None:
    """The embedder search uses, built at most once per model name.

    This is a policy decision and it lives here rather than in each frontend
    on purpose: "an unavailable embedder is a None, not an error" is a rule
    about how search degrades, and a rule enforced in the service is one that
    every frontend - the CLI, the MCP server, and whatever comes next - gets
    for free instead of re-deriving with its own try/except.

    Note what does NOT live here: `bag embed` calls `load_embedder`
    directly and fails loudly, because there an unavailable embedder is the
    command failing at its entire job rather than a tier quietly missing.
    """
    if model_name in _EMBEDDERS:
        return _EMBEDDERS[model_name]
    try:
        embedder: Embedder | None = load_embedder(model_name)
    except EmbedderUnavailable:
        embedder = None
    except Exception:
        # Deliberately broader than the declared exception. Constructing a
        # LocalEmbedder does not only import fastembed - it builds an ONNX
        # session and runs one real inference call to learn its dimension,
        # and that call fails in ways the embed layer never promised to
        # wrap: a corrupt download, a missing shared library, an OOM. Every
        # one of them means the same thing here, which is what this
        # function exists to say: no semantic tier, carry on with two.
        embedder = None
    _EMBEDDERS[model_name] = embedder
    return embedder


def _clamped(query: Query) -> Query:
    if query.limit <= MAX_LIMIT:
        return query
    return replace(query, limit=MAX_LIMIT)


def find(
    store: SearchStore,
    owner_id: UUID,
    query: Query,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    include_handoffs: bool = False,
    include_archived: bool = False,
    embedder: Embedder | None | _Unspecified = _UNSPECIFIED,
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    embed_model: str = DEFAULT_EMBED_MODEL,
    source: str | None = None,
    session_id: str | None = None,
    log_project: str | None = None,
) -> list[Hit]:
    """Three tiers, each running only when the one above returned nothing.

        exact full-text  ->  semantic  ->  trigram

    Fallback rather than blending. The durable reason is that a fused
    result set makes `Hit.match` unanswerable: it would hold hits from
    tiers whose rankings are not comparable - ts_rank, cosine distance and
    trigram similarity are three different numbers - and the caller would
    have to reason about which one each position came from. With three
    tiers that matters more, not less.

    What it is NOT is a precision trade, which is what this docstring
    claimed before anything measured it. Over 165 questions on 2026-09-15,
    reciprocal-rank fusion of the exact and semantic tiers scored 60.6%
    hit@1 against the cascade's 59.4% - discordant 3-1, p=0.625. Blending
    is inert here, not harmful. So do not defend the chain on the grounds
    that fusing would cost accuracy; defend it on the grounds above.

    Semantic sits above trigram because meaning beats spelling. A query that
    matches nothing lexically is far more often a different wording than a
    typo, and trigram remains what it always was: the typo net, tried last.

    `embedder` is optional and its absence is not an error. The local
    embedder is an optional dependency; without it search degrades to the two
    tiers it has always had. Same for an embedder that fails at query time -
    the user asked a question, and two tiers can still answer it.

    Left unspecified - which is what every frontend does - the embedder is
    the shared one for `embed_model`, and it is built inside the semantic
    tier rather than here. That ordering is the point: an exact match returns
    above, so a search the exact tier can answer never imports fastembed, and
    never triggers the model download that importing it can start. Pass an
    explicit embedder (or an explicit None) to override, as the tests do.

    Handoffs and archived document chunks are excluded from the default
    origins unless `include_handoffs` / `include_archived` is set, or the
    caller already named specific origins.

    `source` turns on usage logging (`services/usage`); None - every test,
    the eval script, every internal caller - logs nothing. `log_project` is
    the project the frontend resolved for the log row, which is not
    `query.project`: a search usually has no project filter.
    """
    if query.limit <= 0:
        return []
    if not query.origins:
        # An explicit origins list is the caller saying exactly what they
        # want, and is never overridden. Otherwise start from the default
        # allowlist and add back only what was asked for.
        origins = list(DEFAULT_ORIGINS)
        if include_handoffs:
            origins.append(Origin.HANDOFF)
        if include_archived:
            origins.append(Origin.ARCHIVED)
        query = replace(query, origins=origins)
    query = _clamped(query)

    started = time.perf_counter()
    text = (query.text or "").strip()
    hits = store.search(query, owner_id)
    if not hits and text:
        # No text means a listing query - filters only. There is nothing for
        # the fallbacks to be approximately like, so an empty exact result
        # only falls through to semantic/trigram when there was a query to
        # retry.
        hits = _semantic(
            store, owner_id, query, text, embedder, semantic_threshold, embed_model
        )
        if not hits:
            hits = store.fuzzy_search(query, owner_id, fuzzy_threshold)

    if source is not None:
        usage.log_access(
            store,
            AccessRecord(
                owner_id=owner_id,
                source=source,
                op="search",
                hits=len(hits),
                entry_ids=tuple(h.entry.id for h in hits),
                elapsed_ms=usage.elapsed_ms(started),
                project=log_project,
                session_id=session_id,
                # A listing query (no text) has neither a length nor a tier:
                # nothing was matched, only filtered.
                query_len=len(text) if text else None,
                tier=(str(hits[0].match) if hits else "none") if text else None,
            ),
        )
    return hits


def _semantic(
    store: SearchStore,
    owner_id: UUID,
    query: Query,
    text: str,
    embedder: Embedder | None | _Unspecified,
    threshold: float,
    embed_model: str,
) -> list[Hit]:
    """The middle tier, and everything that can go wrong with it.

    Kept separate so the degradation reads as one idea rather than three
    try/excepts inside the tier chain. Every failure here means the same
    thing to the caller: no semantic results, carry on to trigram.

    This is also the only place an embedder gets built for a search, and it
    is reached only after the exact tier came back empty - so the cost of
    constructing one is paid by the searches that can actually use it.
    """
    if isinstance(embedder, _Unspecified):
        embedder = shared_embedder(embed_model)
    if embedder is None:
        return []
    try:
        vectors = embedder.embed([query_text(embedder.name, text)])
    except Exception:
        # A missing model file, a corrupt download, an out-of-memory ONNX
        # session. All of them cost this tier and none of them should cost
        # the search. Deliberately broad: the failure modes of a model
        # runtime are not enumerable, and the response is the same for all.
        return []
    if not vectors:
        return []
    return store.semantic_search(query, owner_id, vectors[0], embedder.name, threshold)
