"""Load the sidebar panel in headless Chrome against a self-signed tablet.

The browser never talks to the tablet. It talks to Home Assistant, and Home
Assistant talks to the tablet with the pinned certificate. The assertion is
the iframe text, not a screenshot: a certificate failure used to leave the
frame blank.
"""

from __future__ import annotations

import asyncio
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


async def test_panel_iframe_shows_a_self_signed_admin_page(
    hass: Any, hass_client: Any, hass_access_token: str
) -> None:
    """The panel iframe renders the tablet through the proxy."""
    if _CHROME is None:
        return
    del hass_access_token
    async with serve() as kiosk:
        entry = await _entry(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
        signed = signed_proxy_url(
            hass,
            entry.entry_id,
            "manual:1",
            refresh_token_id=await _admin_refresh(hass),
        )
        page = f"""<!DOCTYPE html>
<html><body>
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
        model: "KFTUWI",
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
</body></html>
"""

        async def parent(_request: web.Request) -> web.Response:
            return web.Response(text=page, content_type="text/html")

        hass.http.app.router.add_get("/_kiosk_mgmt_panel_test", parent)
        client = await hass_client()
        assert client.port
        url = f"http://127.0.0.1:{client.port}/_kiosk_mgmt_panel_test"
        with TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            process = await asyncio.create_subprocess_exec(
                _CHROME,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stderr is not None
            debug_port = None
            try:
                while debug_port is None:
                    line = await asyncio.wait_for(process.stderr.readline(), 20)
                    text = line.decode(errors="replace")
                    if "DevTools listening on" in text:
                        debug_port = int(text.split("127.0.0.1:")[1].split("/")[0])
                expression = """
                  (() => {
                    const el = document.querySelector("kiosk-mgmt-panel");
                    const frame = el && el.shadowRoot && el.shadowRoot.querySelector("iframe");
                    const card = el && el.shadowRoot && el.shadowRoot.querySelector(".card");
                    const text = frame && frame.contentDocument && frame.contentDocument.body
                      ? frame.contentDocument.body.innerText : "";
                    return {text, card: card ? card.innerText : ""};
                  })()
                """
                seen = ""
                for _ in range(40):
                    value = await _evaluate(debug_port, expression)
                    seen = "" if not value else value.get("text") or ""
                    if "Kiosk admin ready" in seen:
                        break
                    await asyncio.sleep(0.25)
                assert "Kiosk admin ready" in seen, value
            finally:
                process.terminate()
                await process.wait()
