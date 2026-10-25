"""Run a full GEO audit and assemble the final report."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from app.aggregation import aggregate_sections, unknown_check
from app.checks import CHECK_SPECS, Aggregation, checks_for_category
from app.config import CATEGORY_META, SCORE_WEIGHTS, TOTAL_POSSIBLE_SCORE, settings
from app.engines import content_engine, entity_engine, intelligence_engine, technical_engine
from app.models import (
    AuditReport,
    AuditSection,
    CheckResult,
    CheckStatus,
    EffortLevel,
    FetchHealth,
    Finding,
    ImpactLevel,
    PageAudit,
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
from app.services import llm_service
from app.services.crawl import (
    CrawlPlan,
    FetchedPage,
    discover_pages,
    fetch_pages,
    site_hosts_from_url,
)
from app.services.fetcher import fetch_html
from app.services.html_utils import ParsedPage, parse_html
from app.services.robots import RobotsRules, get_robots_rules
from app.services.sitemap import SitemapResult, get_sitemap_info
from app.services.url_validator import normalize_url

logger = logging.getLogger("geo_auditor.orchestrator")

SCORING_VERSION = "2"

# Categories that are evaluated per page. Site-level categories
# (crawler access, query corpus, trust pages) are handled once.
PAGE_SCOPED_CATEGORIES: tuple[str, ...] = tuple(
    category
    for category in SCORE_WEIGHTS
    if any(
        spec.aggregation not in (Aggregation.SITE_SINGLETON, Aggregation.SITE_CORPUS)
        for spec in checks_for_category(category)
    )
)

PAGE_SCOPED_POINTS: float = round(
    sum(
        spec.points
        for spec in CHECK_SPECS.values()
        if spec.aggregation not in (Aggregation.SITE_SINGLETON, Aggregation.SITE_CORPUS)
    ),
    2,
)

# Robots tokens are comma/space separated. ``none`` is the legacy shorthand
# for ``noindex, nofollow`` and must be treated like an explicit noindex.
_NOINDEX_RE = re.compile(r"(?:^|[\s,;:])(?:noindex|none)(?=$|[\s,;:])", re.IGNORECASE)


def _is_noindex(value: str | None) -> bool:
    return bool(value and _NOINDEX_RE.search(value))


def _health_from_result(result) -> FetchHealth:
    return FetchHealth(
        http_status=result.status_code,
        fetch_time_ms=result.elapsed_ms,
        # httpx doesn't expose TTFB separately from total fetch time.
        ttfb_ms=result.elapsed_ms,
        redirect_count=result.redirect_count,
        html_size_bytes=result.bytes_observed or result.content_length,
        reachable=bool(result.ok),
        error_message=result.error,
        bytes_observed=result.bytes_observed,
        truncated=result.truncated,
    )


def _unreachable_report(raw_url: str, url: str, error_message: str, start: float) -> AuditReport:
    report_id = uuid.uuid4().hex
    sections: list[AuditSection] = []
    for key, weight in SCORE_WEIGHTS.items():
        meta = CATEGORY_META[key]
        section = AuditSection(
            key=key,
            label=meta["label"],
            group=meta["group"],
            points_possible=weight,
        )
        section.add_check(
            CheckResult(
                check_id=f"unreachable.{key}",
                category=key,
                status=CheckStatus.FAIL,
                points_awarded=0,
                points_possible=weight,
                severity=Severity.CRITICAL,
                title="Could not be evaluated — site unreachable",
                why_it_matters="No score can be computed because the page could not be fetched.",
                evidence=[error_message],
                recommendation=(
                    "Verify the URL is correct and the site is publicly reachable, then re-run the audit."
                ),
                impact=ImpactLevel.HIGH,
                effort=EffortLevel.LOW,
            )
        )
        section.points_evaluated = 0
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
        audit_scope="single",
        scoring_version=SCORING_VERSION,
        pages_discovered=0,
        pages_audited=0,
        pages=[],
        partial=True,
        points_evaluated=0,
        crawl_stats={},
    )


def _unknown_section(
    section_key: str,
    reason: str,
    *,
    page_url: str | None = None,
    page_scope: bool = False,
) -> AuditSection:
    """Build a section whose checks all failed to run."""
    meta = CATEGORY_META[section_key]
    specs = checks_for_category(section_key)
    if page_scope:
        specs = [
            spec for spec in specs
            if spec.aggregation not in (Aggregation.SITE_SINGLETON, Aggregation.SITE_CORPUS)
        ]
    section = AuditSection(
        key=section_key,
        label=meta["label"],
        group=meta["group"],
        points_possible=round(sum(spec.points for spec in specs), 2),
    )
    for spec in specs:
        section.add_check(unknown_check(spec, reason, page_url))
    section.points_evaluated = 0
    return section


async def _run_engine_safely(
    coro,
    section_key: str,
    fallback_label: str,
    *,
    page_url: str | None = None,
    page_scope: bool = False,
) -> AuditSection:
    """Run an engine with error isolation.

    A failure in one category becomes UNKNOWN checks for that category so the
    remaining categories still complete and the report can disclose missing
    evidence.
    """
    try:
        result = await coro
        if result is None:
            return _unknown_section(
                section_key,
                f"{fallback_label}: engine returned no result.",
                page_url=page_url,
                page_scope=page_scope,
            )
        return result
    except Exception as exc:  # noqa: BLE001 — report isolation
        logger.error("Engine failure in %s: %s", section_key, exc, exc_info=True)
        return _unknown_section(
            section_key,
            f"{fallback_label} failed: {type(exc).__name__}: {exc}",
            page_url=page_url,
            page_scope=page_scope,
        )


def _unknown_page_sections(reason: str, page_url: str) -> list[AuditSection]:
    return [
        _unknown_section(category, reason, page_url=page_url, page_scope=True)
        for category in PAGE_SCOPED_CATEGORIES
    ]


async def _audit_page(
    fetched: FetchedPage,
    sitemap: SitemapResult,
    *,
    use_llm_citation: bool,
) -> PageAudit:
    """Audit a single fetched page. Page failures never crash the audit."""
    page_url = fetched.url
    final_url = fetched.result.final_url or fetched.url
    health = _health_from_result(fetched.result)

    if not fetched.ok:
        reason = fetched.error or "Page could not be evaluated."
        sections = _unknown_page_sections(reason, page_url)
        return PageAudit(
            url=page_url,
            final_url=final_url,
            role=fetched.role,
            selected_reason=fetched.selected_reason,
            fetch_health=health,
            points_possible=PAGE_SCOPED_POINTS,
            sections=sections,
            word_count=0,
            indexable=False,
            error=reason,
        )

    parsed = fetched.parsed
    assert parsed is not None

    if fetched.role == "internal" and _is_noindex(parsed.meta_robots):
        sections = _unknown_page_sections(
            "Page is marked noindex and was excluded from scoring.", page_url
        )
        return PageAudit(
            url=page_url,
            final_url=final_url,
            role=fetched.role,
            selected_reason=fetched.selected_reason,
            fetch_health=health,
            points_possible=PAGE_SCOPED_POINTS,
            sections=sections,
            word_count=parsed.word_count,
            indexable=False,
            error="noindex_excluded",
        )

    results = await asyncio.gather(
        _run_engine_safely(
            asyncio.to_thread(technical_engine.score_js_rendering_risk, parsed, page_url),
            "js_rendering_risk", "JS Rendering Risk", page_url=page_url, page_scope=True,
        ),
        _run_engine_safely(
            asyncio.to_thread(technical_engine.score_freshness_signals, parsed, sitemap, page_url),
            "freshness_signals", "Freshness Signals", page_url=page_url, page_scope=True,
        ),
        _run_engine_safely(
            asyncio.to_thread(entity_engine.score_structured_entity_data, parsed, page_url),
            "structured_entity_data", "Structured Entity Data", page_url=page_url, page_scope=True,
        ),
        _run_engine_safely(
            asyncio.to_thread(entity_engine.score_entity_required_fields, parsed, page_url),
            "entity_completeness_trust", "Entity Completeness", page_url=page_url, page_scope=True,
        ),
        _run_engine_safely(
            asyncio.to_thread(content_engine.score_quote_readiness, parsed, page_url),
            "quote_readiness", "Quote Readiness", page_url=page_url, page_scope=True,
        ),
        _run_engine_safely(
            asyncio.to_thread(content_engine.score_fact_density, parsed, page_url),
            "fact_density", "Fact Density", page_url=page_url, page_scope=True,
        ),
        _run_engine_safely(
            asyncio.to_thread(content_engine.score_media_to_text_balance, parsed, page_url),
            "media_to_text_balance", "Media-to-Text Balance", page_url=page_url, page_scope=True,
        ),
        _run_engine_safely(
            intelligence_engine.score_citation_snippet_quality(
                parsed, final_url, llm_enabled=use_llm_citation, page_url=page_url
            ),
            "ai_citation_snippet_quality", "AI Citation Snippet Quality",
            page_url=page_url, page_scope=True,
        ),
    )

    sections = list(results)
    return PageAudit(
        url=page_url,
        final_url=final_url,
        role=fetched.role,
        selected_reason=fetched.selected_reason,
        fetch_health=health,
        points_awarded=round(sum(section.points_awarded for section in sections), 2),
        points_possible=round(sum(section.points_possible for section in sections), 2),
        sections=sections,
        word_count=parsed.word_count,
        indexable=True,
        error=None,
    )


def _build_site_corpus(
    fetches: list[FetchedPage],
    page_audits: list[PageAudit],
    limit: int = 6000,
) -> str:
    """Combine homepage + priority page text for site-level query coverage."""
    by_url = {audit.url: audit for audit in page_audits}
    homepage = next(
        (f for f in fetches if f.role == "homepage" and f.parsed is not None),
        None,
    )
    if homepage is None:
        return ""

    if len(fetches) <= 1 or len([f for f in fetches if f.parsed]) <= 1:
        # Single-page parity: use the full homepage text, matching the v1
        # fallback heuristics and answerability-matrix behavior.
        return homepage.parsed.visible_text

    entries: list[tuple[str, ParsedPage]] = [(homepage.url, homepage.parsed)]
    for fetched in fetches:
        if fetched.role == "homepage" or fetched.parsed is None:
            continue
        audit = by_url.get(fetched.url)
        if audit is not None and not audit.indexable:
            continue
        entries.append((fetched.url, fetched.parsed))

    if len(entries) == 1:
        # All internal pages were noindex/unsuccessful. Treat this as the
        # single-page case so query coverage still sees the full homepage,
        # rather than silently truncating the corpus at 6k characters.
        return entries[0][1].visible_text

    home_budget = int(limit * 0.6)
    remaining = limit - home_budget
    per_page = max(200, remaining // (len(entries) - 1))

    parts = [f"[PAGE: {entries[0][0]}]\n{entries[0][1].visible_text[:home_budget]}"]
    for url, parsed in entries[1:]:
        parts.append(f"[PAGE: {url}]\n{parsed.visible_text[:per_page]}")
    return "\n\n".join(parts)[:limit + 200]


async def run_audit(
    raw_url: str,
    llm_enabled: bool = False,
    max_pages: int = 1,
) -> AuditReport:
    """Run a single-page or multipage GEO audit.

    ``multi-page`` is opt-in: callers pass ``max_pages > 1``. The scoring
    contract is identical in both modes; aggregation reduces to the page
    result when only the homepage is audited.
    """
    start = time.monotonic()
    url = normalize_url(raw_url)

    if not url:
        return _unreachable_report(raw_url, raw_url, "Empty or invalid URL provided.", start)

    html_result, robots_result = await asyncio.gather(
        fetch_html(url),
        get_robots_rules(url),
        return_exceptions=True,
    )

    if isinstance(html_result, BaseException):
        logger.error("Homepage fetch failed unexpectedly: %s", html_result, exc_info=True)
        return _unreachable_report(
            raw_url, url, f"Homepage fetch failed: {type(html_result).__name__}", start
        )
    if not html_result.ok or not html_result.text:
        error_message = html_result.error or f"Site returned HTTP {html_result.status_code}."
        return _unreachable_report(raw_url, url, error_message, start)

    if isinstance(robots_result, BaseException):
        logger.error("robots.txt lookup failed unexpectedly: %s", robots_result, exc_info=True)
        robots_rules = RobotsRules(
            exists=False,
            fetch_error=f"{type(robots_result).__name__}: {robots_result}",
        )
    else:
        robots_rules = robots_result

    final_url = html_result.final_url or url
    try:
        sitemap_info = await get_sitemap_info(
            final_url,
            sitemap_hints=robots_rules.sitemap_urls,
            include_subdomains=settings.crawl_include_subdomains,
        )
    except Exception as exc:  # noqa: BLE001 — sitemap failure must not kill the audit
        logger.error("Sitemap lookup failed unexpectedly: %s", exc, exc_info=True)
        sitemap_info = SitemapResult(
            exists=False, fetch_error=f"{type(exc).__name__}: {exc}"
        )

    try:
        homepage_parsed = await asyncio.to_thread(parse_html, html_result.text)
    except Exception as exc:  # noqa: BLE001
        return _unreachable_report(
            raw_url, final_url, f"Could not parse the homepage: {exc}", start
        )

    requested_pages = max(1, int(max_pages))
    if not settings.crawl_enabled:
        requested_pages = 1
    requested_pages = min(requested_pages, settings.crawl_max_pages)
    audit_scope = "site" if requested_pages > 1 else "single"

    plan = CrawlPlan(homepage_url=final_url)
    internal_fetches: list[FetchedPage] = []
    if requested_pages > 1:
        try:
            plan = discover_pages(
                homepage_parsed,
                final_url,
                sitemap_info.urls,
                robots_rules,
                requested_pages,
                include_subdomains=settings.crawl_include_subdomains,
            )
            hosts = site_hosts_from_url(final_url, settings.crawl_include_subdomains)
            internal_fetches = await fetch_pages(
                plan.selected,
                hosts,
                settings.crawl_total_budget_seconds,
                settings.crawl_concurrency,
                homepage_url=final_url,
            )
        except Exception as exc:  # noqa: BLE001 — discovery must not kill the audit
            logger.error("Crawl discovery failed: %s", exc, exc_info=True)
            plan = CrawlPlan(homepage_url=final_url)
            internal_fetches = []

    homepage_fetch = FetchedPage(
        url=final_url,
        role="homepage",
        selected_reason="homepage (seed)",
        result=html_result,
        parsed=homepage_parsed,
    )
    fetches = [homepage_fetch] + internal_fetches

    # LLM citation budget: homepage plus the first internal pages only, and
    # only when the caller explicitly opted into LLM scoring.
    llm_urls: set[str] = set()
    if llm_enabled and llm_service.is_available():
        llm_urls.add(final_url)
        for fetched in internal_fetches:
            if len(llm_urls) >= settings.crawl_llm_max_pages:
                break
            if fetched.ok:
                llm_urls.add(fetched.url)

    page_audits = list(
        await asyncio.gather(
            *(
                _audit_page(
                    fetched,
                    sitemap_info,
                    use_llm_citation=fetched.url in llm_urls,
                )
                for fetched in fetches
            )
        )
    )

    crawler_section = await _run_engine_safely(
        asyncio.to_thread(technical_engine.score_ai_crawler_accessibility, robots_rules),
        "ai_crawler_accessibility",
        "AI Crawler Accessibility",
    )
    trust_section = await _run_engine_safely(
        entity_engine.score_trust_pages(homepage_parsed, final_url),
        "entity_completeness_trust",
        "Entity Completeness and Trust",
    )

    site_corpus = _build_site_corpus(fetches, page_audits)
    query_section = await _run_engine_safely(
        intelligence_engine.score_query_coverage_site(
            site_corpus,
            final_url,
            page_count=len([f for f in fetches if f.ok]),
            llm_enabled=llm_enabled,
        ),
        "query_coverage",
        "Query Coverage",
    )

    site_checks: list[CheckResult] = []
    for section in (crawler_section, trust_section, query_section):
        site_checks.extend(section.checks)

    page_checks: list[CheckResult] = []
    for audit in page_audits:
        for section in audit.sections:
            page_checks.extend(section.checks)

    page_weights = {
        audit.url: (
            settings.homepage_weight if audit.role == "homepage" else 1.0
        )
        for audit in page_audits
    }

    sections = aggregate_sections(
        site_checks=site_checks,
        page_checks=page_checks,
        page_weights=page_weights,
        homepage_url=final_url,
    )

    # Presentation parity with v1: when robots.txt is absent, show one summary
    # finding while the four crawler checks still own the 15 points.
    if not robots_rules.exists:
        for section in sections:
            if section.key == "ai_crawler_accessibility" and section.checks and all(
                check.status == CheckStatus.PASS for check in section.checks
            ):
                section.findings = [
                    Finding(
                        category="ai_crawler_accessibility",
                        severity=Severity.INFO,
                        title="No robots.txt found — AI crawlers are unrestricted by default",
                        why_it_matters=(
                            "Without a robots.txt, all crawlers are implicitly allowed. "
                            "An explicit robots.txt gives you control and signals intent."
                        ),
                        evidence="robots.txt returned a non-200 response or was unreachable.",
                        recommendation=(
                            "Add a robots.txt that explicitly allows OAI-SearchBot, "
                            "PerplexityBot, ClaudeBot, and Googlebot to make AI crawlability "
                            "unambiguous."
                        ),
                        copy_paste_fix=(
                            "User-agent: OAI-SearchBot\nAllow: /\n\n"
                            "User-agent: PerplexityBot\nAllow: /\n\n"
                            "User-agent: ClaudeBot\nAllow: /\n\n"
                            "User-agent: Googlebot\nAllow: /\n\n"
                            "Sitemap: https://yourdomain.com/sitemap.xml"
                        ),
                        impact=ImpactLevel.LOW,
                        effort=EffortLevel.LOW,
                        points_awarded=15,
                        points_possible=15,
                    )
                ]
                break

    total_score = compute_total_score(sections)
    grade, grade_label = compute_grade(total_score)
    all_findings = collect_findings(sections)
    all_findings.sort(
        key=lambda f: (-f.priority_score, f.check_id or f.title)
    )
    top_priorities = select_top_priorities(
        all_findings, limit=3, homepage_url=final_url
    )
    priority_roadmap = build_priority_roadmap(all_findings, homepage_url=final_url)
    answerability_matrix = build_answerability_matrix(sections, site_corpus)

    llm_enhanced = any(section.llm_enhanced for section in sections)
    llm_error = None
    if llm_enabled and not llm_service.is_available():
        llm_error = (
            "GROQ_API_KEY not configured — semantic checks used deterministic "
            "fallback scoring."
        )

    points_evaluated = round(sum(section.points_evaluated for section in sections), 2)
    # A noindex page was fetched successfully but intentionally excluded. It
    # must not be reported as a crawl failure alongside timeouts/HTTP errors.
    excluded = sum(1 for audit in page_audits if audit.error == "noindex_excluded")
    succeeded = sum(1 for audit in page_audits if audit.error is None)
    failed = sum(
        1
        for audit in page_audits
        if audit.error is not None and audit.error != "noindex_excluded"
    )
    partial = bool(failed) or bool(excluded) or points_evaluated < TOTAL_POSSIBLE_SCORE

    crawl_stats: dict[str, int] = {
        "discovered": plan.discovered_count,
        "selected": len(plan.selected) if requested_pages > 1 else 1,
        "attempted": len(page_audits),
        "succeeded": succeeded,
        "failed": failed,
        "excluded": excluded,
    }
    for reason, count in plan.skipped.items():
        crawl_stats[f"skipped_{reason}"] = count

    duration_ms = (time.monotonic() - start) * 1000
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
        fetch_health=_health_from_result(html_result),
        answerability_matrix=answerability_matrix,
        top_priorities=top_priorities,
        all_findings=all_findings,
        priority_roadmap=priority_roadmap,
        llm_enhanced=llm_enhanced,
        llm_error=llm_error,
        audit_duration_ms=round(duration_ms, 1),
        report_id=report_id,
        audit_scope=audit_scope,
        scoring_version=SCORING_VERSION,
        pages_discovered=plan.discovered_count,
        pages_audited=succeeded,
        pages=page_audits,
        partial=partial,
        points_evaluated=points_evaluated,
        crawl_stats=crawl_stats,
    )
