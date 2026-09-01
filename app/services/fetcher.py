"""Async HTTP fetcher.

High-level fetch helpers that route through the centralized, SSRF-safe HTTP
client (:mod:`app.services.http_client`). All outbound requests — main
HTML page, robots.txt, sitemap.xml — share SSRF validation, streaming body
reads, redirect validation, and connection pooling. Never raises; failures
are captured in :class:`~app.models.FetchResult`.
"""
from __future__ import annotations

from app.config import settings
from app.models import FetchResult
from app.services.http_client import safe_get_text


async def fetch_html(raw_url: str) -> FetchResult:
    """Fetch the main HTML page for a URL, capturing latency and size."""
    url = raw_url.strip()
    if not url:
        return FetchResult(url=raw_url, ok=False, error="Empty or invalid URL provided.")

    result = await safe_get_text(
        url,
        max_bytes=settings.max_html_bytes,
        allow_redirects=True,
        max_redirects=5,
    )

    if not result.ok and not result.error:
        result = result.model_copy(update={"error": f"Site returned HTTP {result.status_code}."})

    return result


async def fetch_text_resource(url: str) -> FetchResult:
    """Fetch a plain-text/XML resource such as robots.txt or sitemap.xml.

    Missing robots.txt / sitemap.xml is a normal, non-fatal case.
    """
    return await safe_get_text(
        url,
        max_bytes=settings.max_html_bytes,
        allow_redirects=True,
        max_redirects=5,
    )
