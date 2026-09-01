"""Strict URL validation against SSRF and unsafe-scheme abuse.

All public functions are async so the DNS-resolution step (the real SSRF
guard) does not block the event loop. It is used by the ``/audit`` endpoint
and the centralized HTTP client to reject URLs that could be used to probe
internal services or trigger unintended protocol handlers.

Rejected:
  * Non-http(s) schemes (file://, ftp://, data:, gopher://, ...)
  * localhost / 127.0.0.1 / ::1
  * Private IP ranges (10/8, 172.16/12, 192.168/16, 100.64/10)
  * Link-local IPs (169.254/16)
  * Unroutable / reserved addresses (0.0.0.0, ::, 127.0.0.0/8, ::1/128)
  * IPv6 private/link-local/reserved ranges
  * .local / .internal / .intranet / .home TLDs
  * Single-label hostnames (e.g. ``myserver``)
  * Userinfo in URLs (credentials)
  * URLs longer than 2048 characters
  * URLs that cannot be parsed
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048

# Schemes we permit. Anything else (file, ftp, data, gopher, ...) is rejected.
ALLOWED_SCHEMES = {"http", "https"}

# Literal hostnames we never allow.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
    }
)

# TLDs that virtually always indicate an internal host.
_BLOCKED_TLDS = (".local", ".internal", ".intranet", ".home")

# Used by URL syntax check and DNS check to reject control characters early.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def normalize_url(raw_url: str) -> str:
    """Auto-prepend ``https://`` if the scheme is missing.

    This is the single canonical place for scheme auto-prepending; callers
    that need validation on top should use :func:`normalize_and_validate`.
    """
    if not raw_url:
        return raw_url
    candidate = raw_url.strip()
    if not candidate:
        return candidate
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    return candidate


@dataclass
class ValidationResult:
    """Outcome of :func:`validate_url`."""

    is_valid: bool
    reason: str | None = None  # populated when is_valid is False


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a rejection reason if *ip* is non-public, otherwise ``None``.

    Covers loopback, private, link-local, multicast, unspecified, and
    reserved ranges for both IPv4 and IPv6. Each branch returns a precise
    message so callers can surface the actual reason.
    """
    if ip.is_global:
        return None

    if ip.is_loopback:
        return "URL resolves to a loopback address."
    if ip.is_private:
        return "URL resolves to a private IP range."
    if ip.is_link_local:
        return "URL resolves to a link-local address."
    if ip.is_multicast:
        return "URL resolves to a multicast address."
    if ip.is_unspecified:
        return "URL resolves to an unspecified address (0.0.0.0 / ::)."
    if ip.is_reserved:
        return "URL resolves to a reserved IP address."
    return "URL resolves to a non-routable IP address."


async def _hostname_is_blocked(hostname: str) -> str | None:
    """Return a rejection reason if *hostname* is unsafe, otherwise ``None``.

    Performs a syntactic check (reject literal hostnames like ``localhost``)
    and a DNS check (reject IPs that resolve to private / loopback / link-
    local ranges). The DNS check resolves *all* addresses (IPv4 *and* IPv6)
    via :func:`socket.getaddrinfo` and rejects if any resolved IP is non-
    routable, preventing dual-stack bypass where one address is public and
    another is private.
    """
    if not hostname:
        return "URL has no hostname."

    if hostname.lower() in _BLOCKED_HOSTNAMES:
        return "URL hostname is a blocked local hostname."

    # IP literal (v4 / v6)
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    if ip is not None:
        return _ip_is_blocked(ip)

    lower = hostname.lower()
    if lower.endswith(_BLOCKED_TLDS):
        return "URL hostname uses a private/internal TLD suffix."

    if "." not in hostname:
        return "URL hostname is a single-label internal name."

    # Resolve via DNS and check ALL addresses.
    try:
        # getaddrinfo returns every resolved address (IPv4 + IPv6). It is a
        # blocking call — run in a thread to avoid blocking the event loop.
        original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5.0)
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
        finally:
            socket.setdefaulttimeout(original_timeout)
    except socket.gaierror:
        # DNS failure — the host may simply not exist, which is not an
        # SSRF risk. Let the fetch layer report the fetch error.
        return None
    except socket.timeout:
        return "URL hostname could not be resolved within the time limit."
    except (ValueError, OSError):
        return None

    for _family, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        blocked = _ip_is_blocked(ip)
        if blocked:
            return blocked

    if not infos:
        return "URL hostname did not resolve to any address."

    return None


async def validate_url(url: str) -> ValidationResult:
    """Validate a URL for safety before fetching.

    Returns a :class:`ValidationResult`. Callers should treat
    ``is_valid=False`` as a hard rejection.
    """
    if not url or not isinstance(url, str):
        return ValidationResult(False, "URL is required.")

    stripped = url.strip()
    if not stripped:
        return ValidationResult(False, "URL is empty.")

    if len(stripped) > MAX_URL_LENGTH:
        return ValidationResult(
            False, "URL exceeds the maximum allowed length (2048 characters)."
        )

    if _CONTROL_CHARS_RE.search(stripped):
        return ValidationResult(False, "URL contains control characters.")

    try:
        parsed = urlsplit(stripped)
    except (ValueError, TypeError):
        return ValidationResult(False, "URL is malformed and could not be parsed.")

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return ValidationResult(
            False,
            f"URL scheme '{scheme}' is not allowed. Only http and https are permitted.",
        )

    if parsed.username or parsed.password:
        return ValidationResult(False, "URLs with credentials (userinfo) are not allowed.")

    hostname = parsed.hostname
    if not hostname:
        return ValidationResult(False, "URL has no hostname.")

    blocked_reason = await _hostname_is_blocked(hostname)
    if blocked_reason:
        return ValidationResult(False, blocked_reason)

    return ValidationResult(True, None)


async def normalize_and_validate(raw_url: str) -> tuple[str | None, str | None]:
    """Auto-prepend ``https://`` when no scheme is present, then validate.

    Returns ``(normalized_url, None)`` on success or ``(None, error_reason)``
    when the URL is rejected.
    """
    candidate = raw_url.strip()
    if not candidate:
        return None, "URL is empty."
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    result = await validate_url(candidate)
    if not result.is_valid:
        return None, result.reason
    return candidate, None
