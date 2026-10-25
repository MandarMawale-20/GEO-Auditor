"""Priority roadmap ordering and unknown exclusion."""
from __future__ import annotations

from app.models import (
    AnswerabilityStatus,
    AuditSection,
    EffortLevel,
    Finding,
    ImpactLevel,
    Severity,
)
from app.scoring import (
    build_answerability_matrix,
    build_priority_roadmap,
    select_top_priorities,
)


def _finding(check_id: str, affected_pages: list[str], severity=Severity.WARNING) -> Finding:
    return Finding(
        category="test",
        severity=severity,
        title=check_id,
        why_it_matters="why",
        evidence="evidence",
        recommendation="fix",
        impact=ImpactLevel.HIGH,
        effort=EffortLevel.LOW,
        points_awarded=0,
        points_possible=10,
        check_id=check_id,
        affected_pages=affected_pages,
        pages_total=3,
        pages_failed=len(affected_pages),
    )


def test_homepage_affected_finding_sorts_before_internal_only() -> None:
    home = "https://example.com"
    internal = "https://example.com/pricing"
    roadmap = build_priority_roadmap(
        [
            _finding("internal.only", [internal]),
            _finding("affects.home", [home, internal]),
        ],
        homepage_url=home,
    )
    assert [f.check_id for f in roadmap] == ["affects.home", "internal.only"]


def test_check_id_is_a_deterministic_final_tiebreaker() -> None:
    roadmap = build_priority_roadmap(
        [
            _finding("b.check", ["https://example.com/pricing"]),
            _finding("a.check", ["https://example.com/about"]),
        ],
        homepage_url="https://example.com",
    )
    assert [f.check_id for f in roadmap] == ["a.check", "b.check"]


def test_unknown_info_findings_are_not_in_roadmap() -> None:
    roadmap = build_priority_roadmap(
        [
            _finding("unknown.check", [], severity=Severity.INFO),
            _finding("warning.check", [], severity=Severity.WARNING),
        ],
        homepage_url="https://example.com",
    )
    assert [f.check_id for f in roadmap] == ["warning.check"]


def test_top_priorities_uses_same_order() -> None:
    home = "https://example.com"
    findings = [
        _finding("internal.only", ["https://example.com/pricing"]),
        _finding("affects.home", [home]),
        _finding("third", ["https://example.com/about"]),
    ]
    top = select_top_priorities(findings, limit=2, homepage_url=home)
    assert [f.check_id for f in top][0] == "affects.home"
    assert len(top) == 2


def test_unknown_query_dimension_is_uncertain_not_a_failure() -> None:
    section = AuditSection(
        key="query_coverage",
        label="Query Coverage",
        group="Value and Freshness",
        points_possible=15,
        findings=[
            _finding("query.what", [], severity=Severity.INFO),
        ],
    )
    section.findings[0].dimension = "what"
    section.findings[0].evidence = "query engine unavailable"

    rows = build_answerability_matrix([section], "")
    what = next(row for row in rows if row.question == "What does this company do?")
    assert what.status == AnswerabilityStatus.PARTIAL
    assert "Not evaluated" in what.evidence
