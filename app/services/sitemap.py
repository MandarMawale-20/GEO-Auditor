"""
Sitemap discovery and freshness extraction.

Looks up /sitemap.xml (or a sitemap URL declared in robots.txt), and
extracts the most recent <lastmod> value found, used by the Freshness
Signals category.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from app.services.fetcher import fetch_text_resource

_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.IGNORECASE)


@dataclass
class SitemapResult:
    exists: bool = False
    url: str | None = None
    fetch_error: str | None = None
    most_recent_lastmod: datetime | None = None
    url_count: int = 0


def _parse_iso_date(value: str) -> datetime | None:
    value = value.strip()
    try:
        # Handle trailing 'Z' which fromisoformat rejects on older Pythons.
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


async def get_sitemap_info(page_url: str, sitemap_hints: list[str] | None = None) -> SitemapResult:
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
        result = await fetch_text_resource(candidate_url)
        if result.ok and result.text:
            lastmods = [
                dt
                for raw in _LASTMOD_RE.findall(result.text)
                if (dt := _parse_iso_date(raw)) is not None
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
