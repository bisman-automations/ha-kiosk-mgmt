"""Connectivity for each Kiosk Satellite."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import KioskCoordinator, KioskMgmtConfigEntry
from .entity import KioskEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskMgmtConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a connectivity sensor for each kiosk, including ones found later."""
    _track(entry, async_add_entities, KioskConnectivityBinarySensor)


def _track(
    entry: KioskMgmtConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: type[KioskEntity],
) -> None:
    """Add an entity whenever a kiosk id shows up in the coordinator."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    def _sync() -> None:
        fresh = [
            factory(coordinator, kiosk_id)
            for kiosk_id in (coordinator.data or {})
            if kiosk_id not in known
        ]
        known.update(entity.kiosk_id for entity in fresh)
        if fresh:
            async_add_entities(fresh)

    entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()


class KioskConnectivityBinarySensor(KioskEntity, BinarySensorEntity):
    """On while the admin port answered the health probe."""

    _attr_translation_key = "connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: KioskCoordinator, kiosk_id: str) -> None:
        """Set the stable unique id."""
        super().__init__(coordinator, kiosk_id)
        self._attr_unique_id = f"{kiosk_id}_connectivity"

    @property
    def is_on(self) -> bool:
        """Return True when the last probe reached the tablet."""
        runtime = self.runtime
        return bool(runtime and runtime.online)
