"""Probe a Kiosk Satellite admin port.

``GET /api/health`` is the one request the app answers without a token
(``remote_manager.dart``). The body is the hardware section: name, model,
brand, app version, addresses, and a few fleet flags. A tablet that is not
running Kiosk Satellite does not return that shape.

Scheme ``auto`` tries HTTPS first, then HTTP. The app serves whichever
protocol Remote Administration is set to, on the same port, and it does not
redirect between them. ``verify_ssl`` is off unless the user opts in, because
the certificate the app generates is self-signed. Those probes use Home
Assistant's shared session with ``verify_ssl=False``. A kiosk set to check
the certificate uses the verifying session, and only for its HTTPS attempt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import ssl
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientSession, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DEFAULT_PORT,
    HEALTH_PATH,
    PROBE_TIMEOUT,
    SCHEME_AUTO,
    SCHEME_HTTP,
    SCHEME_HTTPS,
)

_LOGGER = logging.getLogger(__name__)

# Keys _health() always puts on the object. Three of them, including the
# device name, is enough to tell a kiosk from an unrelated server on 2324.
_HEALTH_KEYS = frozenset(
    {
        "androidVersion",
        "appVersion",
        "brand",
        "ip",
        "leader",
        "model",
        "name",
        "uptime",
    }
)


@dataclass(slots=True)
class ProbeResult:
    """What one pass over the admin port found."""

    ok: bool
    scheme: str | None
    ssl_error: bool
    name: str | None = None
    model: str | None = None
    brand: str | None = None
    app_version: str | None = None
    ip: str | None = None


def schemes_to_try(scheme_mode: str, preferred: str | None = None) -> list[str]:
    """Return the protocols to try, in order.

    A chosen protocol is tried alone. Auto tries the protocol that worked
    last time first, and otherwise HTTPS then HTTP, which is what the user
    asked for: a self-signed admin page should win over plain HTTP when both
    could be guessed.
    """
    if scheme_mode == SCHEME_HTTP:
        return [SCHEME_HTTP]
    if scheme_mode == SCHEME_HTTPS:
        return [SCHEME_HTTPS]
    if preferred in (SCHEME_HTTP, SCHEME_HTTPS):
        other = SCHEME_HTTP if preferred == SCHEME_HTTPS else SCHEME_HTTPS
        return [preferred, other]
    return [SCHEME_HTTPS, SCHEME_HTTP]


def health_document(payload: Any) -> bool:
    """Return True when payload looks like ``GET /api/health``."""
    if not isinstance(payload, dict):
        return False
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return False
    return len(_HEALTH_KEYS.intersection(payload)) >= 3


def _ssl_failure(err: BaseException) -> bool:
    """Return True when the failure is a certificate problem."""
    if isinstance(err, ssl.SSLError | aiohttp.ClientSSLError):
        return True
    return "SSL" in type(err).__name__ or "Certificate" in type(err).__name__


def _session(hass: HomeAssistant, scheme: str, verify_ssl: bool) -> ClientSession:
    """Return the shared session for this attempt.

    Unverified HTTPS, and every plain HTTP probe, uses ``verify_ssl=False``.
    The verifying session is only for an HTTPS kiosk whose option is on.
    """
    check = scheme == SCHEME_HTTPS and verify_ssl
    return async_get_clientsession(hass, verify_ssl=check)


def admin_url(scheme: str, host: str, port: int) -> str:
    """Build the remote admin URL for a host."""
    bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{bracketed}:{port}/"


async def async_probe(
    hass: HomeAssistant,
    host: str,
    port: int = DEFAULT_PORT,
    scheme_mode: str = SCHEME_AUTO,
    verify_ssl: bool = False,
    preferred: str | None = None,
) -> ProbeResult:
    """Probe ``host`` and return the first health document, or a failure.

    ``ssl_error`` is set when an HTTPS attempt failed the certificate check
    and no later attempt succeeded. Callers use it for the repair that tells
    the user to turn verification off.
    """
    timeout = ClientTimeout(total=PROBE_TIMEOUT)
    saw_ssl = False
    for scheme in schemes_to_try(scheme_mode, preferred):
        url = f"{admin_url(scheme, host, port).rstrip('/')}{HEALTH_PATH}"
        session = _session(hass, scheme, verify_ssl)
        try:
            async with (
                asyncio.timeout(PROBE_TIMEOUT),
                session.get(url, timeout=timeout, allow_redirects=False) as response,
            ):
                if response.status != 200:
                    _LOGGER.debug("Probe %s returned HTTP %s", url, response.status)
                    continue
                payload = await response.json(content_type=None)
        except TimeoutError:
            _LOGGER.debug("Probe %s timed out", url)
            continue
        except (ClientError, OSError, ssl.SSLError) as err:
            if _ssl_failure(err):
                saw_ssl = True
            _LOGGER.debug("Probe %s failed: %s", url, err)
            continue
        if not health_document(payload):
            _LOGGER.debug("Probe %s is not a Kiosk Satellite health document", url)
            continue
        return ProbeResult(
            ok=True,
            scheme=scheme,
            ssl_error=False,
            name=_text(payload.get("name")),
            model=_text(payload.get("model")),
            brand=_text(payload.get("brand")),
            app_version=_text(payload.get("appVersion")),
            ip=_text(payload.get("ip")),
        )
    return ProbeResult(ok=False, scheme=None, ssl_error=saw_ssl)


def _text(value: Any) -> str | None:
    """Return a stripped string, or None."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None
