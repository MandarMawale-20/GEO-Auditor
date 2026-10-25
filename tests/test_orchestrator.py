"""Orchestrator behavior: single-page parity, multipage crawl, isolation."""
from __future__ import annotations

import base64
import gzip
import json

import pytest

import app.orchestrator as orchestrator
import app.services.crawl as crawl
from app.checks import build_check
from app.models import AuditSection, AuditReport, CheckStatus, FetchResult
from app.services.robots import RobotsRules
from app.services.sitemap import SitemapResult

HOME = "https://example.com"
PRICING = "https://example.com/pricing"

HOME_HTML = """<!doctype html>
<html><head>
<title>Example</title>
<link rel="canonical" href="https://example.com/">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Example",
 "url":"https://example.com","logo":"https://example.com/logo.png",
 "description":"Example helps teams automate reports",
 "sameAs":["https://www.linkedin.com/company/example"]}
</script></head>
<body>
<nav><a href="/pricing">Pricing</a><a href="/about">About</a></nav>
<main>
<h1>Example</h1>
<h2>What is Example?</h2>
<p>Example is a software platform that helps growing teams automate their reporting workflow and save ten hours every single week while reducing manual errors.</p>
<p>Teams use Example to save 30 percent on reporting costs and ship reliable results faster.</p>
<table><tr><th>Plan</th><th>Price</th></tr><tr><td>Starter</td><td>$10</td></tr></table>
<ul><li>10 hours saved</li><li>30 percent lower cost</li></ul>
</main></body></html>"""

PRICING_HTML = """<!doctype html>
<html><head><title>Pricing</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Example",
 "url":"https://example.com","logo":"https://example.com/logo.png",
 "description":"Example helps teams automate reports",
 "sameAs":["https://www.linkedin.com/company/example"]}
</script></head>
<body><main>
<h1>Pricing</h1>
<h2>How much does Example cost?</h2>
<p>Example costs ten dollars per month for the starter plan and includes reports, dashboards, and support for growing teams that need reliable results every week.</p>
<ul><li>10 dollars</li><li>30 percent savings</li></ul>
</main></body></html>"""


def _fetch_result(url: str, text: str | None, ok: bool = True) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200 if ok else 500,
        ok=ok,
        text=text,
        headers={"content-type": "text/html"},
        content_length=len(text or ""),
        bytes_observed=len(text or ""),
        bytes_retained=len(text or ""),
        elapsed_ms=10,
        error=None if ok else "Upstream failed.",
    )


async def _fake_robots(url: str) -> RobotsRules:
    return RobotsRules(exists=False)


async def _fake_sitemap(url: str, sitemap_hints=None, **kwargs) -> SitemapResult:
    return SitemapResult(
        exists=True,
        url=f"{url}/sitemap.xml",
        urls=[PRICING],
        url_count=1,
    )


async def _fake_trust(parsed, base_url) -> AuditSection:
    section = AuditSection(
        key="entity_completeness_trust",
        label="Entity Completeness and Trust Signals",
        group="Entity and Trust",
        points_possible=5,
    )
    section.add_check(
        build_check(
            "entity.trust_pages",
            status=CheckStatus.PASS,
            severity="pass",
            title="Trust pages found: About",
            points_awarded=5,
            evidence="about link resolves",
        )
    )
    return section


def _install_fakes(monkeypatch, homepage_ok=True, internal_ok=True, internal_html=PRICING_HTML):
    async def fake_fetch(url):
        if url.rstrip("/") == HOME:
            return _fetch_result(HOME, HOME_HTML if homepage_ok else None, ok=homepage_ok)
        if url.rstrip("/") == PRICING:
            return _fetch_result(PRICING, internal_html if internal_ok else None, ok=internal_ok)
        return _fetch_result(url, None, ok=False)

    monkeypatch.setattr(orchestrator, "fetch_html", fake_fetch)
    monkeypatch.setattr(crawl, "fetch_html", fake_fetch)
    monkeypatch.setattr(orchestrator, "get_robots_rules", _fake_robots)
    monkeypatch.setattr(orchestrator, "get_sitemap_info", _fake_sitemap)
    monkeypatch.setattr(orchestrator.entity_engine, "score_trust_pages", _fake_trust)


