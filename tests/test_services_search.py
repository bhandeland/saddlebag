import contextlib
from typing import Any, override
from uuid import UUID

import psycopg
import pytest

from saddlebag.backends.postgres.migrate import migrate
from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.domain import Entry, Hit, Kind, Match, Principal, Query, new_id
from saddlebag.embed import Embedder, EmbedderUnavailable
from saddlebag.services import search
from saddlebag.services.search import find
from saddlebag.services.write import remember

pytestmark = pytest.mark.db


@pytest.fixture
def store(conn: psycopg.Connection[Any]) -> PostgresStore:
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store: PostgresStore) -> Principal:
    return store.ensure_principal("brandon")


def test_find_delegates_to_the_store(store: PostgresStore, owner: Principal) -> None:
    remember(store, owner.id, title="Postgres", body="tune work_mem")
    hits = find(store, owner.id, Query(text="work_mem"))
    assert [h.entry.title for h in hits] == ["Postgres"]


def test_find_caps_an_absurd_limit(store: PostgresStore, owner: Principal) -> None:
    for i in range(3):
        remember(store, owner.id, title=f"E{i}", body="shared")
    hits = find(store, owner.id, Query(text="shared", limit=100000))
    assert len(hits) == 3


def test_find_rejects_a_nonpositive_limit(
    store: PostgresStore, owner: Principal
) -> None:
    remember(store, owner.id, title="E", body="shared")
    assert find(store, owner.id, Query(text="shared", limit=0)) == []


# Three tiers, tried in order, never blended.


class StubStore:
    """Records which tiers were called, and answers with what it was told to.

    A stub rather than a database, because the question here is tier
    ORDERING - a policy decision that lives in the service - and a real store
    would make the test about SQL instead.
    """

    def __init__(
        self,
        exact: list[Hit] | None = None,
        semantic: list[Hit] | None = None,
        fuzzy: list[Hit] | None = None,
    ) -> None:
        self._exact = exact or []
        self._semantic = semantic or []
        self._fuzzy = fuzzy or []
        self.called: list[str] = []

    def search(self, query: Query, owner_id: UUID) -> list[Hit]:
        self.called.append("exact")
        return list(self._exact)

    def semantic_search(
        self,
        query: Query,
        owner_id: UUID,
        vector: list[float],
        model: str,
        threshold: float,
    ) -> list[Hit]:
        self.called.append("semantic")
        return list(self._semantic)

    def fuzzy_search(self, query: Query, owner_id: UUID, threshold: float) -> list[Hit]:
        self.called.append("fuzzy")
        return list(self._fuzzy)

    def log_access(self, record: Any) -> None:
        pass

    def transaction(self) -> Any:
        return contextlib.nullcontext()


class StubEmbedder:
    name = "stub"
    dim = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def _hit(match: Match = Match.EXACT) -> Hit:
    e = Entry(id=new_id(), kind=Kind.NOTE, title="t", body="b", owner_id=new_id())
    return Hit(entry=e, rank=1.0, snippet="s", match=match)


def test_exact_results_stop_the_chain() -> None:
    store = StubStore(exact=[_hit()], semantic=[_hit(Match.SEMANTIC)])

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact"]
    assert hits[0].match is Match.EXACT


