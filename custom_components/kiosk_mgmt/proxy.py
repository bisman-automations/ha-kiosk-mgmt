"""Authenticated reverse proxy to a Kiosk Satellite admin page.

The sidebar iframe cannot send Home Assistant's bearer token, and a signed
path (``async_sign_path``) only authorizes one exact GET. The panel therefore
loads a signed URL, and that response sets a short-lived HttpOnly cookie
scoped to this kiosk. Later requests from the embedded app, including POST,
websockets and EventSource, present the cookie. The cookie is checked for
every method and every path under the prefix. A signed path is not required
again.

``requires_auth`` stays off on the view class because Home Assistant rejects
the request before the handler when that flag is set, which would drop the
cookie. ``_authorize`` applies the same rule itself: no credentials is 401,
a signed-in user who is not an administrator is 403.

The tablet's admin page sends its own session as ``Authorization: Bearer``.
Home Assistant's auth middleware treats that header as a Home Assistant
access token. On current Home Assistant an unknown bearer does not stop the
request (the middleware only leaves it unauthenticated, and the cookie is
what lets the view run). The header was still removed before the tablet saw
it, so the tablet answered 401 and the login looked like it had failed. The
injected shim moves the tablet token to ``X-Kiosk-Authorization`` so Home
Assistant never has to accept it. The proxy puts that value back as
``Authorization`` on the upstream request. A bearer that is not a valid
Home Assistant token is forwarded as well, which covers a page whose shim
did not run. A bearer Home Assistant did accept is never forwarded: that
one is the administrator's token.

The tablet is contacted with the pinned certificate (or the system trust
store when Verify SSL certificate is on). Responses drop ``X-Frame-Options``
and Content-Security-Policy so the panel can embed the page and the shim
can run. Absolute URLs are rewritten onto the proxy prefix where that is
safe. Home Assistant's own header middleware then sets
``X-Frame-Options: SAMEORIGIN``, which still allows the panel because the
iframe is on the Home Assistant origin.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from datetime import timedelta
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

from homeassistant.components.http.auth import SIGN_QUERY_PARAM, async_sign_path
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.http import KEY_AUTHENTICATED, KEY_HASS, HomeAssistantView

from .client import admin_url
from .const import (
    ATTR_CERT_SHA256,
    CONF_SCHEME,
    CONF_VERIFY_SSL,
    DOMAIN,
    PROXY_COOKIE,
    PROXY_HOOK,
    SCHEME_HTTP,
    SCHEME_HTTPS,
    SIGNED_PATH_SECONDS,
)
from .records import kiosks_from_options
from .tls import keyed_digest, pinned_ssl

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


def proxy_prefix(entry_id: str, kiosk_id: str) -> str:
    """Return the proxy path for one tablet, without a trailing slash."""
    return f"/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_segment(kiosk_id)}"


def signed_proxy_url(
    hass: HomeAssistant,
    entry_id: str,
    kiosk_id: str,
    *,
    refresh_token_id: str | None,
) -> str:
    """Return a temporary URL an iframe can open without a bearer token.

    The signature covers the root path only. The response sets the session
    cookie that authorizes everything under that prefix.
    """
    return async_sign_path(
        hass,
        f"{proxy_prefix(entry_id, kiosk_id)}/",
        timedelta(seconds=SIGNED_PATH_SECONDS),
        refresh_token_id=refresh_token_id,
    )


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


def hook_javascript(prefix: str, origins: tuple[str, str]) -> str:
    """Return the script that rewrites URLs the static pass cannot see.

    The functions live in ``frontend/shim.mjs`` (shipped next to the built
    panel) so the Node tests and this response cannot drift apart. Fetch,
    XHR, websockets, EventSource, forms and location changes run after the
    document is parsed. The constants are JSON so a tablet name cannot break
    out of the script.
    """
    http_origin, https_origin = origins
    ws_origins = [
        "ws" + http_origin.removeprefix("http"),
        "wss" + https_origin.removeprefix("https"),
    ]
    source = _SHIM_PATH.read_text(encoding="utf-8")
    source = source.replace("export function ", "function ")
    source = source.replace("export const ", "const ")
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
        f"{source}\n"
        "  installNetworkShim(window, __KM_CTX__);\n"
        "})();\n"
    )


def _error_page(status: int, title: str, message: str) -> web.Response:
    """Return a small page the panel can tell apart from a blank frame."""
    safe_title = json.dumps(title)[1:-1]
    safe_message = json.dumps(message)[1:-1]
    body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<title>{safe_title}</title></head><body>"
        f"<h1>{safe_title}</h1><p>{safe_message}</p>"
        "<script>parent.postMessage({source:"
        f"{json.dumps(PROXY_SOURCE)},ok:false}}, location.origin);</script>"
        "</body></html>"
    )
    return web.Response(
        status=status,
        text=body,
        content_type="text/html",
        headers={
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
    url = "/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_id}/{path:.+}"
    extra_urls = [
        "/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_id}",
        "/api/kiosk_mgmt/proxy/{entry_id}/{kiosk_id}/",
    ]
    name = "api:kiosk_mgmt:proxy"
    # See the module docstring. Authentication is enforced in ``_authorize``.
    requires_auth = False

    def __init__(self) -> None:
        """Bind every verb to the same handler. The tablet's UI uses more than GET."""
        for method in ("post", "put", "patch", "delete", "head", "options"):
            setattr(self, method, self.get)

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        kiosk_id: str,
        path: str = "",
    ) -> web.StreamResponse:
        """Forward one request, or serve the rewrite hook."""
        user, failure = await _authorize(request, entry_id, kiosk_id)
        if failure is not None or user is None:
            return _error_page(
                failure or 401,
                "Not allowed",
                "Sign in to Home Assistant as an administrator to open this kiosk.",
            )
        record = _record(request, entry_id, kiosk_id)
        if record is None:
            return _error_page(
                404,
                "Kiosk not found",
                "That tablet is not in Kiosk Mgmt.",
            )
        prefix = proxy_prefix(entry_id, kiosk_id)
        if path.rstrip("/") == PROXY_HOOK:
            origins = _origins(record)
            return web.Response(
                text=hook_javascript(prefix, origins),
                content_type="application/javascript",
                headers={
                    "Cache-Control": "no-store",
                    "Set-Cookie": _cookie(request, user.id, entry_id, kiosk_id),
                },
            )
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_websocket(
                request, record, path, user, entry_id, kiosk_id
            )
        return await _proxy_http(request, record, path, user, entry_id, kiosk_id)


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
    request: web.Request, entry_id: str, kiosk_id: str
) -> tuple[Any, int | None]:
    """Return ``(user, None)`` or ``(None, status)``.

    A bearer token or a signed path is checked by Home Assistant's auth
    middleware, which sets ``KEY_AUTHENTICATED``. The cookie covers the
    requests an iframe makes after that first page.
    """
    hass: HomeAssistant = request.app[KEY_HASS]
    if request.get(KEY_AUTHENTICATED):
        user = request.get(KEY_HASS_USER)
        if user is None:
            return None, 401
        if not user.is_admin:
            return None, 403
        return user, None
    user, rejected = await _user_from_cookie(hass, request, entry_id, kiosk_id)
    if rejected:
        return None, 403
    if user is None:
        return None, 401
    return user, None


