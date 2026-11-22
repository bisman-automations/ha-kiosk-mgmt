"""Kiosk Mgmt: find Kiosk Satellite tablets and open their admin pages."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
import logging
from typing import Any

import probatio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType
from homeassistant.setup import async_setup_component

from .const import (
    CONF_KIOSK_ID,
    DOMAIN,
    ESPHOME_DOMAIN,
    SERVICE_IGNORE,
    SERVICE_REFRESH,
    SERVICE_REMOVE,
    SIGNAL_KIOSKS_UPDATED,
)
from .coordinator import KioskCoordinator, async_link_esphome_devices
from .discovery import async_watch_mdns, is_kiosk_device
from .panel import async_register_panel, async_unregister_panel
from .proxy import async_load_shim, register_proxy
from .records import kiosks_from_options, without
from .websocket_api import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type KioskMgmtConfigEntry = ConfigEntry[KioskCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the panel script, the websocket command, and the services."""
    hass.data.setdefault(DOMAIN, {})
    # Tests construct Home Assistant without the HTTP server. A normal
    # startup has already created it before this integration loads.
    if hass.http is None:
        await async_setup_component(hass, "http", {})
    async_register_websocket(hass)
    register_proxy(hass)
    # Read the iframe hook's script now, in the executor, so the proxy never
    # reads it on the event loop. A failed read is retried on first use.
    try:
        await async_load_shim(hass)
    except OSError:
        _LOGGER.exception("Kiosk Mgmt could not read its proxy script")
    await async_register_panel(hass, static=True, panel=False)
    _async_register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Move verify_ssl false onto certificate pinning.

    Version 1.1 stored ``verify_ssl: false`` as "do not check the
    certificate". 1.2 keeps that flag as "pin the certificate" and leaves
    ``cert_sha256`` empty so the next successful HTTPS probe records the
    leaf. ``verify_ssl: true`` stays a certificate-authority check.
    """
    if config_entry.version > 1:
        return False
    if config_entry.minor_version >= 2:
        return True
    options = dict(config_entry.options)
    raw = options.get("kiosks")
    kiosks: dict[str, Any] = {}
    if isinstance(raw, dict):
        for kiosk_id, record in raw.items():
            if not isinstance(record, dict):
                continue
            updated = dict(record)
            updated.setdefault("cert_sha256", None)
            kiosks[str(kiosk_id)] = updated
    options["kiosks"] = kiosks
    hass.config_entries.async_update_entry(
        config_entry,
        options=options,
        minor_version=2,
        version=1,
    )
    _LOGGER.debug(
        "Migrated Kiosk Mgmt entry %s to certificate pinning", config_entry.entry_id
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: KioskMgmtConfigEntry) -> bool:
    """Set up the coordinator, entities, and sidebar panel."""
    coordinator = KioskCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_scan_registry()
    entry.async_on_unload(await async_watch_mdns(hass, coordinator.async_adopt))
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_link_esphome_devices(hass, coordinator)
    await async_register_panel(hass, static=False, panel=True)
    entry.async_on_unload(coordinator.async_add_listener(lambda: _dispatch(hass)))

    @callback
    def _scan_registry(_event: object) -> None:
        """Look again when Home Assistant's device list changes."""
        hass.async_create_task(coordinator.async_scan_registry())

    entry.async_on_unload(
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            _scan_registry,
            event_filter=partial(_may_be_kiosk_device, hass),
        )
    )
    entry.async_on_unload(coordinator.async_shutdown)
    _dispatch(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KioskMgmtConfigEntry) -> bool:
    """Unload entities and the sidebar panel."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_unregister_panel(hass)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: KioskMgmtConfigEntry) -> None:
    """Drop repairs when the entry is deleted."""
    registry = ir.async_get(hass)
    for issue in list(registry.issues.values()):
        if issue.domain == DOMAIN:
            ir.async_delete_issue(hass, DOMAIN, issue.issue_id)


# Device fields the registry scan reads. An update that changes none of them
# can't add or move a kiosk.
_SCANNED_FIELDS = frozenset(
    {
        "config_entry_id",
        "configuration_url",
        "connections",
        "manufacturer",
        "model",
        "name",
    }
)


@callback
def _may_be_kiosk_device(hass: HomeAssistant, data: Mapping[str, Any]) -> bool:
    """Return True for a device registry change the kiosk scan could act on.

    The scan probes every Kiosk Satellite over the network, so it must not
    run for every device change in the home, nor for our own devices.
    """
    if data.get("action") == "remove":
        return False
    device = dr.async_get(hass).async_get(str(data.get("device_id")))
    if device is None or not is_kiosk_device(device):
        return False
    entry = hass.config_entries.async_get_entry(device.config_entry_id or "")
    if entry is None or entry.domain != ESPHOME_DOMAIN:
        return False
    if data.get("action") == "update":
        return bool(_SCANNED_FIELDS.intersection(data.get("changes") or {}))
    return True


@callback
def _dispatch(hass: HomeAssistant) -> None:
    """Tell websocket subscribers the list changed."""
    async_dispatcher_send(hass, SIGNAL_KIOSKS_UPDATED)


def _coordinator(hass: HomeAssistant) -> KioskCoordinator | None:
    """Return the running coordinator, if the entry is set up."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, KioskCoordinator):
            return runtime
    return None


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the mutating services. Each one requires an admin."""
    kiosk_schema = probatio.Schema(
        {probatio.Required(CONF_KIOSK_ID): str},
        extra=probatio.PREVENT_EXTRA,
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_REFRESH, _async_refresh, probatio.Schema({})
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_IGNORE, _async_ignore, kiosk_schema
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_REMOVE, _async_remove, kiosk_schema
    )


async def _async_refresh(call: ServiceCall) -> None:
    """Probe every kiosk now."""
    coordinator = _coordinator(call.hass)
    if coordinator is None:
        raise ServiceValidationError("Kiosk Mgmt is not set up")
    await coordinator.async_request_refresh()


async def _async_ignore(call: ServiceCall) -> None:
    """Ignore one kiosk."""
    await _async_mutate(call, ignore=True)


async def _async_remove(call: ServiceCall) -> None:
    """Remove one kiosk."""
    await _async_mutate(call, ignore=False)


async def _async_mutate(call: ServiceCall, *, ignore: bool) -> None:
    """Ignore or remove a kiosk from the stored list."""
    coordinator = _coordinator(call.hass)
    if coordinator is None:
        raise ServiceValidationError("Kiosk Mgmt is not set up")
    kiosk_id = str(call.data[CONF_KIOSK_ID])
    if kiosk_id not in coordinator.records:
        raise ServiceValidationError(f"Unknown kiosk {kiosk_id}")
    if ignore:
        records = kiosks_from_options({"kiosks": coordinator.records})
        records[kiosk_id]["ignored"] = True
    else:
        records = without(coordinator.records, kiosk_id)
    coordinator.replace_records(records)
    await coordinator.async_request_refresh()
