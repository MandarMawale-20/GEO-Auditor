"""Pydantic v2 models shared across services, engines, and scoring.

Scoring contract
----------------
Checks (:class:`CheckResult`) are the only objects that award points. Sections
sum their checks, and the report sums its sections. Findings are a
presentation projection of checks, not a second scoring system.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    PASS = "pass"


class ImpactLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EffortLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AnswerabilityStatus(str, Enum):
    YES = "yes"
    PARTIAL = "partial"
    NO = "no"


class CheckStatus(str, Enum):
    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class FetchResult(BaseModel):
    """Raw result of fetching a single resource (HTML page, robots.txt, sitemap).

    ``bytes_observed`` is the total bytes streamed from the response, and
    ``bytes_retained`` is what was kept after applying the size cap.
    ``truncated`` is True when the body was cut short.
    """

    url: str
    final_url: str | None = None
    status_code: int | None = None
    ok: bool = False
    text: str | None = None
    content_length: int | None = None
    bytes_observed: int | None = None
    bytes_retained: int | None = None
    truncated: bool = False
    elapsed_ms: float | None = None
    redirect_count: int = 0
    error: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class FetchHealth(BaseModel):
    """Non-scored crawler diagnostics shown in the report sidebar."""

    http_status: int | None = None
    fetch_time_ms: float | None = None
    ttfb_ms: float | None = None
    redirect_count: int = 0
    html_size_bytes: int | None = None
    reachable: bool = True
    error_message: str | None = None
    bytes_observed: int | None = None
    truncated: bool = False


class Finding(BaseModel):
    """A single, evidence-backed presentation finding tied to one check.

    ``dimension`` optionally tags a finding with a query-coverage dimension
    (e.g. ``"what"``, ``"who"``) so the answerability matrix doesn't have
    to match on display titles.

    ``check_id`` is required on new check results but optional here so
    legacy share payloads keep validating.
    """

    category: str
    severity: Severity
    title: str
    why_it_matters: str
    evidence: str
    recommendation: str
    copy_paste_fix: str | None = None
    impact: ImpactLevel
    effort: EffortLevel
    points_awarded: float = 0
    points_possible: float = 0
    dimension: str | None = None
    check_id: str | None = None
    page_url: str | None = None
    affected_pages: list[str] = Field(default_factory=list)
    pages_passed: int | None = None
    pages_failed: int | None = None
    pages_unknown: int | None = None
    pages_total: int | None = None

    @property
    def priority_score(self) -> float:
        """Higher = fix first. Impact collapsed against effort into a sortable number."""
        impact_str = self.impact.value if isinstance(self.impact, Enum) else str(self.impact)
        effort_str = self.effort.value if isinstance(self.effort, Enum) else str(self.effort)

        impact_weight = {"high": 3, "medium": 2, "low": 1}
        effort_weight = {"low": 3, "medium": 2, "high": 1}
        return impact_weight.get(impact_str, 1) * effort_weight.get(effort_str, 1)


class CheckResult(BaseModel):
    """One scored check for one page (or the site when ``page_url`` is None).

    ``points_awarded`` is the only value aggregation reads. The remaining
    fields carry the evidence and copy needed to present the check as a
    :class:`Finding` without re-deriving any score.
    """

    check_id: str
    category: str
    status: CheckStatus
    points_awarded: float
    points_possible: float

    severity: Severity = Severity.WARNING
    title: str = ""
    why_it_matters: str = ""
    evidence: list[str] = Field(default_factory=list)
    recommendation: str = ""
    copy_paste_fix: str | None = None
    impact: ImpactLevel = ImpactLevel.MEDIUM
    effort: EffortLevel = EffortLevel.MEDIUM
    dimension: str | None = None
    page_url: str | None = None
    llm_enhanced: bool = False

    # Aggregated-site metadata.
    pages_passed: int | None = None
    pages_failed: int | None = None
    pages_unknown: int | None = None
    pages_total: int | None = None
    affected_pages: list[str] = Field(default_factory=list)

    # Free-form diagnostic values used by aggregation (e.g. entity identity).
    metrics: dict[str, Any] = Field(default_factory=dict)

    def to_finding(self) -> Finding:
        """Project this check into the public presentation model."""
        return Finding(
            category=self.category,
            severity=self.severity,
            title=self.title or self.check_id,
            why_it_matters=self.why_it_matters,
            evidence=" ".join(e for e in self.evidence if e),
            recommendation=self.recommendation,
            copy_paste_fix=self.copy_paste_fix,
            impact=self.impact,
            effort=self.effort,
            points_awarded=round(self.points_awarded, 2),
            points_possible=round(self.points_possible, 2),
            dimension=self.dimension,
            check_id=self.check_id,
            page_url=self.page_url,
            affected_pages=list(self.affected_pages),
            pages_passed=self.pages_passed,
            pages_failed=self.pages_failed,
            pages_unknown=self.pages_unknown,
            pages_total=self.pages_total,
        )


class AuditSection(BaseModel):
    """One scored category: its weight, awarded points, checks, and findings."""

    key: str
    label: str
    group: str
    points_possible: float
    points_awarded: float = 0
    points_evaluated: float = 0
    findings: list[Finding] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list, exclude=True)
    llm_enhanced: bool = False

    @property
    def percentage(self) -> float:
        if self.points_possible == 0:
            return 0.0
        return round(100 * self.points_awarded / self.points_possible, 1)

    def add_check(self, check: CheckResult) -> None:
        """Append a check and derive its finding from the same object."""
        self.checks.append(check)
        if check.status != CheckStatus.NOT_APPLICABLE:
            self.findings.append(check.to_finding())
        self.points_awarded = round(sum(c.points_awarded for c in self.checks), 2)
        self.points_evaluated = round(
            sum(
                c.points_possible
                for c in self.checks
                if c.status not in (CheckStatus.UNKNOWN, CheckStatus.NOT_APPLICABLE)
            ),
            2,
        )
        if check.llm_enhanced:
            self.llm_enhanced = True


class PageAudit(BaseModel):
    """Per-page audit summary plus internal drilldown detail.

    ``sections`` stay in-process (excluded from serialization) so PDF/HTML
    exports can render per-page detail without inflating shared report URLs.
    """

    url: str
    final_url: str | None = None
    role: Literal["homepage", "internal"]
    selected_reason: str = ""
    fetch_health: FetchHealth
    points_awarded: float = 0
    points_possible: float = 0
    sections: list[AuditSection] = Field(default_factory=list, exclude=True)
    word_count: int = 0
    indexable: bool = True
    error: str | None = None


class AnswerabilityRow(BaseModel):
    question: str
    status: AnswerabilityStatus
    evidence: str = ""


class AuditReport(BaseModel):
    url: str
    final_url: str | None = None
    generated_at: str
    total_score: float
    max_score: int
    grade: str
    grade_label: str
    sections: list[AuditSection]
    fetch_health: FetchHealth
    answerability_matrix: list[AnswerabilityRow]
    top_priorities: list[Finding]
    all_findings: list[Finding]
    priority_roadmap: list[Finding] = Field(default_factory=list)
    llm_enhanced: bool
    llm_error: str | None = None
    audit_duration_ms: float
    report_id: str | None = None

    # Multipage / scoring semantics.
    audit_scope: Literal["single", "site"] = "single"
    scoring_version: str = "1"
    pages_discovered: int = 0
    pages_audited: int = 1
    pages: list[PageAudit] = Field(default_factory=list)
    partial: bool = False
    points_evaluated: float = 100
    crawl_stats: dict[str, int] = Field(default_factory=dict)

    model_config = {"use_enum_values": False}
