"""Pure aggregation of per-page checks into the 100-point site report.

Scoring reads only ``CheckResult.points_awarded``. Findings are generated
afterwards from the aggregated checks, so the presentation layer can never
change a score.
"""
from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlparse

from app.checks import CHECK_SPECS, Aggregation, CheckSpec, checks_for_category
from app.config import CATEGORY_META, SCORE_WEIGHTS, TOTAL_POSSIBLE_SCORE
from app.models import (
    AuditSection,
    CheckResult,
    CheckStatus,
    EffortLevel,
    ImpactLevel,
    Severity,
)

def _short_url(url: str | None) -> str:
    if not url:
        return "site"
    parsed = urlparse(url)
    path = parsed.path or "/"
    return path if len(path) <= 60 else path[:57] + "..."


def _check_from_spec(
    spec: CheckSpec,
    *,
    status: CheckStatus,
    title: str,
    evidence: list[str] | None = None,
    page_url: str | None = None,
    points: float = 0.0,
    severity: Severity = Severity.WARNING,
    impact: ImpactLevel = ImpactLevel.MEDIUM,
    effort: EffortLevel = EffortLevel.MEDIUM,
) -> CheckResult:
    return CheckResult(
        check_id=spec.check_id,
        category=spec.category,
        status=status,
        points_awarded=round(points, 2),
        points_possible=spec.points,
        severity=severity,
        title=title,
        evidence=evidence or [],
        page_url=page_url,
        impact=impact,
        effort=effort,
    )


def unknown_check(spec: CheckSpec, reason: str, page_url: str | None = None) -> CheckResult:
    return _check_from_spec(
        spec,
        status=CheckStatus.UNKNOWN,
        title="Could not be evaluated",
        evidence=[reason],
        page_url=page_url,
        points=0,
        severity=Severity.INFO,
        impact=ImpactLevel.MEDIUM,
        effort=EffortLevel.MEDIUM,
    )


def _severity_rank(severity: Severity) -> int:
    return {
        Severity.CRITICAL: 3,
        Severity.WARNING: 2,
        Severity.INFO: 1,
        Severity.PASS: 0,
    }.get(severity, 0)


def _max_severity(checks: list[CheckResult], default: Severity) -> Severity:
    evaluated = [c for c in checks if c.status == CheckStatus.FAIL]
    if not evaluated:
        return default
    return max((c.severity for c in evaluated), key=_severity_rank)


def _copy_meta(source: CheckResult, **updates) -> dict:
    data = {
        "severity": source.severity,
        "title": source.title,
        "why_it_matters": source.why_it_matters,
        "evidence": list(source.evidence),
        "recommendation": source.recommendation,
        "copy_paste_fix": source.copy_paste_fix,
        "impact": source.impact,
        "effort": source.effort,
        "dimension": source.dimension,
        "llm_enhanced": source.llm_enhanced,
        "metrics": dict(source.metrics),
    }
    data.update(updates)
    return data


def _best_page_check(
    page_checks: list[CheckResult],
    homepage_url: str | None,
) -> CheckResult | None:
    evaluated = [
        c for c in page_checks
        if c.status in (CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.PARTIAL)
    ]
    if not evaluated:
        return None
    return max(
        evaluated,
        key=lambda c: (
            c.points_awarded,
            c.status == CheckStatus.PASS,
            c.page_url == homepage_url,
            _short_url(c.page_url),
        ),
    )


def _aggregate_best(
    spec: CheckSpec,
    page_checks: list[CheckResult],
    homepage_url: str | None,
) -> CheckResult:
    winner = _best_page_check(page_checks, homepage_url)
    if winner is None:
        return unknown_check(spec, "No page could be evaluated for this check.")
    data = _copy_meta(winner, page_url=None)
    passed = [c for c in page_checks if c.status == CheckStatus.PASS]
    failed = [
        c for c in page_checks
        if c.status in (CheckStatus.FAIL, CheckStatus.PARTIAL)
    ]
    return CheckResult(
        check_id=spec.check_id,
        category=spec.category,
        status=winner.status,
        points_awarded=winner.points_awarded,
        points_possible=spec.points,
        pages_total=len(page_checks),
        pages_passed=len(passed),
        pages_failed=len(failed),
        pages_unknown=sum(1 for c in page_checks if c.status == CheckStatus.UNKNOWN),
        affected_pages=[c.page_url for c in failed if c.page_url],
        **data,
    )


