"""SSRF-safe HTTP client redirect edge cases."""
from __future__ import annotations

import socket

import httpcore
import httpx
import pytest

import app.services.http_client as http_client
import app.services.url_validator as url_validator
from app.services.http_client import (
    PinnedNetworkBackend,
    _resolve_location,
    clear_pinned_ips,
    get_pinned_ips,
    safe_get_text,
    set_pinned_ips,
)
from app.services.url_validator import ValidationResult

_PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _clear_dns_pins():
    clear_pinned_ips()
    yield
    clear_pinned_ips()


def test_relative_redirect_location_uses_rfc3986_resolution() -> None:
    base = "https://example.com/dir/page"
    assert _resolve_location(base, "final?x=1") == "https://example.com/dir/final?x=1"
    assert _resolve_location(base, "../other") == "https://example.com/other"
    assert _resolve_location(base, "?page=2") == "https://example.com/dir/page?page=2"
    assert _resolve_location(base, "#section") == base
    assert _resolve_location(base, "//cdn.example.com/a") == "https://cdn.example.com/a"


@pytest.mark.asyncio
async def test_validate_url_returns_the_addresses_that_passed_validation(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]

    monkeypatch.setattr(url_validator.socket, "getaddrinfo", fake_getaddrinfo)

    result = await url_validator.validate_url("https://example.com/")

    assert result.is_valid is True
    assert result.resolved_ips == (_PUBLIC_IP,)


@pytest.mark.asyncio
async def test_query_only_redirect_is_resolved_against_current_page(monkeypatch) -> None:
    async def fake_validate(url):
        return ValidationResult(is_valid=True, resolved_ips=(_PUBLIC_IP,))

    monkeypatch.setattr(http_client, "validate_url", fake_validate)

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
async def test_redirect_pins_the_new_hosts_validated_address(monkeypatch) -> None:
    async def fake_validate(url):
        ip = "93.184.216.35" if "cdn.example.com" in url else _PUBLIC_IP
        return ValidationResult(is_valid=True, resolved_ips=(ip,))

    monkeypatch.setattr(http_client, "validate_url", fake_validate)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(
                302, headers={"location": "https://cdn.example.com/final"}
            )
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await safe_get_text(
            "https://example.com/start", client=client, max_bytes=1024
        )

    assert result.ok is True
    assert get_pinned_ips("cdn.example.com") == ("93.184.216.35",)


@pytest.mark.asyncio
async def test_redirect_limit_is_an_error_not_a_successful_page(monkeypatch) -> None:
    async def fake_validate(url):
        return ValidationResult(is_valid=True, resolved_ips=(_PUBLIC_IP,))

    monkeypatch.setattr(http_client, "validate_url", fake_validate)

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


class _FakeNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, fail_hosts: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self._fail_hosts = set(fail_hosts)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        self.calls.append(host)
        if host in self._fail_hosts:
            raise httpcore.ConnectError(f"cannot connect to {host}")
        return object()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,
    ):
        return object()

    async def sleep(self, seconds: float) -> None:
        return None


@pytest.mark.asyncio
async def test_pinned_backend_connects_to_validated_ip() -> None:
    fake = _FakeNetworkBackend()
    backend = PinnedNetworkBackend(fake)
    set_pinned_ips("example.com", [_PUBLIC_IP])

    await backend.connect_tcp("example.com", 443)

    assert fake.calls == [_PUBLIC_IP]


@pytest.mark.asyncio
async def test_pinned_backend_tries_next_validated_ip() -> None:
    fake = _FakeNetworkBackend(fail_hosts=("2001:db8::1",))
    backend = PinnedNetworkBackend(fake)
    set_pinned_ips("example.com", ["2001:db8::1", _PUBLIC_IP])

    await backend.connect_tcp("example.com", 443)

    assert fake.calls == ["2001:db8::1", _PUBLIC_IP]


@pytest.mark.asyncio
async def test_pinned_backend_falls_back_without_a_pin() -> None:
    fake = _FakeNetworkBackend()
    backend = PinnedNetworkBackend(fake)

    await backend.connect_tcp("unpinned.example", 443)

    assert fake.calls == ["unpinned.example"]


@pytest.mark.asyncio
async def test_safe_get_text_blocks_a_host_without_a_validated_address(monkeypatch) -> None:
    async def fake_validate(url):
        return ValidationResult(is_valid=True)

    async def fake_resolve(host):
        return False, "URL hostname did not resolve to any address.", ()

    monkeypatch.setattr(http_client, "validate_url", fake_validate)
    monkeypatch.setattr(http_client, "resolve_public_ips", fake_resolve)

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"should not be reached")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await safe_get_text(
            "https://unresolved.example/page", client=client, max_bytes=1024
        )

    assert result.ok is False
    assert called is False
    assert "did not resolve" in (result.error or "")


@pytest.mark.asyncio
async def test_safe_get_text_pins_the_validated_address(monkeypatch) -> None:
    async def fake_validate(url):
        return ValidationResult(is_valid=True, resolved_ips=(_PUBLIC_IP,))

    monkeypatch.setattr(http_client, "validate_url", fake_validate)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await safe_get_text(
            "https://example.com/page", client=client, max_bytes=1024
        )

    assert result.ok is True
    assert get_pinned_ips("example.com") == (_PUBLIC_IP,)
