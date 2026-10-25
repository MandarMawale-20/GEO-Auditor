"""API hardening around the legacy shared-report endpoint."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_render_shared_rejects_oversized_payload() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/render-shared",
        content=b"x" * 1_000_001,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Report payload is too large."
