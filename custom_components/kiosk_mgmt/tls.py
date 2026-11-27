"""TLS for a Kiosk Satellite admin port.

Kiosk Satellite generates its own certificate for Remote Administration.
That certificate is self-signed, so a normal authority check fails and the
old "turn Verify SSL certificate off" path accepted whatever certificate
was presented. Pinning records the leaf certificate's SHA-256 the first
time the tablet answers and checks that fingerprint on every later
connection. ``verify_ssl`` stays the opt-in for a certificate a public
authority already signed.

aiohttp's :class:`~aiohttp.Fingerprint` hashes the leaf DER after the
handshake, which is the check we want. The first observation has nothing
to compare yet, so it uses a context that does not validate the authority
and reads the peer certificate off the connection. Callers decide whether
to show that fingerprint before storing it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import ssl
from typing import Any

from aiohttp import Fingerprint

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.ssl import get_default_no_verify_context

from .const import DOMAIN

_HEX = frozenset("0123456789abcdef")


def unverified_context() -> ssl.SSLContext:
    """Return a TLS client context that still negotiates, without CA checks.

    This is Home Assistant's shared no-verify context: it is built once,
    when Home Assistant starts, so asking for it here never builds an
    :class:`ssl.SSLContext` (and never loads anything from disk) on the
    event loop. ``check_hostname`` is off and ``verify_mode`` is
    ``CERT_NONE``, and the peer certificate is still readable with
    ``getpeercert(binary_form=True)``, which is all pinning needs.
    """
    return get_default_no_verify_context()


def normalize_fingerprint(value: Any) -> str | None:
    """Return lowercase hex SHA-256, or None when the value is not one.

    Stored records and the repair flow both accept the colon form a person
    would read off a screen.
    """
    if not isinstance(value, str):
        return None
    raw = value.replace(":", "").replace(" ", "").strip().lower()
    if len(raw) != 64 or any(char not in _HEX for char in raw):
        return None
    return raw


def format_fingerprint(value: str) -> str:
    """Return the colon form shown in the config flow, ``AA:BB:...``."""
    raw = normalize_fingerprint(value) or ""
    if len(raw) != 64:
        return value
    return ":".join(raw[i : i + 2] for i in range(0, 64, 2)).upper()


def fingerprint_sha256(der: bytes) -> str:
    """Return the SHA-256 hex digest of a leaf certificate's DER bytes."""
    return hashlib.sha256(der).hexdigest()


def peer_fingerprint(response: object) -> str | None:
    """Return the leaf SHA-256 of an open response, if TLS was used.

    Must be called before the response is released. A plain HTTP response
    and a stand-in used by tests have no SSL object, and both return None.
    """
    connection = getattr(response, "connection", None)
    transport = getattr(connection, "transport", None)
    if transport is None:
        return None
    sslobj = transport.get_extra_info("ssl_object")
    if sslobj is None:
        return None
    der = sslobj.getpeercert(binary_form=True)
    if not der:
        return None
    return fingerprint_sha256(der)


def pinned_ssl(fingerprint: str) -> Fingerprint:
    """Return the aiohttp checker for a stored fingerprint."""
    normalized = normalize_fingerprint(fingerprint)
    if normalized is None:
        raise ValueError("certificate fingerprint must be SHA-256 hex")
    return Fingerprint(bytes.fromhex(normalized))


# The proxy's own signing key, kept in Kiosk Mgmt's storage. Home
# Assistant's signed-path secret belongs to the http component and is not
# touched (hacs/default#14090).
_KEY_STORE = "kiosk_mgmt.proxy_key"
_KEY_STORE_VERSION = 1
_DATA_KEY = "proxy_key"


async def async_load_proxy_key(hass: HomeAssistant) -> bytes:
    """Return the proxy signing key, creating and saving it the first time.

    The key is random, lives in ``.storage/kiosk_mgmt.proxy_key`` and is
    cached for the rest of the run. Proxy sessions therefore survive a
    restart until they expire, and nothing else in Home Assistant can mint
    or read one.
    """
    data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    cached = data.get(_DATA_KEY)
    if isinstance(cached, bytes):
        return cached
    store: Store[dict[str, Any]] = Store(hass, _KEY_STORE_VERSION, _KEY_STORE)
    stored = await store.async_load() or {}
    key = stored.get("key") if isinstance(stored, dict) else None
    if not isinstance(key, str) or len(key) < 64:
        key = secrets.token_hex(32)
        await store.async_save({"key": key})
    # Another caller may have loaded it while this one waited.
    cached = data.get(_DATA_KEY)
    if isinstance(cached, bytes):
        return cached
    data[_DATA_KEY] = key.encode()
    return data[_DATA_KEY]


def proxy_key(hass: HomeAssistant) -> bytes:
    """Return the loaded key. ``async_load_proxy_key`` must have run first."""
    key = hass.data.get(DOMAIN, {}).get(_DATA_KEY)
    if not isinstance(key, bytes):
        raise RuntimeError("Kiosk Mgmt's proxy key is not loaded")
    return key


def keyed_digest(hass: HomeAssistant, payload: bytes) -> str:
    """Return hex HMAC-SHA256 of ``payload`` under Kiosk Mgmt's own key."""
    return hmac.new(proxy_key(hass), payload, hashlib.sha256).hexdigest()
