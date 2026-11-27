"""The admin proxy: sessions, the sandbox, rewriting and streaming."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_mgmt.const import DOMAIN, PROXY_SANDBOX
from custom_components.kiosk_mgmt.proxy import (
    _read_shim_source,
    async_proxy_url,
    hook_javascript,
    proxy_prefix,
    rewrite_location,
    rewrite_set_cookie,
    rewrite_text,
    session_token,
    strip_frame_ancestors,
)
from custom_components.kiosk_mgmt.tls import async_load_proxy_key
from homeassistant.components.http.auth import DATA_SIGN_SECRET
from tests.fake_kiosk import ADMIN_PASSWORD, ADMIN_TOKEN, serve
from tests.test_pinning import _record


def test_location_csp_and_cookie_rewrites() -> None:
    """Redirects, framing policy, and cookie paths stay on the proxy."""
    origins = ("http://192.0.2.10:2324", "https://192.0.2.10:2324")
    prefix = "/api/kiosk_mgmt/proxy/entry/manual:1/session"
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
        "ks_session=abc; Path=/; Domain=192.0.2.10; Secure; SameSite=None; HttpOnly",
        prefix=prefix,
        secure=False,
    )
    assert "Domain=" not in cookie
    assert "Secure" not in cookie
    assert "SameSite=Lax" in cookie
    assert "SameSite=None" not in cookie
    assert f"Path={prefix}" in cookie
    assert "HttpOnly" in cookie
    secure_cookie = rewrite_set_cookie(
        "ks_session=abc; Path=/api; Domain=192.0.2.10; Secure; SameSite=Strict",
        prefix=prefix,
        secure=True,
    )
    assert f"Path={prefix}/api" in secure_cookie
    assert "Secure" in secure_cookie
    assert "SameSite=Lax" in secure_cookie
    html = rewrite_text(
        '<html><head></head><body><a href="/settings">x</a></body></html>',
        "text/html",
        prefix=prefix,
        origins=origins,
    )
    assert f'href="{prefix}/settings"' in html
    assert f'<base href="{prefix}/">' in html
    assert "hook.js" in html
    css = rewrite_text(
        '@import "/static/extra.css"; .logo{background:url(/static/logo.svg)}',
        "text/css",
        prefix=prefix,
        origins=origins,
    )
    assert f'@import "{prefix}/static/extra.css"' in css
    assert f"url({prefix}/static/logo.svg)" in css
    script = rewrite_text(
        'const reads = {"/api/info": "info"}; const file = "/static/app.js";',
        "application/javascript",
        prefix=prefix,
        origins=origins,
    )
    assert '"/api/info"' in script
    assert f'"{prefix}/static/app.js"' in script
    page = rewrite_text(
        '<img srcset="/static/logo.svg 1x, /static/logo.svg 2x">',
        "text/html",
        prefix=prefix,
        origins=origins,
    )
    assert f'srcset="{prefix}/static/logo.svg 1x, {prefix}/static/logo.svg 2x"' in page


def test_hook_carries_the_shim_and_the_prefix() -> None:
    """The served script is the shim, with this tablet's prefix filled in."""
    prefix = "/api/kiosk_mgmt/proxy/entry/manual:1/session"
    script = hook_javascript(
        prefix,
        ("http://192.0.2.10:2324", "https://192.0.2.10:2324"),
        _read_shim_source(),
    )
    assert "installNetworkShim" in script
    # The module's exports are inlined as plain declarations inside the IIFE.
    assert "export " not in script
    assert "X-Kiosk-Authorization" in script
    assert prefix in script
    root = Path(__file__).parents[1]
    assert (root / "frontend" / "shim.mjs").read_bytes() == (
        root / "custom_components" / "kiosk_mgmt" / "frontend" / "shim.mjs"
    ).read_bytes()


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


async def _admin_user(hass: Any) -> Any:
    """Return an administrator."""
    for user in await hass.auth.async_get_users():
        if user.is_admin and not user.system_generated:
            return user
    return await hass.auth.async_create_user("Admin", group_ids=["system-admin"])


async def admin_prefix(hass: Any, entry_id: str, *, seconds: int | None = None) -> str:
    """Return a proxy prefix with a session for an administrator."""
    user = await _admin_user(hass)
    await async_load_proxy_key(hass)
    extra = {} if seconds is None else {"seconds": seconds}
    session = session_token(hass, user.id, entry_id, "manual:1", **extra)
    return proxy_prefix(entry_id, "manual:1", session)


def _sandboxed(response: Any) -> None:
    """The page can never run on Home Assistant's origin."""
    policy = response.headers.get("Content-Security-Policy", "")
    assert policy == PROXY_SANDBOX
    assert "allow-same-origin" not in policy
    assert response.headers.get("Access-Control-Allow-Origin") == "*"
    assert "Access-Control-Allow-Credentials" not in response.headers
    assert response.headers.get("Referrer-Policy") == "no-referrer"


