"""Pydantic v2 models shared across services, engines, and scoring."""
from __future__ import annotations

from enum import Enum

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
    """A single, evidence-backed audit finding tied to one scoring category.

    ``dimension`` optionally tags a finding with a query-coverage dimension
    (e.g. ``"what"``, ``"who"``) so the answerability matrix doesn't have
    to match on display titles.
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

    @property
    def priority_score(self) -> float:
        """Higher = fix first. Impact collapsed against effort into a sortable number."""
        impact_str = self.impact.value if isinstance(self.impact, Enum) else str(self.impact)
        effort_str = self.effort.value if isinstance(self.effort, Enum) else str(self.effort)

        impact_weight = {"high": 3, "medium": 2, "low": 1}
        effort_weight = {"low": 3, "medium": 2, "high": 1}
        return impact_weight.get(impact_str, 1) * effort_weight.get(effort_str, 1)


class AuditSection(BaseModel):
    """One scored category: its weight, awarded points, and supporting findings."""

    key: str
    label: str
    group: str
    points_possible: float
    points_awarded: float = 0
    findings: list[Finding] = Field(default_factory=list)
    llm_enhanced: bool = False

    @property
    def percentage(self) -> float:
        if self.points_possible == 0:
            return 0.0
        return round(100 * self.points_awarded / self.points_possible, 1)


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

    model_config = {"use_enum_values": False}
