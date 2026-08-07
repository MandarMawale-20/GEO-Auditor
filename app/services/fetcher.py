"""
Async HTTP fetcher.

Fetches a URL with browser-like headers, follows redirects, and captures
timing/size diagnostics needed for the Crawler Fetch Health sidebar.
Never raises — all failure modes are captured in FetchResult.error.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlparse

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


def is_public_url(url: str) -> bool:
    """Return True only if the URL's resolved IP is globally routable.

    Guarding against private/loopback/link-local IPs prevents SSRF attacks
    that could probe internal services (e.g. cloud metadata endpoints).
    """
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    try:
        ip = socket.gethostbyname(hostname)
        return ipaddress.ip_address(ip).is_global
    except (socket.error, ValueError):
        return False


async def fetch_html(raw_url: str) -> FetchResult:
    """Fetch the main HTML page for a URL, capturing latency and size."""
    url = normalize_url(raw_url)
    if not url:
        return FetchResult(url=raw_url, ok=False, error="Empty or invalid URL provided.")
    if not is_public_url(url):
        return FetchResult(url=url, ok=False, error="URL must resolve to a public IP address.")

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
        ) as client:
            response = await client.get(url)
            elapsed_ms = (time.monotonic() - start) * 1000

            body_bytes = response.content if response.content else b""
            if len(body_bytes) > settings.max_html_bytes:
                # Truncate defensively; extremely large pages shouldn't blow memory.
                body_bytes = body_bytes[: settings.max_html_bytes]

            body = body_bytes.decode("utf-8", errors="ignore")

            return FetchResult(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                ok=response.status_code < 400,
                text=body,
                content_length=len(body_bytes),
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
