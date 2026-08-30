"""
Sitemap discovery and freshness extraction.

Looks up /sitemap.xml (or a sitemap URL declared in robots.txt), and
extracts the most recent <lastmod> value found, used by the Freshness
Signals category.

All sitemap URLs — whether discovered from robots.txt or constructed by
default — are validated through the centralized SSRF-safe HTTP client
before any request is made.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from app.services.http_client import safe_get_text
from app.utils import parse_iso_datetime

_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.IGNORECASE)


@dataclass
class SitemapResult:
    exists: bool = False
    url: str | None = None
    fetch_error: str | None = None
    most_recent_lastmod: datetime | None = None
    url_count: int = 0


async def get_sitemap_info(
    page_url: str, sitemap_hints: list[str] | None = None
) -> SitemapResult:
    """Try declared sitemap URLs first, then fall back to /sitemap.xml.

    Every candidate URL is validated by the centralized SSRF-safe client,
    including redirect destinations.
    """
    parsed = urlparse(page_url)
    if not parsed.scheme or not parsed.netloc:
        return SitemapResult(exists=False, fetch_error="Invalid base URL for sitemap lookup.")

    candidates = list(sitemap_hints or [])
    default_sitemap = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/sitemap.xml")
    if default_sitemap not in candidates:
        candidates.append(default_sitemap)

    last_error: str | None = None
    for candidate_url in candidates:
        result = await safe_get_text(candidate_url, allow_redirects=True, max_redirects=5)
        if result.ok and result.text:
            lastmods = [
                dt
                for raw in _LASTMOD_RE.findall(result.text)
                if (dt := parse_iso_datetime(raw)) is not None
            ]
            most_recent = max(lastmods) if lastmods else None
            url_count = result.text.lower().count("<url>")
            return SitemapResult(
                exists=True,
                url=candidate_url,
                most_recent_lastmod=most_recent,
                url_count=url_count,
            )
        last_error = result.error

    return SitemapResult(exists=False, fetch_error=last_error or "No sitemap found.")
