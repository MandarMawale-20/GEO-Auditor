"""
Async HTTP fetcher.

Fetches a URL with browser-like headers, follows redirects, and captures
timing/size diagnostics needed for the Crawler Fetch Health sidebar.
Never raises — all failure modes are captured in FetchResult.error.
"""
from __future__ import annotations

import time

import httpx

from app.config import DEFAULT_HEADERS, settings
from app.models import FetchResult


def normalize_url(raw_url: str) -> str:
    """Auto-prepend https:// if the scheme is missing."""
    candidate = raw_url.strip()
    if not candidate:
        return candidate
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    return candidate


async def fetch_html(raw_url: str) -> FetchResult:
    """Fetch the main HTML page for a URL, capturing latency and size."""
    url = normalize_url(raw_url)
    if not url:
        return FetchResult(url=raw_url, ok=False, error="Empty or invalid URL provided.")

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
        ) as client:
            response = await client.get(url)
            elapsed_ms = (time.monotonic() - start) * 1000

            body = response.text
            if len(body.encode("utf-8", errors="ignore")) > settings.max_html_bytes:
                # Truncate defensively; extremely large pages shouldn't blow memory.
                body = body[: settings.max_html_bytes]

            return FetchResult(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                ok=response.status_code < 400,
                text=body,
                content_length=len(body.encode("utf-8", errors="ignore")),
                elapsed_ms=round(elapsed_ms, 2),
                redirect_count=len(response.history),
                headers=dict(response.headers),
            )
    except httpx.ConnectTimeout:
        return FetchResult(url=url, ok=False, error="Connection timed out (unreachable host).")
    except httpx.ConnectError as exc:
        return FetchResult(url=url, ok=False, error=f"DNS or connection failure: {exc}")
    except httpx.TimeoutException:
        return FetchResult(url=url, ok=False, error="Request timed out.")
    except httpx.HTTPError as exc:
        return FetchResult(url=url, ok=False, error=f"HTTP error: {exc}")
    except Exception as exc:  # noqa: BLE001 - final safety net, must never crash the audit
        return FetchResult(url=url, ok=False, error=f"Unexpected fetch error: {exc}")


async def fetch_text_resource(url: str) -> FetchResult:
    """Fetch a plain-text/XML resource such as robots.txt or sitemap.xml."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
        ) as client:
            response = await client.get(url)
            elapsed_ms = (time.monotonic() - start) * 1000
            return FetchResult(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                ok=response.status_code < 400,
                text=response.text if response.status_code < 400 else None,
                content_length=len(response.content or b""),
                elapsed_ms=round(elapsed_ms, 2),
                redirect_count=len(response.history),
            )
    except Exception as exc:  # noqa: BLE001
        # Missing robots.txt / sitemap.xml is a normal, non-fatal case.
        return FetchResult(url=url, ok=False, error=str(exc))