async def _user_from_cookie(
    hass: HomeAssistant,
    request: web.Request,
    entry_id: str,
    kiosk_id: str,
) -> tuple[Any, bool]:
    """Return ``(user, rejected)``. ``rejected`` means the user is not an admin."""
    raw = request.cookies.get(PROXY_COOKIE)
    if not raw or "." not in raw:
        return None, False
    encoded, signature = raw.split(".", 1)
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = base64.urlsafe_b64decode((encoded + padding).encode())
    except ValueError:
        return None, False
    if not _digest_matches(hass, payload, signature):
        return None, False
    try:
        data = json.loads(payload)
    except ValueError:
        return None, False
    if not isinstance(data, dict):
        return None, False
    if data.get("e") != entry_id or data.get("k") != kiosk_id:
        return None, False
    try:
        expires = int(data.get("exp") or 0)
    except TypeError, ValueError:
        return None, False
    if expires < int(time.time()):
        return None, False
    user = await hass.auth.async_get_user(str(data.get("u") or ""))
    if user is None or not user.is_active:
        return None, False
    if not user.is_admin:
        return None, True
    return user, False


def _digest_matches(hass: HomeAssistant, payload: bytes, signature: str) -> bool:
    """Compare the cookie HMAC without leaking the length through timing."""
    expected = keyed_digest(hass, payload)
    return hmac.compare_digest(expected, signature)