async def test_proxy_needs_a_session_and_streams(
    hass: Any, hass_client: Any, hass_client_no_auth: Any
) -> None:
    """No session is 401, even with Home Assistant's own token; a session works."""
    async with serve() as kiosk:
        entry = await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        anonymous = await hass_client_no_auth()
        refused = await anonymous.get(
            f"/api/kiosk_mgmt/proxy/{entry.entry_id}/manual:1/nope/"
        )
        assert refused.status == 401
        _sandboxed(refused)
        admin = await hass_client()
        bearer_only = await admin.get(
            f"/api/kiosk_mgmt/proxy/{entry.entry_id}/manual:1/nope/"
        )
        assert bearer_only.status == 401

        prefix = await admin_prefix(hass, entry.entry_id)
        page = await anonymous.get(f"{prefix}/")
        assert page.status == 200, await page.text()
        _sandboxed(page)
        assert "Set-Cookie" not in page.headers
        text = await page.text()
        assert "Kiosk admin ready" in text
        assert f'<base href="{prefix}/">' in text
        assert f"{prefix}/__kiosk_mgmt__/hook.js" in text
        hook = await anonymous.get(f"{prefix}/__kiosk_mgmt__/hook.js")
        assert hook.status == 200
        _sandboxed(hook)
        assert "X-Kiosk-Authorization" in await hook.text()

        chunks = await anonymous.get(f"{prefix}/chunks")
        assert chunks.status == 200
        _sandboxed(chunks)
        assert await chunks.read() == b"chunk-0-chunk-1-chunk-2-chunk-3-"

        posted = await anonymous.post(
            f"{prefix}/echo?x=1", data=b"hello", headers={"Cookie": "ha=secret"}
        )
        assert await posted.read() == b"hello"
        assert posted.headers["X-Saw-Authorization"] == ""
        assert "x=1" in posted.headers["X-Saw-Query"]
        # Cookies for Home Assistant never reach the tablet.
        assert "secret" not in posted.headers["X-Saw-Cookie"]

        redirect = await anonymous.get(f"{prefix}/go", allow_redirects=False)
        assert redirect.status == 302
        assert redirect.headers["Location"] == f"{prefix}/settings"

        async with anonymous.ws_connect(f"{prefix}/ws") as websocket:
            await websocket.send_str("hi")
            assert await websocket.receive_str() == "echo:hi"


async def test_sessions_are_bound_and_expire(
    hass: Any, hass_client_no_auth: Any
) -> None:
    """A session is for one tablet and one admin, and stops when it expires
    or when that user is no longer an administrator."""
    async with serve() as kiosk:
        entry = await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        client = await hass_client_no_auth()

        expired = await admin_prefix(hass, entry.entry_id, seconds=-30)
        assert (await client.get(f"{expired}/")).status == 401

        prefix = await admin_prefix(hass, entry.entry_id)
        session = prefix.rsplit("/", 1)[1]
        other_tablet = f"/api/kiosk_mgmt/proxy/{entry.entry_id}/manual:2/{session}/"
        assert (await client.get(other_tablet)).status == 401
        encoded, signature = session.split(".", 1)
        forged = prefix.replace(session, f"{encoded}.{'0' * len(signature)}")
        assert (await client.get(f"{forged}/")).status == 401

        guest = await hass.auth.async_create_user("Guest")
        await async_load_proxy_key(hass)
        guest_session = session_token(hass, guest.id, entry.entry_id, "manual:1")
        guest_prefix = proxy_prefix(entry.entry_id, "manual:1", guest_session)
        assert (await client.get(f"{guest_prefix}/")).status == 403

        assert (await client.get(f"{prefix}/")).status == 200


async def test_the_key_is_the_proxys_own(
    hass: Any, hass_storage: dict[str, Any]
) -> None:
    """Sessions are signed with a random key in Kiosk Mgmt's own storage;
    Home Assistant's signed-path secret is neither read nor created."""
    hass.data.pop(DATA_SIGN_SECRET, None)
    key = await async_load_proxy_key(hass)
    assert len(key) == 64
    assert DATA_SIGN_SECRET not in hass.data
    assert hass_storage["kiosk_mgmt.proxy_key"]["data"]["key"] == key.decode()

    # A restart reads the same key back, so open sessions keep working.
    hass.data[DOMAIN].pop("proxy_key")
    assert await async_load_proxy_key(hass) == key


async def test_preflight_is_answered_without_the_tablet(
    hass: Any, hass_client_no_auth: Any
) -> None:
    """The sandboxed page's JSON calls are cross-origin; the proxy allows them."""
    async with serve() as kiosk:
        entry = await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        prefix = await admin_prefix(hass, entry.entry_id)
        client = await hass_client_no_auth()
        answer = await client.options(
            f"{prefix}/api/login",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-kiosk-authorization",
            },
        )
        assert answer.status == 204
        _sandboxed(answer)
        assert answer.headers["Access-Control-Allow-Methods"] == "POST"
        assert (
            answer.headers["Access-Control-Allow-Headers"]
            == "content-type,x-kiosk-authorization"
        )
        assert kiosk.seen is None or "login_body" not in kiosk.seen

        unsigned = await client.options(
            f"/api/kiosk_mgmt/proxy/{entry.entry_id}/manual:1/nope/api/login",
            headers={"Access-Control-Request-Method": "POST"},
        )
        assert unsigned.status == 401


