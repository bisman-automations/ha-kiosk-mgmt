"""Scheme detection and verify_ssl session selection."""

from __future__ import annotations

import ssl
from typing import Any

import pytest

from custom_components.kiosk_mgmt.client import (
    async_probe,
    health_document,
    schemes_to_try,
)
from custom_components.kiosk_mgmt.const import SCHEME_AUTO, SCHEME_HTTP, SCHEME_HTTPS
from tests.common import HEALTH


class _Response:
    """Minimal async context manager around a JSON body."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type: str | None = None) -> Any:
        """Return the canned body."""
        return self._payload

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _Session:
    """Records the URL and returns what the handler decides."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        """Fetch one URL."""
        self.urls.append(url)
        outcome = self.handler(url)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[bool]:
    """Patch the shared session factory and record verify_ssl."""
    flags: list[bool] = []

    def _session(hass: Any, verify_ssl: bool = True, **kwargs: Any) -> _Session:
        flags.append(verify_ssl)
        return _Session(handler)

    monkeypatch.setattr(
        "custom_components.kiosk_mgmt.client.async_get_clientsession", _session
    )
    return flags


def test_auto_order_is_https_then_http() -> None:
    """Auto detection prefers HTTPS, then falls back."""
    assert schemes_to_try(SCHEME_AUTO) == [SCHEME_HTTPS, SCHEME_HTTP]
    assert schemes_to_try(SCHEME_AUTO, preferred=SCHEME_HTTP) == [
        SCHEME_HTTP,
        SCHEME_HTTPS,
    ]
    assert schemes_to_try(SCHEME_HTTPS) == [SCHEME_HTTPS]


def test_health_document_matches_the_app() -> None:
    """The real /api/health shape is accepted, and a stranger is not."""
    assert health_document(HEALTH)
    assert not health_document({"ok": True})
    assert not health_document({"name": "x"})


async def test_auto_uses_https_when_it_answers(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working HTTPS health document stops the search."""
    flags = _install(monkeypatch, lambda url: HEALTH)

    result = await async_probe(
        hass, "192.0.2.10", scheme_mode=SCHEME_AUTO, verify_ssl=False
    )

    assert result.ok is True
    assert result.scheme == SCHEME_HTTPS
    assert result.name == "Kitchen tablet"
    assert result.model == "KFTUWI"
    assert flags == [False]


async def test_auto_falls_back_to_http(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTPS failure is followed by HTTP."""

    def handler(url: str) -> Any:
        if url.startswith("https:"):
            raise ConnectionError("refused")
        return HEALTH

    flags = _install(monkeypatch, handler)
    result = await async_probe(
        hass, "192.0.2.10", scheme_mode=SCHEME_AUTO, verify_ssl=False
    )

    assert result.ok is True
    assert result.scheme == SCHEME_HTTP
    assert flags == [False, False]


async def test_verify_ssl_uses_the_verifying_session(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTPS with verification on uses the shared verifying session."""
    flags = _install(monkeypatch, lambda url: HEALTH)
    result = await async_probe(
        hass, "192.0.2.10", scheme_mode=SCHEME_HTTPS, verify_ssl=True
    )
    assert result.ok is True
    assert flags == [True]


async def test_unverified_https_uses_verify_ssl_false(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default self-signed probe does not verify the certificate."""
    flags = _install(monkeypatch, lambda url: HEALTH)
    await async_probe(hass, "192.0.2.10", scheme_mode=SCHEME_HTTPS, verify_ssl=False)
    assert flags == [False]


async def test_https_certificate_failure_is_reported(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate error is distinct from a tablet that is simply down."""
    _install(monkeypatch, lambda url: ssl.SSLCertVerificationError("self-signed"))
    result = await async_probe(
        hass, "192.0.2.10", scheme_mode=SCHEME_HTTPS, verify_ssl=True
    )
    assert result.ok is False
    assert result.ssl_error is True
