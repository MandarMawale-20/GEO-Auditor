"""SSRF-safe HTTP client redirect edge cases."""
from __future__ import annotations

import httpx
import pytest

import app.services.http_client as http_client
from app.services.http_client import _resolve_location, safe_get_text
from app.services.url_validator import ValidationResult


def test_relative_redirect_location_uses_rfc3986_resolution() -> None:
    base = "https://example.com/dir/page"
    assert _resolve_location(base, "final?x=1") == "https://example.com/dir/final?x=1"
    assert _resolve_location(base, "../other") == "https://example.com/other"
    assert _resolve_location(base, "?page=2") == "https://example.com/dir/page?page=2"
    assert _resolve_location(base, "#section") == base
    assert _resolve_location(base, "//cdn.example.com/a") == "https://cdn.example.com/a"


@pytest.mark.asyncio
async def test_query_only_redirect_is_resolved_against_current_page(monkeypatch) -> None:
    async def fake_validate(url):
        return ValidationResult(is_valid=True)

    async def fake_resolve(host):
        return True, None

    monkeypatch.setattr(http_client, "validate_url", fake_validate)
    monkeypatch.setattr(http_client, "resolve_and_validate_ips", fake_resolve)

    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "?page=2"})
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await safe_get_text(
            "https://example.com/dir/page", client=client, max_bytes=1024
        )

    assert result.ok is True
    assert calls == [
        "https://example.com/dir/page",
        "https://example.com/dir/page?page=2",
    ]


@pytest.mark.asyncio
async def test_redirect_limit_is_an_error_not_a_successful_page(monkeypatch) -> None:
    async def fake_validate(url):
        return ValidationResult(is_valid=True)

    async def fake_resolve(host):
        return True, None

    monkeypatch.setattr(http_client, "validate_url", fake_validate)
    monkeypatch.setattr(http_client, "resolve_and_validate_ips", fake_resolve)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/next"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await safe_get_text(
            "https://example.com/start",
            client=client,
            max_bytes=1024,
            max_redirects=2,
        )

    assert result.ok is False
    assert result.redirect_count == 2
    assert result.status_code == 302
    assert "Too many redirects" in (result.error or "")