def test_semantic_runs_only_when_exact_is_empty() -> None:
    store = StubStore(semantic=[_hit(Match.SEMANTIC)], fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact", "semantic"]
    assert [h.match for h in hits] == [Match.SEMANTIC]


def test_fuzzy_runs_only_when_both_above_are_empty() -> None:
    store = StubStore(fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=StubEmbedder())

    assert store.called == ["exact", "semantic", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_no_embedder_degrades_to_two_tiers() -> None:
    # The documented degradation: without the optional dependency installed,
    # search still works and simply skips the middle tier. It must not error.
    store = StubStore(fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="q"), embedder=None)

    assert store.called == ["exact", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_omitting_the_embedder_builds_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentinel's whole reason for existing, and the half no test pinned.

    An omitted `embedder` is not the same as `embedder=None`: None says
    "skip the semantic tier", while saying nothing says "build the shared
    embedder if and when that tier is reached". Collapsing the two reads as
    a harmless simplification and silently costs every frontend the middle
    tier - none of them pass an embedder.
    """
    built: list[str] = []

    def fake_shared(model: str) -> StubEmbedder:
        built.append(model)
        return StubEmbedder()

    monkeypatch.setattr(search, "shared_embedder", fake_shared)
    store = StubStore(semantic=[_hit(Match.SEMANTIC)])

    hits = find(store, new_id(), Query(text="q"), embed_model="some-model")

    assert built == ["some-model"], "the shared embedder was never built"
    assert store.called == ["exact", "semantic"]
    assert [h.match for h in hits] == [Match.SEMANTIC]


def test_the_shared_embedder_is_not_built_when_the_exact_tier_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing one imports fastembed and can download ~130MB, so the
    tier that never runs must never pay for it."""
    built: list[str] = []

    def fake_shared_embedder(model: str) -> StubEmbedder:
        built.append(model)
        return StubEmbedder()

    monkeypatch.setattr(search, "shared_embedder", fake_shared_embedder)
    store = StubStore(exact=[_hit(Match.EXACT)])

    find(store, new_id(), Query(text="q"))

    assert built == []


def test_an_embedder_that_raises_degrades_rather_than_failing_the_search() -> None:
    # A missing model file must cost the semantic tier, not the search. The
    # user asked a question; two tiers can still answer it. An embedder that
    # raises never reaches store.semantic_search, so "semantic" never lands
    # in store.called - only ["exact", "fuzzy"] is a correct outcome here.
    class Broken(StubEmbedder):
        @override
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("no model")

    store = StubStore(fuzzy=[_hit(Match.FUZZY)])
    hits = find(store, new_id(), Query(text="q"), embedder=Broken())

    assert store.called == ["exact", "fuzzy"]
    assert [h.match for h in hits] == [Match.FUZZY]


def test_the_semantic_tier_embeds_the_query_with_its_models_instruction() -> None:
    """Passages are embedded bare and queries are not, for models trained
    that way. The prefix is chosen by the embedder's name, so the stub
    borrows a real one."""
    seen: list[str] = []

    class Recording(StubEmbedder):
        name = "BAAI/bge-base-en-v1.5"

        @override
        def embed(self, texts: list[str]) -> list[list[float]]:
            seen.extend(texts)
            return super().embed(texts)

    find(StubStore(), new_id(), Query(text="q"), embedder=Recording())

    assert seen == ["Represent this sentence for searching relevant passages: q"]


def test_empty_query_text_skips_both_fallbacks() -> None:
    # A listing query - no text, just filters. There is nothing to be
    # approximately like.
    store = StubStore(semantic=[_hit(Match.SEMANTIC)], fuzzy=[_hit(Match.FUZZY)])

    hits = find(store, new_id(), Query(text="  "), embedder=StubEmbedder())

    assert store.called == ["exact"]
    assert hits == []


def test_an_exact_match_never_constructs_an_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cost of building the local embedder is a model download on a cold
    # machine and an ONNX session on a warm one, and an exact match needs
    # neither. Blowing up in load_embedder is the only way to assert that it
    # was not called anywhere down the chain.
    def explode(model_name: str) -> Embedder:
        raise AssertionError(f"built an embedder for {model_name!r}")

    monkeypatch.setattr("saddlebag.services.search.load_embedder", explode)
    monkeypatch.setattr("saddlebag.services.search._EMBEDDERS", {})
    store = StubStore(exact=[_hit()])

    hits = find(store, new_id(), Query(text="q"))

    assert [h.match for h in hits] == [Match.EXACT]


def test_the_semantic_tier_builds_the_shared_embedder_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def build(model_name: str) -> StubEmbedder:
        calls.append(model_name)
        return StubEmbedder()

    monkeypatch.setattr("saddlebag.services.search.load_embedder", build)
    monkeypatch.setattr("saddlebag.services.search._EMBEDDERS", {})
    store = StubStore(semantic=[_hit(Match.SEMANTIC)])

    for _ in range(3):
        find(store, new_id(), Query(text="q"), embed_model="m")

    assert calls == ["m"]


def test_a_raising_embedder_constructor_costs_the_tier_and_not_the_search(
    store: PostgresStore, owner: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe is a real inference call, and it is outside the old try.

    shared_embedder caught EmbedderUnavailable only. A LocalEmbedder that
    imports fine and then fails while measuring its own dimension raises
    something else, which escaped the tier and crashed the search - the
    exact opposite of what _semantic's docstring promises.
    """
    monkeypatch.setattr("saddlebag.services.search._EMBEDDERS", {})

    def explode(name: str) -> Embedder:
        raise RuntimeError("onnxruntime session failed")

    monkeypatch.setattr("saddlebag.services.search.load_embedder", explode)

    remember(store, owner.id, title="config command", body="body")
    hits = find(store, owner.id, Query(text="zzzz nothing", limit=5))
    assert hits == []  # degraded to two tiers, did not raise


def test_an_unavailable_embedder_is_a_none_and_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The policy the frontends used to each restate: search degrades, and it
    # degrades once. Re-attempting the fastembed import on every search would
    # repay the failure without ever changing the answer.
    calls: list[str] = []

    def unavailable(model_name: str) -> Embedder:
        calls.append(model_name)
        raise EmbedderUnavailable("no extra")

    monkeypatch.setattr("saddlebag.services.search.load_embedder", unavailable)
    monkeypatch.setattr("saddlebag.services.search._EMBEDDERS", {})
    store = StubStore(fuzzy=[_hit(Match.FUZZY)])

    for _ in range(3):
        hits = find(store, new_id(), Query(text="q"), embed_model="m")

    assert calls == ["m"]
    assert [h.match for h in hits] == [Match.FUZZY]
