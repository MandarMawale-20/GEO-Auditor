"""Crawl discovery policies: host scope, canonical validation, budgets."""
from __future__ import annotations

import asyncio

import pytest

from app.models import FetchResult
from app.services import crawl
from app.services.crawl import (
    CrawlCandidate,
    discover_pages,
    fetch_candidate,
    normalize_candidate_url,
    site_hosts_from_url,
)
from app.services.html_utils import LinkRef, ParsedPage
from app.services.robots import RobotsRules

BASE = "https://example.com"


def _parsed(*links: tuple[str, int]) -> ParsedPage:
    return ParsedPage(
        raw_html="",
        visible_text="Example",
        word_count=100,
        links=[LinkRef(href=href, text=href, weight=weight) for href, weight in links],
    )


def test_apex_and_www_are_equivalent() -> None:
    hosts = site_hosts_from_url(BASE)
    url, reason = normalize_candidate_url(BASE, "https://www.example.com/pricing", hosts)
    assert reason is None
    assert url == "https://www.example.com/pricing"


def test_subdomains_excluded_by_default() -> None:
    hosts = site_hosts_from_url(BASE)
    url, reason = normalize_candidate_url(BASE, "https://blog.example.com/post", hosts)
    assert url is None
    assert reason == "off_host"

    hosts_with_subdomains = site_hosts_from_url(BASE, include_subdomains=True)
    url, reason = normalize_candidate_url(
        BASE, "https://blog.example.com/post", hosts_with_subdomains
    )
    assert reason is None
    assert url == "https://blog.example.com/post"


def test_offhost_assets_queries_and_utility_paths_are_skipped() -> None:
    hosts = site_hosts_from_url(BASE)
    cases = [
        ("https://evil.example.net/", "off_host"),
        ("/brochure.pdf", "asset"),
        ("/pricing?plan=pro", "query"),
        ("/login", "utility"),
        ("/", "homepage"),
        ("mailto:hi@example.com", "non_http"),
    ]
    for href, expected in cases:
        url, reason = normalize_candidate_url(BASE, href, hosts)
        assert url is None
        assert reason == expected


def test_trailing_slashes_are_normalized() -> None:
    hosts = site_hosts_from_url(BASE)
    url, reason = normalize_candidate_url(BASE, "/pricing/", hosts)
    assert reason is None
    assert url == "https://example.com/pricing"


def test_homepage_with_query_is_still_recognized_as_homepage() -> None:
    hosts = site_hosts_from_url(BASE)
    url, reason = normalize_candidate_url(
        f"{BASE}/?utm_source=newsletter", BASE, hosts
    )
    assert url is None
    assert reason == "homepage"


def test_discovery_is_deterministic_and_priority_ordered() -> None:
    parsed = _parsed(
        ("/pricing", 3),
        ("/about", 3),
        ("/blog", 1),
        ("/login", 3),
        ("https://blog.example.com/post", 1),
    )
    plan = discover_pages(
        parsed,
        BASE,
        ["https://example.com/customers"],
        RobotsRules(),
        max_pages=3,
    )
    assert plan.selected[0].url == BASE
    assert plan.selected[0].role == "homepage"
    assert [candidate.url for candidate in plan.selected[1:]] == [
        "https://example.com/pricing",
        "https://example.com/about",
    ]
    assert plan.discovered_count == 4
    assert plan.skipped["utility"] >= 1
    assert plan.skipped["off_host"] >= 1


def test_discovery_respects_robots_paths() -> None:
    parsed = _parsed(("/pricing", 3), ("/private", 3))
    rules = RobotsRules(
        exists=True,
        allow_by_agent={"*": []},
        disallow_by_agent={"*": ["/private"]},
    )
    plan = discover_pages(parsed, BASE, [], rules, max_pages=5)
    assert [candidate.url for candidate in plan.selected[1:]] == [
        "https://example.com/pricing"
    ]
    assert plan.skipped["robots"] == 1


def test_single_page_plan_has_only_homepage() -> None:
    plan = discover_pages(_parsed(("/pricing", 3)), BASE, [], RobotsRules(), max_pages=1)
    assert [candidate.url for candidate in plan.selected] == [BASE]


