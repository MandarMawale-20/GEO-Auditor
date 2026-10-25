"""Aggregation contract: coverage weights, unknown handling, consistency."""
from __future__ import annotations

from app.aggregation import aggregate_sections
from app.checks import CHECK_SPECS, Aggregation
from app.config import SCORE_WEIGHTS
from app.models import CheckStatus, CheckResult, Severity

PAGE_SPECS = [
    spec
    for spec in CHECK_SPECS.values()
    if spec.aggregation not in (Aggregation.SITE_SINGLETON, Aggregation.SITE_CORPUS)
]
SITE_SPECS = [
    spec
    for spec in CHECK_SPECS.values()
    if spec.aggregation in (Aggregation.SITE_SINGLETON, Aggregation.SITE_CORPUS)
]
HOME = "https://example.com"
PRICING = "https://example.com/pricing"
ABOUT = "https://example.com/about"


def _check(spec, url, *, status=CheckStatus.PASS, points=None, metrics=None, title=None):
    if points is None:
        points = spec.points if status == CheckStatus.PASS else 0.0
    return CheckResult(
        check_id=spec.check_id,
        category=spec.category,
        status=status,
        points_awarded=points,
        points_possible=spec.points,
        severity=Severity.PASS if status == CheckStatus.PASS else Severity.WARNING,
        title=title or f"{spec.check_id} on {url}",
        evidence=[f"evidence for {spec.check_id}"],
        page_url=url,
        metrics=metrics or {},
    )


def _site_checks():
    return [
        _check(spec, None, title=f"{spec.check_id} site")
        for spec in SITE_SPECS
    ]


def _page_checks(url, overrides=None):
    overrides = overrides or {}
    checks = []
    for spec in PAGE_SPECS:
        override = overrides.get(spec.check_id, {})
        default_metrics = {}
        if spec.check_id in ("entity.org_schema", "entity.required_fields"):
            default_metrics = {"entity_present": True}
        if spec.check_id == "entity.org_schema":
            default_metrics["fingerprint"] = "same"
        if spec.check_id == "freshness.latest_signal":
            default_metrics["days_old"] = 1
        metrics = {**default_metrics, **override.get("metrics", {})}
        checks.append(
            _check(
                spec,
                url,
                status=override.get("status", CheckStatus.PASS),
                points=override.get("points"),
                metrics=metrics,
                title=override.get("title"),
            )
        )
    return checks


def _aggregate(overrides_by_url):
    page_checks = []
    for url, overrides in overrides_by_url.items():
        page_checks.extend(_page_checks(url, overrides))
    return aggregate_sections(
        site_checks=_site_checks(),
        page_checks=page_checks,
        page_weights={HOME: 2.0, PRICING: 1.0},
        homepage_url=HOME,
    )


def _by_id(sections):
    return {check.check_id: check for section in sections for check in section.checks}


def test_full_pass_scores_100() -> None:
    sections = _aggregate({HOME: {}})
    assert sum(section.points_awarded for section in sections) == 100
    assert sum(section.points_possible for section in sections) == 100
    assert sum(section.points_evaluated for section in sections) == 100


def test_single_page_parity_preserves_check_titles_and_points() -> None:
    sections = _aggregate({HOME: {}})
    aggregated = _by_id(sections)
    sources = {check.check_id: check for check in _page_checks(HOME)}
    for spec in PAGE_SPECS:
        source = sources[spec.check_id]
        result = aggregated[spec.check_id]
        assert result.status == source.status
        assert result.points_awarded == source.points_awarded
        assert result.title == source.title


def test_coverage_weights_homepage_double() -> None:
    sections = _aggregate(
        {
            HOME: {},
            PRICING: {
                "js.raw_text": {
                    "status": CheckStatus.FAIL,
                    "points": 0,
                }
            },
        }
    )
    js = _by_id(sections)["js.raw_text"]
    assert js.status == CheckStatus.PARTIAL
    assert js.points_awarded == round(5 * (2 / 3), 2)
    assert js.pages_passed == 1
    assert js.pages_failed == 1
    assert js.pages_total == 2


def test_coverage_weights_reverse() -> None:
    sections = _aggregate(
        {
            HOME: {
                "js.raw_text": {
                    "status": CheckStatus.FAIL,
                    "points": 0,
                }
            },
            PRICING: {},
        }
    )
    js = _by_id(sections)["js.raw_text"]
    assert js.status == CheckStatus.PARTIAL
    assert js.points_awarded == round(5 * (1 / 3), 2)


def test_coverage_partial_title_does_not_claim_zero_pages_passed() -> None:
    sections = _aggregate(
        {
            HOME: {"js.raw_text": {"status": CheckStatus.PARTIAL, "points": 2.5}},
            PRICING: {"js.raw_text": {"status": CheckStatus.PARTIAL, "points": 2.5}},
        }
    )
    js = _by_id(sections)["js.raw_text"]
    assert js.status == CheckStatus.PARTIAL
    assert "partially met on 2 applicable pages" in js.title
    assert any("2 were partial or failed" in line for line in js.evidence)


