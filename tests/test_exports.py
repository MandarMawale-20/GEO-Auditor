"""PDF and standalone HTML exports include multipage summaries and detail."""
from __future__ import annotations

from app.models import (
    AuditReport,
    AuditSection,
    CheckResult,
    CheckStatus,
    FetchHealth,
    PageAudit,
    Severity,
)
from app.services.html_export import render
from app.services.pdf_service import generate_pdf_bytes


def _section() -> AuditSection:
    section = AuditSection(
        key="quote_readiness",
        label="Quote Readiness / Answer Extractability",
        group="Content Extractability",
        points_possible=15,
    )
    section.add_check(
        CheckResult(
            check_id="quote.question_headings",
            category="quote_readiness",
            status=CheckStatus.PASS,
            points_awarded=5,
            points_possible=5,
            severity=Severity.PASS,
            title="Found 2 question-style headings",
            why_it_matters="Questions are extractable.",
            evidence=["Example heading."],
            recommendation="No action needed.",
        )
    )
    return section


def _report() -> AuditReport:
    section = _section()
    page = PageAudit(
        url="https://example.com",
        final_url="https://example.com/",
        role="homepage",
        selected_reason="homepage (seed)",
        fetch_health=FetchHealth(http_status=200, reachable=True),
        points_awarded=5,
        points_possible=65,
        sections=[section],
        word_count=500,
    )
    return AuditReport(
        url="https://example.com",
        final_url="https://example.com/",
        generated_at="2026-01-01T00:00:00+00:00",
        total_score=5,
        max_score=100,
        grade="F",
        grade_label="High risk of AI invisibility",
        sections=[section],
        fetch_health=FetchHealth(http_status=200, reachable=True),
        answerability_matrix=[],
        top_priorities=[],
        all_findings=section.findings,
        priority_roadmap=[],
        llm_enhanced=False,
        audit_duration_ms=10,
        report_id="export-test",
        audit_scope="site",
        scoring_version="2",
        pages_discovered=4,
        pages_audited=1,
        pages=[page],
        partial=True,
        points_evaluated=65,
        crawl_stats={"discovered": 4, "attempted": 1, "succeeded": 1, "failed": 0},
    )


def test_pdf_is_generated() -> None:
    pdf = generate_pdf_bytes(_report())
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_html_export_includes_pages_and_scope() -> None:
    html = render(_report())
    assert "Audited Pages" in html
    assert "Per-Page Detail" in html
    assert "Site crawl" in html
    assert "Points evaluated" in html
    assert "https://example.com/" in html
    assert "Partial coverage" in html
