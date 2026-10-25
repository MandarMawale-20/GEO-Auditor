"""Check-level scoring contract.

Each category is composed of one or more checks. A check is the only object
allowed to award points; sections and findings are derived from checks.

The aggregation strategy is declared here (config), not inside engines, so the
scoring semantics are explicit and testable:

  SITE_SINGLETON  evaluated once for the whole site (robots.txt, trust pages)
  BEST            one strong page satisfies the check (schema, citation)
  COVERAGE        weighted share of applicable, evaluated pages
  SITE_CORPUS     one evaluation over combined site text (query coverage)
  CONSISTENCY     cross-page identity checks and conflict detection
  LATEST          the most recent reliable signal wins (freshness)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.config import SCORE_WEIGHTS
from app.models import CheckResult, CheckStatus, EffortLevel, ImpactLevel, Severity


class Aggregation(str, Enum):
    SITE_SINGLETON = "site_singleton"
    BEST = "best"
    COVERAGE = "coverage"
    SITE_CORPUS = "site_corpus"
    CONSISTENCY = "consistency"
    LATEST = "latest"


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    category: str
    points: float
    aggregation: Aggregation
    site_label: str


CHECK_SPECS: dict[str, CheckSpec] = {}


def _register(
    check_id: str,
    category: str,
    points: float,
    aggregation: Aggregation,
    site_label: str,
) -> None:
    CHECK_SPECS[check_id] = CheckSpec(check_id, category, points, aggregation, site_label)


# --- Technical -------------------------------------------------------------
for _bot, _label, _points in (
    ("openai_searchbot", "OAI-SearchBot access", 5),
    ("perplexitybot", "PerplexityBot access", 5),
    ("claudebot", "ClaudeBot access", 3),
    ("googlebot", "Googlebot access", 2),
):
    _register(
        f"crawler.{_bot}",
        "ai_crawler_accessibility",
        _points,
        Aggregation.SITE_SINGLETON,
        _label,
    )

_register("js.raw_text", "js_rendering_risk", 5, Aggregation.COVERAGE, "Server-rendered text")
_register("freshness.latest_signal", "freshness_signals", 5, Aggregation.LATEST, "Freshness signal")

# --- Entity ----------------------------------------------------------------
_register(
    "entity.org_schema",
    "structured_entity_data",
    7,
    Aggregation.CONSISTENCY,
    "Organization JSON-LD",
)
_register("entity.same_as_trusted", "structured_entity_data", 3, Aggregation.BEST, "sameAs links")
_register(
    "entity.required_fields",
    "entity_completeness_trust",
    5,
    Aggregation.COVERAGE,
    "Organization schema fields",
)
_register("entity.trust_pages", "entity_completeness_trust", 5, Aggregation.SITE_SINGLETON, "Trust pages")

# --- Content ---------------------------------------------------------------
_register(
    "quote.question_headings",
    "quote_readiness",
    5,
    Aggregation.COVERAGE,
    "Question-style headings",
)
_register(
    "quote.lead_paragraph",
    "quote_readiness",
    5,
    Aggregation.COVERAGE,
    "Direct-answer lead paragraphs",
)
_register("quote.low_fluff", "quote_readiness", 5, Aggregation.COVERAGE, "Marketing fluff")
_register("facts.tables", "fact_density", 5, Aggregation.COVERAGE, "Structured tables")
_register(
    "facts.numeric_density",
    "fact_density",
    5,
    Aggregation.COVERAGE,
    "Numeric density and lists",
)
_register("media.balance", "media_to_text_balance", 5, Aggregation.COVERAGE, "Text-to-image balance")

# --- Intelligence ----------------------------------------------------------
for _dimension in ("what", "who", "problem", "outcome"):
    _register(
        f"query.{_dimension}",
        "query_coverage",
        3.75,
        Aggregation.SITE_CORPUS,
        f"Query coverage: {_dimension}",
    )
_register(
    "citation.snippet",
    "ai_citation_snippet_quality",
    10,
    Aggregation.BEST,
    "Citation-ready snippet",
)

# Deterministic order used by reports, PDFs, and tests.
CHECK_ORDER: tuple[str, ...] = tuple(CHECK_SPECS)

CATEGORY_ORDER: tuple[str, ...] = tuple(SCORE_WEIGHTS)


def checks_for_category(category: str) -> list[CheckSpec]:
    return [spec for spec in CHECK_SPECS.values() if spec.category == category]


def build_check(
    check_id: str,
    *,
    status: CheckStatus,
    title: str,
    why_it_matters: str = "",
    evidence: str | list[str] | tuple[str, ...] = (),
    recommendation: str = "",
    severity: Severity = Severity.WARNING,
    impact: ImpactLevel = ImpactLevel.MEDIUM,
    effort: EffortLevel = EffortLevel.MEDIUM,
    copy_paste_fix: str | None = None,
    dimension: str | None = None,
    page_url: str | None = None,
    points_awarded: float | None = None,
    metrics: dict[str, Any] | None = None,
    llm_enhanced: bool = False,
) -> CheckResult:
    """Create a check from the registry, keeping points in one place.

    ``points_awarded`` defaults to full credit for PASS and zero otherwise.
    PARTIAL checks should pass an explicit award.
    """
    spec = CHECK_SPECS[check_id]
    if points_awarded is None:
        points_awarded = spec.points if status == CheckStatus.PASS else 0.0
    if isinstance(evidence, str):
        evidence_list = [evidence] if evidence else []
    elif isinstance(evidence, (list, tuple, set)):
        evidence_list = [str(item) for item in evidence if item not in (None, "")]
    elif evidence:
        evidence_list = [str(evidence)]
    else:
        evidence_list = []

    return CheckResult(
        check_id=check_id,
        category=spec.category,
        status=status,
        points_awarded=round(min(max(points_awarded, 0.0), spec.points), 2),
        points_possible=spec.points,
        severity=severity,
        title=title,
        why_it_matters=why_it_matters,
        evidence=evidence_list,
        recommendation=recommendation,
        copy_paste_fix=copy_paste_fix,
        impact=impact,
        effort=effort,
        dimension=dimension,
        page_url=page_url,
        llm_enhanced=llm_enhanced,
        metrics=metrics or {},
    )


def validate_check_registry() -> None:
    """Ensure checks exactly cover the frozen 100-point model."""
    for category, weight in SCORE_WEIGHTS.items():
        total = round(sum(spec.points for spec in checks_for_category(category)), 2)
        if total != weight:
            raise ValueError(
                f"Check points for {category!r} sum to {total}, expected {weight}."
            )
    if len(CHECK_SPECS) != 21:
        raise ValueError(f"Expected 21 checks, found {len(CHECK_SPECS)}.")


validate_check_registry()
