"""Load the sidebar panel in headless Chrome against a self-signed tablet.

The browser never talks to the tablet. It talks to Home Assistant, and Home
Assistant talks to the tablet with the pinned certificate. The assertion is
the iframe text, not a screenshot: a certificate failure used to leave the
frame blank.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import shutil
from tempfile import TemporaryDirectory
from typing import Any

from aiohttp import ClientSession, web

from custom_components.kiosk_mgmt.proxy import signed_proxy_url
from tests.fake_kiosk import serve
from tests.test_proxy import _admin_refresh, _entry

_CHROME = shutil.which("google-chrome") or shutil.which("chromium")


async def _evaluate(debug_port: int, expression: str) -> Any:
    """Run one expression in the first page target and return its value."""
    async with (
        ClientSession() as session,
        session.get(f"http://127.0.0.1:{debug_port}/json/list") as response,
    ):
        targets = await response.json()
    page = next(item for item in targets if item.get("type") == "page")
    async with (
        ClientSession() as session,
        session.ws_connect(page["webSocketDebuggerUrl"]) as websocket,
    ):
        await websocket.send_json(
            {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            }
        )
        message = await websocket.receive_json()
    result = message["result"]["result"]
    if "value" in result:
        return result["value"]
    return None


_MEASURE = """
(() => {
  const el = document.querySelector("kiosk-mgmt-panel");
  const frame = el && el.shadowRoot && el.shadowRoot.querySelector("iframe");
  const doc = frame && frame.contentDocument;
  const password = doc && doc.querySelector("#password");
  const box = password ? password.getBoundingClientRect() : null;
  const text = doc && doc.body ? doc.body.innerText : "";
  return {
    text,
    shim: doc && doc.documentElement.dataset.kioskMgmtShim || "",
    passwordTop: box ? box.top : null,
    passwordBottom: box ? box.bottom : null,
    viewHeight: doc ? doc.documentElement.clientHeight : 0,
    frameHeight: frame ? frame.getBoundingClientRect().height : 0,
    frameWidth: frame ? frame.getBoundingClientRect().width : 0,
    hostHeight: el ? el.getBoundingClientRect().height : 0,
  };
})()
"""

_LOGIN = """
(() => {
  const frame = document.querySelector("kiosk-mgmt-panel").shadowRoot.querySelector("iframe");
  const doc = frame.contentDocument;
  doc.querySelector("#password").value = "secret";
  doc.querySelector("#loginBtn").click();
  return true;
})()
"""

# The splash sentence is "Starting remote administration". The login card
# replaces it with the heading "Remote administration" on its own line.
# "remote administration" is in both, so a substring of the splash is not
# the loaded page.
_SPLASH_TEXT = "Starting remote administration"
_LOGIN_HEADING = "Remote administration"
_POLL_INTERVAL = 0.25
_READY_TIMEOUT = 10.0


def _visible_lines(value: dict[str, Any]) -> list[str]:
    """Non-empty lines of text the page is actually showing."""
    return [
        line.strip()
        for line in str(value.get("text") or "").splitlines()
        if line.strip()
    ]


def _login_card_ready(value: dict[str, Any]) -> bool:
    """True once the splash is gone and the login field is laid out."""
    lines = _visible_lines(value)
    top = value.get("passwordTop")
    bottom = value.get("passwordBottom")
    return (
        value.get("shim") == "1"
        and _LOGIN_HEADING in lines
        and _SPLASH_TEXT not in lines
        and isinstance(top, (int, float))
        and isinstance(bottom, (int, float))
        and bottom > top
    )


def _admin_ready(value: dict[str, Any]) -> bool:
    """True once login has opened the admin shell."""
    lines = _visible_lines(value)
    return "Kiosk admin ready" in lines and "Kitchen tablet" in lines


async def _wait_for(
    debug_port: int, ready: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """Poll the iframe until ``ready`` holds, then return that measurement.

    Same shape as Playwright's ``expect`` / ``wait_for_function``: retry on a
    short interval until a timeout, and fail with the last measurement.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _READY_TIMEOUT
    value: dict[str, Any] = {}
    while True:
        value = await _evaluate(debug_port, _MEASURE) or {}
        if ready(value):
            return value
        if loop.time() >= deadline:
            raise AssertionError(value)
        await asyncio.sleep(_POLL_INTERVAL)


