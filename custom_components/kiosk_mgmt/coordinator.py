"""Polling, adoption, and repairs for every Kiosk Satellite we manage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .client import ProbeResult, async_probe
from .const import (
    ATTR_CERT_SHA256,
    ATTR_IGNORED,
    ATTR_MAC,
    ATTR_SCHEME_MODE,
    CONF_SCHEME,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
    SCHEME_AUTO,
    SIGNAL_KIOSKS_UPDATED,
    UPDATE_INTERVAL,
)
from .discovery import Candidate, candidates_from_registry
from .records import apply_candidate, find_kiosk, kiosks_from_options, record_admin_url
from .tls import format_fingerprint, normalize_fingerprint

_LOGGER = logging.getLogger(__name__)

type KioskMgmtConfigEntry = ConfigEntry["KioskCoordinator"]


@dataclass(slots=True)
class KioskRuntime:
    """Live view of one kiosk. ``last_seen`` stays set across a failed probe."""

    kiosk_id: str
    name: str
    host: str
    port: int
    scheme: str
    scheme_mode: str
    verify_ssl: bool
    model: str | None
    brand: str | None
    mac: str | None
    app_version: str | None
    link_url: str | None
    online: bool
    last_seen: datetime | None
    ssl_error: bool
    cert_mismatch: bool
    presented_sha256: str | None
    ignored: bool

    @property
    def admin_url(self) -> str:
        """Return the page the panel opens."""
        return record_admin_url(
            {
                "host": self.host,
                "port": self.port,
                CONF_SCHEME: self.scheme,
                "link_url": self.link_url,
            }
        )


def runtime_from_record(
    kiosk_id: str,
    record: dict[str, Any],
    probe: ProbeResult | None,
    previous: KioskRuntime | None,
) -> KioskRuntime:
    """Build the live view from a stored record and the latest probe."""
    online = bool(probe and probe.ok)
    last_seen = previous.last_seen if previous else None
    if online:
        last_seen = dt_util.utcnow()
    scheme = record.get(CONF_SCHEME) or (probe.scheme if probe else None) or "http"
    return KioskRuntime(
        kiosk_id=kiosk_id,
        name=str(record.get("name") or record["host"]),
        host=str(record["host"]),
        port=int(record.get("port") or DEFAULT_PORT),
        scheme=str(scheme),
        scheme_mode=str(record.get(ATTR_SCHEME_MODE) or SCHEME_AUTO),
        verify_ssl=bool(record.get(CONF_VERIFY_SSL)),
        model=record.get("model"),
        brand=record.get("brand"),
        mac=record.get(ATTR_MAC),
        app_version=record.get("app_version"),
        link_url=record.get("link_url"),
        online=online,
        last_seen=last_seen,
        ssl_error=bool(probe and probe.ssl_error and not online),
        cert_mismatch=bool(probe and probe.cert_mismatch and not online),
        presented_sha256=probe.cert_sha256 if probe else None,
        ignored=bool(record.get(ATTR_IGNORED)),
    )


class KioskCoordinator(DataUpdateCoordinator[dict[str, KioskRuntime]]):
    """Probe each kiosk and publish the list to entities and the panel."""

    config_entry: KioskMgmtConfigEntry

    def __init__(self, hass: HomeAssistant, entry: KioskMgmtConfigEntry) -> None:
        """Load the stored kiosks and start the poll interval."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.config_entry = entry
        self.records = kiosks_from_options(entry.options)

    async def _async_update_data(self) -> dict[str, KioskRuntime]:
        """Probe every kiosk that is not ignored."""
        previous = self.data or {}
        current: dict[str, KioskRuntime] = {}
        for kiosk_id, record in self.records.items():
            if record.get(ATTR_IGNORED):
                continue
            prior = previous.get(kiosk_id)
            probe = await async_probe(
                self.hass,
                str(record["host"]),
                int(record.get("port") or DEFAULT_PORT),
                str(record.get(ATTR_SCHEME_MODE) or SCHEME_AUTO),
                bool(record.get(CONF_VERIFY_SSL)),
                preferred=str(record.get(CONF_SCHEME) or "") or None,
                pinned_sha256=record.get(ATTR_CERT_SHA256),
            )
            # Migrated kiosks, and tablets found in the background, have no
            # pin yet. The first health document that presents a leaf is the
            # one we remember. A later change is a repair, not a silent update.
            if (
                probe.ok
                and not record.get(CONF_VERIFY_SSL)
                and probe.cert_sha256
                and not record.get(ATTR_CERT_SHA256)
            ):
                record[ATTR_CERT_SHA256] = normalize_fingerprint(probe.cert_sha256)
                self._persist()
            if probe.ok and probe.scheme and probe.scheme != record.get(CONF_SCHEME):
                record[CONF_SCHEME] = probe.scheme
                self._persist()
            if probe.ok and probe.name and probe.name != record.get("name"):
                record["name"] = probe.name
                self._persist()
            if probe.ok and probe.model and probe.model != record.get("model"):
                record["model"] = probe.model
                record["brand"] = probe.brand
                record["app_version"] = probe.app_version or record.get("app_version")
                self._persist()
            current[kiosk_id] = runtime_from_record(kiosk_id, record, probe, prior)
        self._sync_repairs(current)
        return current

    @callback
    def _persist(self) -> None:
        """Write the records back without reloading the entry."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            options={"kiosks": self.records},
        )

    def replace_records(self, records: dict[str, dict[str, Any]]) -> None:
        """Replace the stored list and tell listeners."""
        self.records = records
        self._persist()
        async_dispatcher_send(self.hass, SIGNAL_KIOSKS_UPDATED)

    async def async_adopt(
        self,
        candidate: Candidate,
        *,
        verify_ssl: bool = False,
        scheme_mode: str | None = None,
        refresh: bool = True,
    ) -> bool:
        """Probe a candidate and keep it when the health document answers.

        Returns True when the stored list changed. Ignored kiosks are not
        brought back.
        """
        existing = find_kiosk(self.records, candidate)
        if existing is not None and self.records[existing].get(ATTR_IGNORED):
            return False
        record = self.records.get(existing) if existing else None
        use_verify = (
            verify_ssl
            if scheme_mode is not None
            else bool((record or {}).get(CONF_VERIFY_SSL, verify_ssl))
        )
        probe = await async_probe(
            self.hass,
            candidate.host,
            candidate.port,
            scheme_mode
            or (record or {}).get(ATTR_SCHEME_MODE)
            or candidate.scheme_mode,
            use_verify,
            pinned_sha256=(record or {}).get(ATTR_CERT_SHA256),
        )
        merged = apply_candidate(
            self.records,
            candidate,
            probe,
            verify_ssl=use_verify,
            scheme_mode=scheme_mode,
            cert_sha256=probe.cert_sha256 if probe.ok else None,
        )
        if merged == self.records:
            return False
        self.records = merged
        self._persist()
        if refresh and self.data is not None:
            await self.async_request_refresh()
        return True

    async def async_scan_registry(self) -> None:
        """Adopt Kiosk Satellite rows already in the device registry."""
        changed = False
        for candidate in candidates_from_registry(self.hass):
            if await self.async_adopt(candidate, refresh=False):
                changed = True
        if changed and self.data is not None:
            await self.async_request_refresh()
            _LOGGER.debug("Device registry scan added or moved a kiosk")

    @callback
    def _sync_repairs(self, current: dict[str, KioskRuntime]) -> None:
        """Raise a repair while a kiosk is offline or its certificate fails."""
        seen: set[str] = set()
        for state in current.values():
            offline_id = _issue_id("offline", state.kiosk_id)
            cert_id = _issue_id("certificate", state.kiosk_id)
            seen.add(offline_id)
            seen.add(cert_id)
            placeholders = {"name": state.name, "host": state.host}
            changed_id = _issue_id("certificate_changed", state.kiosk_id)
            seen.add(changed_id)
            if state.online:
                ir.async_delete_issue(self.hass, DOMAIN, offline_id)
                ir.async_delete_issue(self.hass, DOMAIN, cert_id)
                ir.async_delete_issue(self.hass, DOMAIN, changed_id)
                continue
            if state.cert_mismatch:
                ir.async_delete_issue(self.hass, DOMAIN, offline_id)
                ir.async_delete_issue(self.hass, DOMAIN, cert_id)
                stored = normalize_fingerprint(
                    self.records.get(state.kiosk_id, {}).get(ATTR_CERT_SHA256)
                )
                presented = normalize_fingerprint(state.presented_sha256)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    changed_id,
                    is_fixable=True,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="certificate_changed",
                    translation_placeholders={
                        **placeholders,
                        "fingerprint": format_fingerprint(stored) if stored else "",
                        "new_fingerprint": format_fingerprint(presented)
                        if presented
                        else "",
                    },
                    data={
                        "entry_id": self.config_entry.entry_id,
                        "kiosk_id": state.kiosk_id,
                        "fingerprint": stored,
                        "new_fingerprint": presented,
                    },
                )
                continue
            ir.async_delete_issue(self.hass, DOMAIN, changed_id)
            if state.ssl_error:
                ir.async_delete_issue(self.hass, DOMAIN, offline_id)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    cert_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="certificate",
                    translation_placeholders=placeholders,
                )
                continue
            ir.async_delete_issue(self.hass, DOMAIN, cert_id)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                offline_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="offline",
                translation_placeholders=placeholders,
            )
        # Drop repairs for kiosks that were removed or ignored.
        registry = ir.async_get(self.hass)
        for issue in list(registry.issues.values()):
            if issue.domain != DOMAIN or issue.issue_id in seen:
                continue
            if issue.issue_id.startswith(("offline_", "certificate_")):
                ir.async_delete_issue(self.hass, DOMAIN, issue.issue_id)

    def payload(self) -> list[dict[str, Any]]:
        """Return the admin panel's view of the kiosks."""
        rows: list[dict[str, Any]] = []
        data = self.data or {}
        for kiosk_id, record in self.records.items():
            if record.get(ATTR_IGNORED):
                continue
            state = data.get(kiosk_id)
            rows.append(
                {
                    "id": kiosk_id,
                    "name": state.name if state else record.get("name"),
                    "host": state.host if state else record.get("host"),
                    "port": state.port if state else record.get("port"),
                    "scheme": state.scheme if state else record.get(CONF_SCHEME),
                    "verify_ssl": state.verify_ssl
                    if state
                    else bool(record.get(CONF_VERIFY_SSL)),
                    "url": state.admin_url if state else record_admin_url(record),
                    "model": (state.model if state else record.get("model")),
                    "brand": (state.brand if state else record.get("brand")),
                    "online": bool(state and state.online),
                    "last_seen": state.last_seen.isoformat()
                    if state and state.last_seen
                    else None,
                    "app_version": state.app_version
                    if state
                    else record.get("app_version"),
                }
            )
        rows.sort(key=lambda row: str(row["name"] or "").lower())
        return rows


