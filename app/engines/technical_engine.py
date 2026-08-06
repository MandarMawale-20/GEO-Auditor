"""Deterministic checks for crawlability, rendering, and freshness."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import AI_CRAWLER_POINTS, CRAWLER_ALIASES, CATEGORY_META
from app.models import AuditSection, EffortLevel, Finding, ImpactLevel, Severity
from app.services.html_utils import ParsedPage, get_meta_content, has_spa_markers
from app.services.robots import RobotsRules
from app.services.sitemap import SitemapResult


def score_ai_crawler_accessibility(robots: RobotsRules) -> AuditSection:
    meta = CATEGORY_META["ai_crawler_accessibility"]
    section = AuditSection(
        key="ai_crawler_accessibility",
        label=meta["label"],
        group=meta["group"],
        points_possible=15,
    )

    if not robots.exists:
        # No robots.txt = nothing is disallowed, so every bot earns full points.
        section.points_awarded = 15
        section.findings.append(
            Finding(
                category="ai_crawler_accessibility",
                severity=Severity.INFO,
                title="No robots.txt found — AI crawlers are unrestricted by default",
                why_it_matters=(
                    "Without a robots.txt file, all crawlers (including AI search bots) "
                    "are implicitly allowed. This is fine, but an explicit robots.txt "
                    "gives you control and signals intentionality."
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
                points_awarded=15,
                points_possible=15,
            )
        )
        return section

    points_awarded = 0.0
    for bot_name, points in AI_CRAWLER_POINTS.items():
        aliases = [bot_name] + [
            alias for alias, canonical in CRAWLER_ALIASES.items() if canonical == bot_name
        ]
        blocked = any(robots.is_blocked(alias) for alias in aliases)
        mentioned = any(robots.is_mentioned(alias) for alias in aliases)

        if blocked:
            section.findings.append(
                Finding(
                    category="ai_crawler_accessibility",
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
                    points_awarded=0,
                    points_possible=points,
                )
            )
        else:
            points_awarded += points
            section.findings.append(
                Finding(
                    category="ai_crawler_accessibility",
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
                    points_awarded=points,
                    points_possible=points,
                )
            )

    section.points_awarded = points_awarded
    return section


def score_js_rendering_risk(parsed: ParsedPage) -> AuditSection:
    meta = CATEGORY_META["js_rendering_risk"]
    section = AuditSection(
        key="js_rendering_risk",
        label=meta["label"],
        group=meta["group"],
        points_possible=5,
    )

    word_count = parsed.word_count
    spa_markers_present = has_spa_markers(parsed.raw_html)

    if word_count >= 150:
        section.points_awarded = 5
        severity = Severity.PASS
        title = "Raw HTML contains sufficient text for AI crawlers"
        recommendation = "No action needed — content is server-rendered or static."
    elif word_count >= 50:
        section.points_awarded = 2
        severity = Severity.WARNING
        title = "Raw HTML has limited text — partial rendering risk"
        recommendation = (
            "Increase server-rendered text content, or implement SSR/pre-rendering "
            "so AI crawlers (which typically do not execute JavaScript) see full content."
        )
    else:
        section.points_awarded = 0
        severity = Severity.CRITICAL if spa_markers_present else Severity.WARNING
        title = (
            "Raw HTML is nearly empty — likely a client-rendered SPA"
            if spa_markers_present
            else "Raw HTML has very little text"
        )
        recommendation = (
            "This page appears to render content client-side via JavaScript. Most AI "
            "crawlers do not execute JS, so implement server-side rendering (SSR), "
            "static generation, or dynamic rendering for bot user-agents."
        )

    section.findings.append(
        Finding(
            category="js_rendering_risk",
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
                f"(checked for __NEXT_DATA__, id=\"root\", bundle.js, __NUXT__)."
            ),
            recommendation=recommendation,
            copy_paste_fix=None,
            impact=ImpactLevel.HIGH if word_count < 50 else ImpactLevel.MEDIUM,
            effort=EffortLevel.HIGH,
            points_awarded=section.points_awarded,
            points_possible=5,
        )
    )
    return section


def score_freshness_signals(parsed: ParsedPage, sitemap: SitemapResult) -> AuditSection:
    meta = CATEGORY_META["freshness_signals"]
    section = AuditSection(
        key="freshness_signals",
        label=meta["label"],
        group=meta["group"],
        points_possible=5,
    )

    candidates: list[tuple[str, datetime]] = []

    article_modified = get_meta_content(parsed.soup, "article:modified_time", attr="property")
    if article_modified:
        try:
            dt = datetime.fromisoformat(article_modified.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            candidates.append(("meta article:modified_time", dt))
        except ValueError:
            pass

    for block in parsed.json_ld_blocks:
        date_modified = block.get("dateModified")
        if isinstance(date_modified, str):
            try:
                dt = datetime.fromisoformat(date_modified.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                candidates.append(("JSON-LD dateModified", dt))
            except ValueError:
                pass

    if sitemap.most_recent_lastmod:
        candidates.append(("sitemap.xml <lastmod>", sitemap.most_recent_lastmod))

    if not candidates:
        section.points_awarded = 0
        section.findings.append(
            Finding(
                category="freshness_signals",
                severity=Severity.WARNING,
                title="No freshness signal found",
                why_it_matters=(
                    "AI answer engines weight recency when selecting sources. Without a "
                    "dateModified, article:modified_time, or sitemap lastmod, the page "
                    "looks potentially stale even if content is current."
                ),
                evidence="No JSON-LD dateModified, article:modified_time meta tag, or sitemap lastmod found.",
                recommendation="Add a dateModified field to your Organization/Article JSON-LD and keep sitemap.xml lastmod values current.",
                copy_paste_fix='"dateModified": "' + datetime.now(timezone.utc).strftime("%Y-%m-%d") + '"',
                impact=ImpactLevel.MEDIUM,
                effort=EffortLevel.LOW,
                points_awarded=0,
                points_possible=5,
            )
        )
        return section

    source_label, most_recent = max(candidates, key=lambda c: c[1])
    days_old = (datetime.now(timezone.utc) - most_recent).days

    if days_old < 90:
        points, severity = 5, Severity.PASS
    elif days_old <= 365:
        points, severity = 2, Severity.WARNING
    else:
        points, severity = 0, Severity.WARNING

    section.points_awarded = points
    section.findings.append(
        Finding(
            category="freshness_signals",
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
            points_awarded=points,
            points_possible=5,
        )
    )
    return section