async def _chrome(url: str, width: int, height: int):
    """Launch headless Chrome at one window size and yield its debug port."""
    profile = TemporaryDirectory(ignore_cleanup_errors=True)
    process = await asyncio.create_subprocess_exec(
        _CHROME,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--remote-debugging-port=0",
        f"--window-size={width},{height}",
        f"--user-data-dir={profile.name}",
        url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stderr is not None
    debug_port = None
    while debug_port is None:
        line = await asyncio.wait_for(process.stderr.readline(), 20)
        text = line.decode(errors="replace")
        if "DevTools listening on" in text:
            debug_port = int(text.split("127.0.0.1:")[1].split("/")[0])
    return process, profile, debug_port


async def _panel_page(hass: Any, hass_client: Any, kiosk: Any) -> str:
    """Register the panel harness and return its URL."""
    signed = signed_proxy_url(
        hass,
        hass.config_entries.async_entries("kiosk_mgmt")[0].entry_id,
        "manual:1",
        refresh_token_id=await _admin_refresh(hass),
    )
    page = f"""<!DOCTYPE html>
<html><head>
<style>
  html, body {{ margin: 0; height: 100%; background: #fafafa; }}
  ha-panel-custom {{ display: block; }}
</style>
</head><body>
<ha-panel-custom style="display:block">
<script src="/kiosk_mgmt/kiosk-mgmt-panel.js"></script>
<kiosk-mgmt-panel></kiosk-mgmt-panel>
<script>
const el = document.querySelector("kiosk-mgmt-panel");
const signed = {json.dumps(signed)};
el.hass = {{
  connection: {{
    subscribeMessage(callback) {{
      callback({{kiosks: [{{
        id: "manual:1",
        name: "Kitchen tablet",
        host: {json.dumps(kiosk.host)},
        model: "Test panel",
        online: true,
        last_seen: null,
        url: {json.dumps(kiosk.origin + "/")},
        scheme: "https"
      }}]}});
      return Promise.resolve(() => {{}});
    }},
    sendMessagePromise() {{
      return Promise.resolve({{url: signed, expires_in: 300}});
    }}
  }},
  callService() {{ return Promise.resolve(); }}
}};
</script>
</ha-panel-custom>
</body></html>
"""

    async def parent(_request: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html")

    hass.http.app.router.add_get("/_kiosk_mgmt_panel_test", parent)
    client = await hass_client()
    assert client.port
    return f"http://127.0.0.1:{client.port}/_kiosk_mgmt_panel_test"


async def test_panel_iframe_fills_and_login_reaches_the_tablet(
    hass: Any, hass_client: Any, hass_access_token: str
) -> None:
    """The iframe fills the panel, and a correct password opens the admin page."""
    if _CHROME is None:
        return
    del hass_access_token
    async with serve() as kiosk:
        await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        url = await _panel_page(hass, hass_client, kiosk)
        process, profile, debug_port = await _chrome(url, 1280, 800)
        try:
            # The password element is in the document while the splash still
            # says "Starting remote administration". Wait until that splash is
            # gone and the login heading is its own visible line. A substring
            # of the splash would match the loading text.
            value = await _wait_for(debug_port, _login_card_ready)
            lines = _visible_lines(value)
            assert value.get("shim") == "1", value
            assert value["frameHeight"] > 400, value
            assert value["frameWidth"] > 1000, value
            assert value["hostHeight"] > value["frameHeight"], value
            assert value["passwordTop"] >= 0, value
            assert value["passwordBottom"] <= value["viewHeight"] + 1, value
            assert _LOGIN_HEADING in lines, value
            assert _SPLASH_TEXT not in lines, value

            await _evaluate(debug_port, _LOGIN)
            # Login leaves the card and opens the admin shell. Wait for that
            # shell's heading, not the first text that shares a word with it.
            value = await _wait_for(debug_port, _admin_ready)
            lines = _visible_lines(value)
            assert "Kiosk admin ready" in lines, value
            assert "Kitchen tablet" in lines, value
        finally:
            process.terminate()
            await process.wait()
            profile.cleanup()


async def test_panel_iframe_fills_a_phone_width(
    hass: Any, hass_client: Any, hass_access_token: str
) -> None:
    """On a narrow viewport the iframe still fills the width and the password is visible."""
    if _CHROME is None:
        return
    del hass_access_token
    async with serve() as kiosk:
        await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        url = await _panel_page(hass, hass_client, kiosk)
        process, profile, debug_port = await _chrome(url, 390, 844)
        try:
            # Same splash race as the desktop test: a hidden password has a
            # zero box, which still looks "in view". Wait for the login card.
            value = await _wait_for(debug_port, _login_card_ready)
            lines = _visible_lines(value)
            assert _LOGIN_HEADING in lines, value
            assert _SPLASH_TEXT not in lines, value
            assert value["frameWidth"] > 320, value
            assert value["frameHeight"] > 400, value
            assert value["passwordTop"] >= 0, value
            assert value["passwordBottom"] <= value["viewHeight"] + 1, value
        finally:
            process.terminate()
            await process.wait()
            profile.cleanup()