async def test_websocket_command_returns_a_session_url(
    hass: Any, hass_ws_client: Any, hass_client_no_auth: Any
) -> None:
    """The panel's URL opens the tablet, and Open in new tab goes direct."""
    async with serve() as kiosk:
        entry = await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        ws = await hass_ws_client(hass)
        await ws.send_json(
            {"id": 1, "type": "kiosk_mgmt/sign_proxy", "kiosk_id": "manual:1"}
        )
        reply = await ws.receive_json()
        assert reply["success"], reply
        url = reply["result"]["url"]
        assert url.startswith(f"/api/kiosk_mgmt/proxy/{entry.entry_id}/manual:1/")
        assert "authSig" not in url
        assert reply["result"]["direct_url"].startswith(kiosk.origin)
        client = await hass_client_no_auth()
        opened = await client.get(url)
        assert opened.status == 200
        assert "Kiosk admin ready" in await opened.text()
        assert await async_proxy_url(hass, entry.entry_id, "manual:1", "x") != url


async def test_login_round_trip_reaches_the_tablet(
    hass: Any, hass_client: Any, hass_client_no_auth: Any, caplog: Any
) -> None:
    """A correct password logs in. The 401 for a bad one comes from the tablet.

    The follow-up ``GET /api/info`` carries the tablet token. Home Assistant's
    own bearer, which the test client sends on ``admin``, must not replace it.
    """
    caplog.set_level("DEBUG", logger="custom_components.kiosk_mgmt.proxy")
    async with serve() as kiosk:
        entry = await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        prefix = await admin_prefix(hass, entry.entry_id)
        admin = await hass_client()
        anonymous = await hass_client_no_auth()

        bad_body = json.dumps({"password": "nope"}).encode()
        bad = await anonymous.post(
            f"{prefix}/api/login",
            data=bad_body,
            headers={"Content-Type": "application/json", "Origin": "null"},
        )
        assert bad.status == 401
        assert bad.headers.get("X-Kiosk-Mgmt-Error") is None
        assert "unauthorized" in await bad.text()

        good_body = json.dumps({"password": ADMIN_PASSWORD}).encode()
        good = await anonymous.post(
            f"{prefix}/api/login",
            data=good_body,
            headers={"Content-Type": "application/json", "Origin": "null"},
        )
        assert good.status == 200, await good.text()
        assert (await good.json())["token"] == ADMIN_TOKEN
        session = next(
            item
            for item in good.headers.getall("Set-Cookie")
            if item.startswith("ks_session=")
        )
        assert "Domain=" not in session
        assert f"Path={prefix}" in session
        assert kiosk.seen is not None
        assert kiosk.seen["login_body"] == good_body
        assert kiosk.seen["login_length"] == str(len(good_body))
        assert kiosk.seen["login_transfer"] == ""
        assert kiosk.seen["login_type"].startswith("application/json")
        assert kiosk.seen["login_auth"] == ""
        assert kiosk.seen["login_origin"] == kiosk.origin
        assert kiosk.host in kiosk.seen["login_host"]
        assert ADMIN_PASSWORD not in caplog.text

        direct = await anonymous.get(
            f"{prefix}/api/info",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert direct.status == 200
        assert (await direct.json())["name"] == "Kitchen tablet"
        assert kiosk.seen["info_auth"] == f"Bearer {ADMIN_TOKEN}"

        moved = await admin.get(
            f"{prefix}/api/info",
            headers={"X-Kiosk-Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert moved.status == 200
        assert kiosk.seen["info_auth"] == f"Bearer {ADMIN_TOKEN}"

        # Without the tablet's token the tablet refuses it, not Home Assistant.
        refused = await anonymous.get(f"{prefix}/api/info")
        assert refused.status == 401
        assert refused.headers.get("X-Kiosk-Mgmt-Error") is None
        assert "Proxied" in caplog.text

        patched = await anonymous.patch(f"{prefix}/api/any", data=b'{"a":1}')
        assert await patched.text() == 'PATCH:{"a":1}'

        events = await anonymous.get(f"{prefix}/api/events")
        assert events.status == 200
        assert b"hello" in await events.read()

        css = await anonymous.get(f"{prefix}/static/app.css")
        assert css.status == 200
        stylesheet = await css.text()
        assert f"{prefix}/static/logo.svg" in stylesheet
        assert "gzip" not in css.headers.get("Content-Encoding", "")

        async with anonymous.ws_connect(
            f"{prefix}/api/ws?token={ADMIN_TOKEN}"
        ) as websocket:
            assert "Kitchen tablet" in await websocket.receive_str()
        assert ADMIN_TOKEN not in caplog.text
        # The session in the prefix opens the tablet by itself, so the log
        # shows only the path after it (hacs/default#14090).
        session_part = prefix.rsplit("/", 1)[1]
        ours = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name.startswith("custom_components.kiosk_mgmt")
        )
        assert session_part not in ours
        assert "Proxied GET /api/info" in ours
        assert "Proxied websocket /api/ws" in ours
