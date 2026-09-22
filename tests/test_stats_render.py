"""Rendering is pure: no db marker, runs on CI."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from saddlebag.domain import (
    AccessSummary,
    Entry,
    EntryCounts,
    IngestRun,
    IngestTrigger,
    InjectionSummary,
    Kind,
    MemoryRun,
    MemoryTrigger,
    Origin,
    PipelineCounts,
)
from saddlebag.services import stats as st

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
WEEK_AGO = NOW - timedelta(days=30)

#: The `Stats` fields that are not sections, in order. Named here so the
#: drift guard below can say where the sections start.
META_FIELDS = ("project", "window", "now")


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
                sessions_read=9,
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


def test_sections_is_every_stats_section_and_nothing_else() -> None:
    """`SECTIONS` drives both `render` and `to_dict`.

    `render` already raises a KeyError for a section missing from
    `_RENDERERS`, but a section added to `Stats` and forgotten in
    `SECTIONS` raises nothing at all - it simply never renders and never
    appears in the JSON, which is the "silently vanishes" failure this
    repo keeps warning about for origins.
    """
    names = tuple(f.name for f in fields(st.Stats))
    assert names[: len(META_FIELDS)] == META_FIELDS
    assert st.SECTIONS == names[len(META_FIELDS) :]


@pytest.mark.parametrize(
    ("n", "d", "want"),
    [
        (37, 40, "92%"),  # floors: never overstates a rate
        (1, 40, "2%"),
        (0.29, 1, "29%"),  # the epsilon: 100 * 0.29 is 28.999999999999996
        (0, 40, "0%"),
        (40, 40, "100%"),
        (0, 0, "-"),  # a zero denominator is not a zero percent
        (7, 0, "-"),
    ],
)
def test_percentages(n: float, d: float, want: str) -> None:
    assert st._pct(n, d) == want


def test_a_zero_denominator_reaches_the_rendered_line_as_a_dash() -> None:
    s = _stats(
        injection=st.Injected(
            summary=InjectionSummary(
                first_at=WEEK_AGO,
                sessions=3,
                sessions_read=0,
                mean_rules=0.0,
                mean_notes=0.0,
                mean_tokens=940.0,
                injected=0,
                opened=0,
            ),
            budget_fraction=None,
        )
    )
    line = _line(st.render(s), "inject")
    assert "follow-through - (0/0)" in line
    # No knowledge base, so no budget clause at all rather than a zero.
    assert "budget" not in line
    # Under a thousand, an estimate is the count itself.
    assert "~940 tokens avg (est)" in line


def test_pipes_names_every_run_state_and_where_to_read_advisories() -> None:
    """The four spellings `_run_age` has, in one line.

    "never" (no row), "unknown" (a row that cannot say when it started),
    an age, and an age plus "(did not finish)" are four different
    statements, and a run that crashed must not read as one that ran.
    """
    s = _stats(
        pipelines=st.Pipelines(
            counts=PipelineCounts(
                transcript_sessions=155,
                transcript_subagents=288,
                last_transcript_run=None,
                last_memory_run=MemoryRun(
                    id=UUID(int=2),
                    owner_id=UUID(int=1),
                    project="demo",
                    trigger=MemoryTrigger.AUTO,
                    started_at=NOW - timedelta(hours=3),
                    finished_at=NOW - timedelta(hours=3),
                ),
                last_ingest_run=IngestRun(
                    id=UUID(int=3),
                    owner_id=UUID(int=1),
                    project="demo",
                    trigger=IngestTrigger.MANUAL,
                    started_at=NOW - timedelta(days=1),
                    finished_at=None,
                ),
                extracted_since=12,
            ),
            advisories=2,
        )
    )
    assert _line(st.render(s), "pipes") == (
        "pipes     transcripts 155 sessions 288 subagents, last never"
        " · memory last 3h ago · ingest last 1d ago (did not finish)"
        " · 2 advisories - run bag record status"
    )


def test_a_run_row_that_cannot_say_when_it_started_is_not_never() -> None:
    s = _stats(
        pipelines=st.Pipelines(
            counts=PipelineCounts(
                transcript_sessions=0,
                transcript_subagents=0,
                last_transcript_run=None,
                last_memory_run=MemoryRun(
                    id=UUID(int=2),
                    owner_id=UUID(int=1),
                    project="demo",
                    trigger=MemoryTrigger.AUTO,
                ),
                last_ingest_run=None,
                extracted_since=0,
            ),
            advisories=0,
        )
    )
    line = _line(st.render(s), "pipes")
    assert "memory last unknown" in line
    # No advisories, so no pointer to a command with nothing to show.
    assert "run bag record status" not in line


def test_a_long_title_is_cut_and_later_entries_line_up_under_the_first() -> None:
    long_title = "R" * 70
    s = _stats(
        recent=[
            Entry(
                id=UUID("01a0ac4c-9ad9-75f9-9819-2cca6840d456"),
                kind=Kind.NOTE,
                title=long_title,
                body="",
                owner_id=UUID(int=1),
                origin=Origin.AGENT,
                project="demo",
                created_at=NOW - timedelta(minutes=2),
            ),
            Entry(
                id=UUID("01a0ac4c-9ad9-75f9-9819-2cca6840d457"),
                kind=Kind.RULE,
                title="Short",
                body="",
                owner_id=UUID(int=1),
                origin=Origin.HUMAN,
                project="demo",
                created_at=NOW - timedelta(hours=5),
            ),
        ]
    )
    lines = st.render(s)
    first = _line(lines, "recent")
    second = lines[lines.index(first) + 1]
    assert f"{'R' * 57}..." in first
    assert len(long_title) > st.MAX_TITLE and "R" * 58 not in first
    assert second == (
        f"{' ' * st.LABEL_WIDTH}5h ago  rule"
        "  01a0ac4c-9ad9-75f9-9819-2cca6840d457  demo  Short  [human]"
    )


def test_nothing_written_yet_says_so() -> None:
    assert _line(st.render(_stats(recent=[])), "recent") == (
        "recent    nothing written yet"
    )


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
        "recall    7d: 48 reads in 9/14 sessions · hit 92% (37/40)"
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
                sessions_read=9,
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


def test_recent_lists_age_kind_the_whole_id_the_project_and_the_title() -> None:
    """The id is a handle, and the project says which store it came from.

    `cli._entry_id` parses a full UUID and refuses a prefix, and uuid7 is
    time-ordered, so an eight-character prefix is neither unique across a
    batch nor accepted by any command - it invited a reader to type it and
    fail. `recent_entries` is owner-wide, so the project is what explains a
    list of ten chunks from a file in another project.
    """
    lines = st.render(_stats())
    assert _line(lines, "recent") == (
        "recent    2m ago  note  01a0ac4c-9ad9-75f9-9819-2cca6840d456"
        "  demo  Reranker follow-up  [agent]"
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


@pytest.mark.parametrize(
    ("window", "want"),
    [
        (timedelta(days=7), "7d"),
        (timedelta(hours=6), "6h"),
        (timedelta(hours=36), "36h"),  # not the "1d" that window.days gives
        (timedelta(hours=24), "1d"),
        (timedelta(minutes=90), "90m"),
    ],
)
def test_the_window_phrase_is_the_unit_the_window_actually_divides_into(
    window: timedelta, want: str
) -> None:
    assert st._window_phrase(window) == want


def test_the_extract_line_names_the_same_window_the_recall_line_does() -> None:
    """One phrase, one function.

    `--window 6h` rendered `extract ... 0d +N entries` while the recall
    line correctly said `6h`, because this line printed `window.days` raw.
    """
    s = _stats(window=timedelta(hours=6))
    assert "6h +12 entries (sonnet)" in _line(st.render(s), "extract")
    assert "0d" not in _line(st.render(s), "extract")


def test_an_awaiting_count_that_hit_its_limit_says_so() -> None:
    """A capped count printed bare reports a backlog of thousands as 25."""
    s = _stats(
        extraction=st.Extraction(
            jobs={"done": 43}, awaiting=25, extracted=12, model="sonnet", capped=True
        )
    )
    assert "25+ waiting" in _line(st.render(s), "extract")


def test_the_session_ratio_is_drawn_from_the_injected_population() -> None:
    """Numerator and denominator both come from `injection_log`.

    The numerator used to be distinct `access_log` session ids, a different
    population: a cursor or opencode read carries no session id at all, so
    it could never enter the numerator while `bag hook context` still wrote
    an injection row into the denominator - and the pair could render
    `1/0`.
    """
    line = _line(st.render(_stats()), "recall")
    # 9, not the fixture's 11 distinct reading sessions: the two differ on
    # purpose, or this assertion passes under the old access_log numerator
    # too and guards nothing.
    assert "48 reads in 9/14 sessions" in line


def test_without_the_injection_section_there_is_no_ratio_to_draw() -> None:
    line = _line(st.render(_stats(injection=st.Unavailable("x"))), "recall")
    assert "48 reads in 11 sessions" in line


def test_every_section_failing_the_same_way_renders_no_lines_at_all() -> None:
    """A connection lost mid-collection is one problem, not seven.

    The per-section savepoint exists so one failure costs one line; without
    this guard the one failure mode that hits every section costs seven
    copies of the same driver message, and the banner is meant to fall back
    to its single line.
    """
    dead = {
        name: st.Unavailable("OperationalError: connection lost")
        for name in st.SECTIONS
    }
    s = _stats(**dead)
    assert st.render(s) == []
    assert st.collapsed_line(s) == (
        "stats     unavailable (OperationalError: connection lost)"
    )


def test_sections_failing_for_different_reasons_still_each_say_why() -> None:
    s = _stats(vectors=st.Unavailable("a"), recent=st.Unavailable("b"))
    assert st.total_failure(s) is None
    assert len(st.render(s)) >= len(st.SECTIONS)