def _issue_id(kind: str, kiosk_id: str) -> str:
    """Return a repair id. Issue ids are shown in logs, so keep them readable."""
    slug = kiosk_id.replace(":", "_").replace("/", "_")
    return f"{kind}_{slug}"


@callback
def async_link_esphome_devices(
    hass: HomeAssistant, coordinator: KioskCoordinator
) -> None:
    """Link our device to the ESPHome device with via_device_id.

    Home Assistant 2026.10 keeps each device on one config entry, so a shared
    MAC no longer merges the rows. The MAC is still stored on our device.
    ``via_device_id`` is written only on our device and points at the ESPHome
    device with the same MAC, or the same name when there is exactly one.
    """
    from .const import ESPHOME_DOMAIN

    registry = dr.async_get(hass)
    entry_id = coordinator.config_entry.entry_id
    for state in (coordinator.data or {}).values():
        ours = registry.async_get_device_by_identifier(
            (DOMAIN, state.kiosk_id), entry_id
        )
        if ours is None:
            continue
        esphome_device = _esphome_device(hass, registry, state, ESPHOME_DOMAIN)
        if esphome_device is None or esphome_device.id == ours.id:
            continue
        if ours.via_device_id != esphome_device.id:
            registry.async_update_device(ours.id, via_device_id=esphome_device.id)


def _esphome_device(
    hass: HomeAssistant,
    registry: dr.DeviceRegistry,
    state: KioskRuntime,
    esphome_domain: str,
) -> dr.DeviceEntry | None:
    """Return the ESPHome device for this tablet."""
    connection = (dr.CONNECTION_NETWORK_MAC, state.mac) if state.mac else None
    named: list[dr.DeviceEntry] = []
    for entry in hass.config_entries.async_entries(esphome_domain):
        if connection is not None:
            device = registry.async_get_device_by_connection(connection, entry.entry_id)
            if device is not None:
                return device
        named.extend(
            device
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
            if device.name == state.name
        )
    if len(named) == 1:
        return named[0]
    return None
