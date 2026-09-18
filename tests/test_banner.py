"""The one-line session banner. Pure: no db marker, runs on CI.

Rendered from an `Injection`, never from the block text, so what it says
is what the model was handed rather than a guess parsed back out of it.
"""

from __future__ import annotations

from saddlebag.services.context import Handoff, Injection, banner


def injection(**kw: object) -> Injection:
    base: dict[str, object] = dict(
        text="",
        project="demo",
        found=True,
        rules=0,
        notes=0,
        recording=False,
        handoff=None,
        entry_ids=(),
    )
    base.update(kw)
    return Injection(**base)  # type: ignore[arg-type]


def test_a_full_session_reads_left_to_right() -> None:
    line = banner(
        injection(
            rules=20,
            notes=3,
            recording=True,
            handoff=Handoff(topic="release", age="1h ago"),
        )
    )
    assert line == (
        "saddlebag · kb demo: 20 rules, 3 notes · recording on"
        " · handoff: release (1h ago)"
    )


def test_singulars_are_spelled() -> None:
    assert "1 rule, 1 note" in banner(injection(rules=1, notes=1))


def test_no_handoff_omits_the_segment() -> None:
    assert "handoff" not in banner(injection(rules=2))


def test_a_missing_knowledge_base_names_the_fix() -> None:
    """The likeliest reason a session gets no context, and the one the
    fail-soft hook has always swallowed. It gets a banner, not silence."""
    line = banner(injection(found=False))
    assert (
        line == "saddlebag · no knowledge base 'demo' (bag kb new demo) · recording off"
    )


def test_an_empty_knowledge_base_says_empty_not_zero_rules() -> None:
    assert "kb demo: empty" in banner(injection(found=True, rules=0, notes=0))


def test_the_banner_is_one_line() -> None:
    line = banner(
        injection(rules=1, notes=1, recording=True, handoff=Handoff("t", "2d ago"))
    )
    assert "\n" not in line


def test_stats_lines_follow_the_first_line_unchanged() -> None:
    got = injection(rules=2)
    plain = banner(got)
    assert banner(got, ["store     1 live", "recall    no reads logged yet"]) == (
        plain + "\nstore     1 live\nrecall    no reads logged yet"
    )


def test_no_stats_is_exactly_todays_banner() -> None:
    got = injection(rules=2)
    assert banner(got, None) == banner(got) == banner(got, [])
