"""Last time each Kiosk Satellite answered."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .binary_sensor import _track
from .coordinator import KioskCoordinator, KioskMgmtConfigEntry
from .entity import KioskEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KioskMgmtConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a last-seen sensor for each kiosk."""
    _track(entry, async_add_entities, KioskLastSeenSensor)


class KioskLastSeenSensor(KioskEntity, RestoreEntity, SensorEntity):
    """When the health probe last succeeded.

    The value is restored after a restart so a tablet that is offline at
    startup still shows the previous check-in until the next one.
    """

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: KioskCoordinator, kiosk_id: str) -> None:
        """Set the stable unique id."""
        super().__init__(coordinator, kiosk_id)
        self._attr_unique_id = f"{kiosk_id}_last_seen"
        self._restored: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous timestamp before the first probe returns."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        self._restored = dt_util.parse_datetime(last.state)
        self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        """Return the newest successful probe, or the restored one."""
        runtime = self.runtime
        if runtime and runtime.last_seen:
            return runtime.last_seen
        return self._restored
