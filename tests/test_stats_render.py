"""Rendering is pure: no db marker, runs on CI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from saddlebag.domain import (
    AccessSummary,
    Entry,
    EntryCounts,
    InjectionSummary,
    Kind,
    Origin,
    PipelineCounts,
)
from saddlebag.services import stats as st

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
WEEK_AGO = NOW - timedelta(days=30)


def _stats(**kw: object) -> st.Stats:
    base: dict[str, object] = dict(
        project="demo",
        window=timedelta(days=7),
        now=NOW,
        store=EntryCounts(
            live=1204,
            project_live=612,
            by_kind={"rule": 41, "note": 1100, "doc": 63},
            by_origin={"human": 900, "extracted": 304},
            superseded=318,
            collections=9,
            projects=12,
        ),
        retrieval=AccessSummary(
            first_at=WEEK_AGO,
            reads=48,
            by_source={"cli": 40, "mcp": 8},
            by_op={"search": 40, "get": 6, "handoff": 2},
            searches=40,
            search_hits=37,
            tiers={"exact": 22, "semantic": 16, "fuzzy": 1, "none": 1},
            p50_ms=180,
            sessions=11,
        ),
        injection=st.Injected(
            summary=InjectionSummary(
                first_at=WEEK_AGO,
                sessions=14,
                mean_rules=22.0,
                mean_notes=8.0,
                mean_tokens=4100.0,
                injected=400,
                opened=24,
            ),
            budget_fraction=0.61,
        ),
        vectors=st.Vectors(embedded=1158, total=1204, model="BAAI/bge-small-en-v1.5"),
        extraction=st.Extraction(
            jobs={"done": 43}, awaiting=0, extracted=12, model="sonnet"
        ),
        pipelines=st.Pipelines(
            counts=PipelineCounts(
                transcript_sessions=155,
                transcript_subagents=288,
                last_transcript_run=None,
                last_memory_run=None,
                last_ingest_run=None,
                extracted_since=12,
            ),
            advisories=0,
        ),
        recent=[
            Entry(
                id=UUID("01a0ac4c-9ad9-75f9-9819-2cca6840d456"),
                kind=Kind.NOTE,
                title="Reranker follow-up",
                body="",
                owner_id=UUID(int=1),
                origin=Origin.AGENT,
                project="demo",
                created_at=NOW - timedelta(minutes=2),
            )
        ],
    )
    base.update(kw)
    return st.Stats(**base)  # type: ignore[arg-type]


def _line(lines: list[str], label: str) -> str:
    return next(line for line in lines if line.startswith(label))


def test_every_section_has_a_labelled_line() -> None:
    lines = st.render(_stats())
    for label in ("store", "recall", "inject", "vectors", "extract", "pipes", "recent"):
        assert _line(lines, label)


def test_store_line() -> None:
    assert _line(st.render(_stats()), "store") == (
        "store     1204 live (demo 612) · 41 rules · 9 kbs · 12 projects"
        " · 318 superseded"
    )


def test_recall_line_carries_rate_tiers_and_latency() -> None:
    line = _line(st.render(_stats()), "recall")
    assert line == (
        "recall    7d: 48 reads in 11/14 sessions · hit 92% (37/40)"
        " · exact 55% semantic 40% fuzzy 2% none 2% · p50 180ms"
    )


def test_inject_line_labels_the_estimate_and_follow_through() -> None:
    line = _line(st.render(_stats()), "inject")
    assert line == (
        "inject    7d: 14 sessions · 22 rules 8 notes avg · ~4.1k tokens avg (est)"
        " · budget 61% · follow-through 6% (24/400)"
    )


def test_a_percentage_floors_without_paying_for_binary_float_error() -> None:
    """0.61 renders 61% above; 0.29 is the fraction that proves the epsilon.

    Percentages floor, so a rate never overstates - but `100 * 0.29` is
    28.999999999999996, and flooring that gives 28%, which is a wrong
    number rather than a conservative one. Delete the epsilon in `_pct`
    and this is the test that goes red.
    """
    s = _stats(
        injection=st.Injected(
            summary=InjectionSummary(
                first_at=WEEK_AGO,
                sessions=14,
                mean_rules=22.0,
                mean_notes=8.0,
                mean_tokens=4100.0,
                injected=400,
                opened=24,
            ),
            budget_fraction=0.29,
        )
    )
    assert "· budget 29% ·" in _line(st.render(s), "inject")


def test_a_young_log_says_since_instead_of_the_window() -> None:
    young = NOW - timedelta(days=2)
    s = _stats(
        retrieval=AccessSummary(
            first_at=young,
            reads=1,
            by_source={"cli": 1},
            by_op={"search": 1},
            searches=1,
            search_hits=1,
            tiers={"exact": 1},
            p50_ms=5,
            sessions=1,
        )
    )
    assert _line(st.render(s), "recall").startswith("recall    since 2026-09-14:")


def test_no_log_rows_yet_says_so() -> None:
    s = _stats(
        retrieval=AccessSummary(
            first_at=None,
            reads=0,
            by_source={},
            by_op={},
            searches=0,
            search_hits=0,
            tiers={},
            p50_ms=None,
            sessions=0,
        )
    )
    assert _line(st.render(s), "recall") == "recall    no reads logged yet"


def test_an_unavailable_section_says_why_and_the_rest_render() -> None:
    lines = st.render(_stats(vectors=st.Unavailable("timeout")))
    assert _line(lines, "vectors") == "vectors   unavailable (timeout)"
    assert _line(lines, "store")


def test_recent_lists_age_kind_id_prefix_and_title() -> None:
    lines = st.render(_stats())
    assert _line(lines, "recent") == (
        "recent    2m ago  note  01a0ac4c  Reranker follow-up  [agent]"
    )


def test_no_em_dashes_anywhere() -> None:
    assert "—" not in "\n".join(st.render(_stats()))


def test_to_dict_has_the_same_keys_whether_or_not_sections_are_available() -> None:
    full = st.to_dict(_stats())
    broken = st.to_dict(
        _stats(
            **{
                k: st.Unavailable("x")
                for k in (
                    "store",
                    "retrieval",
                    "injection",
                    "vectors",
                    "extraction",
                    "pipelines",
                    "recent",
                )
            }
        )
    )
    assert full.keys() == broken.keys()
    assert broken["vectors"] is None
    assert broken["unavailable"]["vectors"] == "x"
