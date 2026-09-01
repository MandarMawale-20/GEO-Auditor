"""Centralized SSRF-safe HTTP client with connection pooling and lifespan management.

All outbound HTTP operations (HTML pages, robots.txt, sitemaps, trust-page
checks, and any future external fetches) route through this module so that:

  * SSRF validation is applied consistently.
  * Redirects are validated independently before being followed.
  * Response bodies are streamed and bounded in memory.
  * Default headers are applied uniformly.
  * A single ``httpx.AsyncClient`` is reused for connection pooling.

The client is created during FastAPI startup via :func:`startup_http_client`
and closed during shutdown via :func:`shutdown_http_client`. Tests may inject
a custom client by calling :func:`set_client`.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.config import DEFAULT_HEADERS, settings
from app.models import FetchResult
from app.services.url_validator import validate_url

logger = logging.getLogger("geo_auditor.http")

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


@dataclass
class ReadResult:
    data: bytes
    bytes_observed: int
    truncated: bool


async def read_limited_stream(
    response: httpx.Response,
    max_bytes: int,
) -> ReadResult:
    """Stream a response body, enforcing ``max_bytes`` without buffering the whole body.

    Must be called inside the ``httpx.AsyncClient.stream()`` context manager.
    Stops reading once ``max_bytes`` have been retained, so gigabytes are never
    downloaded just to confirm their size.
    """
    retained: bytearray = bytearray()
    observed = 0
    truncated = False

    async for chunk in response.aiter_bytes():
        observed += len(chunk)
        if len(retained) < max_bytes:
            remaining = max_bytes - len(retained)
            retained.extend(chunk[:remaining])
        if len(retained) >= max_bytes:
            truncated = True
            break

    return ReadResult(
        data=bytes(retained),
        bytes_observed=observed,
        truncated=truncated,
    )


async def resolve_and_validate_ips(hostname: str) -> tuple[bool, str | None]:
    """Resolve *hostname* and validate that every returned IP is public.

    Returns ``(True, None)`` when every resolved address (IPv4 and IPv6) is
    globally routable, or ``(False, reason)`` when any is internal, loopback,
    link-local, multicast, reserved, or unspecified. Checking every address
    matters: a dual-stack host may resolve to one public and one private IP,
    and a connection attempt may land on either.
    """
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

    for _family, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue

        if ip.is_global:
            continue
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
    """Resolve a (possibly relative) redirect ``Location`` header against the base URL."""
    base_split = urlsplit(base_url)
    if not base_split.scheme or not base_split.netloc:
        return location

    base_origin = f"{base_split.scheme}://{base_split.netloc}"

    if location.startswith(("http://", "https://")):
        return location
    if location.startswith("//"):
        return f"{base_split.scheme}:{location}"
    if location.startswith("/"):
        return f"{base_origin}{location}"

    base_path = base_split.path.rstrip("/")
    return f"{base_origin}{base_path}/{location}"


# FastAPI lifespan-managed singleton.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


def _create_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        follow_redirects=False,  # redirects are validated manually
        timeout=settings.request_timeout_seconds,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


async def get_client() -> httpx.AsyncClient:
    """Return the shared client, creating it lazily if needed.

    The lifespan normally creates the client at startup; this fallback lets
    non-FastAPI code paths still work.
    """
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = _create_client()
    return _client


def set_client(client: httpx.AsyncClient | None) -> None:
    """Inject a custom client (primarily for testing)."""
    global _client
    _client = client


async def startup_http_client() -> None:
    """Create the shared HTTP client. Called from FastAPI lifespan."""
    global _client
    _client = _create_client()
    logger.info("HTTP client initialized (pool: 20 connections).")


async def shutdown_http_client() -> None:
    """Close the shared HTTP client. Called from FastAPI lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("HTTP client closed.")


