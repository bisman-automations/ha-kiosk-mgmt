"""Admin-only websocket subscription for the sidebar panel."""

from __future__ import annotations

from typing import Any

import probatio

from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_KIOSKS_UPDATED
from .coordinator import KioskCoordinator


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the subscribe command once."""
    if hass.data[DOMAIN].get("ws"):
        return
    websocket_api.async_register_command(hass, ws_subscribe)
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
