"""Deterministic checks for crawlability, rendering, and freshness."""
from __future__ import annotations

from datetime import datetime, timezone

from app.checks import build_check
from app.config import (
    AI_CRAWLER_POINTS,
    CATEGORY_META,
    CRAWLER_ALIASES,
    LIMITED_TEXT_WORDS,
    SUFFICIENT_TEXT_WORDS,
)
from app.models import AuditSection, CheckStatus, EffortLevel, ImpactLevel, Severity
from app.services.html_utils import ParsedPage
from app.services.robots import RobotsRules
from app.services.sitemap import SitemapResult
from app.utils import parse_iso_datetime

_CRAWLER_CHECK_IDS = {
    "OAI-SearchBot": "crawler.openai_searchbot",
    "PerplexityBot": "crawler.perplexitybot",
    "ClaudeBot": "crawler.claudebot",
    "Googlebot": "crawler.googlebot",
}


def _robots_lookup_state(robots: RobotsRules) -> str:
    """Distinguish a missing robots.txt from an unevaluable lookup.

    RFC 9309 treats 4xx as "no restrictions", while 5xx/429 and transport
    failures mean the rules are unknown. Scoring an unreachable robots.txt as
    a full pass would hide a real crawlability risk.
    """
    status = robots.fetch_status
    if status is not None:
        if 200 <= status < 300:
            return "missing"  # Successful response with an empty body.
        if 400 <= status < 500 and status != 429:
            return "missing"
        return "unknown"
    if robots.fetch_error is None:
        return "missing"  # Explicit no-robots fixture / caller decision.
    return "unknown"


def score_ai_crawler_accessibility(robots: RobotsRules) -> AuditSection:
    meta = CATEGORY_META["ai_crawler_accessibility"]
    section = AuditSection(
        key="ai_crawler_accessibility",
        label=meta["label"],
        group=meta["group"],
        points_possible=15,
    )

    if not robots.exists:
        if _robots_lookup_state(robots) == "unknown":
            detail = robots.fetch_error or (
                f"HTTP {robots.fetch_status}" if robots.fetch_status else "unreachable"
            )
            for bot_name in AI_CRAWLER_POINTS:
                section.add_check(
                    build_check(
                        _CRAWLER_CHECK_IDS[bot_name],
                        status=CheckStatus.UNKNOWN,
                        severity=Severity.INFO,
                        title=f"Could not evaluate {bot_name}: robots.txt unavailable",
                        why_it_matters=(
                            "Without a readable robots.txt, this audit cannot tell "
                            "whether the crawler is blocked."
                        ),
                        evidence=f"robots.txt lookup failed: {detail}",
                        recommendation=(
                            "Make robots.txt publicly reachable, then re-run the audit. "
                            "Do not assume the bot is allowed."
                        ),
                        impact=ImpactLevel.LOW,
                        effort=EffortLevel.LOW,
                    )
                )
            return section

        # No robots.txt = nothing is disallowed, so every bot earns full points.
        for bot_name in AI_CRAWLER_POINTS:
            section.add_check(
                build_check(
                    _CRAWLER_CHECK_IDS[bot_name],
                    status=CheckStatus.PASS,
                    severity=Severity.INFO,
                    title=f"{bot_name} is unrestricted (no robots.txt found)",
                    why_it_matters=(
                        "Without a robots.txt, all crawlers are implicitly allowed. "
                        "An explicit robots.txt gives you control and signals intent."
                    ),
                    evidence="robots.txt returned a non-200 response or was unreachable.",
                    recommendation=(
                        "Add a robots.txt that explicitly allows OAI-SearchBot, PerplexityBot, "
                        "ClaudeBot, and Googlebot to make AI crawlability unambiguous."
                    ),
                    copy_paste_fix=(
                        "User-agent: OAI-SearchBot\nAllow: /\n\n"
                        "User-agent: PerplexityBot\nAllow: /\n\n"
                        "User-agent: ClaudeBot\nAllow: /\n\n"
                        "User-agent: Googlebot\nAllow: /\n\n"
                        "Sitemap: https://yourdomain.com/sitemap.xml"
                    ),
                    impact=ImpactLevel.LOW,
                    effort=EffortLevel.LOW,
                )
            )
        return section

    for bot_name, points in AI_CRAWLER_POINTS.items():
        aliases = [bot_name] + [
            alias for alias, canonical in CRAWLER_ALIASES.items() if canonical == bot_name
        ]
        blocked = any(robots.is_blocked(alias) for alias in aliases)
        mentioned = any(robots.is_mentioned(alias) for alias in aliases)
        check_id = _CRAWLER_CHECK_IDS[bot_name]

        if blocked:
            section.add_check(
                build_check(
                    check_id,
                    status=CheckStatus.FAIL,
                    severity=Severity.CRITICAL,
                    title=f"{bot_name} is blocked in robots.txt",
                    why_it_matters=(
                        f"{bot_name} cannot crawl this site at all, so content is "
                        "structurally invisible to that AI answer engine regardless "
                        "of content quality."
                    ),
                    evidence=f"robots.txt disallows '/' for user-agent matching {bot_name}.",
                    recommendation=f"Remove the 'Disallow: /' rule for {bot_name} or scope it narrowly.",
                    copy_paste_fix=f"User-agent: {bot_name}\nAllow: /",
                    impact=ImpactLevel.HIGH,
                    effort=EffortLevel.LOW,
                )
            )
        else:
            section.add_check(
                build_check(
                    check_id,
                    status=CheckStatus.PASS,
                    severity=Severity.PASS,
                    title=f"{bot_name} is allowed to crawl",
                    why_it_matters=f"{bot_name} can access and index this site's content.",
                    evidence=(
                        f"robots.txt {'explicitly allows' if mentioned else 'does not mention'} "
                        f"{bot_name} (no 'Disallow: /' found)."
                    ),
                    recommendation="No action needed.",
                    impact=ImpactLevel.LOW,
                    effort=EffortLevel.LOW,
                )
            )

    return section


