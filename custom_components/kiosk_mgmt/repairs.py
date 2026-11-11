"""Repair flow for a tablet whose pinned certificate changed.

The issue is raised by the coordinator when a stored SHA-256 does not match
the leaf the tablet presents. Submitting this flow stores the new
fingerprint and probes again. Reconfigure offers the same choice.
"""

from __future__ import annotations

from typing import Any

import probatio

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .client import async_probe
from .const import (
    ATTR_CERT_SHA256,
    ATTR_SCHEME_MODE,
    CONF_SCHEME,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
    SCHEME_AUTO,
)
from .coordinator import KioskCoordinator
from .records import kiosks_from_options
from .tls import format_fingerprint, normalize_fingerprint


class CertificateChangedFlow(RepairsFlow):
    """Ask the user to accept a new leaf certificate."""

    def __init__(self, entry_id: str, kiosk_id: str) -> None:
        """Remember which tablet the issue is about."""
        self._entry_id = entry_id
        self._kiosk_id = kiosk_id
        self._presented: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Show the stored and presented fingerprints, then store the new one."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="cannot_connect")
        records = kiosks_from_options(entry.options)
        record = records.get(self._kiosk_id)
        if record is None:
            return self.async_abort(reason="cannot_connect")
        # The repairs flow manager always calls this step with
        # ``{"issue_id": ...}`` so a custom integration can read it. That is
        # not the user pressing Submit. An empty schema submits ``{}``.
        if user_input is not None and "issue_id" not in user_input:
            return await self._async_accept(entry, records, record)
        probe = await async_probe(
            self.hass,
            str(record["host"]),
            int(record.get("port") or DEFAULT_PORT),
            str(record.get(ATTR_SCHEME_MODE) or SCHEME_AUTO),
            bool(record.get(CONF_VERIFY_SSL)),
            preferred=str(record.get(CONF_SCHEME) or "") or None,
            pinned_sha256=record.get(ATTR_CERT_SHA256),
        )
        if probe.ok or not probe.cert_mismatch:
            # The pin matches again, or the tablet is simply offline. Either
            # way this repair has nothing left to accept.
            return self.async_create_entry(title="", data={})
        self._presented = normalize_fingerprint(probe.cert_sha256)
        stored = normalize_fingerprint(record.get(ATTR_CERT_SHA256))
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema({}),
            description_placeholders={
                "name": str(record.get("name") or record.get("host")),
                "host": str(record.get("host")),
                "fingerprint": format_fingerprint(stored) if stored else "",
                "new_fingerprint": format_fingerprint(self._presented)
                if self._presented
                else "",
            },
        )

    async def _async_accept(
        self,
        entry: Any,
        records: dict[str, dict[str, Any]],
        record: dict[str, Any],
    ) -> RepairsFlowResult:
        """Store the fingerprint the tablet just presented and probe again."""
        presented = self._presented
        if presented is None:
            probe = await async_probe(
                self.hass,
                str(record["host"]),
                int(record.get("port") or DEFAULT_PORT),
                str(record.get(ATTR_SCHEME_MODE) or SCHEME_AUTO),
                False,
            )
            presented = normalize_fingerprint(probe.cert_sha256)
        if presented is None:
            return self.async_abort(reason="cannot_connect")
        records[self._kiosk_id][ATTR_CERT_SHA256] = presented
        records[self._kiosk_id][CONF_VERIFY_SSL] = False
        self.hass.config_entries.async_update_entry(entry, options={"kiosks": records})
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, KioskCoordinator):
            runtime.records = records
            # async_request_refresh is debounced and would keep the stale
            # mismatch for several seconds. This probe has to run now.
            await runtime.async_refresh()
            state = (runtime.data or {}).get(self._kiosk_id)
            if state is not None and state.cert_mismatch:
                return self.async_abort(reason="cannot_connect")
        ir.async_delete_issue(
            self.hass, DOMAIN, f"certificate_changed_{_slug(self._kiosk_id)}"
        )
        return self.async_create_entry(title="", data={})


def _slug(kiosk_id: str) -> str:
    """Match the issue id the coordinator builds."""
    return kiosk_id.replace(":", "_").replace("/", "_")


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Start the certificate repair, or a confirm step for anything else."""
    from homeassistant.components.repairs import ConfirmRepairFlow

    if data and issue_id.startswith("certificate_changed_"):
        entry_id = data.get("entry_id")
        kiosk_id = data.get("kiosk_id")
        if isinstance(entry_id, str) and isinstance(kiosk_id, str):
            return CertificateChangedFlow(entry_id, kiosk_id)
    return ConfirmRepairFlow()
