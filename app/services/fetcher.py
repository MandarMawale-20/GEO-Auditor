"""
Async HTTP fetcher.

High-level fetch functions that route through the centralized, SSRF-safe
HTTP client (:mod:`app.services.http_client`).  Every outbound request —
whether for the main HTML page or a plain-text resource (robots.txt,
sitemap.xml) — goes through SSRF validation, streaming body reads, redirect
validation, and connection pooling.

Never raises — all failure modes are captured in :class:`~app.models.FetchResult`.
"""
from __future__ import annotations

from app.config import settings
from app.models import FetchResult
from app.services.http_client import safe_get_text


async def fetch_html(raw_url: str) -> FetchResult:
    """Fetch the main HTML page for a URL, capturing latency and size.

    Uses the centralized SSRF-safe streaming client.  The response body
    is bounded by ``MAX_HTML_BYTES`` and streamed (not buffered entirely).
    """
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

    Uses the same SSRF-safe, streaming client as :func:`fetch_html`.
    Missing robots.txt / sitemap.xml is a normal, non-fatal case.
    """
    result = await safe_get_text(
        url,
        max_bytes=settings.max_html_bytes,
        allow_redirects=True,
        max_redirects=5,
    )

    # Missing robots.txt / sitemap.xml is a normal, non-fatal case.
    return result
