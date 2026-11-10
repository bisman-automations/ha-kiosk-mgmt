"""Config flow: zeroconf, manual add, options, and reconfigure."""

from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_mgmt.const import DOMAIN
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    SOURCE_ZEROCONF,
)
from tests.common import HOST, fleet_service, probe_fail, probe_ok


def _patch_probe(monkeypatch: Any, result: Any) -> None:
    """Make every flow probe return one result."""

    async def _probe(*args: Any, **kwargs: Any) -> Any:
        return result

    monkeypatch.setattr("custom_components.kiosk_mgmt.config_flow.async_probe", _probe)
    monkeypatch.setattr("custom_components.kiosk_mgmt.coordinator.async_probe", _probe)


async def test_manual_add_creates_one_entry(hass: Any, monkeypatch: Any) -> None:
    """Typing a host probes it and creates the single entry."""
    _patch_probe(monkeypatch, probe_ok(scheme="https"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    schema = result["data_schema"].schema
    assert "host" in schema
    assert "port" in schema
    assert "scheme" in schema
    assert "verify_ssl" in schema

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": HOST, "port": 2324, "scheme": "auto", "verify_ssl": False},
    )
    assert result["type"] == "create_entry"
    kiosks = result["options"]["kiosks"]
    assert len(kiosks) == 1
    record = next(iter(kiosks.values()))
    assert record["host"] == HOST
    assert record["scheme"] == "https"
    assert record["scheme_mode"] == "auto"
    assert record["verify_ssl"] is False
    assert record["name"] == "Kitchen tablet"
    assert record["model"] == "KFTUWI"


async def test_manual_add_reports_ssl_and_connection_errors(
    hass: Any, monkeypatch: Any
) -> None:
    """A failed probe stays on the form."""
    _patch_probe(monkeypatch, probe_fail(ssl_error=True))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": HOST, "port": 2324, "scheme": "https", "verify_ssl": True},
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "ssl_error"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": "not a host", "port": 2324, "scheme": "auto", "verify_ssl": False},
    )
    assert result["errors"]["base"] == "invalid_host"


async def test_zeroconf_confirm_and_esphome_service(
    hass: Any, monkeypatch: Any
) -> None:
    """A fleet advertisement asks to confirm, and an ESPHome one is recognized."""
    _patch_probe(monkeypatch, probe_ok())
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=fleet_service(),
    )
    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["name"] == "Kitchen tablet"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    record = next(iter(result["options"]["kiosks"].values()))
    assert record["fleet_id"] == "android-1"
    assert record["scheme_mode"] == "http"


async def test_unrelated_esphome_advertisement_is_ignored(
    hass: Any, monkeypatch: Any
) -> None:
    """An ordinary ESPHome device does not start Kiosk Mgmt."""
    _patch_probe(monkeypatch, probe_ok())
    info = fleet_service()
    info.type = "_esphomelib._tcp.local."
    info.name = "other._esphomelib._tcp.local."
    info.properties = {"project_name": "esphome.demo", "mac": "001122334455"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=info,
    )
    assert result["type"] == "abort"
    assert result["reason"] == "not_kiosk_satellite"


async def test_zeroconf_https_flag_and_second_kiosk(
    hass: Any, monkeypatch: Any
) -> None:
    """tls=1 selects HTTPS, and a later tablet joins the existing entry."""
    _patch_probe(monkeypatch, probe_ok(scheme="https"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=fleet_service(tls="1"),
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert next(iter(result["options"]["kiosks"].values()))["scheme_mode"] == "https"

    # A second advertisement cannot open another flow: the manifest is
    # single-instance, so Home Assistant stops it before our step runs.
    # The running entry adopts that tablet from the mDNS listener instead.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=fleet_service(id="android-2", name="Hall tablet", port="2325"),
    )
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"


async def test_second_user_flow_is_refused(hass: Any, monkeypatch: Any) -> None:
    """The integration is single-instance."""
    _patch_probe(monkeypatch, probe_ok())
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"kiosks": {}})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["reason"] == "single_instance_allowed"


async def test_options_add_ignore_remove_and_reconfigure(
    hass: Any, monkeypatch: Any
) -> None:
    """Options and reconfigure edit the stored connection."""
    _patch_probe(monkeypatch, probe_ok(scheme="http"))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"kiosks": {}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"host": HOST, "port": 2324, "scheme": "https", "verify_ssl": False},
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    kiosk_id, record = next(iter(entry.options["kiosks"].items()))
    assert record["scheme_mode"] == "https"
    assert record["verify_ssl"] is False

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    # One kiosk skips the picker.
    assert result["step_id"] == "reconfigure_connection"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": HOST, "port": 2443, "scheme": "https", "verify_ssl": True},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    record = entry.options["kiosks"][kiosk_id]
    assert record["port"] == 2443
    assert record["verify_ssl"] is True

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"kiosk_id": kiosk_id}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ignore"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options["kiosks"][kiosk_id]["ignored"] is True

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"kiosk_id": kiosk_id}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options["kiosks"] == {}
