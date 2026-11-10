"""Shared entity base for one Kiosk Satellite."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import KioskCoordinator, KioskRuntime


class KioskEntity(CoordinatorEntity[KioskCoordinator]):
    """An entity attached to one stored kiosk."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: KioskCoordinator, kiosk_id: str) -> None:
        """Bind the entity to one kiosk id."""
        super().__init__(coordinator)
        self.kiosk_id = kiosk_id

    @property
    def runtime(self) -> KioskRuntime | None:
        """Return the live view, if this kiosk is still managed."""
        data = self.coordinator.data
        if not data:
            return None
        return data.get(self.kiosk_id)

    @property
    def available(self) -> bool:
        """Stay available while the kiosk is configured, even if it is offline.

        Connectivity has to be able to read "off". An unavailable entity would
        hide that.
        """
        return super().available and self.runtime is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Remove the entity when the kiosk is ignored or deleted."""
        if self.runtime is None:
            self.hass.async_create_task(self.async_remove())
            return
        super()._handle_coordinator_update()

    @property
    def device_info(self) -> DeviceInfo:
        """Describe our device. The MAC connection merges it with ESPHome."""
        runtime = self.runtime
        if runtime is None:
            return DeviceInfo(identifiers={(DOMAIN, self.kiosk_id)})
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, self.kiosk_id)},
            "name": runtime.name,
            "configuration_url": runtime.admin_url,
        }
        if runtime.mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, runtime.mac)}
        else:
            info["manufacturer"] = "Kiosk Satellite"
            if runtime.model:
                info["model"] = runtime.model
        if runtime.app_version and not runtime.mac:
            info["sw_version"] = runtime.app_version
        return DeviceInfo(**info)