def _decode_share(encoded: str) -> dict:
    padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
    return json.loads(
        gzip.decompress(base64.urlsafe_b64decode(padded)).decode("utf-8")
    )


@pytest.mark.asyncio
async def test_single_page_report_contract(monkeypatch) -> None:
    _install_fakes(monkeypatch)
    report = await orchestrator.run_audit(HOME, max_pages=1)

    assert report.audit_scope == "single"
    assert report.scoring_version == "2"
    assert len(report.pages) == 1
    assert report.pages_audited == 1
    assert report.points_evaluated == 100
    assert report.partial is False
    assert len(report.sections) == 10
    assert round(sum(section.points_possible for section in report.sections), 2) == 100
    assert round(sum(section.points_awarded for section in report.sections), 2) == report.total_score
    assert report.llm_enhanced is False
    assert report.llm_error is None

    crawler = next(section for section in report.sections if section.key == "ai_crawler_accessibility")
    assert len(crawler.findings) == 1
    assert crawler.findings[0].title.startswith("No robots.txt found")

    dumped = report.model_dump()
    assert "checks" not in dumped["sections"][0]
    assert "sections" not in dumped["pages"][0]


@pytest.mark.asyncio
async def test_multipage_crawl_discovers_and_audits(monkeypatch) -> None:
    _install_fakes(monkeypatch)
    report = await orchestrator.run_audit(HOME, max_pages=2)

    assert report.audit_scope == "site"
    assert len(report.pages) == 2
    assert report.pages[0].role == "homepage"
    assert report.pages[1].role == "internal"
    assert report.pages[1].url == PRICING
    assert report.pages_discovered >= 1
    assert report.crawl_stats["attempted"] == 2
    assert report.crawl_stats["succeeded"] == 2
    assert report.points_evaluated == 100
    assert report.partial is False


@pytest.mark.asyncio
async def test_failed_internal_page_is_partial_and_unknown_checks_are_visible(monkeypatch) -> None:
    _install_fakes(monkeypatch, internal_ok=False)
    report = await orchestrator.run_audit(HOME, max_pages=2)

    assert report.partial is True
    assert len(report.pages) == 2
    failed = report.pages[1]
    assert failed.error is not None
    assert failed.sections
    assert failed.points_awarded == 0
    assert failed.points_possible > 0
    assert all(
        check.status == CheckStatus.UNKNOWN
        for section in failed.sections
        for check in section.checks
    )
    # Unknown page checks are excluded, not converted into failures.
    js = next(
        check
        for section in report.sections
        if section.key == "js_rendering_risk"
        for check in section.checks
    )
    assert js.status != CheckStatus.FAIL
    assert js.pages_unknown == 1


@pytest.mark.asyncio
async def test_internal_noindex_page_is_excluded_from_scoring(monkeypatch) -> None:
    noindex_html = PRICING_HTML.replace(
        "<head>", "<head><meta name='robots' content='noindex, follow'>"
    )
    _install_fakes(monkeypatch, internal_html=noindex_html)
    report = await orchestrator.run_audit(HOME, max_pages=2)

    excluded = report.pages[1]
    assert excluded.error == "noindex_excluded"
    assert excluded.indexable is False
    assert all(
        check.status == CheckStatus.UNKNOWN
        for section in excluded.sections
        for check in section.checks
    )
    assert report.partial is True
    assert report.crawl_stats["excluded"] == 1
    assert report.crawl_stats["failed"] == 0


