"""Probe a Kiosk Satellite admin port.

``GET /api/health`` is the one request the app answers without a token
(``remote_manager.dart``). The body is the hardware section: name, model,
brand, app version, addresses, and a few fleet flags. A tablet that is not
running Kiosk Satellite does not return that shape.

Scheme ``auto`` tries HTTPS first, then HTTP. The app serves whichever
protocol Remote Administration is set to, on the same port, and it does not
redirect between them.

HTTPS has three outcomes:

- ``verify_ssl`` uses Home Assistant's verifying session (a certificate a
  public authority signed).
- A stored fingerprint uses :class:`aiohttp.Fingerprint` on the shared
  unverified session, so the leaf must match and the authority is not
  consulted.
- No fingerprint yet observes the leaf once, without accepting it as
  trusted. The config flow shows the digest before storing it. A kiosk
  migrated from "do not check" is pinned by the coordinator on that same
  observation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import ssl
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import ClientError, ClientResponse, ClientTimeout
from aiohttp.client_exceptions import ServerFingerprintMismatch

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
from .tls import (
    fingerprint_sha256,
    normalize_fingerprint,
    peer_fingerprint,
    pinned_ssl,
    unverified_context,
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
    """What one pass over the admin port found.

    ``cert_sha256`` is the leaf that was presented, lowercase hex, whenever
    the handshake produced one. ``cert_mismatch`` means a stored pin did not
    match that leaf. ``ok`` stays false in that case so callers do not treat
    a changed certificate as a normal connect.
    """

    ok: bool
    scheme: str | None
    ssl_error: bool
    name: str | None = None
    model: str | None = None
    brand: str | None = None
    app_version: str | None = None
    ip: str | None = None
    cert_sha256: str | None = None
    cert_mismatch: bool = False


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
    if isinstance(
        err, ssl.SSLError | aiohttp.ClientSSLError | ServerFingerprintMismatch
    ):
        return True
    return "SSL" in type(err).__name__ or "Certificate" in type(err).__name__


def admin_url(scheme: str, host: str, port: int) -> str:
    """Build the remote admin URL for a host."""
    bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{bracketed}:{port}/"


def _health_url(scheme: str, host: str, port: int) -> str:
    """Return the health URL for one attempt."""
    return f"{admin_url(scheme, host, port).rstrip('/')}{HEALTH_PATH}"


def _from_payload(
    scheme: str,
    payload: Any,
    *,
    fingerprint: str | None,
    ok: bool,
    ssl_error: bool = False,
    cert_mismatch: bool = False,
) -> ProbeResult:
    """Build a result from a health body, or an empty failure."""
    if not health_document(payload):
        return ProbeResult(
            ok=False,
            scheme=scheme,
            ssl_error=ssl_error,
            cert_sha256=fingerprint,
            cert_mismatch=cert_mismatch,
        )
    return ProbeResult(
        ok=ok,
        scheme=scheme,
        ssl_error=ssl_error,
        name=_text(payload.get("name")),
        model=_text(payload.get("model")),
        brand=_text(payload.get("brand")),
        app_version=_text(payload.get("appVersion")),
        ip=_text(payload.get("ip")),
        cert_sha256=fingerprint,
        cert_mismatch=cert_mismatch,
    )


async def _body_and_fingerprint(
    response: ClientResponse,
) -> tuple[Any, str | None]:
    """Read the JSON body and the leaf fingerprint while the connection is open."""
    fingerprint = peer_fingerprint(response)
    if response.status != 200:
        _LOGGER.debug("Probe %s returned HTTP %s", response.url, response.status)
        return None, fingerprint
    try:
        payload = await response.json(content_type=None)
    except (ClientError, ValueError, TypeError) as err:
        _LOGGER.debug("Probe %s did not return JSON: %s", response.url, err)
        return None, fingerprint
    return payload, fingerprint


async def _observe(
    hass: HomeAssistant,
    url: str,
    timeout: ClientTimeout,
) -> tuple[Any, str | None]:
    """Handshake without a CA check and return the body plus the leaf hash.

    This is the one request that does not yet know which certificate to
    expect. It is not a successful pin by itself.

    aiohttp releases the connection as soon as a short body arrives, which
    drops the SSL object before the caller can read it. A real session
    therefore reads the leaf on a second handshake. Test doubles are left
    alone so they do not open a socket.
    """
    session = async_get_clientsession(hass, verify_ssl=False)
    async with session.get(
        url,
        timeout=timeout,
        allow_redirects=False,
        ssl=unverified_context(),
    ) as response:
        payload, fingerprint = await _body_and_fingerprint(response)
    if (
        fingerprint is None
        and health_document(payload)
        and type(session).__module__.startswith("aiohttp")
    ):
        fingerprint = await _leaf_on_new_connection(url, timeout)
    return payload, fingerprint


async def async_probe(
    hass: HomeAssistant,
    host: str,
    port: int = DEFAULT_PORT,
    scheme_mode: str = SCHEME_AUTO,
    verify_ssl: bool = False,
    preferred: str | None = None,
    *,
    pinned_sha256: str | None = None,
) -> ProbeResult:
    """Probe ``host`` and return the first health document, or a failure.

    ``ssl_error`` is set when HTTPS failed the certificate check and no
    later attempt succeeded. ``cert_mismatch`` is set when a stored
    fingerprint did not match. A mismatch does not fall through to HTTP:
    the tablet answered TLS, and a different certificate is a reason to
    stop rather than a reason to try the other protocol.
    """
    timeout = ClientTimeout(total=PROBE_TIMEOUT)
    pin = None if verify_ssl else normalize_fingerprint(pinned_sha256)
    saw_ssl = False
    captured: str | None = None
    for scheme in schemes_to_try(scheme_mode, preferred):
        url = _health_url(scheme, host, port)
        try:
            result = await _probe_scheme(
                hass, url, scheme, timeout, verify_ssl=verify_ssl, pin=pin
            )
        except TimeoutError:
            _LOGGER.debug("Probe %s timed out", url)
            continue
        except ServerFingerprintMismatch as err:
            # ``got`` is the SHA-256 digest aiohttp computed, not the
            # certificate. Hex of those bytes is the fingerprint we store.
            got = err.got.hex() if err.got else None
            _LOGGER.debug("Probe %s certificate pin did not match", url)
            observed = await _observe_after_mismatch(hass, url, timeout, got)
            return observed
        except (ClientError, OSError, ssl.SSLError) as err:
            if _ssl_failure(err):
                saw_ssl = True
                captured = (
                    await _capture_after_ssl_error(hass, url, timeout) or captured
                )
            _LOGGER.debug("Probe %s failed: %s", url, err)
            if scheme_mode == SCHEME_HTTPS and _ssl_failure(err):
                break
            continue
        if result is None:
            continue
        if result.cert_mismatch:
            return result
        if result.ok:
            return result
        if result.ssl_error:
            saw_ssl = True
            captured = result.cert_sha256 or captured
            if scheme_mode == SCHEME_HTTPS:
                break
    return ProbeResult(
        ok=False,
        scheme=None,
        ssl_error=saw_ssl,
        cert_sha256=captured,
    )


async def _probe_scheme(
    hass: HomeAssistant,
    url: str,
    scheme: str,
    timeout: ClientTimeout,
    *,
    verify_ssl: bool,
    pin: str | None,
) -> ProbeResult | None:
    """Try one scheme. None means try the next scheme."""
    if scheme != SCHEME_HTTPS:
        session = async_get_clientsession(hass, verify_ssl=False)
        async with session.get(
            url, timeout=timeout, allow_redirects=False, ssl=False
        ) as response:
            payload, _fingerprint = await _body_and_fingerprint(response)
        if not health_document(payload):
            _LOGGER.debug("Probe %s is not a Kiosk Satellite health document", url)
            return None
        return _from_payload(scheme, payload, fingerprint=None, ok=True)

    if verify_ssl:
        return await _probe_authority(hass, url, timeout)

    if pin:
        session = async_get_clientsession(hass, verify_ssl=False)
        async with session.get(
            url, timeout=timeout, allow_redirects=False, ssl=pinned_ssl(pin)
        ) as response:
            payload, fingerprint = await _body_and_fingerprint(response)
        if not health_document(payload):
            _LOGGER.debug("Probe %s is not a Kiosk Satellite health document", url)
            return None
        return _from_payload(
            SCHEME_HTTPS, payload, fingerprint=fingerprint or pin, ok=True
        )

    payload, fingerprint = await _observe(hass, url, timeout)
    if not health_document(payload):
        _LOGGER.debug("Probe %s is not a Kiosk Satellite health document", url)
        return None
    return _from_payload(SCHEME_HTTPS, payload, fingerprint=fingerprint, ok=True)


async def _probe_authority(
    hass: HomeAssistant, url: str, timeout: ClientTimeout
) -> ProbeResult | None:
    """Validate the certificate against the system trust store."""
    session = async_get_clientsession(hass, verify_ssl=True)
    try:
        async with session.get(
            url, timeout=timeout, allow_redirects=False, ssl=True
        ) as response:
            payload, fingerprint = await _body_and_fingerprint(response)
    except (ClientError, OSError, ssl.SSLError) as err:
        if not _ssl_failure(err):
            raise
        fingerprint = await _capture_after_ssl_error(hass, url, timeout)
        _LOGGER.debug("Probe %s failed authority validation: %s", url, err)
        return ProbeResult(
            ok=False,
            scheme=SCHEME_HTTPS,
            ssl_error=True,
            cert_sha256=fingerprint,
        )
    if not health_document(payload):
        _LOGGER.debug("Probe %s is not a Kiosk Satellite health document", url)
        return None
    return _from_payload(SCHEME_HTTPS, payload, fingerprint=fingerprint, ok=True)


async def _capture_after_ssl_error(
    hass: HomeAssistant, url: str, timeout: ClientTimeout
) -> str | None:
    """Read the leaf after a rejected handshake so the flow can show it."""
    try:
        _payload, fingerprint = await _observe(hass, url, timeout)
    except (ClientError, OSError, ssl.SSLError, TimeoutError) as err:
        _LOGGER.debug("Could not read the certificate from %s: %s", url, err)
        return None
    return fingerprint


async def _observe_after_mismatch(
    hass: HomeAssistant,
    url: str,
    timeout: ClientTimeout,
    got: str | None,
) -> ProbeResult:
    """Confirm the new certificate still belongs to a Kiosk Satellite.

    The pin check fails before a response exists, so the health document is
    read with a separate observation. ``ok`` stays false: the stored pin is
    unchanged until the user accepts the new fingerprint.
    """
    payload: Any = None
    fingerprint = got
    try:
        payload, observed = await _observe(hass, url, timeout)
        fingerprint = observed or got
    except (ClientError, OSError, ssl.SSLError, TimeoutError) as err:
        _LOGGER.debug("Probe %s mismatched and could not be re-read: %s", url, err)
    if health_document(payload):
        return _from_payload(
            SCHEME_HTTPS,
            payload,
            fingerprint=fingerprint,
            ok=False,
            ssl_error=True,
            cert_mismatch=True,
        )
    return ProbeResult(
        ok=False,
        scheme=SCHEME_HTTPS,
        ssl_error=True,
        cert_sha256=fingerprint,
        cert_mismatch=True,
    )


async def _leaf_on_new_connection(url: str, timeout: ClientTimeout) -> str | None:
    """Return the leaf SHA-256 from a handshake this code still holds."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    limit = timeout.total or PROBE_TIMEOUT
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host, port, ssl=unverified_context(), server_hostname=host
            ),
            limit,
        )
    except TimeoutError, OSError, ssl.SSLError:
        return None
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError, OSError:
            pass
    if not der:
        return None
    return fingerprint_sha256(der)


def _text(value: Any) -> str | None:
    """Return a stripped string, or None."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None
