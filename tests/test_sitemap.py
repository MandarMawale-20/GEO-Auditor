"""Sitemap discovery stays same-site and resolves XML entities."""
from __future__ import annotations

import pytest

import app.services.sitemap as sitemap_module
from app.models import FetchResult
from app.services.sitemap import _fetch_sitemap, get_sitemap_info

BASE = "https://example.com"


@pytest.mark.asyncio
async def test_off_host_sitemap_hints_and_children_are_not_fetched(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str):
        calls.append(url)
        if url == f"{BASE}/custom.xml":
            return [
                f"{BASE}/child.xml",
                "https://evil.example.net/child.xml",
            ], None, None
        if url == f"{BASE}/child.xml":
            return [f"{BASE}/pricing"], None, None
        return [], None, "unexpected sitemap requested"

    monkeypatch.setattr(sitemap_module, "_fetch_sitemap", fake_fetch)

    info = await get_sitemap_info(
        BASE,
        sitemap_hints=[
            "https://evil.example.net/sitemap.xml",
            f"{BASE}/custom.xml",
        ],
    )

    assert info.exists is True
    assert info.urls == [f"{BASE}/pricing"]
    assert calls == [f"{BASE}/custom.xml", f"{BASE}/child.xml"]


@pytest.mark.asyncio
async def test_subdomain_sitemap_is_allowed_only_when_opted_in(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str):
        calls.append(url)
        return [f"https://docs.example.com/pricing"], None, None

    monkeypatch.setattr(sitemap_module, "_fetch_sitemap", fake_fetch)

    hint = "https://docs.example.com/sitemap.xml"
    excluded = await get_sitemap_info(BASE, sitemap_hints=[hint])
    assert excluded.url != hint
    assert hint not in calls

    calls.clear()
    included = await get_sitemap_info(
        BASE, sitemap_hints=[hint], include_subdomains=True
    )
    assert included.exists is True
    assert included.url == hint
    assert calls == [hint]


@pytest.mark.asyncio
async def test_sitemap_hint_count_is_capped(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str):
        calls.append(url)
        return [], None, "missing"

    monkeypatch.setattr(sitemap_module, "_fetch_sitemap", fake_fetch)
    hints = [f"{BASE}/sitemap-{i}.xml" for i in range(10)]
    info = await get_sitemap_info(BASE, sitemap_hints=hints)

    assert info.exists is False
    assert len(calls) == sitemap_module._MAX_SITEMAP_HINTS + 1
    assert f"{BASE}/sitemap.xml" in calls


@pytest.mark.asyncio
async def test_sitemap_loc_entities_are_unescaped(monkeypatch) -> None:
    async def fake_get_text(url: str, **kwargs):
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            ok=True,
            text=(
                "<urlset><url>"
                "<loc>https://example.com/search?a=1&amp;b=2</loc>"
                "</url></urlset>"
            ),
        )

    monkeypatch.setattr(sitemap_module, "safe_get_text", fake_get_text)
    locs, lastmod, error = await _fetch_sitemap(f"{BASE}/sitemap.xml")

    assert error is None
    assert lastmod is None
    assert locs == ["https://example.com/search?a=1&b=2"]
