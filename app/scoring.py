"""Turn scored sections into the final audit report."""
from __future__ import annotations

from app.config import TOTAL_POSSIBLE_SCORE
from app.models import AnswerabilityRow, AnswerabilityStatus, AuditSection, Finding, Severity

_DIFFERENTIATION_KEYWORDS = (
    "unlike", "compared to", "instead of", "vs.", "vs ", "better than",
    "alternative to", "why choose", "why us", "what sets us apart",
)

_DIMENSION_QUESTIONS = {
    "what": "What does this company do?",
    "who": "Who is it for?",
    "problem": "What problem does it solve?",
    "outcome": "What measurable result does it provide?",
}

GRADE_BANDS: list[tuple[float, str, str]] = [
    (85, "A", "Excellent AI answerability"),
    (70, "B", "Strong AI visibility"),
    (55, "C", "Moderate / partial visibility"),
    (40, "D", "Weak AI visibility"),
    (0, "F", "High risk of AI invisibility"),
]


def compute_grade(total_score: float) -> tuple[str, str]:
    for threshold, grade, label in GRADE_BANDS:
        if total_score >= threshold:
            return grade, label
    return "F", "High risk of AI invisibility"


def compute_total_score(sections: list[AuditSection]) -> float:
    return round(sum(s.points_awarded for s in sections), 1)


def collect_findings(sections: list[AuditSection]) -> list[Finding]:
    findings: list[Finding] = []
    for section in sections:
        findings.extend(section.findings)
    return findings


def build_priority_roadmap(findings: list[Finding]) -> list[Finding]:
    """Actionable findings sorted by impact-vs-effort priority, worst-first."""
    actionable = [f for f in findings if f.severity in (Severity.CRITICAL, Severity.WARNING)]
    actionable.sort(key=lambda f: (f.priority_score, f.points_possible - f.points_awarded), reverse=True)
    return actionable


def select_top_priorities(findings: list[Finding], limit: int = 3) -> list[Finding]:
    """Top N findings for the headline row."""
    return build_priority_roadmap(findings)[:limit]


def build_answerability_matrix(sections: list[AuditSection], page_text: str) -> list[AnswerabilityRow]:
    """Build the answerability matrix from structured ``dimension`` fields.

    The query_coverage engine tags each finding with a ``dimension`` value
    (``"what"``, ``"who"``, ``"problem"``, ``"outcome"``); we use that field
    rather than fragile title matching.
    """
    dimension_findings: dict[str, Finding] = {}
    for section in sections:
        if section.key == "query_coverage":
            for f in section.findings:
                if f.dimension:
                    dimension_findings[f.dimension] = f

    rows: list[AnswerabilityRow] = []
    for dim, question in _DIMENSION_QUESTIONS.items():
        finding = dimension_findings.get(dim)
        if finding is None:
            rows.append(AnswerabilityRow(
                question=question,
                status=AnswerabilityStatus.PARTIAL,
                evidence="Not evaluated.",
            ))
        elif finding.severity == Severity.PASS:
            rows.append(AnswerabilityRow(
                question=question,
                status=AnswerabilityStatus.YES,
                evidence=finding.evidence,
            ))
        else:
            rows.append(AnswerabilityRow(
                question=question,
                status=AnswerabilityStatus.NO,
                evidence=finding.evidence,
            ))

    # Differentiation / "Why choose it?"
    lowered_text = page_text.lower()
    has_differentiation = any(k in lowered_text for k in _DIFFERENTIATION_KEYWORDS)
    rows.append(AnswerabilityRow(
        question="Why choose it over alternatives?",
        status=AnswerabilityStatus.YES if has_differentiation else AnswerabilityStatus.NO,
        evidence=(
            "Found comparative/differentiation language on the page."
            if has_differentiation
            else "No comparative language (e.g. 'unlike', 'vs.', 'why choose us') found."
        ),
    ))

    return rows


def validate_total_weight() -> None:
    assert TOTAL_POSSIBLE_SCORE == 100, "Scoring weights no longer sum to 100 — check config.py"
