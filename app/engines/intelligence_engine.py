"""Intelligence (semantic) engine: hybrid LLM scoring with deterministic fallback.

Query Coverage is evaluated once for the site over a combined text corpus.
Citation Snippet Quality is evaluated per page (LLM for the top pages,
heuristics for the rest) and aggregated with BEST semantics.

When ``GROQ_API_KEY`` is set the LLM scores these checks; otherwise
deterministic heuristics run so the audit always completes.
"""
from __future__ import annotations

import hashlib
import re

from app.checks import build_check
from app.config import CATEGORY_META
from app.models import AuditSection, CheckStatus, EffortLevel, ImpactLevel, Severity
from app.services import llm_service
from app.services.html_utils import ParsedPage

_DIMENSIONS = ("what", "who", "problem", "outcome")
_DIMENSION_LABELS = {
    "what": "What the product/service is",
    "who": "Who it is for",
    "problem": "What problem it solves",
    "outcome": "What measurable outcome it delivers",
}
_DIMENSION_CHECK_IDS = {
    "what": "query.what",
    "who": "query.who",
    "problem": "query.problem",
    "outcome": "query.outcome",
}
_POINTS_PER_DIMENSION = 3.75

_OUTCOME_KEYWORDS = (
    "%", "percent", "faster", "reduce", "increase", "save", "hours", "minutes",
    "revenue", "roi", "conversion", "growth", "x faster", "fewer", "more",
)
_WHO_KEYWORDS = (
    "for teams", "for businesses", "for startups", "for companies", "for developers",
    "for marketers", "for sales", "for founders", "b2b", "smb", "enterprises",
    "for saas", "for agencies",
)
_PROBLEM_KEYWORDS = (
    "struggle", "problem", "challenge", "without", "manual", "time-consuming",
    "difficult", "hard to", "instead of", "no longer", "eliminate",
)


def _build_query_coverage_prompt(page_text: str, page_count: int = 1) -> str:
    truncated = page_text[:6000]
    return f"""You are auditing a company's website content for AI-search answer readiness.
The text below is sampled from {page_count} page(s) of the site, separated by [PAGE: url] markers.
Using ONLY the text provided, evaluate whether the site clearly answers four questions.
Do not infer anything not stated in the text. If uncertain, score 0.

For each dimension, return a score of 0 or 1 (1 = clearly answered in the text, 0 = not answered),
plus a short evidence quote (max 15 words) from the text.

Return ONLY this JSON structure, no other text:
{{
  "what": 0,
  "who": 0,
  "problem": 0,
  "outcome": 0,
  "evidence": {{
    "what": "",
    "who": "",
    "problem": "",
    "outcome": ""
  }}
}}

SITE TEXT:
{truncated}
"""


def _fallback_query_coverage(page_text: str) -> dict:
    lowered = page_text.lower()
    what_score = 1 if len(page_text.split()) > 40 else 0
    who_score = 1 if any(k in lowered for k in _WHO_KEYWORDS) else 0
    problem_score = 1 if any(k in lowered for k in _PROBLEM_KEYWORDS) else 0
    outcome_score = 1 if any(k in lowered for k in _OUTCOME_KEYWORDS) else 0
    return {
        "what": what_score,
        "who": who_score,
        "problem": problem_score,
        "outcome": outcome_score,
        "evidence": {
            "what": "Heuristic: page has substantive text content." if what_score else "",
            "who": "Heuristic: found audience-targeting language." if who_score else "",
            "problem": "Heuristic: found problem-framing language." if problem_score else "",
            "outcome": "Heuristic: found outcome/metric language." if outcome_score else "",
        },
    }


async def score_query_coverage_site(
    site_text: str,
    final_url: str,
    page_count: int = 1,
    llm_enabled: bool = False,
) -> AuditSection:
    """Evaluate the four site-level query dimensions over a text corpus."""
    meta = CATEGORY_META["query_coverage"]
    section = AuditSection(
        key="query_coverage",
        label=meta["label"],
        group=meta["group"],
        points_possible=15,
    )

    llm_used = False
    result_data: dict | None = None

    if llm_enabled and llm_service.is_available() and site_text.strip():
        prompt = _build_query_coverage_prompt(site_text, page_count=page_count)
        digest = hashlib.sha1(site_text.encode("utf-8", errors="ignore")).hexdigest()[:12]
        cache_url = f"{final_url}#site-{digest}"
        llm_result = await llm_service.get_json_completion(cache_url, "query_coverage", prompt)
        if llm_result.ok and isinstance(llm_result.data, dict):
            result_data = llm_result.data
            llm_used = True

    if result_data is None:
        result_data = _fallback_query_coverage(site_text)

    evidence_map = result_data.get("evidence", {}) if isinstance(result_data.get("evidence"), dict) else {}

    for dimension in _DIMENSIONS:
        raw_score = result_data.get(dimension, 0)
        try:
            answered = bool(int(raw_score))
        except (TypeError, ValueError):
            answered = bool(raw_score)

        section.add_check(
            build_check(
                _DIMENSION_CHECK_IDS[dimension],
                status=CheckStatus.PASS if answered else CheckStatus.FAIL,
                severity=Severity.PASS if answered else Severity.WARNING,
                title=(
                    f"{_DIMENSION_LABELS[dimension]}: "
                    f"{'answered' if answered else 'not clearly answered'}"
                ),
                why_it_matters=(
                    "AI systems assemble answers from pages that clearly cover these four "
                    "fundamentals. Missing any one makes the site a weaker citation candidate."
                ),
                evidence=evidence_map.get(dimension) or "No clear evidence found in site text.",
                recommendation=(
                    "No action needed."
                    if answered
                    else f"Add a clear, explicit statement covering: {_DIMENSION_LABELS[dimension].lower()}."
                ),
                impact=ImpactLevel.HIGH if not answered else ImpactLevel.LOW,
                effort=EffortLevel.MEDIUM,
                dimension=dimension,
                llm_enhanced=llm_used,
                metrics={"pages_sampled": page_count},
            )
        )

    section.llm_enhanced = llm_used
    return section


