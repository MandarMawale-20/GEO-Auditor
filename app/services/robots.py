"""Async robots.txt parser with RFC 9309-style path matching.

Deliberately avoids ``urllib.robotparser`` because we need per-AI-crawler
visibility (OAI-SearchBot, PerplexityBot, ClaudeBot/Claude-Web, Googlebot)
rather than a single allow/deny verdict, and we need to keep going on
partially malformed robots.txt files without raising.

Crawl filtering uses the ``*`` group (the generic crawler contract), while
scoring uses the AI-bot groups.

Fetches robots.txt through the centralized SSRF-safe HTTP client.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from app.services.http_client import safe_get_text


@dataclass
class RobotsRules:
    """Parsed robots.txt, keyed by exact user-agent string as written in the file."""

    exists: bool = False
    fetch_error: str | None = None
    raw_text: str = ""
    # user_agent (lowercased) -> list of Disallow paths
    disallow_by_agent: dict[str, list[str]] = field(default_factory=dict)
    # user_agent (lowercased) -> list of Allow paths
    allow_by_agent: dict[str, list[str]] = field(default_factory=dict)
    sitemap_urls: list[str] = field(default_factory=list)

    def is_blocked(self, user_agent: str) -> bool:
        """True if this user-agent is disallowed from the site root."""
        return not self.is_path_allowed("/", user_agent)

    def is_path_allowed(self, path: str, user_agent: str = "*") -> bool:
        """Longest-match evaluation of Allow/Disallow rules for a path.

        Missing rules mean allowed. Allow wins ties, per RFC 9309.
        """
        agent = (user_agent or "*").lower()
        allow = self.allow_by_agent.get(agent)
        disallow = self.disallow_by_agent.get(agent)
        if allow is None and disallow is None:
            allow = self.allow_by_agent.get("*", [])
            disallow = self.disallow_by_agent.get("*", [])

        if not allow and not disallow:
            return True

        best_len = -1
        best_allowed = True
        for pattern in allow or []:
            if _pattern_matches(pattern, path):
                if len(pattern) > best_len:
                    best_len = len(pattern)
                    best_allowed = True
        for pattern in disallow or []:
            if _pattern_matches(pattern, path):
                if len(pattern) > best_len:
                    best_len = len(pattern)
                    best_allowed = False
        return best_allowed if best_len >= 0 else True

    def is_mentioned(self, user_agent: str) -> bool:
        return user_agent.lower() in self.disallow_by_agent


def _pattern_matches(pattern: str, path: str) -> bool:
    if not pattern:
        return False
    anchored_end = pattern.endswith("$")
    if anchored_end:
        pattern = pattern[:-1]
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    if anchored_end:
        return re.match(f"^{regex}$", path) is not None
    return re.match(f"^{regex}", path) is not None


def _parse_robots_txt(
    raw_text: str, base_url: str
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Parse robots.txt into allow/disallow maps + sitemap URLs.

    ``base_url`` is the fully-qualified robots.txt URL, used to resolve
    relative sitemap paths per RFC 9309.
    """
    disallow_by_agent: dict[str, list[str]] = {}
    allow_by_agent: dict[str, list[str]] = {}
    sitemaps: list[str] = []

    current_agents: list[str] = []
    group_has_rules = False

    for line in raw_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            agent = value.lower()
            if group_has_rules:
                current_agents = [agent]
                group_has_rules = False
            elif agent not in current_agents:
                current_agents = current_agents + [agent] if current_agents else [agent]
            for agent_name in current_agents:
                disallow_by_agent.setdefault(agent_name, [])
                allow_by_agent.setdefault(agent_name, [])
        elif field_name in ("allow", "disallow") and current_agents:
            group_has_rules = True
            target = allow_by_agent if field_name == "allow" else disallow_by_agent
            for agent_name in current_agents:
                if value:
                    target.setdefault(agent_name, []).append(value)
                else:
                    target.setdefault(agent_name, [])
        elif field_name == "sitemap":
            sitemaps.append(urljoin(base_url, value))

    return disallow_by_agent, allow_by_agent, sitemaps


async def get_robots_rules(page_url: str) -> RobotsRules:
    """Fetch and parse robots.txt for the domain hosting *page_url*."""
    parsed = urlparse(page_url)
    if not parsed.scheme or not parsed.netloc:
        return RobotsRules(
            exists=False, fetch_error="Invalid base URL for robots.txt lookup."
        )

    robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
    result = await safe_get_text(robots_url, allow_redirects=True, max_redirects=5)

    if not result.ok or not result.text:
        # Missing robots.txt means "no restrictions" per spec.
        return RobotsRules(exists=False, fetch_error=result.error)

    disallow_by_agent, allow_by_agent, sitemaps = _parse_robots_txt(result.text, robots_url)
    return RobotsRules(
        exists=True,
        raw_text=result.text,
        disallow_by_agent=disallow_by_agent,
        allow_by_agent=allow_by_agent,
        sitemap_urls=sitemaps,
    )
