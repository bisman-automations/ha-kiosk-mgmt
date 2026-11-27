"""Sandboxed reverse proxy to a Kiosk Satellite admin page.

The panel's iframe cannot send Home Assistant's bearer token. The panel
asks the ``kiosk_mgmt/sign_proxy`` websocket command (administrators only)
for a proxy URL. That URL carries a session: an HMAC-signed token naming
the administrator, the entry, the tablet and an expiry, signed with Kiosk
Mgmt's own random key (``tls.async_load_proxy_key``). Every request under
the prefix, including POST, websockets and EventSource, is checked against
it, and the administrator must still be an active admin.

The tablet's page is never allowed to share Home Assistant's origin
(hacs/default#14090). Every response carries a
``Content-Security-Policy: sandbox`` without ``allow-same-origin``, and the
panel's iframe has the same sandbox, so the page runs on an opaque origin.
It can't read Home Assistant's local storage or tokens, and it can't call
Home Assistant's API as the admin. Opening it as a top-level page gets the
same sandbox. A sandboxed page has no cookies, which is why the session is
in the path and not in a cookie, and its requests back to Home Assistant
are cross-origin, which is why the proxy answers CORS (no credentials are
ever involved) and preflight requests itself. The injected shim gives the
page in-memory stand-ins for ``localStorage``, ``sessionStorage`` and
``document.cookie``, which a sandboxed page can't use.

``requires_auth`` is off on the view class because the session in the path
is the credential. ``_authorize`` returns 401 for a missing, forged or
expired session and 403 for a user who is no longer an administrator.

The tablet's admin page sends its own session as ``Authorization: Bearer``.
Home Assistant's auth middleware treats that header as a Home Assistant
access token. On current Home Assistant an unknown bearer does not stop the
request (the middleware only leaves it unauthenticated). The header was still
removed before the tablet saw it, so the tablet answered 401 and the login
looked like it had failed. The injected shim moves the tablet token to
``X-Kiosk-Authorization`` so Home Assistant never has to accept it. The
proxy puts that value back as ``Authorization`` on the upstream request. A
bearer that is not a valid Home Assistant token is forwarded as well, which
covers a page whose shim did not run. A bearer Home Assistant did accept is
never forwarded: that one is an administrator's token.

The tablet is contacted with the pinned certificate (or the system trust
store when Verify SSL certificate is on). The tablet's own
``X-Frame-Options`` and Content-Security-Policy are replaced by the sandbox
policy so the panel can embed the page and the shim can run. Absolute URLs
are rewritten onto the proxy prefix where that is safe.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
import gzip
import hmac
import json
import logging
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import quote, urlencode
import zlib

from aiohttp import ClientError, ClientTimeout, ClientWSTimeout, WSMsgType, web
from aiohttp.client_exceptions import ServerFingerprintMismatch
from aiohttp.hdrs import AUTHORIZATION, CONTENT_LENGTH, CONTENT_TYPE, LOCATION
from multidict import CIMultiDict

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.http import (
    KEY_HASS,
    HomeAssistantView,
    request_handler_factory,
)

from .client import admin_url
from .const import (
    ATTR_CERT_SHA256,
    CONF_SCHEME,
    CONF_VERIFY_SSL,
    DOMAIN,
    PROXY_HOOK,
    PROXY_SANDBOX,
    PROXY_SESSION_SECONDS,
    SCHEME_HTTP,
    SCHEME_HTTPS,
)
from .records import kiosks_from_options
from .tls import async_load_proxy_key, keyed_digest, pinned_ssl

_LOGGER = logging.getLogger(__name__)

PROXY_SOURCE = "kiosk-mgmt-proxy"
# The iframe shim moves the tablet token here. Home Assistant does not
# treat this name as one of its own credentials.
KIOSK_AUTHORIZATION = "X-Kiosk-Authorization"
_MAX_REWRITE = 8 * 1024 * 1024
# Request bodies larger than this are streamed with the original
# Content-Length (an APK upload). Smaller ones, including login, are
# buffered so the tablet sees a Content-Length rather than chunked encoding.
_MAX_BUFFER = 1024 * 1024
_CHUNK = 64 * 1024
_SHIM_PATH = Path(__file__).parent / "frontend" / "shim.mjs"
# Where the shim's source is cached once it has been read (in the executor).
_DATA_SHIM = "shim"
_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_DROP_RESPONSE = _HOP | frozenset(
    {
        "x-frame-options",
        "strict-transport-security",
        # A nonce or a strict script-src blocks the injected shim, and the
        # page then never gets past the static header. The proxy cannot keep
        # a policy that forbids the script it has to add.
        "content-security-policy",
        "content-security-policy-report-only",
    }
)
# The tablet's own copies of the headers the proxy sets.
_SANDBOX_KEYS = frozenset(
    {
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "access-control-expose-headers",
        "referrer-policy",
    }
)
_REWRITABLE = (
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "text/plain",
    "application/xml",
    "text/xml",
    "image/svg+xml",
)
_ATTR_URL = re.compile(
    r"""(?P<attr>\b(?:href|src|action|poster|formaction|cite)\s*=\s*)"""
    r"""(?P<quote>["'])(?P<url>[^"']+)(?P=quote)""",
    re.IGNORECASE,
)
_CSS_URL = re.compile(
    r"""url\(\s*(?P<quote>["']?)(?P<url>[^)"']+)(?P=quote)\s*\)""",
    re.IGNORECASE,
)
_CSS_IMPORT = re.compile(
    r"""@import\s+(?P<quote>["'])(?P<url>[^"']+)(?P=quote)""",
    re.IGNORECASE,
)
_SRCSET = re.compile(
    r"""(?P<attr>\bsrcset\s*=\s*)(?P<quote>["'])(?P<value>[^"']+)(?P=quote)""",
    re.IGNORECASE,
)
# Quoted ``/static/...`` paths in a script. ``/api/...`` strings are left
# alone: the tablet's client uses them as command names (`core.js`) and
# resolves them with ``apiUrl`` at runtime.
_QUOTED_STATIC = re.compile(r"""(?P<quote>["'])/static/(?P<rest>[^"'\\\s?#]*)""")
_TOKEN_QUERY = re.compile(r"(token=)[^&]*", re.IGNORECASE)


def kiosk_segment(kiosk_id: str) -> str:
    """Return one path segment. Colons in ``mac:`` and ``fleet:`` ids stay."""
    return quote(kiosk_id, safe=":")


def proxy_prefix(entry_id: str, kiosk_id: str, session: str) -> str:
    """Return the proxy path for one tablet and session, without a trailing slash."""
    return f"/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_segment(kiosk_id)}/{session}"


def session_token(
    hass: HomeAssistant,
    user_id: str,
    entry_id: str,
    kiosk_id: str,
    *,
    seconds: int = PROXY_SESSION_SECONDS,
) -> str:
    """Return a signed session for one admin, entry and tablet.

    Base64url and hex only, so it is one safe path segment. The key must be
    loaded (``async_load_proxy_key``).
    """
    payload = json.dumps(
        {
            "e": entry_id,
            "k": kiosk_id,
            "u": user_id,
            "exp": int(time.time()) + seconds,
        },
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{encoded}.{keyed_digest(hass, payload)}"


async def async_proxy_url(
    hass: HomeAssistant, entry_id: str, kiosk_id: str, user_id: str
) -> str:
    """Return the URL the panel's iframe opens for one tablet."""
    await async_load_proxy_key(hass)
    session = session_token(hass, user_id, entry_id, kiosk_id)
    return f"{proxy_prefix(entry_id, kiosk_id, session)}/"


def _origins(record: Mapping[str, Any]) -> tuple[str, str]:
    """Return the http and https origins of the tablet, including the port."""
    host = str(record[CONF_HOST])
    port = int(record["port"])
    return (
        admin_url(SCHEME_HTTP, host, port).rstrip("/"),
        admin_url(SCHEME_HTTPS, host, port).rstrip("/"),
    )


def _scheme(record: Mapping[str, Any]) -> str:
    """Return the protocol the last successful probe used."""
    scheme = record.get(CONF_SCHEME)
    if scheme in (SCHEME_HTTP, SCHEME_HTTPS):
        return str(scheme)
    return SCHEME_HTTP


def rewrite_location(value: str, *, prefix: str, origins: tuple[str, str]) -> str:
    """Point a redirect at the proxy, keeping it on Home Assistant's origin."""
    for origin in origins:
        if value == origin or _has_boundary(value, origin):
            rest = value[len(origin) :]
            if rest and not rest.startswith(("/", "?", "#")):
                continue
            return prefix + (rest or "/")
    if (
        value.startswith("/")
        and not value.startswith("//")
        and not _already(value, prefix)
    ):
        return prefix + value
    return value


def _has_boundary(value: str, origin: str) -> bool:
    """True when ``value`` starts with ``origin`` and does not swallow a longer host."""
    return value.startswith(origin) and (
        len(value) == len(origin) or value[len(origin)] in "/?#"
    )


def _already(url: str, prefix: str) -> bool:
    """True when a URL is already under the proxy prefix."""
    return url == prefix or url.startswith(prefix + "/")


def strip_frame_ancestors(policy: str) -> str:
    """Remove ``frame-ancestors`` and keep the rest of a CSP."""
    kept: list[str] = []
    for part in policy.split(";"):
        directive = part.strip()
        if not directive:
            continue
        if directive.split()[0].lower() == "frame-ancestors":
            continue
        kept.append(directive)
    return "; ".join(kept)


def rewrite_set_cookie(header: str, *, prefix: str, secure: bool) -> str:
    """Keep a tablet cookie on the proxy path and on this Home Assistant.

    ``Domain`` would attach the cookie to the tablet's host, which the
    browser never sends back to Home Assistant. ``Path=/`` would attach it
    to every Home Assistant page. ``Secure`` is kept only when Home
    Assistant itself is HTTPS, so an HTTP Home Assistant can still store the
    cookie. ``SameSite`` is ``Lax``: the iframe is same-site with Home
    Assistant, and ``None`` without ``Secure`` is dropped by the browser.
    """
    pieces = [piece.strip() for piece in header.split(";") if piece.strip()]
    if not pieces:
        return header
    rewritten = [pieces[0]]
    saw_path = False
    for piece in pieces[1:]:
        name, _, raw = piece.partition("=")
        lowered = name.lower()
        if lowered in {"domain", "secure", "samesite"}:
            continue
        if lowered == "path":
            saw_path = True
            path = raw.strip() or "/"
            if not path.startswith("/"):
                path = f"/{path}"
            if not _already(path, prefix):
                path = prefix if path == "/" else prefix + path
            rewritten.append(f"Path={path}")
            continue
        rewritten.append(piece)
    if not saw_path:
        rewritten.append(f"Path={prefix}")
    rewritten.append("SameSite=Lax")
    if secure:
        rewritten.append("Secure")
    return "; ".join(rewritten)


def rewrite_text(
    body: str,
    content_type: str,
    *,
    prefix: str,
    origins: tuple[str, str],
) -> str:
    """Rewrite absolute and root-relative URLs so the app stays on the proxy."""
    text = body
    for origin in origins:
        text = _replace_origin(text, origin, prefix)
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "text/html":
        text = _ATTR_URL.sub(lambda match: _rewrite_attr(match, prefix), text)
        text = _SRCSET.sub(lambda match: _rewrite_srcset(match, prefix), text)
        text = _inject_hook(text, prefix)
    elif media == "text/css":
        text = _CSS_IMPORT.sub(lambda match: _rewrite_import(match, prefix), text)
        text = _CSS_URL.sub(lambda match: _rewrite_css(match, prefix), text)
    elif media in {
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
    }:
        text = _QUOTED_STATIC.sub(lambda match: _rewrite_static(match, prefix), text)
    return text


def _replace_origin(text: str, origin: str, prefix: str) -> str:
    """Replace ``origin`` only when it is a whole origin, not a longer host."""
    parts: list[str] = []
    start = 0
    while True:
        found = text.find(origin, start)
        if found < 0:
            parts.append(text[start:])
            break
        end = found + len(origin)
        if end == len(text) or text[end] in "/?#":
            parts.append(text[start:found])
            parts.append(prefix)
            start = end
            continue
        parts.append(text[start:end])
        start = end
    return "".join(parts)


def _rewrite_attr(match: re.Match[str], prefix: str) -> str:
    """Prefix a root-relative HTML attribute. Already-proxied URLs stay put."""
    url = match.group("url")
    if url.startswith("/") and not url.startswith("//") and not _already(url, prefix):
        url = prefix + url
    return f"{match.group('attr')}{match.group('quote')}{url}{match.group('quote')}"


def _rewrite_css(match: re.Match[str], prefix: str) -> str:
    """Prefix a root-relative CSS ``url()``."""
    url = match.group("url")
    quote = match.group("quote")
    if url.startswith("/") and not url.startswith("//") and not _already(url, prefix):
        url = prefix + url
    return f"url({quote}{url}{quote})"


def _rewrite_import(match: re.Match[str], prefix: str) -> str:
    """Prefix a root-relative CSS ``@import``."""
    url = match.group("url")
    quote = match.group("quote")
    if url.startswith("/") and not url.startswith("//") and not _already(url, prefix):
        url = prefix + url
    return f"@import {quote}{url}{quote}"


def _rewrite_srcset(match: re.Match[str], prefix: str) -> str:
    """Prefix root-relative candidates in an HTML ``srcset``."""
    parts: list[str] = []
    for item in match.group("value").split(","):
        piece = item.strip()
        if not piece:
            continue
        bits = piece.split()
        url = bits[0]
        if (
            url.startswith("/")
            and not url.startswith("//")
            and not _already(url, prefix)
        ):
            bits[0] = prefix + url
        parts.append(" ".join(bits))
    value = ", ".join(parts)
    return f"{match.group('attr')}{match.group('quote')}{value}{match.group('quote')}"


def _rewrite_static(match: re.Match[str], prefix: str) -> str:
    """Prefix a quoted ``/static/...`` path inside a script."""
    return f"{match.group('quote')}{prefix}/static/{match.group('rest')}"


def _log_path(path: str) -> str:
    """Return the part of a proxied path after the prefix, for the log.

    The prefix carries the session, which opens the tablet's admin page by
    itself, so it never goes into the log (hacs/default#14090).
    """
    return "/" + path.lstrip("/")


def _redact(url: str) -> str:
    """Hide a websocket token before a URL is written to the log."""
    return _TOKEN_QUERY.sub(r"\1***", url)


def _decode_body(body: bytes, encoding: str) -> tuple[bytes, bool]:
    """Return plain bytes, and whether a content encoding was removed.

    aiohttp usually decodes gzip itself and drops the header. A tablet that
    gzips anyway, or a client that leaves the header in place, is handled
    here so the rewrite step never treats compressed bytes as text and the
    browser is not told to decode a body that is already plain.
    """
    enc = encoding.lower().strip()
    if body.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(body), True
        except OSError, EOFError, zlib.error:
            return body, bool(enc)
    if enc == "deflate":
        try:
            return zlib.decompress(body), True
        except zlib.error:
            try:
                return zlib.decompress(body, -zlib.MAX_WBITS), True
            except zlib.error:
                return body, False
    if enc in {"gzip", "x-gzip"}:
        return body, True
    return body, False


def _inject_hook(html: str, prefix: str) -> str:
    """Insert ``<base>`` and the rewrite hook as the first thing in ``<head>``."""
    snippet = f'<base href="{prefix}/"><script src="{prefix}/{PROXY_HOOK}"></script>'
    match = re.search(r"<head\b[^>]*>", html, re.IGNORECASE)
    if match:
        return html[: match.end()] + snippet + html[match.end() :]
    return snippet + html


def _read_shim_source() -> str:
    """Read ``shim.mjs`` and turn its exports into plain declarations.

    Blocking file I/O: only ever run in the executor, through
    :func:`async_load_shim`. The hook script inlines the module inside an
    IIFE, where ``export`` is a syntax error, so the keywords are dropped
    here once instead of on every request.
    """
    source = _SHIM_PATH.read_text(encoding="utf-8")
    source = source.replace("export function ", "function ")
    return source.replace("export const ", "const ")


@dataclass
class _ShimCache:
    """The shim's source, read at most once per Home Assistant run.

    ``lock`` makes concurrent first requests share a single executor read
    instead of each starting their own.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    source: str | None = None


def _shim_cache(hass: HomeAssistant) -> _ShimCache:
    """Return this instance's shim cache, creating it on first use."""
    data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    cache = data.get(_DATA_SHIM)
    if not isinstance(cache, _ShimCache):
        cache = data[_DATA_SHIM] = _ShimCache()
    return cache


async def async_load_shim(hass: HomeAssistant) -> str:
    """Return the shim's source, reading the file in the executor the first time.

    Called from ``async_setup`` so the file is normally read before the
    first tablet page is opened, and again from the proxy view in case that
    read failed. The event loop never touches the disk: the read runs in
    the executor and later calls return the cached string.

    Raises :class:`OSError` when the file can't be read (a broken install).
    """
    cache = _shim_cache(hass)
    if cache.source is not None:
        return cache.source
    async with cache.lock:
        if cache.source is None:  # another request may have read it meanwhile
            cache.source = await hass.async_add_executor_job(_read_shim_source)
        return cache.source


def hook_javascript(prefix: str, origins: tuple[str, str], shim_source: str) -> str:
    """Return the script that rewrites URLs the static pass cannot see.

    The functions live in ``frontend/shim.mjs`` (shipped next to the built
    panel) so the Node tests and this response cannot drift apart.
    ``shim_source`` is that file as returned by :func:`async_load_shim`;
    this function does no I/O, so it is safe to call from the event loop.
    Fetch, XHR, websockets, EventSource, forms and location changes run
    after the document is parsed. The constants are JSON so a tablet name
    cannot break out of the script.
    """
    http_origin, https_origin = origins
    ws_origins = [
        "ws" + http_origin.removeprefix("http"),
        "wss" + https_origin.removeprefix("https"),
    ]
    ctx = {
        "prefix": prefix,
        "origins": [http_origin, https_origin],
        "wsOrigins": ws_origins,
        "source": PROXY_SOURCE,
    }
    return (
        "(() => {\n"
        f"  const __KM_CTX__ = {json.dumps(ctx)};\n"
        "  __KM_CTX__.pageProtocol = location.protocol;\n"
        "  __KM_CTX__.pageHost = location.host;\n"
        f"{shim_source}\n"
        "  installNetworkShim(window, __KM_CTX__);\n"
        "})();\n"
    )


# Sent with every proxied response, the hook and the error pages. The page
# runs on an opaque origin, so its requests back here are cross-origin: they
# never carry credentials, and the session in the path is what authorizes
# them, so any origin may read the answer.
SANDBOX_HEADERS: dict[str, str] = {
    "Content-Security-Policy": PROXY_SANDBOX,
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Expose-Headers": "*",
    # The session is in the URL; don't hand it to another site.
    "Referrer-Policy": "no-referrer",
}


def _preflight(request: web.Request) -> web.Response:
    """Answer a CORS preflight from the sandboxed page without the tablet."""
    headers = dict(SANDBOX_HEADERS)
    headers["Access-Control-Allow-Methods"] = request.headers.get(
        "Access-Control-Request-Method", "GET"
    )
    wanted = request.headers.get("Access-Control-Request-Headers")
    if wanted:
        headers["Access-Control-Allow-Headers"] = wanted
    headers["Access-Control-Max-Age"] = "600"
    return web.Response(status=204, headers=headers)


def _error_page(status: int, title: str, message: str) -> web.Response:
    """Return a small page the panel can tell apart from a blank frame."""
    safe_title = json.dumps(title)[1:-1]
    safe_message = json.dumps(message)[1:-1]
    body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<title>{safe_title}</title></head><body>"
        f"<h1>{safe_title}</h1><p>{safe_message}</p>"
        "<script>parent.postMessage({source:"
        f"{json.dumps(PROXY_SOURCE)},ok:false}}, '*');</script>"
        "</body></html>"
    )
    return web.Response(
        status=status,
        text=body,
        content_type="text/html",
        headers={
            **SANDBOX_HEADERS,
            "Cache-Control": "no-store",
            # The iframe shim reads this to tell a Home Assistant refusal
            # from a refusal the tablet sent.
            "X-Kiosk-Mgmt-Error": "home-assistant",
        },
    )


def _media_type(header: str | None) -> str:
    """Return the content type without parameters."""
    if not header:
        return ""
    return header.split(";", 1)[0].strip().lower()


class KioskProxyView(HomeAssistantView):
    """Proxy one kiosk's admin origin under Home Assistant."""

    # aiohttp treats ``{name:regex}`` as a regular expression, not a Flask-style
    # converter. ``.+`` is the rest of the path, including nested segments.
    # Kiosk ids contain colons (``manual:1``), which is fine: ``{kiosk_id}``
    # is one segment, and the path is everything after it.
    url = "/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_id}/{session}/{path:.+}"
    extra_urls = [
        "/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_id}/{session}",
        "/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_id}/{session}/",
    ]
    name = "api:kiosk_mgmt:proxy"
    # The session in the path is the credential; see ``_authorize``.
    requires_auth = False

    def __init__(self) -> None:
        """Bind every verb to the same handler. The tablet's UI uses more than GET."""
        for method in ("post", "put", "patch", "delete", "head", "options"):
            setattr(self, method, self.get)

    def register(
        self, hass: HomeAssistant, app: web.Application, router: web.UrlDispatcher
    ) -> None:
        """Register the routes without Home Assistant's CORS handling.

        Home Assistant answers a CORS preflight itself for every view and
        refuses an origin it wasn't configured with. The sandboxed page's
        origin is "null", so its JSON calls never reached this view. The
        proxy answers its own preflights instead (``_preflight``), and only
        for a valid session.
        """
        assert self.url is not None
        for method in ("get", "post", "delete", "put", "patch", "head", "options"):
            handler = request_handler_factory(hass, self, getattr(self, method))
            for url in (self.url, *self.extra_urls):
                router.add_route(method, url, handler)

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        kiosk_id: str,
        session: str,
        path: str = "",
    ) -> web.StreamResponse:
        """Forward one request, or serve the rewrite hook."""
        user, failure = await _authorize(request, entry_id, kiosk_id, session)
        if failure is not None or user is None:
            return _error_page(
                failure or 401,
                "Not allowed",
                "This link has expired or isn't for an administrator. "
                "Open the tablet again from the Kiosk Mgmt panel.",
            )
        record = _record(request, entry_id, kiosk_id)
        if record is None:
            return _error_page(
                404,
                "Kiosk not found",
                "That tablet is not in Kiosk Mgmt.",
            )
        if request.method == "OPTIONS" and request.headers.get(
            "Access-Control-Request-Method"
        ):
            return _preflight(request)
        prefix = proxy_prefix(entry_id, kiosk_id, session)
        if path.rstrip("/") == PROXY_HOOK:
            origins = _origins(record)
            hass: HomeAssistant = request.app[KEY_HASS]
            try:
                # Cached after the first read, which runs in the executor.
                shim_source = await async_load_shim(hass)
            except OSError:
                _LOGGER.exception("Kiosk Mgmt could not read %s", _SHIM_PATH.name)
                return _error_page(
                    500,
                    "Kiosk Mgmt is damaged",
                    "A file Kiosk Mgmt needs is missing. Reinstall Kiosk Mgmt.",
                )
            return web.Response(
                text=hook_javascript(prefix, origins, shim_source),
                content_type="application/javascript",
                headers={**SANDBOX_HEADERS, "Cache-Control": "no-store"},
            )
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_websocket(request, record, path, prefix)
        return await _proxy_http(request, record, path, prefix)


