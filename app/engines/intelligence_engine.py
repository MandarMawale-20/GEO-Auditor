"""Intelligence (semantic) engine: hybrid LLM scoring with deterministic fallback.

When ``GROQ_API_KEY`` is set the LLM scores query coverage and citation
snippet quality; otherwise deterministic heuristics run so the audit
always completes.
"""
from __future__ import annotations

import re

from app.config import CATEGORY_META
from app.models import AuditSection, EffortLevel, Finding, ImpactLevel, Severity
from app.services import llm_service
from app.services.html_utils import ParsedPage

_DIMENSIONS = ("what", "who", "problem", "outcome")
_DIMENSION_LABELS = {
    "what": "What the product/service is",
    "who": "Who it is for",
    "problem": "What problem it solves",
    "outcome": "What measurable outcome it delivers",
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


def _build_query_coverage_prompt(page_text: str) -> str:
    truncated = page_text[:6000]
    return f"""You are auditing a company homepage for AI-search answer readiness.
Using ONLY the text provided below, evaluate whether it clearly answers four questions.
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

PAGE TEXT:
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


async def score_query_coverage(
    parsed: ParsedPage, final_url: str, llm_enabled: bool = False
) -> AuditSection:
    meta = CATEGORY_META["query_coverage"]
    section = AuditSection(
        key="query_coverage",
        label=meta["label"],
        group=meta["group"],
        points_possible=15,
    )

    llm_used = False
    result_data: dict | None = None

    if llm_enabled and llm_service.is_available():
        prompt = _build_query_coverage_prompt(parsed.visible_text)
        llm_result = await llm_service.get_json_completion(final_url, "query_coverage", prompt)
        if llm_result.ok and isinstance(llm_result.data, dict):
            result_data = llm_result.data
            llm_used = True

    if result_data is None:
        result_data = _fallback_query_coverage(parsed.visible_text)

    evidence_map = result_data.get("evidence", {}) if isinstance(result_data.get("evidence"), dict) else {}
    total_points = 0.0

    for dimension in _DIMENSIONS:
        raw_score = result_data.get(dimension, 0)
        try:
            answered = bool(int(raw_score))
        except (TypeError, ValueError):
            answered = bool(raw_score)
        points = _POINTS_PER_DIMENSION if answered else 0.0
        total_points += points

        section.findings.append(
            Finding(
                category="query_coverage",
                dimension=dimension,
                severity=Severity.PASS if answered else Severity.WARNING,
                title=f"{_DIMENSION_LABELS[dimension]}: {'answered' if answered else 'not clearly answered'}",
                why_it_matters=(
                    "AI systems assemble answers from pages that clearly cover these four "
                    "fundamentals. Missing any one makes the page a weaker citation candidate."
                ),
                evidence=evidence_map.get(dimension) or "No clear evidence found in page text.",
                recommendation=(
                    "No action needed."
                    if answered
                    else f"Add a clear, explicit statement covering: {_DIMENSION_LABELS[dimension].lower()}."
                ),
                impact=ImpactLevel.HIGH if not answered else ImpactLevel.LOW,
                effort=EffortLevel.MEDIUM,
                points_awarded=points,
                points_possible=_POINTS_PER_DIMENSION,
            )
        )

    section.points_awarded = total_points
    section.llm_enhanced = llm_used
    return section


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
    parsed: ParsedPage, final_url: str, llm_enabled: bool = False
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

    points = 10.0 if is_strong else 0.0
    section.points_awarded = points
    section.llm_enhanced = llm_used
    section.findings.append(
        Finding(
            category="ai_citation_snippet_quality",
            dimension=None,
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
            points_awarded=points,
            points_possible=10,
        )
    )
    return section
