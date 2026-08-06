"""
Central configuration for GEO Auditor.

Holds environment-driven settings, HTTP client defaults, and the FROZEN
100-point scoring model. The scoring weights below are the single source
of truth — engines, scoring.py, and report templates all read from here
so the numbers can never drift out of sync.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------------------------------
# Frozen 100-point scoring model
# --------------------------------------------------------------------------
# NOTE: The original PRD table summed to 110. This is the rebalanced model
# that sums to exactly 100. Category names and grouping are unchanged.
# Query Coverage and Structured Entity Data were rebalanced to better
# reflect real-world GEO behavior on established sites: increasing the
# weight of query coverage (content answerability) and reducing the weight
# of structured entity data, which established brands may lack on-page
# while still performing well via off-page authority signals.
# See README "Scoring Methodology" for rationale.

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
assert TOTAL_POSSIBLE_SCORE == 100, "Scoring model must sum to exactly 100 points"

# Human-readable labels + which engine owns each category, for templates
# and orchestration bookkeeping.
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


# --------------------------------------------------------------------------
# AI crawler user-agents checked against robots.txt
# --------------------------------------------------------------------------
AI_CRAWLER_POINTS: dict[str, int] = {
    "OAI-SearchBot": 5,
    "PerplexityBot": 5,
    "ClaudeBot": 3,
    "Googlebot": 2,
}
# Claude-Web is treated as an alias of ClaudeBot for scoring purposes.
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


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from environment variables (.env supported)."""

    groq_api_key: str | None = field(
        default_factory=lambda: os.getenv("GROQ_API_KEY") or None
    )
    groq_model: str = field(
        default_factory=lambda: os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    )
    groq_base_url: str = field(
        default_factory=lambda: os.getenv(
            "GROQ_BASE_URL", "https://api.groq.com/openai/v1"
        )
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