def _record(
    request: web.Request, entry_id: str, kiosk_id: str
) -> dict[str, Any] | None:
    """Return the stored kiosk, ignoring tablets the user has hidden."""
    hass: HomeAssistant = request.app[KEY_HASS]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None
    record = kiosks_from_options(entry.options).get(kiosk_id)
    if record is None or record.get("ignored"):
        return None
    return record


async def _authorize(
    request: web.Request, entry_id: str, kiosk_id: str, session: str
) -> tuple[Any, int | None]:
    """Return ``(user, None)`` or ``(None, status)`` for the session in the path.

    401: no session, a forged one, one for another tablet, or an expired
    one. 403: the user is no longer an administrator.
    """
    hass: HomeAssistant = request.app[KEY_HASS]
    if "." not in session:
        return None, 401
    encoded, signature = session.split(".", 1)
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = base64.urlsafe_b64decode((encoded + padding).encode())
    except ValueError:
        return None, 401
    await async_load_proxy_key(hass)
    if not hmac.compare_digest(keyed_digest(hass, payload), signature):
        return None, 401
    try:
        data = json.loads(payload)
    except ValueError:
        return None, 401
    if not isinstance(data, dict):
        return None, 401
    if data.get("e") != entry_id or data.get("k") != kiosk_id:
        return None, 401
    try:
        expires = int(data.get("exp") or 0)
    except TypeError, ValueError:
        return None, 401
    if expires < int(time.time()):
        return None, 401
    user = await hass.auth.async_get_user(str(data.get("u") or ""))
    if user is None or not user.is_active:
        return None, 401
    if not user.is_admin:
        return None, 403
    return user, None