def test_unknown_is_never_turned_into_failure() -> None:
    unknown = {"status": CheckStatus.UNKNOWN, "points": 0}
    sections = _aggregate({HOME: {"js.raw_text": unknown}, PRICING: {"js.raw_text": unknown}})
    js = _by_id(sections)["js.raw_text"]
    assert js.status == CheckStatus.UNKNOWN
    assert js.points_awarded == 0
    js_section = next(section for section in sections if section.key == "js_rendering_risk")
    assert js_section.points_evaluated == 0
    assert sum(section.points_possible for section in sections) == 100


def test_not_applicable_pages_leave_denominator() -> None:
    sections = _aggregate(
        {
            HOME: {},
            PRICING: {
                "entity.required_fields": {
                    "status": CheckStatus.NOT_APPLICABLE,
                    "points": 0,
                    "metrics": {"entity_present": False},
                }
            },
        }
    )
    required = _by_id(sections)["entity.required_fields"]
    assert required.status == CheckStatus.PASS
    assert required.points_awarded == 5
    assert required.pages_passed == 1
    assert required.pages_total == 2


def test_all_not_applicable_required_fields_fails_with_parity_copy() -> None:
    na = {
        "status": CheckStatus.NOT_APPLICABLE,
        "points": 0,
        "metrics": {"entity_present": False},
    }
    sections = _aggregate({HOME: {"entity.required_fields": na}})
    required = _by_id(sections)["entity.required_fields"]
    assert required.status == CheckStatus.FAIL
    assert required.points_awarded == 0
    assert required.title == (
        "Organization schema is missing: name, logo, description"
    )
    assert any("No Organization entity" in line for line in required.evidence)
    assert required.recommendation.startswith("Add Organization JSON-LD")


def test_consistency_no_carriers_fails() -> None:
    no_entity = {
        "status": CheckStatus.FAIL,
        "points": 0,
        "metrics": {"entity_present": False},
    }
    sections = _aggregate(
        {HOME: {"entity.org_schema": no_entity}, PRICING: {"entity.org_schema": no_entity}}
    )
    org = _by_id(sections)["entity.org_schema"]
    assert org.status == CheckStatus.FAIL
    assert org.points_awarded == 0
    assert len(org.affected_pages) == 2


def test_consistency_one_carrier_is_partial_50_percent() -> None:
    sections = _aggregate(
        {
            HOME: {},
            PRICING: {
                "entity.org_schema": {
                    "status": CheckStatus.FAIL,
                    "points": 0,
                    "metrics": {"entity_present": False},
                }
            },
        }
    )
    org = _by_id(sections)["entity.org_schema"]
    assert org.status == CheckStatus.PARTIAL
    assert org.points_awarded == 3.5
    assert org.pages_passed == 1


def test_consistency_matching_fingerprints_passes() -> None:
    sections = _aggregate({HOME: {}, PRICING: {}})
    org = _by_id(sections)["entity.org_schema"]
    assert org.status == CheckStatus.PASS
    assert org.points_awarded == 7


def test_consistency_conflicting_fingerprints_fails() -> None:
    sections = _aggregate(
        {
            HOME: {},
            PRICING: {
                "entity.org_schema": {
                    "metrics": {"fingerprint": "different"},
                }
            },
        }
    )
    org = _by_id(sections)["entity.org_schema"]
    assert org.status == CheckStatus.FAIL
    assert org.points_awarded == 0
    assert len(org.affected_pages) >= 2


def test_consistency_conflict_counts_majority_and_minority_pages() -> None:
    sections = _aggregate(
        {
            HOME: {},
            PRICING: {},
            ABOUT: {
                "entity.org_schema": {
                    "metrics": {"fingerprint": "different"},
                }
            },
        }
    )
    org = _by_id(sections)["entity.org_schema"]
    assert org.status == CheckStatus.FAIL
    assert org.pages_passed == 2
    assert org.pages_failed == 1
    assert org.pages_total == 3
    assert len(org.affected_pages) == 3


def test_consistency_all_unknown_stays_unknown() -> None:
    unknown = {"status": CheckStatus.UNKNOWN, "points": 0, "metrics": {"entity_present": False}}
    sections = _aggregate(
        {HOME: {"entity.org_schema": unknown}, PRICING: {"entity.org_schema": unknown}}
    )
    org = _by_id(sections)["entity.org_schema"]
    assert org.status == CheckStatus.UNKNOWN


def test_section_and_report_invariants_hold() -> None:
    sections = _aggregate(
        {
            HOME: {"facts.tables": {"status": CheckStatus.FAIL, "points": 0}},
            PRICING: {"quote.lead_paragraph": {"status": CheckStatus.PARTIAL, "points": 2.5}},
        }
    )
    total_possible = 0.0
    total_awarded = 0.0
    for section in sections:
        assert round(section.points_possible, 2) == SCORE_WEIGHTS[section.key]
        assert round(sum(check.points_possible for check in section.checks), 2) == section.points_possible
        assert round(sum(check.points_awarded for check in section.checks), 2) == section.points_awarded
        assert len({check.check_id for check in section.checks}) == len(section.checks)
        total_possible += section.points_possible
        total_awarded += section.points_awarded
    assert round(total_possible, 2) == 100
    assert 0 <= total_awarded <= 100
