"""The admin websocket subscription."""

from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_mgmt.const import DOMAIN
from homeassistant.auth.models import User
from tests.common import HOST, probe_ok
from tests.test_runtime import _patch, _setup


async def test_admin_receives_the_kiosk_list(
    hass: Any, hass_ws_client: Any, monkeypatch: Any
) -> None:
    """An admin subscription gets the current tablets."""
    _patch(monkeypatch, probe_ok())
    await _setup(
        hass,
        {
            "kiosks": {
                "manual:1": {
                    "name": "Kitchen tablet",
                    "host": HOST,
                    "port": 2324,
                    "scheme": "http",
                    "scheme_mode": "http",
                    "verify_ssl": False,
                    "model": "KFTUWI",
                    "brand": "Amazon",
                    "mac": None,
                    "fleet_id": None,
                    "node_name": None,
                    "app_version": "2026.10.42",
                    "ignored": False,
                    "link_url": None,
                    "source": "manual",
                }
            }
        },
    )
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "kiosk_mgmt/subscribe"})
    result = await client.receive_json()
    assert result["type"] == "result"
    assert result["success"] is True
    event = await client.receive_json()
    assert event["type"] == "event"
    assert event["event"]["kiosks"][0]["name"] == "Kitchen tablet"
    assert event["event"]["kiosks"][0]["host"] == HOST
    assert event["event"]["kiosks"][0]["online"] is True
    assert "password" not in event["event"]["kiosks"][0]


async def test_non_admin_is_rejected(
    hass: Any, hass_ws_client: Any, monkeypatch: Any
) -> None:
    """A regular user cannot subscribe."""
    _patch(monkeypatch, probe_ok())
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"kiosks": {}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    user = await hass.auth.async_create_user("Guest")
    assert isinstance(user, User)
    assert user.is_admin is False
    refresh = await hass.auth.async_create_refresh_token(
        user, client_id="https://example.com"
    )
    token = hass.auth.async_create_access_token(refresh)
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json({"id": 8, "type": "kiosk_mgmt/subscribe"})
    response = await client.receive_json()
    assert response["type"] == "result"
    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"
