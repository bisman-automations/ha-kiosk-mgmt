"""Registry observation, the coordinator, entities, the panel, and services."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_mgmt.const import DOMAIN
from custom_components.kiosk_mgmt.discovery import Candidate
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import device_registry as dr, entity_registry as er
from tests.common import HOST, MAC, OTHER_HOST, esphome_service, probe_fail, probe_ok


def _patch(monkeypatch: Any, result: Any) -> list[Any]:
    """Patch probes and record the calls."""
    calls: list[Any] = []

    async def _probe(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr("custom_components.kiosk_mgmt.coordinator.async_probe", _probe)
    return calls


async def _setup(hass: Any, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options or {"kiosks": {}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_registry_observation_adds_a_kiosk_and_ignores_others(
    hass: Any, monkeypatch: Any
) -> None:
    """ESPHome rows that are Kiosk Satellites are probed and added."""
    _patch(monkeypatch, probe_ok())
    esphome = MockConfigEntry(domain="esphome", data={}, unique_id="esphome-1")
    esphome.add_to_hass(hass)
    other = MockConfigEntry(domain="esphome", data={}, unique_id="esphome-2")
    other.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=esphome.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        manufacturer="kiosk_satellite",
        model="bluetooth_proxy",
        name="Kitchen tablet",
        configuration_url=f"http://{HOST}:2324",
    )
    device_registry.async_get_or_create(
        config_entry_id=other.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, "11:22:33:44:55:66")},
        manufacturer="espressif",
        model="esp32",
        name="Plant",
        configuration_url="http://192.0.2.50:80",
    )
    entry = await _setup(hass)
    kiosks = entry.options["kiosks"]
    assert len(kiosks) == 1
    record = next(iter(kiosks.values()))
    assert record["host"] == HOST
    assert record["mac"] == MAC
    assert record["source"] == "device_registry"

    kiosk_id = next(iter(kiosks))
    ours = device_registry.async_get_device_by_identifier(
        (DOMAIN, kiosk_id), entry.entry_id
    )
    esphome_device = device_registry.async_get_device_by_connection(
        (dr.CONNECTION_NETWORK_MAC, MAC), esphome.entry_id
    )
    assert ours is not None
    assert esphome_device is not None
    assert (dr.CONNECTION_NETWORK_MAC, MAC) in ours.connections
    assert ours.via_device_id == esphome_device.id
    assert ours.configuration_url == f"http://{HOST}:2324/"


async def test_coordinator_online_last_seen_and_ip_change(
    hass: Any, monkeypatch: Any
) -> None:
    """A probe sets online and last seen, and a new address follows the MAC."""
    calls = _patch(monkeypatch, probe_ok())
    entry = await _setup(
        hass,
        {
            "kiosks": {
                "mac:aa:bb:cc:dd:ee:ff": {
                    "name": "Kitchen tablet",
                    "host": HOST,
                    "port": 2324,
                    "scheme": "http",
                    "scheme_mode": "auto",
                    "verify_ssl": False,
                    "model": "KFTUWI",
                    "brand": "Amazon",
                    "mac": MAC,
                    "fleet_id": None,
                    "node_name": "ks-amazon-kftuwi",
                    "app_version": "2026.10.42",
                    "ignored": False,
                    "link_url": None,
                    "source": "manual",
                }
            }
        },
    )
    state = hass.states.get("binary_sensor.kitchen_tablet_connectivity")
    assert state is not None
    assert state.state == STATE_ON
    seen = hass.states.get("sensor.kitchen_tablet_last_seen")
    assert seen is not None
    assert seen.state != "unknown"

    async def _fail(*args: Any, **kwargs: Any) -> Any:
        return probe_fail()

    monkeypatch.setattr("custom_components.kiosk_mgmt.coordinator.async_probe", _fail)
    coordinator = entry.runtime_data
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    assert (
        hass.states.get("binary_sensor.kitchen_tablet_connectivity").state == STATE_OFF
    )
    assert hass.states.get("sensor.kitchen_tablet_last_seen").state == seen.state

    monkeypatch.setattr(
        "custom_components.kiosk_mgmt.coordinator.async_probe",
        AsyncMock(return_value=probe_ok()),
    )
    await coordinator.async_adopt(
        Candidate(
            host=OTHER_HOST, port=2324, scheme_mode="http", mac=MAC, source="zeroconf"
        )
    )
    await hass.async_block_till_done()
    record = entry.options["kiosks"]["mac:aa:bb:cc:dd:ee:ff"]
    assert record["host"] == OTHER_HOST
    assert calls  # the first setup probed the original host


async def test_adopt_adds_a_second_tablet(hass: Any, monkeypatch: Any) -> None:
    """A tablet found after setup joins the same entry."""
    _patch(monkeypatch, probe_ok())
    entry = await _setup(
        hass,
        {
            "kiosks": {
                "fleet:android-1": {
                    "name": "Kitchen tablet",
                    "host": HOST,
                    "port": 2324,
                    "scheme": "http",
                    "scheme_mode": "http",
                    "verify_ssl": False,
                    "model": "KFTUWI",
                    "brand": None,
                    "mac": None,
                    "fleet_id": "android-1",
                    "node_name": None,
                    "app_version": None,
                    "ignored": False,
                    "link_url": None,
                    "source": "zeroconf",
                }
            }
        },
    )
    added = await entry.runtime_data.async_adopt(
        Candidate(
            host=OTHER_HOST,
            port=2324,
            scheme_mode="https",
            name="Hall tablet",
            fleet_id="android-2",
            source="zeroconf",
        )
    )
    await hass.async_block_till_done()
    assert added is True
    assert len(entry.options["kiosks"]) == 2
    assert len(entry.runtime_data.payload()) == 2


async def test_ignored_kiosk_is_not_rediscovered(hass: Any, monkeypatch: Any) -> None:
    """An ignored tablet stays hidden when the same MAC is seen again."""
    _patch(monkeypatch, probe_ok())
    entry = await _setup(
        hass,
        {
            "kiosks": {
                "mac:aa:bb:cc:dd:ee:ff": {
                    "name": "Kitchen tablet",
                    "host": HOST,
                    "port": 2324,
                    "scheme": "http",
                    "scheme_mode": "http",
                    "verify_ssl": False,
                    "model": None,
                    "brand": None,
                    "mac": MAC,
                    "fleet_id": "android-1",
                    "node_name": None,
                    "app_version": None,
                    "ignored": True,
                    "link_url": None,
                    "source": "zeroconf",
                }
            }
        },
    )
    coordinator = entry.runtime_data
    assert coordinator.payload() == []
    changed = await coordinator.async_adopt(
        Candidate(
            host=OTHER_HOST,
            port=2324,
            scheme_mode="http",
            mac=MAC,
            fleet_id="android-1",
            source="zeroconf",
        )
    )
    assert changed is False
    assert entry.options["kiosks"]["mac:aa:bb:cc:dd:ee:ff"]["host"] == HOST
    assert hass.states.get("binary_sensor.kitchen_tablet_connectivity") is None


async def test_esphome_zeroconf_parse_uses_admin_port() -> None:
    """The ESPHome advertisement's port is the API port, not 2324."""
    from custom_components.kiosk_mgmt.discovery import parse_zeroconf

    candidate = parse_zeroconf(esphome_service())
    assert candidate is not None
    assert candidate.port == 2324
    assert candidate.mac == MAC
    assert candidate.scheme_mode == "auto"
    assert candidate.node_name == "ks-amazon-kftuwi"
    assert (
        parse_zeroconf(
            esphome_service(
                project_name="other.device", name="plant._esphomelib._tcp.local."
            )
        )
        is None
    )


