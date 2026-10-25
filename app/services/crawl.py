"""Bounded same-site discovery and fetching for multipage audits.

Discovery is deterministic: candidates come from the homepage's anchors and
the sitemap, are normalized, filtered against robots.txt and a utility-path
deny list, ranked by navigation prominence + path intent, and capped.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from app.config import settings
from app.models import FetchResult
from app.services.fetcher import fetch_html
from app.services.html_utils import ParsedPage, parse_html
from app.services.robots import RobotsRules
from app.services.url_validator import MAX_URL_LENGTH

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "ref", "source", "mc_cid", "mc_eid",
}

_BLOCKED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".zip", ".gz", ".tar", ".mp4", ".mp3", ".avi", ".mov",
    ".woff", ".woff2", ".ttf", ".eot", ".xml", ".json", ".rss", ".txt",
}

_BLOCKED_PATH_PARTS = {
    "login", "signin", "sign-in", "signup", "sign-up", "register", "cart",
    "checkout", "account", "search", "wp-admin", "wp-login", "feed", "tag",
    "tags", "author", "authors", "page", "amp", "print", "share",
}

_PATH_INTENT = {
    "pricing": 4.0,
    "price": 4.0,
    "plans": 3.0,
    "product": 3.0,
    "products": 3.0,
    "features": 3.0,
    "feature": 3.0,
    "solutions": 3.0,
    "solution": 3.0,
    "services": 3.0,
    "service": 3.0,
    "platform": 2.5,
    "use-cases": 2.5,
    "customers": 2.0,
    "about": 2.0,
    "company": 2.0,
    "blog": 1.0,
    "resources": 1.0,
    "docs": 1.0,
    "documentation": 1.0,
}


@dataclass
class CrawlCandidate:
    url: str
    score: float
    reason: str
    role: str = "internal"


@dataclass
class CrawlPlan:
    homepage_url: str
    selected: list[CrawlCandidate] = field(default_factory=list)
    discovered_count: int = 0
    skipped: dict[str, int] = field(default_factory=dict)


@dataclass
class FetchedPage:
    url: str
    role: str
    selected_reason: str
    result: FetchResult
    parsed: ParsedPage | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.parsed is not None and self.error is None


def site_hosts_from_url(final_url: str, include_subdomains: bool = False) -> set[str]:
    host = (urlsplit(final_url).hostname or "").lower().rstrip(".")
    if not host:
        return set()
    hosts = {host}
    if host.startswith("www."):
        hosts.add(host[4:])
    else:
        hosts.add(f"www.{host}")
    if include_subdomains:
        base = host[4:] if host.startswith("www.") else host
        hosts.add(f".{base}")
    return hosts


def _host_matches(host: str, site_hosts: set[str]) -> bool:
    host = host.lower().rstrip(".")
    if host in site_hosts:
        return True
    return any(
        allowed.startswith(".") and host.endswith(allowed)
        for allowed in site_hosts
    )


def normalize_candidate_url(
    base_url: str,
    href: str,
    site_hosts: set[str],
) -> tuple[str | None, str | None]:
    """Return ``(normalized_url, skip_reason)`` for a discovered link."""
    href = (href or "").strip()
    if not href:
        return None, "empty"
    if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None, "non_http"

    absolute = urljoin(base_url, href)
    if len(absolute) > MAX_URL_LENGTH:
        return None, "too_long"

    try:
        parts = urlsplit(absolute)
    except ValueError:
        return None, "malformed"

    if parts.scheme not in ("http", "https"):
        return None, "non_http"
    if parts.username or parts.password:
        return None, "credentials"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not _host_matches(host, site_hosts):
        return None, "off_host"

    path = parts.path or "/"
    lower_path = path.lower()
    if any(lower_path.endswith(ext) for ext in _BLOCKED_EXTENSIONS):
        return None, "asset"

    segments = [seg for seg in lower_path.split("/") if seg]
    if any(seg in _BLOCKED_PATH_PARTS for seg in segments):
        return None, "utility"

    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    if query_pairs:
        # Query-string URLs are a common crawl trap (filters, pagination,
        # sessions). Skip them in v1 unless they are the homepage itself.
        return None, "query"

    # Normalize path: collapse duplicate slashes and drop a trailing slash.
    normalized_path = "/" + "/".join(seg for seg in path.split("/") if seg)
    if normalized_path != "/" and normalized_path.endswith("/"):
        normalized_path = normalized_path[:-1]

    normalized = urlunsplit((parts.scheme, parts.netloc.lower(), normalized_path, "", ""))
    if normalized.rstrip("/") == base_url.rstrip("/"):
        return None, "homepage"
    return normalized, None


def _path_score(path: str) -> float:
    segments = [seg for seg in path.lower().split("/") if seg]
    score = 0.0
    for seg in segments:
        for keyword, value in _PATH_INTENT.items():
            if keyword == seg or keyword in seg:
                score = max(score, value)
                break
    depth = max(0, len(segments) - 1)
    return score - (0.3 * depth)


def _discover_from_links(
    homepage: ParsedPage,
    homepage_url: str,
    site_hosts: set[str],
    robots: RobotsRules,
    skipped: dict[str, int],
    considered: dict[str, dict],
) -> None:
    for link in homepage.links:
        normalized, reason = normalize_candidate_url(homepage_url, link.href, site_hosts)
        if normalized is None:
            skipped[reason or "other"] = skipped.get(reason or "other", 0) + 1
            continue
        path = urlsplit(normalized).path or "/"
        if not robots.is_path_allowed(path, "*"):
            skipped["robots"] = skipped.get("robots", 0) + 1
            continue

        entry = considered.setdefault(
            normalized,
            {"weight": 0, "sitemap": False, "texts": []},
        )
        entry["weight"] += link.weight
        if link.text:
            entry["texts"].append(link.text)


def _discover_from_sitemap(
    sitemap_urls: list[str],
    site_hosts: set[str],
    robots: RobotsRules,
    skipped: dict[str, int],
    considered: dict[str, dict],
    homepage_url: str,
    limit: int,
) -> None:
    for raw in sitemap_urls[:limit]:
        normalized, reason = normalize_candidate_url(homepage_url, raw, site_hosts)
        if normalized is None:
            if reason not in ("homepage", "query"):
                skipped[f"sitemap_{reason or 'other'}"] = skipped.get(f"sitemap_{reason or 'other'}", 0) + 1
            continue
        path = urlsplit(normalized).path or "/"
        if not robots.is_path_allowed(path, "*"):
            skipped["sitemap_robots"] = skipped.get("sitemap_robots", 0) + 1
            continue
        entry = considered.setdefault(
            normalized,
            {"weight": 0, "sitemap": False, "texts": []},
        )
        entry["sitemap"] = True


def discover_pages(
    homepage: ParsedPage,
    homepage_url: str,
    sitemap_urls: list[str] | None,
    robots: RobotsRules,
    max_pages: int,
    include_subdomains: bool = False,
) -> CrawlPlan:
    """Build a deterministic top-N crawl plan. Homepage is always included."""
    site_hosts = site_hosts_from_url(homepage_url, include_subdomains)
    skipped: dict[str, int] = {}
    considered: dict[str, dict] = {}

    _discover_from_links(homepage, homepage_url, site_hosts, robots, skipped, considered)
    _discover_from_sitemap(
        sitemap_urls or [],
        site_hosts,
        robots,
        skipped,
        considered,
        homepage_url,
        settings.crawl_max_discovered_links,
    )

    candidates: list[CrawlCandidate] = []
    for url, entry in considered.items():
        path = urlsplit(url).path or "/"
        score = (
            min(entry["weight"], 12) * 1.0
            + _path_score(path)
            + (1.0 if entry["sitemap"] else 0.0)
        )
        reason_bits = []
        if entry["weight"]:
            reason_bits.append(f"linked from homepage (weight {entry['weight']})")
        if entry["sitemap"]:
            reason_bits.append("listed in sitemap")
        reason_bits.append(f"path intent score {round(_path_score(path), 1)}")
        candidates.append(
            CrawlCandidate(
                url=url,
                score=round(score, 3),
                reason="; ".join(reason_bits),
            )
        )

    candidates.sort(key=lambda c: (-c.score, c.url))
    selected = [
        CrawlCandidate(
            url=homepage_url,
            score=1000.0,
            reason="homepage (seed)",
            role="homepage",
        )
    ]
    selected.extend(candidates[: max(0, max_pages - 1)])

    return CrawlPlan(
        homepage_url=homepage_url,
        selected=selected,
        discovered_count=len(candidates),
        skipped=skipped,
    )


def _response_content_type(result: FetchResult) -> str:
    headers = {str(k).lower(): str(v) for k, v in (result.headers or {}).items()}
    return headers.get("content-type", "").lower()


async def fetch_candidate(
    candidate: CrawlCandidate,
    site_hosts: set[str],
    remaining_seconds: float,
) -> FetchedPage:
    """Fetch and parse one candidate, never raising for page-level failures."""
    if remaining_seconds <= 0:
        return FetchedPage(
            url=candidate.url,
            role=candidate.role,
            selected_reason=candidate.reason,
            result=FetchResult(url=candidate.url, ok=False),
            error="audit_budget_exhausted",
        )

    timeout = min(settings.crawl_page_timeout_seconds, remaining_seconds)
    try:
        result = await asyncio.wait_for(fetch_html(candidate.url), timeout=timeout)
    except asyncio.TimeoutError:
        # If the shared crawl deadline, not the per-page limit, was the
        # binding constraint, report budget exhaustion rather than a timeout.
        budget_bound = remaining_seconds <= settings.crawl_page_timeout_seconds
        return FetchedPage(
            url=candidate.url,
            role=candidate.role,
            selected_reason=candidate.reason,
            result=FetchResult(url=candidate.url, ok=False),
            error="audit_budget_exhausted" if budget_bound else "page_timeout",
        )
    except Exception as exc:  # noqa: BLE001 — page failure must not kill the audit
        return FetchedPage(
            url=candidate.url,
            role=candidate.role,
            selected_reason=candidate.reason,
            result=FetchResult(url=candidate.url, ok=False),
            error=f"{type(exc).__name__}: {exc}",
        )

    if not result.ok or not result.text:
        return FetchedPage(
            url=candidate.url,
            role=candidate.role,
            selected_reason=candidate.reason,
            result=result,
            error=result.error or f"HTTP {result.status_code}",
        )

    content_type = _response_content_type(result)
    if content_type and "html" not in content_type and "xml" not in content_type:
        return FetchedPage(
            url=candidate.url,
            role=candidate.role,
            selected_reason=candidate.reason,
            result=result,
            error=f"non_html_content:{content_type.split(';')[0]}",
        )

    final_url = result.final_url or candidate.url
    final_host = (urlsplit(final_url).hostname or "").lower()
    if not _host_matches(final_host, site_hosts):
        return FetchedPage(
            url=candidate.url,
            role=candidate.role,
            selected_reason=candidate.reason,
            result=result,
            error="redirected_off_host",
        )

    try:
        parsed = await asyncio.to_thread(parse_html, result.text)
    except Exception as exc:  # noqa: BLE001
        return FetchedPage(
            url=candidate.url,
            role=candidate.role,
            selected_reason=candidate.reason,
            result=result,
            error=f"parse_error:{type(exc).__name__}: {exc}",
        )

    # X-Robots-Tag is an HTTP header, not a meta tag; merge it so the
    # orchestrator's noindex handling sees both signals.
    response_headers = {
        str(key).lower(): str(value) for key, value in (result.headers or {}).items()
    }
    x_robots = response_headers.get("x-robots-tag", "").lower().strip()
    if x_robots:
        parsed.meta_robots = f"{parsed.meta_robots} {x_robots}".strip()

    # Canonical validation: never follow/accept a canonical that leaves the
    # site. Same-site cross-page canonicals are valid and are used to
    # de-duplicate the crawl below.
    if parsed.canonical_url:
        canonical_abs = urljoin(final_url, parsed.canonical_url)
        try:
            canonical_parts = urlsplit(canonical_abs)
        except ValueError:
            canonical_parts = None
        if (
            canonical_parts is None
            or canonical_parts.scheme not in ("http", "https")
            or not _host_matches((canonical_parts.hostname or "").lower(), site_hosts)
        ):
            return FetchedPage(
                url=candidate.url,
                role=candidate.role,
                selected_reason=candidate.reason,
                result=result,
                error="canonical_off_host",
            )

    return FetchedPage(
        url=candidate.url,
        role=candidate.role,
        selected_reason=candidate.reason,
        result=result,
        parsed=parsed,
    )


async def fetch_pages(
    candidates: list[CrawlCandidate],
    site_hosts: set[str],
    total_budget_seconds: float,
    concurrency: int,
    homepage_url: str | None = None,
) -> list[FetchedPage]:
    """Fetch candidates concurrently with a shared monotonic deadline."""
    deadline = time.monotonic() + total_budget_seconds
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(candidate: CrawlCandidate) -> FetchedPage:
        async with semaphore:
            remaining = deadline - time.monotonic()
            try:
                return await fetch_candidate(candidate, site_hosts, remaining)
            except Exception as exc:  # noqa: BLE001 — one page never kills the crawl
                return FetchedPage(
                    url=candidate.url,
                    role=candidate.role,
                    selected_reason=candidate.reason,
                    result=FetchResult(url=candidate.url, ok=False),
                    error=f"{type(exc).__name__}: {exc}",
                )

    # Homepage is already fetched by the orchestrator; only fetch the rest.
    internal = [c for c in candidates if c.role != "homepage"]
    if not internal:
        return []
    results = list(await asyncio.gather(*(_one(c) for c in internal)))

    # De-duplicate by final URL and by same-site canonical target. The first
    # successful occurrence wins; failed fetches stay visible as failures.
    seen: set[str] = set()
    if homepage_url:
        seen.add(homepage_url.rstrip("/"))
    deduped: list[FetchedPage] = []
    for fetched in results:
        if not fetched.ok:
            deduped.append(fetched)
            continue
        final = (fetched.result.final_url or fetched.url).rstrip("/")
        canonical = None
        if fetched.parsed and fetched.parsed.canonical_url:
            canonical = urljoin(final, fetched.parsed.canonical_url).rstrip("/")
        key = canonical or final
        if key in seen:
            continue
        seen.add(key)
        seen.add(final)
        deduped.append(fetched)
    return deduped