def _forward_query(request: web.Request) -> list[tuple[str, str]]:
    """Return the query to send to the tablet."""
    return list(request.query.items())


def _kiosk_authorization(request: web.Request) -> str | None:
    """Return the tablet credential to send upstream, if there is one.

    ``X-Kiosk-Authorization`` is the shim's copy and always wins. A raw
    ``Authorization`` is forwarded only when it is not a Home Assistant
    access token. Home Assistant's middleware does not reject an unknown
    bearer; it leaves the request unauthenticated, and the session in the
    path is what authorized the view. Dropping that header is what made a correct
    password look like a 401 from the tablet. A bearer the middleware did
    accept belongs to the administrator and stays here.
    """
    moved = request.headers.get(KIOSK_AUTHORIZATION)
    if moved:
        return moved.strip()
    incoming = request.headers.get(AUTHORIZATION)
    if not incoming:
        return None
    if _is_home_assistant_bearer(request, incoming):
        return None
    return incoming


def _is_home_assistant_bearer(request: web.Request, header: str) -> bool:
    """True when ``header`` is an access token Home Assistant would accept."""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return False
    hass: HomeAssistant = request.app[KEY_HASS]
    return hass.auth.async_validate_access_token(token.strip()) is not None


def _forward_request_headers(
    request: web.Request, upstream_origin: str
) -> dict[str, str]:
    """Copy request headers the tablet should see.

    The Home Assistant bearer token and cookies stay here. The
    tablet's own token is added back as ``Authorization`` (see
    ``_kiosk_authorization``). Origin and Referer are rewritten so a tablet
    that checks them still sees its own admin origin. ``Accept-Encoding`` is
    left for the HTTP client, which decodes gzip before this proxy rewrites
    the body.
    """
    headers: dict[str, str] = {}
    # Cookies stay here: Home Assistant's are not the tablet's business, and
    # the sandboxed page has none of its own to send.
    skip = {AUTHORIZATION.lower(), "cookie", KIOSK_AUTHORIZATION.lower()}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in _HOP or lower in skip:
            continue
        if lower == "origin":
            headers[key] = upstream_origin
            continue
        if lower == "referer":
            headers[key] = f"{upstream_origin}/"
            continue
        headers[key] = value
    token = _kiosk_authorization(request)
    if token:
        headers[AUTHORIZATION] = token
    return headers