async def test_panel_is_admin_only(hass: Any, monkeypatch: Any) -> None:
    """The sidebar panel requires an admin and the script is registered."""
    _patch(monkeypatch, probe_ok())
    await _setup(hass)
    from homeassistant.components.frontend import DATA_PANELS

    panel = hass.data[DATA_PANELS]["kiosk-mgmt"]
    assert panel.require_admin is True
    assert panel.sidebar_title == "Kiosk Mgmt"
    assert panel.config["_panel_custom"]["js_url"] == "/kiosk_mgmt/kiosk-mgmt-panel.js"
    assert hass.data[DOMAIN]["static"] is True


async def test_services_are_admin_only(hass: Any, monkeypatch: Any) -> None:
    """Ignore and remove refuse a non-admin."""
    _patch(monkeypatch, probe_ok())
    entry = await _setup(
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
                    "brand": None,
                    "mac": None,
                    "fleet_id": None,
                    "node_name": None,
                    "app_version": None,
                    "ignored": False,
                    "link_url": None,
                    "source": "manual",
                }
            }
        },
    )
    await hass.auth.async_create_user("Owner")
    user = await hass.auth.async_create_user("Guest")
    assert user.is_admin is False
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "remove_kiosk",
            {"kiosk_id": "manual:1"},
            blocking=True,
            context=Context(user_id=user.id),
        )
    assert "manual:1" in entry.options["kiosks"]

    await hass.services.async_call(
        DOMAIN,
        "ignore_kiosk",
        {"kiosk_id": "manual:1"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options["kiosks"]["manual:1"]["ignored"] is True


async def test_diagnostics_have_no_secrets(hass: Any, monkeypatch: Any) -> None:
    """Diagnostics list the tablets and do not invent a password field."""
    _patch(monkeypatch, probe_ok())
    entry = await _setup(
        hass,
        {
            "kiosks": {
                "manual:1": {
                    "name": "Kitchen tablet",
                    "host": HOST,
                    "port": 2324,
                    "scheme": "https",
                    "scheme_mode": "https",
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
    from custom_components.kiosk_mgmt.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag).lower()
    assert "password" not in text
    assert "token" not in text
    assert diag["kiosks"][0]["online"] is True
    assert (
        er.async_get(hass).async_get("binary_sensor.kitchen_tablet_connectivity")
        is not None
    )
