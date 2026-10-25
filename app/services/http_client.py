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
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx

from app.config import DEFAULT_HEADERS, settings
from app.models import FetchResult
from app.services.url_validator import resolve_public_ips, validate_url

logger = logging.getLogger("geo_auditor.http")

# Redirect status codes that we handle manually (so we can validate the target).
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


# ---------------------------------------------------------------------------
# DNS pinning
# ---------------------------------------------------------------------------
# URL validation resolves every address and checks that all of them are public.
# The transport below connects to one of those exact addresses instead of
# resolving the hostname again, closing the DNS-rebinding (TOCTOU) window.
_PINNED_IPS: dict[str, tuple[str, ...]] = {}


def _pin_key(host: str) -> str:
    """Normalize a hostname to the form httpcore uses for connection origins."""
    key = (host or "").strip().lower().rstrip(".")
    if not key:
        return ""
    try:
        return key.encode("idna").decode("ascii")
    except UnicodeError:
        return key


def set_pinned_ips(host: str, ips: tuple[str, ...] | list[str]) -> None:
    """Remember the validated addresses for *host* (used by the transport)."""
    key = _pin_key(host)
    if not key:
        return
    unique = tuple(dict.fromkeys(str(ip) for ip in ips if ip))
    if unique:
        _PINNED_IPS[key] = unique


def get_pinned_ips(host: str) -> tuple[str, ...]:
    """Return the currently pinned addresses for *host* (diagnostics/tests)."""
    return _PINNED_IPS.get(_pin_key(host), ())


def clear_pinned_ips() -> None:
    """Clear the in-process DNS pin cache (tests / lifecycle cleanup)."""
    _PINNED_IPS.clear()


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that connects to validated IPs instead of re-resolving.

    HTTP ``Host`` headers and TLS SNI still come from the original URL because
    only the TCP connect target is changed. If a host has no pin (for example a
    caller using the transport directly), the wrapped backend resolves as usual.
    """

    def __init__(self, backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        pinned = _PINNED_IPS.get(_pin_key(host), ())
        if pinned:
            last_error: Exception | None = None
            for ip in pinned:
                try:
                    return await self._backend.connect_tcp(
                        host=ip,
                        port=port,
                        timeout=timeout,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error

        return await self._backend.connect_tcp(
            host=host,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path=path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """``httpx`` transport whose TCP connections use validated DNS results.

    ``httpx.AsyncHTTPTransport`` does not expose its ``httpcore`` network
    backend, so the pool it creates is replaced immediately with an identically
    configured pool using :class:`PinnedNetworkBackend`. This is tied to the
    pinned httpx/httpcore versions in ``requirements.txt``.
    """

    def __init__(self, limits: httpx.Limits) -> None:
        super().__init__(limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(
                verify=True, cert=None, trust_env=True
            ),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            retries=0,
            network_backend=PinnedNetworkBackend(),
        )


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

    Kept as a compatibility helper for callers/tests. The safe-fetch paths use
    :func:`app.services.url_validator.resolve_public_ips`, which returns the
    validated addresses so the transport can pin them.
    """
    ok, reason, _ = await resolve_public_ips(hostname)
    return ok, reason


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
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    return httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        follow_redirects=False,  # We validate redirects manually.
        timeout=settings.request_timeout_seconds,
        limits=limits,
        transport=PinnedAsyncHTTPTransport(limits),
    )


async def _ensure_pinned(url: str, validation) -> tuple[bool, Optional[str]]:
    """Pin the validated IPs for *url*'s host before opening a connection.

    ``validate_url`` already resolves and checks every address, so normally
    this only copies its result into the transport cache. A missing result
    (DNS failure during validation, or a test double) triggers one more
    validated resolution; a host that still has no public address is rejected
    rather than allowing httpx to resolve it independently.
    """
    host = (urlsplit(url).hostname or "").strip()
    if not host:
        return False, "URL has no hostname."

    ips = tuple(getattr(validation, "resolved_ips", ()) or ())
    if not ips:
        ok, reason, resolved = await resolve_public_ips(host)
        if not ok:
            return False, reason
        ips = tuple(resolved)

    set_pinned_ips(host, ips)
    return True, None


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
        clear_pinned_ips()
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

    pin_ok, pin_reason = await _ensure_pinned(url, validation)
    if not pin_ok:
        logger.warning("SSRF blocked: %s — %s", url, pin_reason)
        return FetchResult(
            url=url,
            ok=False,
            error=pin_reason,
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

                    # Pin the redirect target's validated IPs before connecting.
                    pin_ok, pin_reason = await _ensure_pinned(
                        new_url, redirect_validation
                    )
                    if not pin_ok:
                        logger.warning(
                            "SSRF blocked on redirect: %s — %s",
                            new_url, pin_reason,
                        )
                        return FetchResult(
                            url=url,
                            ok=False,
                            error=f"Redirect blocked: {pin_reason}",
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

    pin_ok, pin_reason = await _ensure_pinned(url, validation)
    if not pin_ok:
        logger.warning("SSRF blocked: %s — %s", url, pin_reason)
        return FetchResult(
            url=url,
            ok=False,
            error=pin_reason,
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

                    pin_ok, pin_reason = await _ensure_pinned(
                        new_url, redirect_validation
                    )
                    if not pin_ok:
                        logger.warning(
                            "SSRF blocked on HEAD redirect: %s — %s",
                            new_url, pin_reason,
                        )
                        return FetchResult(
                            url=url, ok=False,
                            error=f"Redirect blocked: {pin_reason}",
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