async def score_query_coverage(
    parsed: ParsedPage, final_url: str, llm_enabled: bool = False
) -> AuditSection:
    """Single-page wrapper kept for backwards compatibility and tests."""
    return await score_query_coverage_site(
        parsed.visible_text, final_url, page_count=1, llm_enabled=llm_enabled
    )


def _build_citation_prompt(candidate_paragraph: str, page_text: str) -> str:
    context = candidate_paragraph if candidate_paragraph else page_text[:2000]
    return f"""You are evaluating whether the text below is a strong AI-citation-ready snippet.
A strong snippet is 20-60 words, self-contained (understandable without extra context),
names the company, states the product category, and states a measurable benefit.

Return ONLY this JSON structure, no other text:
{{
  "is_strong_snippet": 0,
  "reason": ""
}}

TEXT TO EVALUATE:
{context}
"""


def _fallback_citation_quality(candidate_paragraph: str | None) -> tuple[bool, str]:
    if not candidate_paragraph:
        return False, "No 20-60 word self-contained paragraph was found on the page."
    has_number = bool(re.search(r"\d", candidate_paragraph))
    has_outcome_word = any(k in candidate_paragraph.lower() for k in _OUTCOME_KEYWORDS)
    if has_number or has_outcome_word:
        return True, "Heuristic: paragraph is well-sized and contains a number or outcome-related term."
    return False, "Heuristic: paragraph is well-sized but lacks a measurable benefit or number."


async def score_citation_snippet_quality(
    parsed: ParsedPage,
    final_url: str,
    llm_enabled: bool = False,
    page_url: str | None = None,
) -> AuditSection:
    meta = CATEGORY_META["ai_citation_snippet_quality"]
    section = AuditSection(
        key="ai_citation_snippet_quality",
        label=meta["label"],
        group=meta["group"],
        points_possible=10,
    )

    candidate = parsed.citation_paragraph
    llm_used = False
    is_strong: bool
    reason: str

    if llm_enabled and llm_service.is_available() and candidate:
        prompt = _build_citation_prompt(candidate, parsed.visible_text)
        llm_result = await llm_service.get_json_completion(final_url, "citation_snippet", prompt)
        if llm_result.ok and isinstance(llm_result.data, dict):
            try:
                is_strong = bool(int(llm_result.data.get("is_strong_snippet", 0)))
            except (TypeError, ValueError):
                is_strong = bool(llm_result.data.get("is_strong_snippet"))
            reason = llm_result.data.get("reason") or "Evaluated by Groq."
            llm_used = True
        else:
            is_strong, reason = _fallback_citation_quality(candidate)
    else:
        is_strong, reason = _fallback_citation_quality(candidate)

    section.add_check(
        build_check(
            "citation.snippet",
            status=CheckStatus.PASS if is_strong else CheckStatus.FAIL,
            severity=Severity.PASS if is_strong else Severity.WARNING,
            title=(
                "Found a strong, citation-ready snippet"
                if is_strong
                else "No strong citation-ready snippet found"
            ),
            why_it_matters=(
                "AI answer engines often quote a single self-contained paragraph verbatim. "
                "Without one, they must stitch together a summary themselves — or skip citing you."
            ),
            evidence=(
                f'Candidate: "{candidate}"' if candidate else "No 20-60 word paragraph found."
            ) + f" — {reason}",
            recommendation=(
                "No action needed."
                if is_strong
                else "Add one 20-60 word paragraph near the top of the page that names your company, "
                "states your product category, and states a measurable outcome."
            ),
            copy_paste_fix=(
                None
                if is_strong
                else "[Company] is a [category] that helps [audience] [achieve outcome], "
                "reducing/increasing [metric] by [X%]."
            ),
            impact=ImpactLevel.HIGH,
            effort=EffortLevel.LOW,
            page_url=page_url,
            llm_enhanced=llm_used,
        )
    )
    section.llm_enhanced = llm_used
    return section