def _aggregate_latest(
    spec: CheckSpec,
    page_checks: list[CheckResult],
    homepage_url: str | None,
) -> CheckResult:
    evaluated = [
        c for c in page_checks
        if c.status in (CheckStatus.PASS, CheckStatus.PARTIAL, CheckStatus.FAIL)
    ]
    if not evaluated:
        return unknown_check(spec, "No page could be evaluated for freshness.")
    winner = min(
        evaluated,
        key=lambda c: (
            c.metrics.get("days_old", 10_000),
            -c.points_awarded,
            c.page_url != homepage_url,
        ),
    )
    data = _copy_meta(winner, page_url=None)
    return CheckResult(
        check_id=spec.check_id,
        category=spec.category,
        status=winner.status,
        points_awarded=winner.points_awarded,
        points_possible=spec.points,
        pages_total=len(page_checks),
        pages_passed=1 if winner.status == CheckStatus.PASS else 0,
        pages_failed=1 if winner.status in (CheckStatus.FAIL, CheckStatus.PARTIAL) else 0,
        pages_unknown=sum(1 for c in page_checks if c.status == CheckStatus.UNKNOWN),
        affected_pages=[winner.page_url] if winner.status in (CheckStatus.FAIL, CheckStatus.PARTIAL) and winner.page_url else [],
        **data,
    )


