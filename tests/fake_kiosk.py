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
import hashlib
import ipaddress
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


@dataclass
class FakeKiosk:
    """One running tablet stand-in."""

    host: str
    port: int
    fingerprint: str
    scheme: str = "https"

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
    """Return an admin app: health, an HTML page, an echo, and a websocket."""
    app = web.Application()
    body = health if health is not None else HEALTH
    seen: dict[str, Any] = {}

    async def health_handler(request: web.Request) -> web.Response:
        seen["authorization"] = request.headers.get("Authorization", "")
        seen["query"] = request.query_string
        return web.json_response(body)

    async def root(request: web.Request) -> web.Response:
        host = request.host
        page = (
            "<!DOCTYPE html><html><head><title>Kiosk admin</title></head>"
            '<body><h1 id="admin">Kiosk admin ready</h1>'
            '<a href="/settings">Settings</a>'
            f'<img src="https://{host}/logo.png">'
            '<script>fetch("/api/health")</script>'
            "</body></html>"
        )
        return web.Response(
            text=page,
            content_type="text/html",
            headers={
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
            },
        )

    async def redirect(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(location="/settings")

    async def echo(request: web.Request) -> web.Response:
        payload = await request.read()
        return web.Response(
            body=payload,
            headers={
                "X-Saw-Authorization": request.headers.get("Authorization", ""),
                "X-Saw-Query": request.query_string,
                "X-Saw-Cookie": request.headers.get("Cookie", ""),
            },
        )

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

    app.router.add_get("/api/health", health_handler)
    app.router.add_get("/", root)
    app.router.add_get("/go", redirect)
    app.router.add_post("/echo", echo)
    app.router.add_get("/chunks", chunked)
    app.router.add_get("/ws", socket)
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
        yield FakeKiosk(host="127.0.0.1", port=port, fingerprint=fingerprint)
    finally:
        loop.set_exception_handler(previous)
        await runner.cleanup()
        directory.cleanup()
