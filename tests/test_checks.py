"""Check registry invariants: 21 checks, frozen weights, unique IDs."""
from __future__ import annotations

from app.checks import CHECK_ORDER, CHECK_SPECS, build_check, checks_for_category
from app.config import SCORE_WEIGHTS, TOTAL_POSSIBLE_SCORE
from app.models import CheckStatus


def test_registry_has_21_unique_checks() -> None:
    assert len(CHECK_SPECS) == 21
    assert len(CHECK_ORDER) == 21
    assert len(set(CHECK_ORDER)) == 21


def test_each_category_sums_to_its_frozen_weight() -> None:
    for category, weight in SCORE_WEIGHTS.items():
        specs = checks_for_category(category)
        assert specs, f"{category} has no checks"
        assert round(sum(spec.points for spec in specs), 2) == weight


def test_total_possible_is_100() -> None:
    assert TOTAL_POSSIBLE_SCORE == 100
    assert round(sum(spec.points for spec in CHECK_SPECS.values()), 2) == 100


def test_check_categories_are_known() -> None:
    for spec in CHECK_SPECS.values():
        assert spec.category in SCORE_WEIGHTS


def test_string_evidence_is_preserved_as_one_readable_item() -> None:
    check = build_check(
        "citation.snippet",
        status=CheckStatus.PASS,
        title="Snippet",
        evidence="A readable sentence.",
    )
    assert check.evidence == ["A readable sentence."]
    assert check.to_finding().evidence == "A readable sentence."