@pytest.mark.asyncio
async def test_internal_none_robots_is_treated_as_noindex(monkeypatch) -> None:
    none_html = PRICING_HTML.replace(
        "<head>", "<head><meta name='robots' content='none'>"
    )
    _install_fakes(monkeypatch, internal_html=none_html)
    report = await orchestrator.run_audit(HOME, max_pages=2)

    excluded = report.pages[1]
    assert excluded.error == "noindex_excluded"
    assert excluded.indexable is False
    assert report.crawl_stats["excluded"] == 1
    assert report.crawl_stats["failed"] == 0


@pytest.mark.asyncio
async def test_crawler_summary_is_not_shown_when_engine_failed(monkeypatch) -> None:
    _install_fakes(monkeypatch)

    def fail_crawler(*args, **kwargs):
        raise RuntimeError("crawler engine unavailable")

    monkeypatch.setattr(
        orchestrator.technical_engine, "score_ai_crawler_accessibility", fail_crawler
    )
    report = await orchestrator.run_audit(HOME, max_pages=1)
    crawler = next(
        section for section in report.sections
        if section.key == "ai_crawler_accessibility"
    )

    assert all(check.status == CheckStatus.UNKNOWN for check in crawler.checks)
    assert crawler.points_awarded == 0
    assert all(
        "No robots.txt found" not in finding.title
        for finding in crawler.findings
    )


@pytest.mark.asyncio
async def test_unreachable_homepage_returns_zero_report(monkeypatch) -> None:
    _install_fakes(monkeypatch, homepage_ok=False)
    report = await orchestrator.run_audit(HOME, max_pages=1)

    assert report.total_score == 0
    assert report.grade == "F"
    assert report.pages_audited == 0
    assert report.partial is True
    assert len(report.sections) == 10
    assert all(
        check.status == CheckStatus.FAIL
        for section in report.sections
        for check in section.checks
    )


@pytest.mark.asyncio
async def test_share_payload_roundtrips_and_excludes_detail(monkeypatch) -> None:
    _install_fakes(monkeypatch)
    report = await orchestrator.run_audit(HOME, max_pages=2)

    from app.main import encode_report_for_share

    encoded = encode_report_for_share(report)
    from app.config import MAX_SHARE_PAYLOAD_BYTES

    assert len(encoded) <= MAX_SHARE_PAYLOAD_BYTES
    data = _decode_share(encoded)
    restored = AuditReport.model_validate(data)

    assert restored.total_score == report.total_score
    assert restored.scoring_version == "2"
    assert "checks" not in data["sections"][0]
    if data.get("pages"):
        assert "sections" not in data["pages"][0]

    # A tight cap drops presentation duplicates first, then page summaries,
    # while keeping scoring sections/findings intact.
    import app.main as main_module

    monkeypatch.setattr(main_module, "MAX_SHARE_PAYLOAD_BYTES", 1)
    compact = _decode_share(main_module.encode_report_for_share(report))
    assert compact["all_findings"] == []
    assert compact["priority_roadmap"] == []
    assert compact["pages"] == []
    assert compact["crawl_stats"] == {}
    assert compact["sections"] == [
        section.model_dump() for section in report.sections
    ]


def test_legacy_payload_still_validates() -> None:
    legacy = {
        "url": "https://example.com",
        "final_url": "https://example.com/",
        "generated_at": "2025-01-01T00:00:00+00:00",
        "total_score": 42,
        "max_score": 100,
        "grade": "D",
        "grade_label": "Weak AI visibility",
        "sections": [
            {
                "key": "quote_readiness",
                "label": "Quote Readiness",
                "group": "Content Extractability",
                "points_possible": 15,
                "points_awarded": 5,
                "findings": [],
            }
        ],
        "fetch_health": {"http_status": 200},
        "answerability_matrix": [],
        "top_priorities": [],
        "all_findings": [],
        "llm_enhanced": False,
        "llm_error": None,
        "audit_duration_ms": 10,
        "report_id": "legacy",
    }
    report = AuditReport.model_validate(legacy)
    assert report.total_score == 42
    assert report.scoring_version == "1"
    assert report.audit_scope == "single"
    assert report.pages == []