def _response_headers(
    upstream: Any,
    *,
    prefix: str,
    origins: tuple[str, str],
    rewritten: bool,
    decoded: bool,
    secure: bool,
) -> CIMultiDict[str]:
    """Build the headers the browser should see."""
    headers: CIMultiDict[str] = CIMultiDict()
    for key, value in upstream.headers.items():
        lower = key.lower()
        if lower in _DROP_RESPONSE or lower in _SANDBOX_KEYS:
            continue
        if lower == "set-cookie":
            headers.add(key, rewrite_set_cookie(value, prefix=prefix, secure=secure))
            continue
        if lower == LOCATION.lower():
            headers.add(key, rewrite_location(value, prefix=prefix, origins=origins))
            continue
        if (rewritten or decoded) and lower == CONTENT_LENGTH.lower():
            continue
        if decoded and lower == "content-encoding":
            continue
        headers.add(key, value)
    for key, value in SANDBOX_HEADERS.items():
        headers[key] = value
    return headers


async def _proxy_http(
    request: web.Request,
    record: Mapping[str, Any],
    path: str,
    prefix: str,
) -> web.StreamResponse:
    """Stream one HTTP response, rewriting text bodies before they go out."""
    security = _security(record)
    if security is None:
        return _error_page(
            503,
            "Certificate not pinned yet",
            "Kiosk Mgmt has not recorded this tablet's certificate. "
            "Wait for the next refresh, or reconfigure the tablet.",
        )
    verify, ssl_param = security
    hass: HomeAssistant = request.app[KEY_HASS]
    session = async_get_clientsession(hass, verify_ssl=verify)
    origins = _origins(record)
    scheme = _scheme(record)
    origin = origins[1] if scheme == SCHEME_HTTPS else origins[0]
    upstream = _upstream_url(record, path, _forward_query(request))
    timeout = ClientTimeout(total=60, sock_connect=10)
    data, headers = await _upstream_body(request, origin)
    try:
        response_cm = session.request(
            request.method,
            upstream,
            headers=headers,
            data=data,
            allow_redirects=False,
            ssl=ssl_param,
            timeout=timeout,
        )
        upstream_response = await response_cm.__aenter__()
    except ServerFingerprintMismatch:
        _LOGGER.warning("Proxy to %s refused a changed certificate", _redact(upstream))
        return _error_page(
            409,
            "Certificate changed",
            "This tablet presented a different certificate from the one "
            "Kiosk Mgmt has pinned. Accept the new certificate from the repair.",
        )
    except (ClientError, OSError, TimeoutError) as err:
        _LOGGER.debug(
            "Proxy upstream error %s %s -> %s: %s",
            request.method,
            _log_path(path),
            _redact(upstream),
            err,
        )
        return _error_page(
            502,
            "Tablet didn't answer",
            "Home Assistant could not reach the tablet's admin page.",
        )
    try:
        _LOGGER.debug(
            "Proxied %s %s -> %s (%s)",
            request.method,
            _log_path(path),
            _redact(upstream),
            upstream_response.status,
        )
        return await _send_upstream(
            request,
            upstream_response,
            prefix=prefix,
            origins=origins,
            secure=request.secure,
        )
    finally:
        await response_cm.__aexit__(None, None, None)


