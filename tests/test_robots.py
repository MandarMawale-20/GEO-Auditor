"""robots.txt parsing: Allow/Disallow longest-match, wildcards, groups."""
from __future__ import annotations

from app.engines.technical_engine import score_ai_crawler_accessibility
from app.models import CheckStatus
from app.services.robots import RobotsRules, _parse_robots_txt, _pattern_matches


def _rules(text: str) -> RobotsRules:
    disallow, allow, sitemaps = _parse_robots_txt(text, "https://example.com/robots.txt")
    return RobotsRules(
        exists=True,
        raw_text=text,
        disallow_by_agent=disallow,
        allow_by_agent=allow,
        sitemap_urls=sitemaps,
    )


def test_no_rules_allows_everything() -> None:
    rules = _rules("")
    assert rules.is_path_allowed("/")
    assert rules.is_path_allowed("/anything")
    assert not rules.is_blocked("OAI-SearchBot")


def test_disallow_root_blocks_agent() -> None:
    rules = _rules("User-agent: *\nDisallow: /\n")
    assert rules.is_blocked("OAI-SearchBot")
    assert not rules.is_path_allowed("/pricing", "*")


def test_allow_wins_tie_and_longest_match() -> None:
    rules = _rules(
        "User-agent: *\n"
        "Disallow: /private\n"
        "Allow: /private/public\n"
    )
    assert rules.is_path_allowed("/private/public/page", "*")
    assert not rules.is_path_allowed("/private/secret", "*")
    assert rules.is_path_allowed("/", "*")


def test_longer_disallow_wins_over_shorter_allow() -> None:
    rules = _rules(
        "User-agent: *\n"
        "Allow: /docs\n"
        "Disallow: /docs/internal\n"
    )
    assert rules.is_path_allowed("/docs/intro", "*")
    assert not rules.is_path_allowed("/docs/internal/page", "*")


def test_wildcards_and_end_anchor() -> None:
    assert _pattern_matches("/files/*.pdf$", "/files/report.pdf")
    assert not _pattern_matches("/files/*.pdf$", "/files/report.pdf?download=1")
    assert _pattern_matches("/search*", "/search/results")


def test_agent_specific_group_overrides_wildcard() -> None:
    rules = _rules(
        "User-agent: *\n"
        "Disallow: /\n\n"
        "User-agent: OAI-SearchBot\n"
        "Allow: /\n"
    )
    assert rules.is_blocked("PerplexityBot")
    assert not rules.is_blocked("OAI-SearchBot")
    assert rules.is_path_allowed("/pricing", "OAI-SearchBot")


def test_group_with_multiple_user_agents() -> None:
    rules = _rules(
        "User-agent: OAI-SearchBot\n"
        "User-agent: PerplexityBot\n"
        "Disallow: /private\n"
    )
    assert not rules.is_path_allowed("/private/x", "OAI-SearchBot")
    assert not rules.is_path_allowed("/private/x", "PerplexityBot")
    assert rules.is_path_allowed("/public", "PerplexityBot")


def test_sitemaps_are_extracted_and_resolved() -> None:
    rules = _rules("Sitemap: /sitemap.xml\nSitemap: https://example.com/news.xml\n")
    assert rules.sitemap_urls == [
        "https://example.com/sitemap.xml",
        "https://example.com/news.xml",
    ]


def test_missing_specific_group_falls_back_to_wildcard() -> None:
    rules = _rules("User-agent: *\nDisallow: /admin\n")
    assert not rules.is_path_allowed("/admin", "ClaudeBot")
    assert rules.is_path_allowed("/blog", "ClaudeBot")


def test_robots_404_is_treated_as_no_restrictions() -> None:
    rules = RobotsRules(exists=False, fetch_status=404, fetch_error=None)
    section = score_ai_crawler_accessibility(rules)
    assert section.points_awarded == 15
    assert all(check.status == CheckStatus.PASS for check in section.checks)


def test_unreachable_robots_is_unknown_not_a_full_pass() -> None:
    rules = RobotsRules(
        exists=False,
        fetch_status=None,
        fetch_error="ConnectTimeout: robots lookup timed out",
    )
    section = score_ai_crawler_accessibility(rules)
    assert section.points_awarded == 0
    assert section.points_evaluated == 0
    assert all(check.status == CheckStatus.UNKNOWN for check in section.checks)


def test_robots_5xx_is_unknown() -> None:
    rules = RobotsRules(exists=False, fetch_status=503, fetch_error="HTTP 503")
    section = score_ai_crawler_accessibility(rules)
    assert section.points_awarded == 0
    assert all(check.status == CheckStatus.UNKNOWN for check in section.checks)
