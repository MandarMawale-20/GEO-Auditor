"""API hardening around the legacy shared-report endpoint."""
from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


def _legacy_report_payload() -> dict:
    return {
        "url": "https://example.com",
        "final_url": "https://example.com/",
        "generated_at": "2025-01-01T00:00:00+00:00",
        "total_score": 42,
        "max_score": 100,
        "grade": "D",
        "grade_label": "Weak AI visibility",
        "sections": [
            {
                "key": "quote_readiness",
                "label": "Quote Readiness",
                "group": "Content Extractability",
                "points_possible": 15,
                "points_awarded": 5,
                "findings": [],
            }
        ],
        "fetch_health": {"http_status": 200},
        "answerability_matrix": [],
        "top_priorities": [],
        "all_findings": [],
        "llm_enhanced": False,
        "llm_error": None,
        "audit_duration_ms": 10,
        "report_id": "legacy",
    }


def test_render_shared_rejects_oversized_payload() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/render-shared",
        content=b"x" * 1_000_001,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Report payload is too large."


def test_render_shared_falls_back_to_cached_view_when_hash_is_too_large(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main_module, "MAX_SHARE_PAYLOAD_BYTES", 1)
    client = TestClient(app)

    response = client.post(
        "/api/render-shared",
        json=_legacy_report_payload(),
        follow_redirects=False,
    )

    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("/report/view/")
    assert location.endswith("?too_large=1")

    view = client.get(location)
    assert view.status_code == 200
    assert "too large to encode in a share URL" in view.text
    assert "GEO Audit Report" in view.text
