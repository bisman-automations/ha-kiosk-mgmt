"""Diagnostics for the single Kiosk Mgmt entry.

Nothing here is a credential. The admin password stays on the tablet, and
this integration never asks for it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import ATTR_CERT_SHA256, ATTR_IGNORED, CONF_SCHEME, CONF_VERIFY_SSL
from .coordinator import KioskCoordinator, KioskMgmtConfigEntry
from .records import kiosks_from_options


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: KioskMgmtConfigEntry,
) -> dict[str, Any]:
    """Return the kiosk list and the last probe result."""
    coordinator = (
        entry.runtime_data if isinstance(entry.runtime_data, KioskCoordinator) else None
    )
    live = (
        {state.kiosk_id: state for state in (coordinator.data or {}).values()}
        if coordinator
        else {}
    )
    kiosks: list[dict[str, Any]] = []
    for kiosk_id, record in kiosks_from_options(entry.options).items():
        state = live.get(kiosk_id)
        kiosks.append(
            {
                "id": kiosk_id,
                "name": record.get("name"),
                "host": record.get("host"),
                "port": record.get("port"),
                "scheme": record.get(CONF_SCHEME),
                "scheme_mode": record.get("scheme_mode"),
                "verify_ssl": record.get(CONF_VERIFY_SSL),
                "cert_sha256": record.get(ATTR_CERT_SHA256),
                "model": record.get("model"),
                "brand": record.get("brand"),
                "mac": record.get("mac"),
                "fleet_id": record.get("fleet_id"),
                "node_name": record.get("node_name"),
                "app_version": record.get("app_version"),
                "ignored": record.get(ATTR_IGNORED),
                "source": record.get("source"),
                "online": bool(state and state.online),
                "ssl_error": bool(state and state.ssl_error),
                "cert_mismatch": bool(state and state.cert_mismatch),
                "last_seen": state.last_seen.isoformat()
                if state and state.last_seen
                else None,
            }
        )
    return {"entry_id": entry.entry_id, "kiosks": kiosks}
