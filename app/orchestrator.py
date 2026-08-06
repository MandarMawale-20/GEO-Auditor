"""
Orchestrator: the single entrypoint that runs a complete GEO audit.

Sequence:
  1. Fetch HTML, robots.txt, sitemap.xml concurrently.
  2. If the page is unreachable, short-circuit into a graceful "unreachable" report.
  3. Parse HTML once, run all four engines concurrently against the parsed page.
  4. Aggregate sections -> score -> grade -> answerability matrix -> priority roadmap.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from app.config import CATEGORY_META, SCORE_WEIGHTS, TOTAL_POSSIBLE_SCORE
from app.engines import content_engine, entity_engine, intelligence_engine, technical_engine
from app.models import AuditReport, AuditSection, EffortLevel, FetchHealth, Finding, ImpactLevel, Severity
from app.scoring import (
    build_answerability_matrix,
    build_priority_roadmap,
    collect_findings,
    compute_grade,
    compute_total_score,
    select_top_priorities,
)
from app.services.fetcher import fetch_html, normalize_url
from app.services.html_utils import parse_html
from app.services import llm_service
from app.services.robots import get_robots_rules
from app.services.sitemap import get_sitemap_info


def _unreachable_report(raw_url: str, url: str, error_message: str, start: float) -> AuditReport:
    """Build a complete, valid AuditReport for a site that could not be fetched."""
    report_id = uuid.uuid4().hex
    sections: list[AuditSection] = []
    for key, weight in SCORE_WEIGHTS.items():
        meta = CATEGORY_META[key]
        section = AuditSection(
            key=key, label=meta["label"], group=meta["group"], points_possible=weight, points_awarded=0
        )
        section.findings.append(
            Finding(
                category=key,
                severity=Severity.CRITICAL,
                title="Could not be evaluated — site unreachable",
                why_it_matters="No score can be computed because the page could not be fetched.",
                evidence=error_message,
                recommendation="Verify the URL is correct and the site is publicly reachable, then re-run the audit.",
                impact=ImpactLevel.HIGH,
                effort=EffortLevel.LOW,
                points_awarded=0,
                points_possible=weight,
            )
        )
        sections.append(section)

    findings = collect_findings(sections)
    duration_ms = (time.monotonic() - start) * 1000

    return AuditReport(
        url=raw_url,
        final_url=url,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_score=0,
        max_score=TOTAL_POSSIBLE_SCORE,
        grade="F",
        grade_label="Website Unreachable",
        report_id=report_id,
        sections=sections,
        fetch_health=FetchHealth(
            http_status=None,
            fetch_time_ms=None,
            ttfb_ms=None,
            redirect_count=0,
            html_size_bytes=None,
            reachable=False,
            error_message=error_message,
        ),
        answerability_matrix=[],
        top_priorities=findings[:1],
        all_findings=findings,
        llm_enhanced=False,
        llm_error=None,
        audit_duration_ms=round(duration_ms, 1),
    )


async def run_audit(raw_url: str) -> AuditReport:
    start = time.monotonic()
    url = normalize_url(raw_url)

    if not url:
        return _unreachable_report(raw_url, raw_url, "Empty or invalid URL provided.", start)

    # Step 1: fetch HTML, robots.txt, sitemap.xml concurrently.
    html_result, robots_rules = await asyncio.gather(
        fetch_html(url),
        get_robots_rules(url),
    )

    if not html_result.ok or not html_result.text:
        error_message = html_result.error or f"Site returned HTTP {html_result.status_code}."
        return _unreachable_report(raw_url, url, error_message, start)

    final_url = html_result.final_url or url
    sitemap_info = await get_sitemap_info(final_url, sitemap_hints=robots_rules.sitemap_urls)

    # Step 2: parse once, share across all engines.
    parsed = parse_html(html_result.text)

    # Step 3: run engines concurrently.
    (
        crawler_section,
        js_section,
        freshness_section,
        entity_data_section,
        entity_trust_section,
        quote_section,
        fact_density_section,
        media_section,
        query_coverage_section,
        citation_section,
    ) = await asyncio.gather(
        _to_coro(technical_engine.score_ai_crawler_accessibility(robots_rules)),
        _to_coro(technical_engine.score_js_rendering_risk(parsed)),
        _to_coro(technical_engine.score_freshness_signals(parsed, sitemap_info)),
        _to_coro(entity_engine.score_structured_entity_data(parsed)),
        entity_engine.score_entity_completeness_trust(parsed, final_url),
        _to_coro(content_engine.score_quote_readiness(parsed)),
        _to_coro(content_engine.score_fact_density(parsed)),
        _to_coro(content_engine.score_media_to_text_balance(parsed)),
        intelligence_engine.score_query_coverage(parsed, final_url),
        intelligence_engine.score_citation_snippet_quality(parsed, final_url),
    )

    sections = [
        crawler_section,
        js_section,
        entity_data_section,
        entity_trust_section,
        quote_section,
        fact_density_section,
        media_section,
        query_coverage_section,
        citation_section,
        freshness_section,
    ]

    total_score = compute_total_score(sections)
    grade, grade_label = compute_grade(total_score)
    all_findings = collect_findings(sections)
    top_priorities = select_top_priorities(all_findings, limit=3)
    _ = build_priority_roadmap(all_findings)  # available for template use via all_findings sort if needed
    answerability_matrix = build_answerability_matrix(sections, parsed.visible_text)

    llm_enhanced = any(s.llm_enhanced for s in sections)
    llm_error = None
    if not llm_service.is_available():
        llm_error = "GEMINI_API_KEY not configured — semantic checks used deterministic fallback scoring."

    duration_ms = (time.monotonic() - start) * 1000

    fetch_health = FetchHealth(
        http_status=html_result.status_code,
        fetch_time_ms=html_result.elapsed_ms,
        ttfb_ms=html_result.elapsed_ms,  # httpx does not expose TTFB separately from total time
        redirect_count=html_result.redirect_count,
        html_size_bytes=html_result.content_length,
        reachable=True,
        error_message=None,
    )

    report_id = uuid.uuid4().hex

    return AuditReport(
        url=raw_url,
        final_url=final_url,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_score=total_score,
        max_score=TOTAL_POSSIBLE_SCORE,
        grade=grade,
        grade_label=grade_label,
        sections=sections,
        fetch_health=fetch_health,
        answerability_matrix=answerability_matrix,
        top_priorities=top_priorities,
        all_findings=sorted(all_findings, key=lambda f: f.priority_score, reverse=True),
        llm_enhanced=llm_enhanced,
        llm_error=llm_error,
        audit_duration_ms=round(duration_ms, 1),
        report_id=report_id,
    )


async def _to_coro(value):
    """Wrap a plain (synchronous) return value so it can sit in an asyncio.gather() call."""
    return value