def _aggregate_coverage(
    spec: CheckSpec,
    page_checks: list[CheckResult],
    page_weights: dict[str, float],
    homepage_url: str | None,
) -> CheckResult:
    evaluated = [
        c for c in page_checks
        if c.status in (CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.PARTIAL)
    ]
    unknown = [c for c in page_checks if c.status == CheckStatus.UNKNOWN]
    not_applicable = [c for c in page_checks if c.status == CheckStatus.NOT_APPLICABLE]
    statuses = {c.status for c in page_checks}
    pages_total = len(page_checks)
    pages_unknown = len(unknown)

    if not evaluated:
        if not_applicable and len(not_applicable) == pages_total:
            base = not_applicable[0]
            data = _copy_meta(
                base,
                page_url=None,
                title="Organization schema is missing: name, logo, description",
                evidence=(
                    ["No Organization entity found on this page."]
                    if pages_total == 1
                    else [
                        "No Organization entity found on any of the "
                        f"{pages_total} audited pages."
                    ]
                ),
                recommendation=(
                    "Add Organization JSON-LD with name, logo, and description."
                ),
                severity=Severity.WARNING,
                impact=ImpactLevel.MEDIUM,
                effort=EffortLevel.LOW,
            )
            return CheckResult(
                check_id=spec.check_id,
                category=spec.category,
                status=CheckStatus.FAIL,
                points_awarded=0,
                points_possible=spec.points,
                pages_total=pages_total,
                pages_failed=pages_total,
                affected_pages=[c.page_url for c in page_checks if c.page_url],
                **data,
            )
        if unknown:
            merged = _copy_meta(unknown[0], page_url=None)
            return CheckResult(
                check_id=spec.check_id,
                category=spec.category,
                status=CheckStatus.UNKNOWN,
                points_awarded=0,
                points_possible=spec.points,
                pages_total=pages_total,
                pages_unknown=pages_unknown,
                **merged,
            )
        return unknown_check(spec, "No page could be evaluated for this check.")

    if len(page_checks) == 1:
        # Single-page parity: the page check is the site result.
        only = page_checks[0]
        return CheckResult(
            check_id=spec.check_id,
            category=spec.category,
            status=only.status,
            points_awarded=only.points_awarded,
            points_possible=spec.points,
            pages_total=1,
            pages_passed=1 if only.status == CheckStatus.PASS else 0,
            pages_failed=1 if only.status in (CheckStatus.FAIL, CheckStatus.PARTIAL) else 0,
            affected_pages=[only.page_url] if only.status != CheckStatus.PASS and only.page_url else [],
            **_copy_meta(only, page_url=None),
        )

    total_weight = sum(page_weights.get(c.page_url or "", 1.0) for c in evaluated) or 1.0
    ratio = sum(
        page_weights.get(c.page_url or "", 1.0) * (c.points_awarded / c.points_possible)
        for c in evaluated
    ) / total_weight
    points = spec.points * ratio

    passed = [c for c in evaluated if c.status == CheckStatus.PASS]
    failed = [c for c in evaluated if c.status in (CheckStatus.FAIL, CheckStatus.PARTIAL)]
    pages_passed = len(passed)
    pages_failed = len(failed)
    affected = [c.page_url for c in failed if c.page_url]

    if ratio >= 0.999:
        status = CheckStatus.PASS
        severity = Severity.PASS
    elif ratio <= 0.001:
        status = CheckStatus.FAIL
        severity = _max_severity(evaluated, Severity.WARNING)
    else:
        status = CheckStatus.PARTIAL
        base_severity = _max_severity(evaluated, Severity.WARNING)
        homepage_failed = any(
            c.page_url == homepage_url and c.status in (CheckStatus.FAIL, CheckStatus.PARTIAL)
            for c in evaluated
        )
        severity = (
            Severity.CRITICAL
            if base_severity == Severity.CRITICAL and homepage_failed
            else Severity.WARNING
        )

    primary = (failed or passed or evaluated)[0]
    applicable = len(evaluated)
    if pages_total <= 1:
        # Single-page parity: preserve the page-level title verbatim.
        title = primary.title
    else:
        label = spec.site_label
        if status == CheckStatus.PASS:
            title = f"{label}: passed on all {applicable} applicable pages"
        elif status == CheckStatus.FAIL:
            title = f"{label}: failed on {pages_failed} of {applicable} applicable pages"
        else:
            title = f"{label}: passed on {pages_passed} of {applicable} applicable pages"

    evidence: list[str] = []
    if pages_total > 1:
        excluded = pages_total - applicable
        evidence.append(
            f"{pages_passed} of {applicable} applicable pages passed"
            + (f" ({excluded} excluded as not applicable or unknown)." if excluded else ".")
        )
    for check in failed[:5]:
        detail = " ".join(check.evidence)[:240]
        evidence.append(f"{_short_url(check.page_url)}: {detail}".strip())
    if not evidence and passed:
        evidence = list(passed[0].evidence[:2])
    if pages_unknown and pages_total <= 1:
        evidence.append(f"{pages_unknown} page(s) could not be evaluated.")

    return CheckResult(
        check_id=spec.check_id,
        category=spec.category,
        status=status,
        points_awarded=round(min(points, spec.points), 2),
        points_possible=spec.points,
        page_url=None,
        pages_passed=pages_passed,
        pages_failed=pages_failed,
        pages_unknown=pages_unknown,
        pages_total=pages_total,
        affected_pages=affected,
        **_copy_meta(
            primary,
            severity=severity,
            title=title,
            evidence=evidence,
        ),
    )


