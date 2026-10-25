"""Sitemap discovery, URL extraction, and freshness extraction.

Looks up ``/sitemap.xml`` (or a sitemap URL declared in robots.txt), extracts
page URLs (including one level of sitemap-index expansion), and finds the most
recent ``<lastmod>`` value used by the Freshness Signals category. All
candidate URLs are validated through the SSRF-safe HTTP client before any
request is made.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from app.services.http_client import safe_get_text
from app.utils import parse_iso_datetime

_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.IGNORECASE)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_MAX_CHILD_SITEMAPS = 3
_MAX_SITEMAP_URLS = 1000


@dataclass
class SitemapResult:
    exists: bool = False
    url: str | None = None
    fetch_error: str | None = None
    most_recent_lastmod: datetime | None = None
    url_count: int = 0
    urls: list[str] = field(default_factory=list)


async def _fetch_sitemap(url: str) -> tuple[list[str], datetime | None, str | None]:
    result = await safe_get_text(url, allow_redirects=True, max_redirects=5)
    if not result.ok or not result.text:
        return [], None, result.error or "Sitemap unavailable."

    locs = [urljoin(url, loc) for loc in _LOC_RE.findall(result.text)]
    lastmods = [
        dt
        for raw in _LASTMOD_RE.findall(result.text)
        if (dt := parse_iso_datetime(raw)) is not None
    ]
    most_recent = max(lastmods) if lastmods else None
    return locs, most_recent, None


async def get_sitemap_info(
    page_url: str, sitemap_hints: list[str] | None = None
) -> SitemapResult:
    """Try declared sitemap URLs first, then fall back to /sitemap.xml."""
    parsed = urlparse(page_url)
    if not parsed.scheme or not parsed.netloc:
        return SitemapResult(exists=False, fetch_error="Invalid base URL for sitemap lookup.")

    candidates = list(sitemap_hints or [])
    default_sitemap = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/sitemap.xml")
    if default_sitemap not in candidates:
        candidates.append(default_sitemap)

    last_error: str | None = None
    for candidate_url in candidates:
        locs, most_recent, error = await _fetch_sitemap(candidate_url)
        if error:
            last_error = error
            continue
        if not locs:
            last_error = "Sitemap contained no <loc> entries."
            continue

        urls: list[str] = []
        # A sitemap index points at other sitemaps; expand a bounded number.
        child_sitemaps = [loc for loc in locs if loc.lower().endswith((".xml", ".xml.gz"))]
        if child_sitemaps:
            for child in child_sitemaps[:_MAX_CHILD_SITEMAPS]:
                child_urls, child_lastmod, _err = await _fetch_sitemap(child)
                urls.extend(child_urls)
                if child_lastmod and (most_recent is None or child_lastmod > most_recent):
                    most_recent = child_lastmod
        else:
            urls.extend(locs)

        # Deduplicate while preserving order and cap the list.
        seen: set[str] = set()
        deduped: list[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
            if len(deduped) >= _MAX_SITEMAP_URLS:
                break

        return SitemapResult(
            exists=True,
            url=candidate_url,
            most_recent_lastmod=most_recent,
            url_count=len(deduped),
            urls=deduped,
        )

    return SitemapResult(exists=False, fetch_error=last_error or "No sitemap found.")
