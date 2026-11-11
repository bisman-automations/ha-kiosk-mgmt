"""Admin-only websocket subscription for the sidebar panel."""

from __future__ import annotations

from typing import Any

import probatio

from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_KIOSKS_UPDATED, SIGNED_PATH_SECONDS, WS_SIGN_PROXY
from .coordinator import KioskCoordinator
from .proxy import signed_proxy_url
from .records import record_admin_url


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the subscribe command once."""
    if hass.data[DOMAIN].get("ws"):
        return
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_sign_proxy)
    hass.data[DOMAIN]["ws"] = True


def _coordinator(hass: HomeAssistant) -> KioskCoordinator | None:
    """Return the running coordinator."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, KioskCoordinator):
            return runtime
    return None


def _payload(hass: HomeAssistant) -> dict[str, Any]:
    """Return the message body. No tokens and no admin password are stored."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        return {"kiosks": []}
    return {"kiosks": coordinator.payload()}


@websocket_api.websocket_command({probatio.Required("type"): "kiosk_mgmt/subscribe"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_subscribe(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stream the kiosk list. The result is sent first, then events."""

    @callback
    def _forward() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], _payload(hass)))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, SIGNAL_KIOSKS_UPDATED, _forward
    )
    connection.send_result(msg["id"])
    _forward()


@websocket_api.websocket_command(
    {
        probatio.Required("type"): WS_SIGN_PROXY,
        probatio.Required("kiosk_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_sign_proxy(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a short-lived proxy URL for one tablet.

    The signature is tied to this admin's refresh token. The panel puts it
    on the iframe and refreshes it before it expires. The first response
    sets the cookie that later requests from the embedded page send.
    """
    coordinator = _coordinator(hass)
    kiosk_id = str(msg["kiosk_id"])
    record = coordinator.records.get(kiosk_id) if coordinator is not None else None
    if coordinator is None or record is None or record.get("ignored"):
        connection.send_error(msg["id"], "not_found", "Unknown kiosk")
        return
    url = signed_proxy_url(
        hass,
        coordinator.config_entry.entry_id,
        kiosk_id,
        refresh_token_id=connection.refresh_token_id,
    )
    connection.send_result(
        msg["id"],
        {
            "url": url,
            "expires_in": SIGNED_PATH_SECONDS,
            "direct_url": record_admin_url(record),
        },
    )