def _aggregate_consistency(
    spec: CheckSpec,
    page_checks: list[CheckResult],
    _page_weights: dict[str, float],
    homepage_url: str | None,
) -> CheckResult:
    if not page_checks:
        return unknown_check(spec, "No page could be evaluated for this check.")
    if len(page_checks) == 1:
        # Single-page parity: the page result is the site result.
        only = page_checks[0]
        return CheckResult(
            check_id=spec.check_id,
            category=spec.category,
            status=only.status,
            points_awarded=only.points_awarded,
            points_possible=spec.points,
            pages_total=1,
            pages_passed=1 if only.status == CheckStatus.PASS else 0,
            pages_failed=1 if only.status == CheckStatus.FAIL else 0,
            affected_pages=[only.page_url] if only.status == CheckStatus.FAIL and only.page_url else [],
            **_copy_meta(only, page_url=None),
        )

    carriers = [c for c in page_checks if c.metrics.get("entity_present")]
    pages_total = len(page_checks)
    if not carriers:
        evaluated = [
            c for c in page_checks
            if c.status in (CheckStatus.PASS, CheckStatus.PARTIAL, CheckStatus.FAIL)
        ]
        if not evaluated:
            # Unknown must never be promoted to failure.
            merged = _copy_meta(page_checks[0], page_url=None)
            return CheckResult(
                check_id=spec.check_id,
                category=spec.category,
                status=CheckStatus.UNKNOWN,
                points_awarded=0,
                points_possible=spec.points,
                pages_total=pages_total,
                pages_unknown=pages_total,
                **merged,
            )
        base = evaluated[0]
        return CheckResult(
            check_id=spec.check_id,
            category=spec.category,
            status=CheckStatus.FAIL,
            points_awarded=0,
            points_possible=spec.points,
            pages_total=pages_total,
            pages_passed=0,
            pages_failed=len(evaluated),
            pages_unknown=sum(1 for c in page_checks if c.status == CheckStatus.UNKNOWN),
            affected_pages=[c.page_url for c in evaluated if c.page_url],
            **_copy_meta(
                base,
                page_url=None,
                severity=Severity.CRITICAL,
                title="No Organization or Product JSON-LD found on any audited page",
                evidence=[
                    f"Checked {len(evaluated)} evaluable page(s); none contained an Organization, "
                    "Product, or SoftwareApplication JSON-LD entity."
                ],
            ),
        )

    if len(carriers) == 1:
        base = carriers[0]
        return CheckResult(
            check_id=spec.check_id,
            category=spec.category,
            status=CheckStatus.PARTIAL,
            points_awarded=round(spec.points * 0.5, 2),
            points_possible=spec.points,
            pages_total=pages_total,
            pages_passed=1,
            pages_failed=0,
            pages_unknown=sum(1 for c in page_checks if c.status == CheckStatus.UNKNOWN),
            affected_pages=[],
            **_copy_meta(
                base,
                page_url=None,
                severity=Severity.WARNING,
                title=(
                    f"Organization identity found on only 1 of {pages_total} pages — "
                    "consistency could not be verified"
                ),
                evidence=[
                    f"Entity present on {_short_url(base.page_url)} only. "
                    "Add the same Organization JSON-LD to /about or another identity page "
                    "so AI systems see a consistent entity."
                ],
            ),
        )

    fingerprints = {str(c.metrics.get("fingerprint", "")) for c in carriers}
    consistent = len(fingerprints) == 1
    if consistent:
        base = carriers[0]
        return CheckResult(
            check_id=spec.check_id,
            category=spec.category,
            status=CheckStatus.PASS,
            points_awarded=spec.points,
            points_possible=spec.points,
            pages_total=pages_total,
            pages_passed=len(carriers),
            pages_failed=0,
            pages_unknown=sum(1 for c in page_checks if c.status == CheckStatus.UNKNOWN),
            **_copy_meta(
                base,
                page_url=None,
                severity=Severity.PASS,
                title=f"Organization identity is consistent across {len(carriers)} pages",
                evidence=[
                    f"Checked {len(carriers)} pages carrying Organization JSON-LD; "
                    "name, url, logo, and sameAs values agree."
                ],
            ),
        )

    # Conflicting identities: report the pages that disagree.
    base = carriers[0]
    conflicting = [
        c.page_url for c in carriers
        if str(c.metrics.get("fingerprint", "")) != str(base.metrics.get("fingerprint", ""))
    ]
    return CheckResult(
        check_id=spec.check_id,
        category=spec.category,
        status=CheckStatus.FAIL,
        points_awarded=0,
        points_possible=spec.points,
        pages_total=pages_total,
        pages_passed=1,
        pages_failed=max(1, len(conflicting)),
        pages_unknown=sum(1 for c in page_checks if c.status == CheckStatus.UNKNOWN),
        affected_pages=[base.page_url] + conflicting if base.page_url else conflicting,
        **_copy_meta(
            base,
            page_url=None,
            severity=Severity.CRITICAL,
            title=f"Conflicting Organization identity across {len(carriers)} pages",
            evidence=[
                "Organization JSON-LD name/url/logo/sameAs values differ between pages: "
                + ", ".join(_short_url(u) for u in ([base.page_url] + conflicting)[:5])
            ],
        ),
    )


def _aggregate_singleton(spec: CheckSpec, site_checks: list[CheckResult]) -> CheckResult:
    matching = [c for c in site_checks if c.check_id == spec.check_id]
    if matching:
        check = matching[0]
        if check.points_possible != spec.points:
            check = check.model_copy(update={"points_possible": spec.points})
        return check
    return unknown_check(spec, "This site-wide check did not run.")