@pytest.mark.asyncio
async def test_canonical_off_host_is_rejected(monkeypatch) -> None:
    html = (
        "<html><head><link rel='canonical' href='https://evil.example.net/'>"
        "</head><body>Hello</body></html>"
    )

    async def fake_fetch(url):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            ok=True,
            text=html,
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(crawl, "fetch_html", fake_fetch)
    page = await fetch_candidate(
        CrawlCandidate(url=f"{BASE}/pricing", score=1, reason="test"),
        site_hosts_from_url(BASE),
        remaining_seconds=5,
    )
    assert page.error == "canonical_off_host"


@pytest.mark.asyncio
async def test_same_site_canonical_is_accepted(monkeypatch) -> None:
    html = (
        "<html><head><link rel='canonical' href='/pricing'>"
        "</head><body>Hello pricing</body></html>"
    )

    async def fake_fetch(url):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            ok=True,
            text=html,
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(crawl, "fetch_html", fake_fetch)
    page = await fetch_candidate(
        CrawlCandidate(url=f"{BASE}/pricing", score=1, reason="test"),
        site_hosts_from_url(BASE),
        remaining_seconds=5,
    )
    assert page.ok
    assert page.parsed is not None
    assert page.parsed.canonical_url == "/pricing"


@pytest.mark.asyncio
async def test_x_robots_tag_header_is_merged_into_noindex_signal(monkeypatch) -> None:
    html = "<html><head><title>Pricing</title></head><body>Pricing</body></html>"

    async def fake_fetch(url):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            ok=True,
            text=html,
            headers={"content-type": "text/html", "x-robots-tag": "noindex, nofollow"},
        )

    monkeypatch.setattr(crawl, "fetch_html", fake_fetch)
    page = await fetch_candidate(
        CrawlCandidate(url=f"{BASE}/pricing", score=1, reason="test"),
        site_hosts_from_url(BASE),
        remaining_seconds=5,
    )
    assert page.ok
    assert "noindex" in page.parsed.meta_robots


@pytest.mark.asyncio
async def test_budget_exhaustion_is_distinct_from_timeout() -> None:
    page = await fetch_candidate(
        CrawlCandidate(url=f"{BASE}/pricing", score=1, reason="test"),
        site_hosts_from_url(BASE),
        remaining_seconds=0,
    )
    assert page.error == "audit_budget_exhausted"


@pytest.mark.asyncio
async def test_page_timeout_is_isolated(monkeypatch) -> None:
    class SlowSettings:
        crawl_page_timeout_seconds = 0.01

    async def slow_fetch(url):
        await asyncio.sleep(0.2)
        raise AssertionError("should have timed out")

    monkeypatch.setattr(crawl, "settings", SlowSettings())
    monkeypatch.setattr(crawl, "fetch_html", slow_fetch)
    page = await fetch_candidate(
        CrawlCandidate(url=f"{BASE}/pricing", score=1, reason="test"),
        site_hosts_from_url(BASE),
        remaining_seconds=1,
    )
    assert page.error == "page_timeout"


@pytest.mark.asyncio
async def test_budget_bound_timeout_is_labelled_budget_exhaustion(monkeypatch) -> None:
    class SlowSettings:
        crawl_page_timeout_seconds = 0.05

    async def slow_fetch(url):
        await asyncio.sleep(0.2)
        raise AssertionError("should have timed out")

    monkeypatch.setattr(crawl, "settings", SlowSettings())
    monkeypatch.setattr(crawl, "fetch_html", slow_fetch)
    page = await fetch_candidate(
        CrawlCandidate(url=f"{BASE}/pricing", score=1, reason="test"),
        site_hosts_from_url(BASE),
        remaining_seconds=0.01,
    )
    assert page.error == "audit_budget_exhausted"


@pytest.mark.asyncio
async def test_page_exception_is_isolated(monkeypatch) -> None:
    async def boom(url):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(crawl, "fetch_html", boom)
    page = await fetch_candidate(
        CrawlCandidate(url=f"{BASE}/pricing", score=1, reason="test"),
        site_hosts_from_url(BASE),
        remaining_seconds=1,
    )
    assert page.error is not None
    assert page.error.startswith("RuntimeError:")