async def safe_get_text(
    url: str,
    *,
    max_bytes: int | None = None,
    client: httpx.AsyncClient | None = None,
    allow_redirects: bool = True,
    max_redirects: int = 5,
) -> FetchResult:
    """SSRF-safe, streaming GET request.

    - Validates URL scheme, length, and SSRF-safety (all resolved IPs).
    - Streams the body, bounded by ``max_bytes`` (default: ``settings.max_html_bytes``).
    - Validates each redirect destination before following.
    - Applies ``DEFAULT_HEADERS`` (or the caller's client defaults).
    - Never raises — every failure mode is captured in ``FetchResult.error``.

    Returns a :class:`~app.models.FetchResult` with accurate size metadata:
    ``bytes_observed`` (total bytes streamed), ``bytes_retained`` (bytes
    kept), and ``truncated`` (whether the body was cut short).
    """
    if max_bytes is None:
        max_bytes = settings.max_html_bytes

    start = time.monotonic()

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
            async with active_client.stream("GET", current_url, follow_redirects=False) as response:
                status = response.status_code

                if status in _REDIRECT_STATUS and allow_redirects and redirect_count < max_redirects:
                    location = response.headers.get("location")
                    if not location:
                        break

                    new_url = _resolve_location(current_url, location)
                    redirect_validation = await validate_url(new_url)
                    if not redirect_validation.is_valid:
                        logger.warning("SSRF blocked on redirect: %s — %s", new_url, redirect_validation.reason)
                        return FetchResult(
                            url=url,
                            ok=False,
                            error=f"Redirect blocked: {redirect_validation.reason}",
                            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                            redirect_count=redirect_count,
                        )

                    redirect_host = (urlsplit(new_url).hostname) or ""
                    ip_ok, ip_reason = await resolve_and_validate_ips(redirect_host)
                    if not ip_ok:
                        logger.warning("SSRF blocked on redirect (IP): %s — %s", new_url, ip_reason)
                        return FetchResult(
                            url=url,
                            ok=False,
                            error=f"Redirect blocked: {ip_reason}",
                            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                            redirect_count=redirect_count,
                        )

                    current_url = new_url
                    redirect_count += 1
                    continue

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
                    # `content_length` is the observed (retained-bytes) size, not
                    # the value of the Content-Length header — that name is
                    # overloaded in the model.
                    content_length=body_result.bytes_observed,
                    bytes_observed=body_result.bytes_observed,
                    bytes_retained=len(body_result.data),
                    truncated=body_result.truncated,
                    elapsed_ms=round(elapsed_ms, 2),
                    redirect_count=redirect_count,
                    headers=dict(response.headers or {}),
                )

    except httpx.ConnectTimeout:
        return _fetch_error(url, start, "Connection timed out (unreachable host).")
    except httpx.ConnectError as exc:
        return _fetch_error(url, start, f"DNS or connection failure: {exc}")
    except httpx.TimeoutException:
        return _fetch_error(url, start, "Request timed out.")
    except httpx.TooManyRedirects:
        return _fetch_error(url, start, "Too many redirects.")
    except httpx.HTTPError as exc:
        return _fetch_error(url, start, f"HTTP error: {exc}")
    except Exception as exc:
        logger.error("Unexpected fetch error for %s: %s", url, exc, exc_info=True)
        return _fetch_error(url, start, f"Unexpected fetch error: {exc}")


def _fetch_error(url: str, start: float, message: str) -> FetchResult:
    return FetchResult(
        url=url,
        ok=False,
        error=message,
        elapsed_ms=round((time.monotonic() - start) * 1000, 2),
    )


async def safe_head_or_get(
    url: str,
    *,
    max_bytes: int | None = None,
    client: httpx.AsyncClient | None = None,
    max_redirects: int = 5,
) -> FetchResult:
    """SSRF-safe HEAD request, falling back to a bounded GET.

    Some servers reject or mishandle HEAD; if the HEAD comes back as 405
    or 501, a bounded GET is attempted. Both paths enforce SSRF protection,
    redirect validation, and response-size limits.
    """
    if max_bytes is None:
        max_bytes = settings.max_html_bytes

    start = time.monotonic()

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
    head_result = await _safe_head(url, active_client, max_redirects)

    # 405/501 -> server rejected HEAD specifically; fall back to GET.
    # Any other error -> fall back too.
    if head_result.status_code in (405, 501) or head_result.error:
        return await safe_get_text(
            url, max_bytes=max_bytes, client=active_client,
            allow_redirects=True, max_redirects=max_redirects,
        )

    elapsed_ms = (time.monotonic() - start) * 1000
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
    """HEAD request with manual redirect validation (no body streaming)."""
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

                    redirect_validation = await validate_url(new_url)
                    if not redirect_validation.is_valid:
                        logger.warning("SSRF blocked on HEAD redirect: %s — %s", new_url, redirect_validation.reason)
                        return FetchResult(
                            url=url, ok=False,
                            error=f"Redirect blocked: {redirect_validation.reason}",
                            elapsed_ms=round((time.monotonic() - start) * 1000, 2),
                            redirect_count=redirect_count,
                        )

                    redirect_host = (urlsplit(new_url).hostname) or ""
                    ip_ok, ip_reason = await resolve_and_validate_ips(redirect_host)
                    if not ip_ok:
                        logger.warning("SSRF blocked on HEAD redirect (IP): %s — %s", new_url, ip_reason)
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
        return _fetch_error(url, start, "Connection timed out (unreachable host).")
    except httpx.ConnectError as exc:
        return _fetch_error(url, start, f"DNS or connection failure: {exc}")
    except httpx.TimeoutException:
        return _fetch_error(url, start, "Request timed out.")
    except httpx.HTTPError as exc:
        return _fetch_error(url, start, f"HTTP error: {exc}")
    except Exception as exc:
        logger.error("Unexpected HEAD error for %s: %s", url, exc, exc_info=True)
        return _fetch_error(url, start, f"Unexpected fetch error: {exc}")