def score_js_rendering_risk(parsed: ParsedPage, page_url: str | None = None) -> AuditSection:
    meta = CATEGORY_META["js_rendering_risk"]
    section = AuditSection(
        key="js_rendering_risk",
        label=meta["label"],
        group=meta["group"],
        points_possible=5,
    )

    word_count = parsed.word_count
    spa_markers_present = parsed.spa_markers_present

    if word_count >= SUFFICIENT_TEXT_WORDS:
        points, status, severity = 5, CheckStatus.PASS, Severity.PASS
        title = "Raw HTML contains sufficient text for AI crawlers"
        recommendation = "No action needed — content is server-rendered or static."
    elif word_count >= LIMITED_TEXT_WORDS:
        points, status, severity = 2, CheckStatus.PARTIAL, Severity.WARNING
        title = "Raw HTML has limited text — partial rendering risk"
        recommendation = (
            "Increase server-rendered text content, or implement SSR/pre-rendering "
            "so AI crawlers (which typically do not execute JavaScript) see full content."
        )
    else:
        points, status = 0, CheckStatus.FAIL
        severity = Severity.CRITICAL if spa_markers_present else Severity.WARNING
        title = (
            "Raw HTML is nearly empty — likely a client-rendered SPA"
            if spa_markers_present
            else "Raw HTML has very little text"
        )
        recommendation = (
            "Most AI crawlers do not execute JavaScript. Implement server-side "
            "rendering, static generation, or dynamic rendering for bot user-agents."
        )

    section.add_check(
        build_check(
            "js.raw_text",
            status=status,
            severity=severity,
            title=title,
            why_it_matters=(
                "AI crawlers (OAI-SearchBot, PerplexityBot, ClaudeBot) generally fetch raw "
                "HTML without executing JavaScript. If content only appears after JS runs, "
                "it is invisible to these systems."
            ),
            evidence=(
                f"Raw HTML contains approximately {word_count} visible words. "
                f"SPA markers detected: {spa_markers_present} "
                f'(checked for __NEXT_DATA__, id="root", bundle.js, __NUXT__).'
            ),
            recommendation=recommendation,
            impact=ImpactLevel.HIGH if word_count < LIMITED_TEXT_WORDS else ImpactLevel.MEDIUM,
            effort=EffortLevel.HIGH,
            page_url=page_url,
            points_awarded=points,
        )
    )
    return section


def score_freshness_signals(
    parsed: ParsedPage, sitemap: SitemapResult, page_url: str | None = None
) -> AuditSection:
    meta = CATEGORY_META["freshness_signals"]
    section = AuditSection(
        key="freshness_signals",
        label=meta["label"],
        group=meta["group"],
        points_possible=5,
    )

    candidates: list[tuple[str, datetime]] = []

    article_modified = parsed.meta_contents.get("article:modified_time")
    if article_modified:
        dt = parse_iso_datetime(article_modified)
        if dt:
            candidates.append(("meta article:modified_time", dt))

    for block in parsed.json_ld_blocks:
        date_modified = block.get("dateModified")
        if isinstance(date_modified, str):
            dt = parse_iso_datetime(date_modified)
            if dt:
                candidates.append(("JSON-LD dateModified", dt))

    if sitemap.most_recent_lastmod:
        candidates.append(("sitemap.xml <lastmod>", sitemap.most_recent_lastmod))

    if not candidates:
        section.add_check(
            build_check(
                "freshness.latest_signal",
                status=CheckStatus.FAIL,
                severity=Severity.WARNING,
                title="No freshness signal found",
                why_it_matters=(
                    "AI answer engines weight recency when selecting sources. Without a "
                    "dateModified, article:modified_time, or sitemap lastmod, the page "
                    "looks potentially stale even if content is current."
                ),
                evidence=(
                    "No JSON-LD dateModified, article:modified_time meta tag, or sitemap "
                    "lastmod found."
                ),
                recommendation=(
                    "Add a dateModified field to your Organization/Article JSON-LD and "
                    "keep sitemap.xml lastmod values current."
                ),
                copy_paste_fix='"dateModified": "' + datetime.now(timezone.utc).strftime("%Y-%m-%d") + '"',
                impact=ImpactLevel.MEDIUM,
                effort=EffortLevel.LOW,
                page_url=page_url,
            )
        )
        return section

    source_label, most_recent = max(candidates, key=lambda c: c[1])
    days_old = (datetime.now(timezone.utc) - most_recent).days

    if days_old < 90:
        points, status, severity = 5, CheckStatus.PASS, Severity.PASS
    elif days_old <= 365:
        points, status, severity = 2, CheckStatus.PARTIAL, Severity.WARNING
    else:
        points, status, severity = 0, CheckStatus.FAIL, Severity.WARNING

    section.add_check(
        build_check(
            "freshness.latest_signal",
            status=status,
            severity=severity,
            title=f"Most recent freshness signal is {days_old} days old",
            why_it_matters="AI systems favor recently updated content as more trustworthy and current.",
            evidence=f"Found via {source_label}: {most_recent.date().isoformat()}.",
            recommendation=(
                "No action needed."
                if days_old < 90
                else "Update this timestamp when content is meaningfully revised, and refresh page content periodically."
            ),
            impact=ImpactLevel.LOW if days_old < 90 else ImpactLevel.MEDIUM,
            effort=EffortLevel.LOW,
            page_url=page_url,
            points_awarded=points,
            metrics={"days_old": days_old, "source": source_label},
        )
    )
    return section