def _cookie(request: web.Request, user_id: str, entry_id: str, kiosk_id: str) -> str:
    """Return a Set-Cookie value bound to this admin, entry, and kiosk."""
    hass: HomeAssistant = request.app[KEY_HASS]
    expires = int(time.time()) + SIGNED_PATH_SECONDS
    payload = json.dumps(
        {"e": entry_id, "k": kiosk_id, "u": user_id, "exp": expires},
        separators=(",", ":"),
    ).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    token = f"{token}.{keyed_digest(hass, payload)}"
    parts = [
        f"{PROXY_COOKIE}={token}",
        f"Path={proxy_prefix(entry_id, kiosk_id)}",
        f"Max-Age={SIGNED_PATH_SECONDS}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if request.secure:
        parts.append("Secure")
    return "; ".join(parts)


def _forward_query(request: web.Request) -> list[tuple[str, str]]:
    """Return the query without Home Assistant's signature parameter."""
    return [
        (key, value) for key, value in request.query.items() if key != SIGN_QUERY_PARAM
    ]


def _kiosk_authorization(request: web.Request) -> str | None:
    """Return the tablet credential to send upstream, if there is one.

    ``X-Kiosk-Authorization`` is the shim's copy and always wins. A raw
    ``Authorization`` is forwarded only when it is not a Home Assistant
    access token. Home Assistant's middleware does not reject an unknown
    bearer; it leaves the request unauthenticated and the proxy cookie is
    what authorized the view. Dropping that header is what made a correct
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

    The Home Assistant bearer token and the proxy cookie stay here. The
    tablet's own token is added back as ``Authorization`` (see
    ``_kiosk_authorization``). Origin and Referer are rewritten so a tablet
    that checks them still sees its own admin origin. ``Accept-Encoding`` is
    left for the HTTP client, which decodes gzip before this proxy rewrites
    the body.
    """
    headers: dict[str, str] = {}
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
    cookie = _upstream_cookie(request)
    if cookie:
        headers["Cookie"] = cookie
    token = _kiosk_authorization(request)
    if token:
        headers[AUTHORIZATION] = token
    return headers


def _upstream_cookie(request: web.Request) -> str:
    """Return the Cookie header without the proxy session cookie."""
    pairs = [
        f"{key}={value}"
        for key, value in request.cookies.items()
        if key != PROXY_COOKIE
    ]
    return "; ".join(pairs)


def _response_headers(
    upstream: Any,
    *,
    prefix: str,
    origins: tuple[str, str],
    cookie: str,
    rewritten: bool,
    decoded: bool,
    secure: bool,
) -> CIMultiDict[str]:
    """Build the headers the browser should see."""
    headers: CIMultiDict[str] = CIMultiDict()
    for key, value in upstream.headers.items():
        lower = key.lower()
        if lower in _DROP_RESPONSE:
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
    headers.add("Set-Cookie", cookie)
    return headers


async def _proxy_http(
    request: web.Request,
    record: Mapping[str, Any],
    path: str,
    user: Any,
    entry_id: str,
    kiosk_id: str,
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
    prefix = proxy_prefix(entry_id, kiosk_id)
    cookie = _cookie(request, user.id, entry_id, kiosk_id)
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
            request.path,
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
            request.path,
            _redact(upstream),
            upstream_response.status,
        )
        return await _send_upstream(
            request,
            upstream_response,
            prefix=prefix,
            origins=origins,
            cookie=cookie,
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
    cookie: str,
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
            cookie=cookie,
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
        cookie=cookie,
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
    user: Any,
    entry_id: str,
    kiosk_id: str,
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
            request.path,
            _redact(upstream_url),
            err,
        )
        await client.close(code=1011, message=b"upstream unavailable")
        return client
    _LOGGER.debug(
        "Proxied websocket %s -> %s",
        request.path,
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