async def _upstream_body(
    request: web.Request, upstream_origin: str
) -> tuple[Any, dict[str, str]]:
    """Return the body and headers to send to the tablet.

    Login and the other JSON calls are small and are buffered so the tablet
    sees ``Content-Length``. Its embedded server is not required to accept a
    chunked body, and a chunked login is what arrives as an empty password.
    Larger bodies (an APK upload) keep the browser's length and are streamed.
    """
    data: Any = None
    if request.can_read_body:
        length = request.content_length
        if length is not None and length <= _MAX_BUFFER:
            data = await request.read()
        else:
            data = request.content
    headers = _forward_request_headers(request, upstream_origin)
    if isinstance(data, bytes):
        headers["Content-Length"] = str(len(data))
    elif request.content_length is not None:
        headers["Content-Length"] = str(request.content_length)
    return data, headers


async def _send_upstream(
    request: web.Request,
    upstream: Any,
    *,
    prefix: str,
    origins: tuple[str, str],
    secure: bool,
) -> web.StreamResponse:
    """Write the upstream response, buffering text that has to be rewritten."""
    media = _media_type(upstream.headers.get(CONTENT_TYPE))
    length = upstream.content_length
    rewrite = media in _REWRITABLE and (length is None or length <= _MAX_REWRITE)
    if rewrite:
        raw = await upstream.read()
        body, decoded = _decode_body(
            raw, upstream.headers.get("Content-Encoding") or ""
        )
        headers = _response_headers(
            upstream,
            prefix=prefix,
            origins=origins,
            rewritten=True,
            decoded=decoded,
            secure=secure,
        )
        if len(body) <= _MAX_REWRITE:
            text = body.decode("utf-8", errors="replace")
            text = rewrite_text(
                text,
                upstream.headers.get(CONTENT_TYPE) or media,
                prefix=prefix,
                origins=origins,
            )
            body = text.encode()
        return web.Response(status=upstream.status, headers=headers, body=body)
    headers = _response_headers(
        upstream,
        prefix=prefix,
        origins=origins,
        rewritten=False,
        decoded=False,
        secure=secure,
    )
    stream = web.StreamResponse(status=upstream.status, headers=headers)
    await stream.prepare(request)
    async for chunk in upstream.content.iter_chunked(_CHUNK):
        await stream.write(chunk)
    await stream.write_eof()
    return stream


