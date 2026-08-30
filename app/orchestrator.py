"""Run a full GEO audit and assemble the final report."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from app.config import CATEGORY_META, SCORE_WEIGHTS, TOTAL_POSSIBLE_SCORE
from app.engines import content_engine, entity_engine, intelligence_engine, technical_engine
from app.models import (
    AuditReport,
    AuditSection,
    EffortLevel,
    FetchHealth,
    Finding,
    ImpactLevel,
    Severity,
)
from app.scoring import (
    build_answerability_matrix,
    build_priority_roadmap,
    collect_findings,
    compute_grade,
    compute_total_score,
    select_top_priorities,
)
from app.services.fetcher import fetch_html
from app.services.html_utils import parse_html
from app.services import llm_service
from app.services.robots import get_robots_rules
from app.services.sitemap import get_sitemap_info
from app.services.url_validator import normalize_url

logger = logging.getLogger("geo_auditor.orchestrator")


def _unreachable_report(raw_url: str, url: str, error_message: str, start: float) -> AuditReport:
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
                dimension=None,
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
        priority_roadmap=build_priority_roadmap(findings),
        llm_enhanced=False,
        llm_error=None,
        audit_duration_ms=round(duration_ms, 1),
    )


async def _run_engine_safely(coro, section_key: str, fallback_label: str) -> AuditSection:
    """Run an engine coroutine with error isolation.

    If the engine raises, log the error server-side and return a
    ``AuditSection`` with a CRITICAL finding — never propagate the
    exception to the caller.
    """
    meta = CATEGORY_META[section_key]
    points_possible = SCORE_WEIGHTS.get(section_key, 0)
    section = AuditSection(
        key=section_key,
        label=meta["label"],
        group=meta["group"],
        points_possible=points_possible,
    )
    try:
        result = await coro
        if result is None:
            section.findings.append(
                Finding(
                    category=section_key,
                    dimension=None,
                    severity=Severity.CRITICAL,
                    title=f"{fallback_label}: engine returned no result",
                    why_it_matters="The audit could not be completed for this category.",
                    evidence="Engine invocation returned None.",
                    recommendation="Re-run the audit. If the problem persists, report this as a bug.",
                    impact=ImpactLevel.HIGH,
                    effort=EffortLevel.LOW,
                    points_awarded=0,
                    points_possible=points_possible,
                )
            )
            section.points_awarded = 0
            return section
        return result
    except Exception as exc:  # noqa: BLE001 — intentional broad catch for isolation
        logger.error(
            "Engine failure in %s: %s", section_key, exc, exc_info=True
        )
        section.points_awarded = 0
        section.findings.append(
            Finding(
                category=section_key,
                dimension=None,
                severity=Severity.CRITICAL,
                title=f"{fallback_label}: engine failed",
                why_it_matters=(
                    "This category could not be scored because an internal error occurred. "
                    "The issue has been logged server-side."
                ),
                evidence=f"Engine raised: {type(exc).__name__}: {exc}",
                recommendation="Re-run the audit. If the problem persists, report this as a bug.",
                impact=ImpactLevel.HIGH,
                effort=EffortLevel.LOW,
                points_awarded=0,
                points_possible=points_possible,
            )
        )
        return section


async def run_audit(raw_url: str, llm_enabled: bool = False) -> AuditReport:
    """Run a full GEO audit.

    When ``llm_enabled`` is ``False`` (the default), the LLM semantic layer is
    bypassed entirely — Query Coverage and Citation Snippet Quality fall back
    to deterministic heuristics and no Groq API calls are made. This keeps
    the product free-to-run when no API key is configured.

    Each engine runs in an isolated wrapper so that a failure in one
    category does not prevent the remaining categories from being scored.
    """
    start = time.monotonic()
    url = normalize_url(raw_url)

    if not url:
        return _unreachable_report(raw_url, raw_url, "Empty or invalid URL provided.", start)

    # Fetch the page and supporting files together.
    html_result, robots_rules = await asyncio.gather(
        fetch_html(url),
        get_robots_rules(url),
    )

    if not html_result.ok or not html_result.text:
        error_message = html_result.error or f"Site returned HTTP {html_result.status_code}."
        return _unreachable_report(raw_url, url, error_message, start)

    final_url = html_result.final_url or url
    sitemap_info = await get_sitemap_info(final_url, sitemap_hints=robots_rules.sitemap_urls)

    # Parse once and reuse the result across all engines.
    parsed_document = parse_html(html_result.text)

    # Score every engine concurrently, with error isolation per engine.
    results = await asyncio.gather(
        _run_engine_safely(
            asyncio.to_thread(technical_engine.score_ai_crawler_accessibility, robots_rules),
            "ai_crawler_accessibility", "AI Crawler Accessibility",
        ),
        _run_engine_safely(
            asyncio.to_thread(technical_engine.score_js_rendering_risk, parsed_document),
            "js_rendering_risk", "JS Rendering Risk",
        ),
        _run_engine_safely(
            asyncio.to_thread(technical_engine.score_freshness_signals, parsed_document, sitemap_info),
            "freshness_signals", "Freshness Signals",
        ),
        _run_engine_safely(
            asyncio.to_thread(entity_engine.score_structured_entity_data, parsed_document),
            "structured_entity_data", "Structured Entity Data",
        ),
        _run_engine_safely(
            entity_engine.score_entity_completeness_trust(parsed_document, final_url),
            "entity_completeness_trust", "Entity Completeness and Trust",
        ),
        _run_engine_safely(
            asyncio.to_thread(content_engine.score_quote_readiness, parsed_document),
            "quote_readiness", "Quote Readiness",
        ),
        _run_engine_safely(
            asyncio.to_thread(content_engine.score_fact_density, parsed_document),
            "fact_density", "Fact Density",
        ),
        _run_engine_safely(
            asyncio.to_thread(content_engine.score_media_to_text_balance, parsed_document),
            "media_to_text_balance", "Media-to-Text Balance",
        ),
        _run_engine_safely(
            intelligence_engine.score_query_coverage(parsed_document, final_url, llm_enabled=llm_enabled),
            "query_coverage", "Query Coverage",
        ),
        _run_engine_safely(
            intelligence_engine.score_citation_snippet_quality(parsed_document, final_url, llm_enabled=llm_enabled),
            "ai_citation_snippet_quality", "AI Citation Snippet Quality",
        ),
    )

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
    ) = results

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
    priority_roadmap = build_priority_roadmap(all_findings)
    answerability_matrix = build_answerability_matrix(sections, parsed_document.visible_text)

    llm_enhanced = any(s.llm_enhanced for s in sections)
    llm_error = None
    if not llm_service.is_available():
        llm_error = "GROQ_API_KEY not configured — semantic checks used deterministic fallback scoring."

    duration_ms = (time.monotonic() - start) * 1000

    fetch_health = FetchHealth(
        http_status=html_result.status_code,
        fetch_time_ms=html_result.elapsed_ms,
        ttfb_ms=html_result.elapsed_ms,  # httpx does not expose TTFB separately from total time
        redirect_count=html_result.redirect_count,
        html_size_bytes=html_result.bytes_observed or html_result.content_length,
        reachable=True,
        error_message=None,
        bytes_observed=html_result.bytes_observed,
        truncated=html_result.truncated,
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
        priority_roadmap=priority_roadmap,
        llm_enhanced=llm_enhanced,
        llm_error=llm_error,
        audit_duration_ms=round(duration_ms, 1),
        report_id=report_id,
    )
