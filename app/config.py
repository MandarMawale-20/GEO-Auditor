"""Central configuration: settings, scoring weights, and shared constants.

`SCORE_WEIGHTS` is the single source of truth — engines, scoring.py, and the
report renderer all read from here. The startup-time assertion guarantees
the model always sums to exactly 100.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# Frozen 100-point scoring model. The original PRD table summed to 110; this
# rebalances Structured Entity Data (15 -> 10) and Query Coverage (10 -> 15)
# so established brands aren't over-penalized for missing on-page schema and
# content answerability is weighted more heavily. See README "Scoring
# Methodology" for the rationale.
SCORE_WEIGHTS: dict[str, int] = {
    "ai_crawler_accessibility": 15,
    "structured_entity_data": 10,
    "entity_completeness_trust": 10,
    "quote_readiness": 15,
    "query_coverage": 15,
    "ai_citation_snippet_quality": 10,
    "fact_density": 10,
    "freshness_signals": 5,
    "js_rendering_risk": 5,
    "media_to_text_balance": 5,
}

TOTAL_POSSIBLE_SCORE = sum(SCORE_WEIGHTS.values())
if TOTAL_POSSIBLE_SCORE != 100:
    raise ValueError(
        f"Scoring model must sum to exactly 100 points, got {TOTAL_POSSIBLE_SCORE}"
    )

CATEGORY_META: dict[str, dict[str, str]] = {
    "ai_crawler_accessibility": {
        "label": "AI Crawler Accessibility",
        "group": "Technical Accessibility",
        "engine": "technical",
    },
    "js_rendering_risk": {
        "label": "JavaScript Rendering Risk",
        "group": "Technical Accessibility",
        "engine": "technical",
    },
    "structured_entity_data": {
        "label": "Structured Entity Data",
        "group": "Entity and Trust",
        "engine": "entity",
    },
    "entity_completeness_trust": {
        "label": "Entity Completeness and Trust Signals",
        "group": "Entity and Trust",
        "engine": "entity",
    },
    "quote_readiness": {
        "label": "Quote Readiness / Answer Extractability",
        "group": "Content Extractability",
        "engine": "content",
    },
    "fact_density": {
        "label": "Fact Density and Structured Tables",
        "group": "Content Extractability",
        "engine": "content",
    },
    "media_to_text_balance": {
        "label": "Media-to-Text Balance",
        "group": "Content Extractability",
        "engine": "content",
    },
    "query_coverage": {
        "label": "Query Coverage",
        "group": "Value and Freshness",
        "engine": "intelligence",
    },
    "ai_citation_snippet_quality": {
        "label": "AI Citation Snippet Quality",
        "group": "Value and Freshness",
        "engine": "intelligence",
    },
    "freshness_signals": {
        "label": "Freshness Signals",
        "group": "Value and Freshness",
        "engine": "technical",
    },
}

# AI bots we score against robots.txt. Higher weights = more impactful bots.
AI_CRAWLER_POINTS: dict[str, int] = {
    "OAI-SearchBot": 5,
    "PerplexityBot": 5,
    "ClaudeBot": 3,
    "Googlebot": 2,
}

# Claude-Web is treated as ClaudeBot for scoring purposes.
CRAWLER_ALIASES: dict[str, str] = {
    "Claude-Web": "ClaudeBot",
}

TRUSTED_SAMEAS_DOMAINS: tuple[str, ...] = (
    "wikipedia.org",
    "wikidata.org",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "crunchbase.com",
)

SPA_MARKERS: tuple[str, ...] = (
    "__next_data__",
    'id="root"',
    "id='root'",
    "bundle.js",
    "__nuxt__",
)

# Scoring thresholds — engines must not hardcode these values.
CITATION_MIN_WORDS: int = 20
CITATION_MAX_WORDS: int = 50

SUFFICIENT_TEXT_WORDS: int = 150
LIMITED_TEXT_WORDS: int = 50

MIN_NUMERIC_DENSITY: int = 3  # numeric tokens per 500 words for full points

MEDIA_RATIO_HEALTHY: int = 50  # words per image — healthy
MEDIA_RATIO_CRITICAL: int = 20  # words per image — critical threshold

# Multipage crawl limits. These are intentionally conservative: every audit
# fans out to at most CRAWL_MAX_PAGES same-site requests, and the whole crawl
# is bounded by CRAWL_TOTAL_BUDGET_SECONDS.
MAX_CRAWL_PAGES: int = 10
# Compressed base64 URL-length guard. Browser address bars commonly truncate
# around 64k characters, so a larger payload is not reliably shareable even
# though the reporting pipeline can build it.
MAX_SHARE_PAYLOAD_BYTES: int = 64_000


@dataclass(frozen=True)
class Settings:
    groq_api_key: str | None = field(
        default_factory=lambda: os.getenv("GROQ_API_KEY") or None
    )
    groq_model: str = field(
        default_factory=lambda: os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    )
    groq_base_url: str = field(
        default_factory=lambda: os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    )
    request_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT_SECONDS", "8.0"))
    )
    llm_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("LLM_TIMEOUT_SECONDS", "6.0"))
    )
    max_html_bytes: int = field(
        default_factory=lambda: int(os.getenv("MAX_HTML_BYTES", str(3_000_000)))
    )
    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))

    # Multipage crawl settings.
    crawl_enabled: bool = field(
        default_factory=lambda: os.getenv("CRAWL_ENABLED", "true").lower() not in ("0", "false", "no")
    )
    crawl_max_pages: int = field(
        default_factory=lambda: max(1, min(int(os.getenv("CRAWL_MAX_PAGES", "10")), MAX_CRAWL_PAGES))
    )
    crawl_concurrency: int = field(
        default_factory=lambda: max(1, min(int(os.getenv("CRAWL_CONCURRENCY", "3")), 8))
    )
    crawl_total_budget_seconds: float = field(
        default_factory=lambda: float(os.getenv("CRAWL_TOTAL_BUDGET_SECONDS", "15.0"))
    )
    crawl_page_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("CRAWL_PAGE_TIMEOUT_SECONDS", "8.0"))
    )
    crawl_max_discovered_links: int = field(
        default_factory=lambda: int(os.getenv("CRAWL_MAX_DISCOVERED_LINKS", "200"))
    )
    crawl_include_subdomains: bool = field(
        default_factory=lambda: os.getenv("CRAWL_INCLUDE_SUBDOMAINS", "false").lower() in ("1", "true", "yes")
    )
    crawl_llm_max_pages: int = field(
        default_factory=lambda: max(1, int(os.getenv("CRAWL_LLM_MAX_PAGES", "2")))
    )
    homepage_weight: float = field(
        default_factory=lambda: max(0.1, float(os.getenv("HOMEPAGE_WEIGHT", "2.0")))
    )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)


settings = Settings()


DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