def aggregate_sections(
    *,
    site_checks: list[CheckResult],
    page_checks: list[CheckResult],
    page_weights: dict[str, float],
    homepage_url: str | None,
) -> list[AuditSection]:
    """Build the ten frozen site sections from check results."""
    by_category: dict[str, list[CheckResult]] = defaultdict(list)
    for spec in CHECK_SPECS.values():
        if spec.aggregation in (Aggregation.SITE_SINGLETON, Aggregation.SITE_CORPUS):
            result = _aggregate_singleton(spec, site_checks)
        elif spec.aggregation == Aggregation.BEST:
            result = _aggregate_best(spec, [c for c in page_checks if c.check_id == spec.check_id], homepage_url)
        elif spec.aggregation == Aggregation.LATEST:
            result = _aggregate_latest(spec, [c for c in page_checks if c.check_id == spec.check_id], homepage_url)
        elif spec.aggregation == Aggregation.CONSISTENCY:
            result = _aggregate_consistency(
                spec,
                [c for c in page_checks if c.check_id == spec.check_id],
                page_weights,
                homepage_url,
            )
        else:
            result = _aggregate_coverage(
                spec,
                [c for c in page_checks if c.check_id == spec.check_id],
                page_weights,
                homepage_url,
            )
        by_category[spec.category].append(result)

    sections: list[AuditSection] = []
    for category, weight in SCORE_WEIGHTS.items():
        meta = CATEGORY_META[category]
        section = AuditSection(
            key=category,
            label=meta["label"],
            group=meta["group"],
            points_possible=weight,
        )
        for check in by_category.get(category, []):
            section.add_check(check)
        section.llm_enhanced = any(c.llm_enhanced for c in section.checks)
        sections.append(section)

    validate_sections(sections)
    return sections


def validate_sections(sections: list[AuditSection]) -> None:
    """Assert the scoring contract; raises ValueError on any violation."""
    if len(sections) != len(SCORE_WEIGHTS):
        raise ValueError(f"Expected {len(SCORE_WEIGHTS)} sections, got {len(sections)}.")

    total_possible = 0.0
    total_awarded = 0.0
    for section in sections:
        expected = SCORE_WEIGHTS.get(section.key)
        if expected is None:
            raise ValueError(f"Unknown section key: {section.key}")
        if round(section.points_possible, 2) != round(expected, 2):
            raise ValueError(
                f"{section.key}: possible {section.points_possible} != weight {expected}"
            )
        check_possible = round(sum(c.points_possible for c in section.checks), 2)
        if check_possible != round(expected, 2):
            raise ValueError(
                f"{section.key}: checks sum to {check_possible} points, expected {expected}"
            )
        check_awarded = round(sum(c.points_awarded for c in section.checks), 2)
        if round(section.points_awarded, 2) != check_awarded:
            raise ValueError(
                f"{section.key}: awarded {section.points_awarded} != checks {check_awarded}"
            )
        if not (-0.001 <= section.points_awarded <= section.points_possible + 0.001):
            raise ValueError(
                f"{section.key}: awarded {section.points_awarded} out of range"
            )
        if not (-0.001 <= section.points_evaluated <= section.points_possible + 0.001):
            raise ValueError(
                f"{section.key}: evaluated {section.points_evaluated} out of range"
            )
        if section.points_awarded > section.points_evaluated + 0.001:
            raise ValueError(
                f"{section.key}: awarded {section.points_awarded} exceeds evaluated "
                f"{section.points_evaluated}"
            )
        if len({c.check_id for c in section.checks}) != len(section.checks):
            raise ValueError(f"{section.key}: duplicate check_id values")
        total_possible += section.points_possible
        total_awarded += section.points_awarded

    if round(total_possible, 2) != TOTAL_POSSIBLE_SCORE:
        raise ValueError(f"Total possible {total_possible} != {TOTAL_POSSIBLE_SCORE}")
    if not (-0.001 <= total_awarded <= TOTAL_POSSIBLE_SCORE + 0.001):
        raise ValueError(f"Total awarded {total_awarded} out of range")
