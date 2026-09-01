"""Async robots.txt parser.

Deliberately avoids ``urllib.robotparser`` because we need per-AI-crawler
visibility (OAI-SearchBot, PerplexityBot, ClaudeBot/Claude-Web, Googlebot)
rather than a single allow/deny verdict, and we need to keep going on
partially malformed robots.txt files without raising.

Fetches robots.txt through the centralized SSRF-safe HTTP client.
"""
from __future__ import annotations

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
    sitemap_urls: list[str] = field(default_factory=list)

    def is_blocked(self, user_agent: str) -> bool:
        """True if this user-agent is explicitly disallowed from '/' (or entirely)."""
        agent_key = user_agent.lower()
        rules = self.disallow_by_agent.get(agent_key)
        if rules is None:
            # Fall back to wildcard rules if the specific bot isn't mentioned.
            rules = self.disallow_by_agent.get("*", [])
        return any(path.strip() == "/" for path in rules)

    def is_mentioned(self, user_agent: str) -> bool:
        return user_agent.lower() in self.disallow_by_agent


def _parse_robots_txt(
    raw_text: str, base_url: str
) -> tuple[dict[str, list[str]], list[str]]:
    """Parse robots.txt into ``{user-agent: [disallow paths]}`` + sitemap URLs.

    ``base_url`` is the fully-qualified robots.txt URL, used to resolve
    relative sitemap paths per RFC 9309.
    """
    disallow_by_agent: dict[str, list[str]] = {}
    sitemaps: list[str] = []

    current_agents: list[str] = []
    for line in raw_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            agent = value.lower()
            if current_agents and _has_rules(disallow_by_agent, current_agents):
                # A new User-agent after a group with rules starts a fresh group.
                current_agents = [agent]
            elif agent not in current_agents:
                current_agents = current_agents + [agent] if current_agents else [agent]
            for agent_name in current_agents:
                disallow_by_agent.setdefault(agent_name, [])
        elif field_name == "disallow" and current_agents:
            for agent_name in current_agents:
                if value:
                    disallow_by_agent.setdefault(agent_name, []).append(value)
                else:
                    disallow_by_agent.setdefault(agent_name, [])
        elif field_name == "sitemap":
            sitemaps.append(urljoin(base_url, value))

    return disallow_by_agent, sitemaps


def _has_rules(
    disallow_by_agent: dict[str, list[str]], agents: list[str]
) -> bool:
    return any(disallow_by_agent.get(a) for a in agents)


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

    disallow_by_agent, sitemaps = _parse_robots_txt(result.text, robots_url)
    return RobotsRules(
        exists=True,
        raw_text=result.text,
        disallow_by_agent=disallow_by_agent,
        sitemap_urls=sitemaps,
    )
