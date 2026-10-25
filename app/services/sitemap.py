"""Sitemap discovery, URL extraction, and freshness extraction.

Looks up ``/sitemap.xml`` (or a sitemap URL declared in robots.txt), extracts
page URLs (including one level of sitemap-index expansion), and finds the most
recent ``<lastmod>`` value used by the Freshness Signals category. All
candidate URLs are validated through the SSRF-safe HTTP client before any
request is made.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from app.services.http_client import safe_get_text
from app.utils import parse_iso_datetime

_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.IGNORECASE)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_MAX_CHILD_SITEMAPS = 3
_MAX_SITEMAP_HINTS = 5
_MAX_SITEMAP_URLS = 1000


def _site_hosts(page_url: str, include_subdomains: bool = False) -> set[str]:
    host = (urlparse(page_url).hostname or "").lower().rstrip(".")
    if not host:
        return set()
    if host.startswith("www."):
        hosts = {host, host[4:]}
        base = host[4:]
    else:
        hosts = {host, f"www.{host}"}
        base = host
    if include_subdomains:
        hosts.add(f".{base}")
    return hosts


def _same_site(url: str, hosts: set[str]) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    if host in hosts:
        return True
    return any(
        allowed.startswith(".") and host.endswith(allowed)
        for allowed in hosts
    )


@dataclass
class SitemapResult:
    exists: bool = False
    url: str | None = None
    fetch_error: str | None = None
    most_recent_lastmod: datetime | None = None
    url_count: int = 0
    urls: list[str] = field(default_factory=list)


async def _fetch_sitemap(url: str) -> tuple[list[str], datetime | None, str | None]:
    result = await safe_get_text(url, allow_redirects=True, max_redirects=5)
    if not result.ok or not result.text:
        return [], None, result.error or "Sitemap unavailable."

    locs = [
        urljoin(url, html.unescape(loc))
        for loc in _LOC_RE.findall(result.text)
    ]
    lastmods = [
        dt
        for raw in _LASTMOD_RE.findall(result.text)
        if (dt := parse_iso_datetime(raw)) is not None
    ]
    most_recent = max(lastmods) if lastmods else None
    return locs, most_recent, None


async def get_sitemap_info(
    page_url: str,
    sitemap_hints: list[str] | None = None,
    include_subdomains: bool = False,
) -> SitemapResult:
    """Try declared sitemap URLs first, then fall back to /sitemap.xml."""
    parsed = urlparse(page_url)
    if not parsed.scheme or not parsed.netloc:
        return SitemapResult(exists=False, fetch_error="Invalid base URL for sitemap lookup.")

    hosts = _site_hosts(page_url, include_subdomains)
    if not hosts:
        return SitemapResult(exists=False, fetch_error="Invalid base URL for sitemap lookup.")

    # Only follow sitemap hints that stay on the audited site (apex/www
    # equivalent). Otherwise a hostile robots.txt could make one audit fan out
    # to an unbounded list of third-party hosts.
    candidates: list[str] = []
    for hint in sitemap_hints or []:
        hint = (hint or "").strip()
        if not hint or not _same_site(hint, hosts) or hint in candidates:
            continue
        candidates.append(hint)
        if len(candidates) >= _MAX_SITEMAP_HINTS:
            break

    default_sitemap = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/sitemap.xml")
    if default_sitemap not in candidates:
        candidates.append(default_sitemap)

    last_error: str | None = None
    for candidate_url in candidates:
        locs, most_recent, error = await _fetch_sitemap(candidate_url)
        if error:
            last_error = error
            continue
        if not locs:
            last_error = "Sitemap contained no <loc> entries."
            continue

        urls: list[str] = []
        # A sitemap index points at other sitemaps; expand a bounded number,
        # still restricted to the audited hosts.
        child_sitemaps = [
            loc
            for loc in locs
            if loc.lower().endswith((".xml", ".xml.gz")) and _same_site(loc, hosts)
        ]
        if child_sitemaps:
            child_success = 0
            for child in child_sitemaps[:_MAX_CHILD_SITEMAPS]:
                child_urls, child_lastmod, child_err = await _fetch_sitemap(child)
                if child_err:
                    last_error = child_err
                    continue
                child_success += 1
                urls.extend(child_urls)
                if child_lastmod and (most_recent is None or child_lastmod > most_recent):
                    most_recent = child_lastmod
            if child_success == 0:
                # The index itself was reachable but none of its children
                # were. Try the next candidate rather than returning an empty
                # "found" sitemap.
                last_error = last_error or "No child sitemap could be fetched."
                continue
        else:
            urls.extend(locs)

        # Deduplicate while preserving order and cap the list.
        seen: set[str] = set()
        deduped: list[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
            if len(deduped) >= _MAX_SITEMAP_URLS:
                break

        return SitemapResult(
            exists=True,
            url=candidate_url,
            most_recent_lastmod=most_recent,
            url_count=len(deduped),
            urls=deduped,
        )

    return SitemapResult(exists=False, fetch_error=last_error or "No sitemap found.")
