"""The admin proxy: auth, rewriting, streaming, and signed-path expiry."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_mgmt.const import DOMAIN, PROXY_COOKIE
from custom_components.kiosk_mgmt.proxy import (
    proxy_prefix,
    rewrite_location,
    rewrite_set_cookie,
    rewrite_text,
    signed_proxy_url,
    strip_frame_ancestors,
)
from homeassistant.components.http.auth import async_sign_path
from tests.fake_kiosk import serve
from tests.test_pinning import _record


def test_location_csp_and_cookie_rewrites() -> None:
    """Redirects, framing policy, and cookie paths stay on the proxy."""
    origins = ("http://192.0.2.10:2324", "https://192.0.2.10:2324")
    prefix = "/api/kiosk_mgmt/proxy/entry/manual:1"
    assert (
        rewrite_location(
            "https://192.0.2.10:2324/settings", prefix=prefix, origins=origins
        )
        == f"{prefix}/settings"
    )
    assert rewrite_location("/settings", prefix=prefix, origins=origins) == (
        f"{prefix}/settings"
    )
    assert "frame-ancestors" not in strip_frame_ancestors(
        "default-src 'self'; frame-ancestors 'none'"
    )
    assert "default-src 'self'" in strip_frame_ancestors(
        "default-src 'self'; frame-ancestors 'none'"
    )
    cookie = rewrite_set_cookie(
        "session=abc; Path=/; Domain=192.0.2.10; HttpOnly", prefix=prefix
    )
    assert "Domain=" not in cookie
    assert f"Path={prefix}" in cookie
    html = rewrite_text(
        '<html><head></head><body><a href="/settings">x</a></body></html>',
        "text/html",
        prefix=prefix,
        origins=origins,
    )
    assert f'href="{prefix}/settings"' in html
    assert f'<base href="{prefix}/">' in html
    assert "hook.js" in html


async def _entry(hass: Any, host: str, port: int, fingerprint: str) -> MockConfigEntry:
    """Set up one pinned kiosk aimed at the stand-in."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        data={},
        options={"kiosks": {"manual:1": _record(host, port, cert_sha256=fingerprint)}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _admin_refresh(hass: Any) -> str:
    """Return the refresh-token id of an administrator."""
    for user in await hass.auth.async_get_users():
        if user.is_admin and user.refresh_tokens:
            return next(iter(user.refresh_tokens))
    raise AssertionError("no admin refresh token")


async def test_proxy_requires_an_admin_and_streams(
    hass: Any, hass_client: Any, hass_client_no_auth: Any
) -> None:
    """Anonymous is 401, a non-admin is 403, an admin receives a streamed body."""
    async with serve() as kiosk:
        entry = await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        prefix = proxy_prefix(entry.entry_id, "manual:1")
        anonymous = await hass_client_no_auth()
        refused = await anonymous.get(f"{prefix}/")
        assert refused.status == 401

        guest = await hass.auth.async_create_user("Guest")
        assert guest.is_admin is False
        guest_refresh = await hass.auth.async_create_refresh_token(
            guest, client_id="https://example.com"
        )
        guest_token = hass.auth.async_create_access_token(guest_refresh)
        guest_client = await hass_client(guest_token)
        forbidden = await guest_client.get(f"{prefix}/")
        assert forbidden.status == 403

        admin = await hass_client()
        page = await admin.get(f"{prefix}/")
        assert page.status == 200, await page.text()
        text = await page.text()
        assert "Kiosk admin ready" in text
        assert f'<base href="{prefix}/">' in text
        assert f"{prefix}/__kiosk_mgmt__/hook.js" in text
        assert "frame-ancestors" not in page.headers.get("Content-Security-Policy", "")
        assert page.headers.get("X-Frame-Options") != "DENY"
        assert PROXY_COOKIE in page.cookies

        chunks = await admin.get(f"{prefix}/chunks")
        assert chunks.status == 200
        assert await chunks.read() == b"chunk-0-chunk-1-chunk-2-chunk-3-"

        posted = await admin.post(f"{prefix}/echo?x=1", data=b"hello")
        assert await posted.read() == b"hello"
        assert posted.headers["X-Saw-Authorization"] == ""
        assert "authSig" not in posted.headers["X-Saw-Query"]
        assert "x=1" in posted.headers["X-Saw-Query"]
        assert PROXY_COOKIE not in posted.headers["X-Saw-Cookie"]

        redirect = await admin.get(f"{prefix}/go", allow_redirects=False)
        assert redirect.status == 302
        assert redirect.headers["Location"] == f"{prefix}/settings"

        cookie = page.cookies[PROXY_COOKIE].value
        with_cookie = await anonymous.get(
            f"{prefix}/chunks",
            headers={"Cookie": f"{PROXY_COOKIE}={cookie}"},
        )
        assert with_cookie.status == 200
        assert await with_cookie.read() == b"chunk-0-chunk-1-chunk-2-chunk-3-"

        async with admin.ws_connect(f"{prefix}/ws") as websocket:
            await websocket.send_str("hi")
            assert await websocket.receive_str() == "echo:hi"


async def test_signed_path_expires_and_a_fresh_one_sets_the_cookie(
    hass: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    """A fresh signature opens the proxy. An expired one does not."""
    del hass_access_token
    async with serve() as kiosk:
        entry = await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        prefix = proxy_prefix(entry.entry_id, "manual:1")
        refresh = await _admin_refresh(hass)
        client = await hass_client_no_auth()

        fresh = signed_proxy_url(
            hass, entry.entry_id, "manual:1", refresh_token_id=refresh
        )
        opened = await client.get(fresh)
        assert opened.status == 200
        assert "Kiosk admin ready" in await opened.text()
        assert PROXY_COOKIE in opened.cookies

        expired = async_sign_path(
            hass,
            f"{prefix}/",
            timedelta(seconds=-30),
            refresh_token_id=refresh,
        )
        # A new client, so the cookie from the fresh signature is not sent.
        expired_client = await hass_client_no_auth()
        rejected = await expired_client.get(expired)
        assert rejected.status == 401