def _upstream_url(
    record: Mapping[str, Any], path: str, query: list[tuple[str, str]]
) -> str:
    """Build the tablet URL for a proxied path."""
    base = admin_url(_scheme(record), str(record[CONF_HOST]), int(record["port"]))
    suffix = path.lstrip("/")
    url = f"{base}{suffix}" if suffix else base
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


def _security(record: Mapping[str, Any]) -> tuple[bool, Any] | None:
    """Return ``(verify_session, ssl arg)``, or None when a pin is required and missing."""
    if _scheme(record) != SCHEME_HTTPS:
        return False, False
    if record.get(CONF_VERIFY_SSL):
        return True, True
    fingerprint = record.get(ATTR_CERT_SHA256)
    if not fingerprint:
        return None
    try:
        return False, pinned_ssl(str(fingerprint))
    except ValueError:
        _LOGGER.debug("Ignoring an unreadable certificate pin")
        return None


async def _proxy_websocket(
    request: web.Request,
    record: Mapping[str, Any],
    path: str,
    prefix: str,
) -> web.StreamResponse:
    """Bridge a browser websocket to the tablet over the pinned connection."""
    security = _security(record)
    if security is None:
        return _error_page(
            503,
            "Certificate not pinned yet",
            "Kiosk Mgmt has not recorded this tablet's certificate.",
        )
    verify, ssl_param = security
    hass: HomeAssistant = request.app[KEY_HASS]
    session = async_get_clientsession(hass, verify_ssl=verify)
    scheme = "wss" if _scheme(record) == SCHEME_HTTPS else "ws"
    http_scheme = SCHEME_HTTPS if scheme == "wss" else SCHEME_HTTP
    base = admin_url(http_scheme, str(record[CONF_HOST]), int(record["port"]))
    suffix = path.lstrip("/")
    upstream_url = f"{scheme}://{base.split('://', 1)[1]}{suffix}"
    query = _forward_query(request)
    if query:
        upstream_url = f"{upstream_url}?{urlencode(query)}"
    protocols = [
        item.strip()
        for item in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if item.strip()
    ]
    client = web.WebSocketResponse(protocols=protocols)
    await client.prepare(request)
    try:
        upstream = await session.ws_connect(
            upstream_url,
            ssl=ssl_param,
            headers=_forward_request_headers(request, base.rstrip("/")),
            protocols=protocols,
            timeout=ClientWSTimeout(ws_close=10),
        )
    except (ClientError, OSError, TimeoutError) as err:
        _LOGGER.debug(
            "Proxy upstream error websocket %s -> %s: %s",
            _log_path(path),
            _redact(upstream_url),
            err,
        )
        await client.close(code=1011, message=b"upstream unavailable")
        return client
    _LOGGER.debug(
        "Proxied websocket %s -> %s",
        _log_path(path),
        _redact(upstream_url),
    )

    async def _from_browser() -> None:
        async for msg in client:
            if msg.type == WSMsgType.TEXT:
                await upstream.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await upstream.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                await upstream.close()
                break

    async def _from_tablet() -> None:
        async for msg in upstream:
            if msg.type == WSMsgType.TEXT:
                await client.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await client.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                await client.close()
                break

    browser_task = asyncio.create_task(_from_browser())
    tablet_task = asyncio.create_task(_from_tablet())
    try:
        await asyncio.wait(
            {browser_task, tablet_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        browser_task.cancel()
        tablet_task.cancel()
        await asyncio.gather(browser_task, tablet_task, return_exceptions=True)
        if not upstream.closed:
            await upstream.close()
        if not client.closed:
            await client.close()
    return client


def register_proxy(hass: HomeAssistant) -> None:
    """Register the proxy view once."""
    if hass.data[DOMAIN].get("proxy"):
        return
    hass.http.register_view(KioskProxyView())
    hass.data[DOMAIN]["proxy"] = True
