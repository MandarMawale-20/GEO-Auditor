"""
Centralized, SSRF-safe HTTP client with connection pooling and lifespan management.

All outbound HTTP operations (HTML pages, robots.txt, sitemaps, trust-page
checks, and any future external fetches) must go through this module so that:

  * SSRF validation is applied consistently (never forgotten by a caller).
  * Redirects are validated independently before being followed.
  * Response bodies are streamed and bounded in memory.
  * Default headers are applied uniformly.
  * A single ``httpx.AsyncClient`` is reused for connection pooling.

The client is created during FastAPI startup via :func:`startup_http_client`
and closed during shutdown via :func:`shutdown_http_client`.  Tests may
inject a custom client by calling :func:`set_client`.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.config import DEFAULT_HEADERS, settings
from app.models import FetchResult
from app.services.url_validator import validate_url

logger = logging.getLogger("geo_auditor.http")

# Redirect status codes that we handle manually (so we can validate the target).
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------
@dataclass
class ReadResult:
    """Result of a bounded streaming read."""

    data: bytes
    bytes_observed: int
    truncated: bool


async def read_limited_stream(
    response: httpx.Response,
    max_bytes: int,
) -> ReadResult:
    """Stream a response body, enforcing ``max_bytes`` without buffering the whole body.

    The response must be consumed **inside** the ``httpx.AsyncClient.stream()``
    context manager.  Stops reading as soon as ``max_bytes`` have been retained,
    so gigabytes are never downloaded just to confirm their size.

    Returns retained bytes, total bytes observed, and whether truncation
    occurred.
    """
    retained: bytearray = bytearray()
    observed = 0
    truncated = False

    async for chunk in response.aiter_bytes():
        observed += len(chunk)
        if len(retained) < max_bytes:
            remaining = max_bytes - len(retained)
            retained.extend(chunk[:remaining])
        # Stop reading once we've reached the limit.
        if len(retained) >= max_bytes:
            truncated = True
            break

    return ReadResult(
        data=bytes(retained),
        bytes_observed=observed,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# DNS resolution + validation (defence against DNS rebinding)
# ---------------------------------------------------------------------------
async def resolve_and_validate_ips(hostname: str) -> tuple[bool, Optional[str]]:
    """Resolve *hostname* and validate that **all** returned IPs are public.

    Returns ``(True, None)`` when every resolved IP (IPv4 and IPv6) is
    globally routable, or ``(False, reason)`` when any IP is internal,
    loopback, link-local, multicast, reserved, or unspecified.

    Checking *all* addresses (not just the first) is critical: a dual-stack
    host may resolve to one public and one private IP, and a connection
    attempt may land on either.
    """
    import ipaddress

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        # DNS failure — let the fetch layer report the fetch error.
        return True, None
    except socket.timeout:
        return False, "URL hostname could not be resolved within the time limit."
    except (ValueError, OSError):
        return False, "DNS resolution failed."

    for family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if not ip.is_global:
            if ip.is_loopback:
                return False, "URL resolves to a loopback address."
            if ip.is_private:
                return False, "URL resolves to a private IP range."
            if ip.is_link_local:
                return False, "URL resolves to a link-local address."
            if ip.is_multicast:
                return False, "URL resolves to a multicast address."
            if ip.is_unspecified:
                return False, "URL resolves to an unspecified address (0.0.0.0 / ::)."
            if ip.is_reserved:
                return False, "URL resolves to a reserved IP address."
            return False, "URL resolves to a non-routable IP address."

    if not infos:
        return False, "URL hostname did not resolve to any address."

    return True, None


def _resolve_location(base_url: str, location: str) -> str:
    """Resolve a (possibly relative) redirect ``Location`` header against the base URL.

    ``urljoin`` implements RFC 3986 reference resolution, which matters for
    query-only (``?page=2``), fragment-only, and ``../`` redirect targets.
    A hand-rolled path append gets those cases wrong.
    """
    location = (location or "").strip()
    base_split = urlsplit(base_url)
    if not base_split.scheme or not base_split.netloc:
        return location  # Let httpx / validate_url sort it out.
    resolved = urljoin(base_url, location)
    # Fragments are never sent to the server and a fragment-only redirect can
    # otherwise loop on the same URL.
    parts = urlsplit(resolved)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


# ---------------------------------------------------------------------------
# Client lifecycle (FastAPI lifespan-managed singleton)
# ---------------------------------------------------------------------------
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


def _create_client() -> httpx.AsyncClient:
    """Create a new shared AsyncClient with SSRF-safe defaults."""
    return httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        follow_redirects=False,  # We validate redirects manually.
        timeout=settings.request_timeout_seconds,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


async def get_client() -> httpx.AsyncClient:
    """Return the shared ``httpx.AsyncClient``, creating it lazily if needed.

    In normal operation the client is created by :func:`startup_http_client`
    during the FastAPI lifespan.  This function provides a lazy fallback so
    that non-FastAPI code paths still work.
    """
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = _create_client()
    return _client


def set_client(client: Optional[httpx.AsyncClient]) -> None:
    """Inject a custom client (primarily for testing)."""
    global _client
    _client = client


async def startup_http_client() -> None:
    """Create the shared HTTP client.  Called from FastAPI lifespan."""
    global _client
    _client = _create_client()
    logger.info("HTTP client initialized (pool: 20 connections).")


async def shutdown_http_client() -> None:
    """Close the shared HTTP client.  Called from FastAPI lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("HTTP client closed.")


