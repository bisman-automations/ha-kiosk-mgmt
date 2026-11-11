"""A local HTTPS stand-in for a Kiosk Satellite admin port.

The certificate is generated in the test and thrown away with the server.
Nothing here is a certificate a person would install.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import ipaddress
import json
import ssl
from tempfile import TemporaryDirectory
from typing import Any

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.common import HEALTH

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# The password the mock admin accepts. Tests and the browser check use it.
ADMIN_PASSWORD = "secret"
ADMIN_TOKEN = "tablet-token"

# A small original mark: a house outline and four bars, the same arrangement
# as the Kiosk Satellite splash, drawn for the mock rather than copied.
_LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="12" fill="#f4f7f8"/>
<path d="M8 30 L32 10 L56 30 V54 H8 Z" fill="#fff" stroke="#5da3a6" stroke-width="3"/>
<rect x="16" y="34" width="6" height="16" fill="#18bcf2"/>
<rect x="26" y="28" width="6" height="22" fill="#f2b705"/>
<rect x="36" y="22" width="6" height="28" fill="#3fbf5f"/>
<rect x="46" y="30" width="6" height="20" fill="#e8604c"/>
</svg>
"""

# Mirrors the remote UI served by Kiosk Satellite (app/remote-ui/index.html):
# a splash, a login card, and an app shell. Paths are the ones that app uses:
# relative ``static/...`` for the stylesheet and module, root-absolute
# ``/static/...`` and ``/api/...`` for everything the shim has to catch.
_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kiosk Satellite Remote</title>
<link rel="stylesheet" href="static/app.css">
<link rel="stylesheet" href="/static/app.css">
</head><body>
<section id="splash">
  <p>Starting remote administration</p>
</section>
<section id="login" class="hidden">
  <form id="loginForm" class="login-card" action="/api/login" method="post">
    <img class="logo" src="/static/logo.svg" alt="" width="64" height="64">
    <h1>Kiosk Satellite</h1>
    <p id="loginHeading">Remote administration</p>
    <input id="password" type="password" placeholder="Admin password" autocomplete="current-password">
    <button id="loginBtn" class="btn-primary" type="button">Log in</button>
    <div id="loginError" class="msg-error"></div>
  </form>
</section>
<section id="app" class="hidden">
  <aside class="rail">
    <div class="brand">Kiosk Satellite</div>
    <button type="button" class="nav on">Overview</button>
    <a href="/settings">Settings</a>
    <button type="button" class="nav">Logs</button>
  </aside>
  <main>
    <h1 id="admin">Kiosk admin ready</h1>
    <p id="deviceName"></p>
    <div class="tiles">
      <div class="tile"><span>Screen</span><strong>On</strong></div>
      <div class="tile"><span>Brightness</span><strong>70%</strong></div>
      <div class="tile"><span>Volume</span><strong>40%</strong></div>
    </div>
    <p id="live"></p>
  </main>
</section>
<script type="module" src="static/main.js"></script>
</body></html>
"""

_ADMIN_CSS = """
html, body { margin: 0; height: 100%; }
body { background: #eef1f4; color: #1c1c1c; font: 15px/1.45 system-ui, sans-serif; }
.hidden { display: none !important; }
#splash, #login { min-height: 100%; display: grid; place-items: center; }
.login-card {
  width: min(360px, calc(100% - 32px));
  background: #fff;
  border-radius: 16px;
  padding: 32px 28px;
  text-align: center;
  box-shadow: 0 8px 30px rgba(0, 0, 0, .08);
}
.login-card h1 { font-size: 20px; margin: 12px 0 4px; }
.login-card p { color: #607d8b; margin: 0 0 20px; }
.logo { width: 64px; height: 64px; background: url(/static/logo.svg) center/contain no-repeat; }
.login-card input, .btn-primary {
  width: 100%; box-sizing: border-box; border-radius: 10px; padding: 12px; font: inherit;
}
.login-card input { border: 1px solid #d0d7de; margin-bottom: 12px; }
.btn-primary { background: #03a9f4; color: #fff; border: 0; cursor: pointer; }
.msg-error { color: #c62828; min-height: 1.2em; margin-top: 8px; }
#app { min-height: 100%; display: flex; background: #f7f8fa; }
.rail { width: 220px; background: #fff; border-right: 1px solid #e4e7eb; padding: 20px 12px; }
.brand { font-weight: 700; margin-bottom: 16px; }
.nav, .rail a {
  display: block; width: 100%; text-align: left; background: transparent;
  border: 0; border-radius: 8px; padding: 10px 12px; color: inherit; text-decoration: none;
  font: inherit; cursor: pointer;
}
.nav.on { background: #e3f2fd; }
main { flex: 1; padding: 24px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
.tile { background: #fff; border-radius: 12px; padding: 16px; }
.tile span { display: block; color: #607d8b; font-size: 12px; }
@media (max-width: 600px) {
  #app { flex-direction: column; }
  .rail { width: auto; display: flex; gap: 8px; overflow-x: auto; }
}
"""

_ADMIN_JS = """
const BASE = new URL('.', location.href).pathname;
function apiUrl(path) {
  return path.startsWith('/api/') ? BASE + path.slice(1) : path;
}
function show(which) {
  for (const id of ['splash', 'login', 'app']) {
    document.getElementById(id).classList.toggle('hidden', id !== which);
  }
}
async function login() {
  const password = document.getElementById('password').value;
  const error = document.getElementById('loginError');
  error.textContent = '';
  const res = await fetch(apiUrl('/api/login'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) {
    error.textContent = 'Invalid password';
    return;
  }
  const body = await res.json();
  sessionStorage.setItem('ks_token', body.token);
  const info = await fetch(apiUrl('/api/info'), {
    headers: { Authorization: 'Bearer ' + body.token },
  });
  if (!info.ok) {
    error.textContent = 'Session refused';
    show('login');
    return;
  }
  const device = await info.json();
  document.getElementById('deviceName').textContent = device.name;
  show('app');
  const wsUrl = new URL(apiUrl('/api/ws?token=' + encodeURIComponent(body.token)), location.href);
  wsUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(wsUrl);
  ws.onmessage = () => {
    document.getElementById('live').textContent = 'Live';
  };
}
document.getElementById('loginBtn').addEventListener('click', login);
document.getElementById('password').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') login();
});
fetch('api/setup/status').then((res) => res.json()).then(() => show('login')).catch(() => show('login'));
"""


@dataclass
class FakeKiosk:
    """One running tablet stand-in."""

    host: str
    port: int
    fingerprint: str
    scheme: str = "https"
    seen: dict[str, Any] | None = None

    @property
    def origin(self) -> str:
        """Return ``https://127.0.0.1:port``."""
        return f"{self.scheme}://{self.host}:{self.port}"


def make_certificate(host: str = "127.0.0.1") -> tuple[bytes, bytes, str]:
    """Return PEM key, PEM certificate, and the leaf SHA-256 hex digest."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem, fingerprint


def _server_context(
    key_pem: bytes, cert_pem: bytes
) -> tuple[ssl.SSLContext, TemporaryDirectory[str]]:
    """Load a certificate into a server context. The directory keeps the files."""
    directory = TemporaryDirectory()
    key_path = f"{directory.name}/key.pem"
    cert_path = f"{directory.name}/cert.pem"
    with open(key_path, "wb") as handle:
        handle.write(key_pem)
    with open(cert_path, "wb") as handle:
        handle.write(cert_pem)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, directory


def build_app(
    *,
    health: dict[str, Any] | None = None,
    extra: dict[str, Handler] | None = None,
) -> web.Application:
    """Return an admin app shaped like Kiosk Satellite's remote UI.

    ``/`` is the login shell. ``POST /api/login`` checks the password and
    returns a token plus a ``Set-Cookie`` that still has the tablet's
    ``Domain``, ``Path=/``, ``Secure`` and ``SameSite=None``. ``GET /api/info``
    and the websocket require that token. ``/static/app.css`` is always
    gzipped, which is what the real server does when the client allows it.
    """
    app = web.Application()
    body = health if health is not None else HEALTH
    seen: dict[str, Any] = {}

    def _note(request: web.Request, prefix: str, payload: bytes | None = None) -> None:
        seen[f"{prefix}_auth"] = request.headers.get("Authorization", "")
        seen[f"{prefix}_origin"] = request.headers.get("Origin", "")
        seen[f"{prefix}_host"] = request.host
        seen[f"{prefix}_type"] = request.headers.get("Content-Type", "")
        seen[f"{prefix}_length"] = request.headers.get("Content-Length", "")
        seen[f"{prefix}_transfer"] = request.headers.get("Transfer-Encoding", "")
        seen[f"{prefix}_cookie"] = request.headers.get("Cookie", "")
        if payload is not None:
            seen[f"{prefix}_body"] = payload

    async def health_handler(request: web.Request) -> web.Response:
        seen["authorization"] = request.headers.get("Authorization", "")
        seen["query"] = request.query_string
        return web.json_response(body)

    async def root(_request: web.Request) -> web.Response:
        return web.Response(
            text=_ADMIN_HTML,
            content_type="text/html",
            headers={
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'nonce-tablet'; "
                    "frame-ancestors 'none'"
                ),
            },
        )

    async def stylesheet(_request: web.Request) -> web.Response:
        # Always gzipped, even when the client asked for identity, so the
        # proxy has to decode before it rewrites url().
        return web.Response(
            body=gzip.compress(_ADMIN_CSS.encode()),
            headers={
                "Content-Type": "text/css; charset=utf-8",
                "Content-Encoding": "gzip",
            },
        )

    async def script(_request: web.Request) -> web.Response:
        return web.Response(text=_ADMIN_JS, content_type="application/javascript")

    async def logo(_request: web.Request) -> web.Response:
        return web.Response(text=_LOGO_SVG, content_type="image/svg+xml")

    async def setup(_request: web.Request) -> web.Response:
        return web.json_response({"setupNeeded": False, "language": "en"})

    async def login(request: web.Request) -> web.Response:
        payload = await request.read()
        _note(request, "login", payload)
        try:
            data = json.loads(payload.decode() or "{}")
        except ValueError:
            data = {}
        if data.get("password") != ADMIN_PASSWORD:
            return web.json_response({"error": "unauthorized"}, status=401)
        response = web.json_response({"token": ADMIN_TOKEN})
        response.set_cookie(
            "ks_session",
            ADMIN_TOKEN,
            path="/",
            domain=request.host,
            secure=True,
            samesite="None",
            httponly=True,
        )
        return response

    async def info(request: web.Request) -> web.Response:
        _note(request, "info")
        token = request.headers.get("Authorization", "")
        cookie = request.cookies.get("ks_session", "")
        if token == f"Bearer {ADMIN_TOKEN}" or cookie == ADMIN_TOKEN:
            return web.json_response(
                {"name": "Kitchen tablet", "model": "Test panel", "currentUrl": ""}
            )
        return web.json_response({"error": "unauthorized"}, status=401)

    async def events(_request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
            }
        )
        await response.prepare(_request)
        await response.write(b"data: hello\n\n")
        await response.write_eof()
        return response

    async def redirect(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(location="/settings")

    async def echo(request: web.Request) -> web.Response:
        payload = await request.read()
        _note(request, "echo", payload)
        return web.Response(
            body=payload,
            headers={
                "X-Saw-Authorization": request.headers.get("Authorization", ""),
                "X-Saw-Query": request.query_string,
                "X-Saw-Cookie": request.headers.get("Cookie", ""),
                "X-Saw-Length": request.headers.get("Content-Length", ""),
                "X-Saw-Transfer": request.headers.get("Transfer-Encoding", ""),
            },
        )

    async def any_method(request: web.Request) -> web.Response:
        payload = await request.read()
        return web.Response(text=f"{request.method}:{payload.decode()}")

    async def chunked(_request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(_request)
        for index in range(4):
            await response.write(f"chunk-{index}-".encode())
        await response.write_eof()
        return response

    async def socket(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        async for message in websocket:
            if message.type == web.WSMsgType.TEXT:
                await websocket.send_str(f"echo:{message.data}")
            elif message.type == web.WSMsgType.BINARY:
                await websocket.send_bytes(message.data)
        return websocket

    async def api_socket(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        if request.query.get("token") != ADMIN_TOKEN:
            await websocket.close(code=1008, message=b"unauthorized")
            return websocket
        await websocket.send_str('{"type":"state","device":{"name":"Kitchen tablet"}}')
        async for message in websocket:
            if message.type == web.WSMsgType.TEXT:
                await websocket.send_str(f"echo:{message.data}")
        return websocket

    app.router.add_get("/api/health", health_handler)
    app.router.add_get("/", root)
    app.router.add_get("/static/app.css", stylesheet)
    app.router.add_get("/static/main.js", script)
    app.router.add_get("/static/logo.svg", logo)
    app.router.add_get("/api/setup/status", setup)
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/info", info)
    app.router.add_get("/api/events", events)
    app.router.add_get("/go", redirect)
    app.router.add_post("/echo", echo)
    app.router.add_route("*", "/api/any", any_method)
    app.router.add_get("/chunks", chunked)
    app.router.add_get("/ws", socket)
    app.router.add_get("/api/ws", api_socket)
    for path, handler in (extra or {}).items():
        app.router.add_route("*", path, handler)
    app["seen"] = seen
    return app


@asynccontextmanager
async def serve(fingerprint_cert: tuple[bytes, bytes, str] | None = None):
    """Start the stand-in on 127.0.0.1 and yield a FakeKiosk."""
    key_pem, cert_pem, fingerprint = fingerprint_cert or make_certificate()
    context, directory = _server_context(key_pem, cert_pem)
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    sockets = site._server.sockets if site._server is not None else None
    if not sockets:
        raise RuntimeError("fake kiosk did not bind")
    port = sockets[0].getsockname()[1]
    # A client that rejects the certificate resets the handshake. That
    # exception lands on Home Assistant's loop and fails the test. Ignore
    # only that reset; everything else still uses the previous handler.
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def _quiet_reset(
        running: asyncio.AbstractEventLoop, context: dict[str, object]
    ) -> None:
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return
        if previous is not None:
            previous(running, context)
            return
        running.default_exception_handler(context)

    loop.set_exception_handler(_quiet_reset)
    try:
        yield FakeKiosk(
            host="127.0.0.1",
            port=port,
            fingerprint=fingerprint,
            seen=runner.app["seen"],
        )
    finally:
        loop.set_exception_handler(previous)
        await runner.cleanup()
        directory.cleanup()