# ---------------------------------------------------------------------------
# High-level safe fetch helpers
# ---------------------------------------------------------------------------
async def safe_get_text(
    url: str,
    *,
    max_bytes: int | None = None,
    client: httpx.AsyncClient | None = None,
    allow_redirects: bool = True,
    max_redirects: int = 5,
) -> FetchResult:
    """Perform an SSRF-safe, streaming GET request.

    - Validates the URL scheme, length, and SSRF-safety (all resolved IPs).
    - Streams the response body, bounded by ``max_bytes`` (defaults to
      ``settings.max_html_bytes``).
    - Validates each redirect destination before following.
    - Applies ``DEFAULT_HEADERS`` (or the caller's client defaults).
    - Never raises — all failure modes are captured in ``FetchResult.error``.

    Returns a :class:`~app.models.FetchResult` with accurate size metadata:
    ``bytes_observed`` (total bytes streamed), ``bytes_retained`` (bytes kept),
    and ``truncated`` (whether the body was cut short).
    """
    if max_bytes is None:
        max_bytes = settings.max_html_bytes

    start = time.monotonic()

    # --- URL validation (syntactic + DNS) ---
    validation = await validate_url(url)
    if not validation.is_valid:
        logger.warning("SSRF blocked: %s — %s", url, validation.reason)
        return FetchResult(
            url=url,
            ok=False,
            error=validation.reason,
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )

    active_client = client if client is not None else await get_client()

    try:
        current_url = url
        redirect_count = 0

        while True:
            async with active_client.stream(
                "GET", current_url, follow_redirects=False
            ) as response:
                status = response.status_code

                # --- Redirect handling with validation ---
                if status in _REDIRECT_STATUS and allow_redirects:
                    if redirect_count >= max_redirects:
                        return FetchResult(
                            url=url,
                            final_url=current_url,
                            status_code=status,
                            ok=False,
                            error=f"Too many redirects (limit: {max_redirects}).",
                            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                            redirect_count=redirect_count,
                        )

                    location = (response.headers.get("location") or "").strip()
                    if not location:
                        # No/empty Location header — treat as a normal response.
                        break

                    new_url = _resolve_location(current_url, location)

                    # Validate redirect destination (syntactic + DNS).
                    redirect_validation = await validate_url(new_url)
                    if not redirect_validation.is_valid:
                        logger.warning(
                            "SSRF blocked on redirect: %s — %s",
                            new_url, redirect_validation.reason,
                        )
                        return FetchResult(
                            url=url,
                            ok=False,
                            error=f"Redirect blocked: {redirect_validation.reason}",
                            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                            redirect_count=redirect_count,
                        )

                    # Validate resolved IPs of redirect target.
                    redirect_host = (urlsplit(new_url).hostname) or ""
                    ip_ok, ip_reason = await resolve_and_validate_ips(redirect_host)
                    if not ip_ok:
                        logger.warning(
                            "SSRF blocked on redirect (IP): %s — %s",
                            new_url, ip_reason,
                        )
                        return FetchResult(
                            url=url,
                            ok=False,
                            error=f"Redirect blocked: {ip_reason}",
                            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                            redirect_count=redirect_count,
                        )

                    current_url = new_url
                    redirect_count += 1
                    continue  # Exit context manager, open new stream for redirect target

                # --- Non-redirect response (including redirects we don't follow) ---
                body_result = await read_limited_stream(response, max_bytes)
                elapsed_ms = (time.monotonic() - start) * 1000

                body_text = None
                if status < 400:
                    body_text = body_result.data.decode("utf-8", errors="ignore")

                return FetchResult(
                    url=url,
                    final_url=current_url,
                    status_code=status,
                    ok=status < 400,
                    text=body_text,
                    content_length=body_result.bytes_observed,  # observed, not header
                    bytes_observed=body_result.bytes_observed,
                    bytes_retained=len(body_result.data),
                    truncated=body_result.truncated,
                    elapsed_ms=round(elapsed_ms, 2),
                    redirect_count=redirect_count,
                    headers=dict(response.headers or {}),
                )

    except httpx.ConnectTimeout:
        return FetchResult(
            url=url, ok=False,
            error="Connection timed out (unreachable host).",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except httpx.ConnectError as exc:
        return FetchResult(
            url=url, ok=False,
            error=f"DNS or connection failure: {exc}",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except httpx.TimeoutException:
        return FetchResult(
            url=url, ok=False,
            error="Request timed out.",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except httpx.TooManyRedirects:
        return FetchResult(
            url=url, ok=False,
            error="Too many redirects.",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except httpx.HTTPError as exc:
        return FetchResult(
            url=url, ok=False,
            error=f"HTTP error: {exc}",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except Exception as exc:  # noqa: BLE001 — final safety net
        logger.error("Unexpected fetch error for %s: %s", url, exc, exc_info=True)
        return FetchResult(
            url=url,
            ok=False,
            error=f"Unexpected fetch error: {exc}",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )


async def safe_head_or_get(
    url: str,
    *,
    max_bytes: int | None = None,
    client: httpx.AsyncClient | None = None,
    max_redirects: int = 5,
) -> FetchResult:
    """Perform an SSRF-safe HEAD request, falling back to a bounded GET.

    Some servers incorrectly reject or mishandle HEAD requests, so if the
    HEAD fails with 405 (Method Not Allowed) or 501 (Not Implemented), a
    bounded GET is attempted.  Both paths enforce SSRF protection, redirect
    validation, and response-size limits.
    """
    if max_bytes is None:
        max_bytes = settings.max_html_bytes

    start = time.monotonic()

    # --- URL validation (syntactic + DNS) ---
    validation = await validate_url(url)
    if not validation.is_valid:
        logger.warning("SSRF blocked: %s — %s", url, validation.reason)
        return FetchResult(
            url=url,
            ok=False,
            error=validation.reason,
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )

    active_client = client if client is not None else await get_client()

    # --- Attempt HEAD first (with redirect validation) ---
    head_result = await _safe_head(url, active_client, max_redirects)

    # 405 / 501 / connection error → fall back to bounded GET.
    if head_result.status_code in (405, 501) or (
        head_result.error and "HEAD" not in head_result.error
    ):
        # Any error means we should try GET.
        return await safe_get_text(
            url, max_bytes=max_bytes, client=active_client,
            allow_redirects=True, max_redirects=max_redirects,
        )

    if head_result.error:
        return head_result

    elapsed_ms = (time.monotonic() - start) * 1000

    # For HEAD, there's no body — just return status + headers.
    return FetchResult(
        url=url,
        final_url=head_result.final_url or url,
        status_code=head_result.status_code,
        ok=(head_result.status_code or 0) < 400,
        text=None,
        content_length=0,
        bytes_observed=0,
        bytes_retained=0,
        truncated=False,
        elapsed_ms=round(elapsed_ms, 2),
        redirect_count=head_result.redirect_count,
        headers=head_result.headers,
    )


async def _safe_head(
    url: str,
    client: httpx.AsyncClient,
    max_redirects: int,
) -> FetchResult:
    """Send a HEAD request with manual redirect validation (no body streaming)."""
    start = time.monotonic()
    current_url = url
    redirect_count = 0

    try:
        while True:
            async with client.stream("HEAD", current_url, follow_redirects=False) as response:
                status = response.status_code

                if status in _REDIRECT_STATUS and redirect_count < max_redirects:
                    location = response.headers.get("location")
                    if not location:
                        break
                    new_url = _resolve_location(current_url, location)

                    # Validate redirect destination.
                    redirect_validation = await validate_url(new_url)
                    if not redirect_validation.is_valid:
                        logger.warning(
                            "SSRF blocked on HEAD redirect: %s — %s",
                            new_url, redirect_validation.reason,
                        )
                        return FetchResult(
                            url=url, ok=False,
                            error=f"Redirect blocked: {redirect_validation.reason}",
                            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                            redirect_count=redirect_count,
                        )

                    redirect_host = (urlsplit(new_url).hostname) or ""
                    ip_ok, ip_reason = await resolve_and_validate_ips(redirect_host)
                    if not ip_ok:
                        logger.warning(
                            "SSRF blocked on HEAD redirect (IP): %s — %s",
                            new_url, ip_reason,
                        )
                        return FetchResult(
                            url=url, ok=False,
                            error=f"Redirect blocked: {ip_reason}",
                            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                            redirect_count=redirect_count,
                        )

                    current_url = new_url
                    redirect_count += 1
                    continue

                return FetchResult(
                    url=url,
                    final_url=current_url,
                    status_code=status,
                    ok=status < 400,
                    text=None,
                    content_length=0,
                    bytes_observed=0,
                    bytes_retained=0,
                    truncated=False,
                    elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                    redirect_count=redirect_count,
                    headers=dict(response.headers or {}),
                )
    except httpx.ConnectTimeout:
        return FetchResult(
            url=url, ok=False,
            error="Connection timed out (unreachable host).",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except httpx.ConnectError as exc:
        return FetchResult(
            url=url, ok=False,
            error=f"DNS or connection failure: {exc}",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except httpx.TimeoutException:
        return FetchResult(
            url=url, ok=False,
            error="Request timed out.",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except httpx.HTTPError as exc:
        return FetchResult(
            url=url, ok=False,
            error=f"HTTP error: {exc}",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected HEAD error for %s: %s", url, exc, exc_info=True)
        return FetchResult(
            url=url, ok=False,
            error=f"Unexpected fetch error: {exc}",
            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
        )
