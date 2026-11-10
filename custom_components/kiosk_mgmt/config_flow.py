"""Config flow for Kiosk Mgmt.

One config entry manages every tablet. Zeroconf starts that entry the first
time a Kiosk Satellite advertisement is seen, and after that each new
advertisement is probed and added to the same entry. Adding a tablet by host
is the user step before the entry exists, and an options step after it does.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import probatio

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .client import async_probe
from .const import (
    CONF_KIOSK_ID,
    CONF_SCHEME,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
    SCHEME_AUTO,
    SCHEMES,
)
from .discovery import Candidate, parse_zeroconf
from .records import apply_candidate, kiosks_from_options, without

_LOGGER = logging.getLogger(__name__)


def _connection_schema(defaults: Mapping[str, Any]) -> probatio.Schema:
    """Schema for host, port, protocol, and certificate checking."""
    return probatio.Schema(
        {
            probatio.Required(
                CONF_HOST, default=defaults.get(CONF_HOST, "")
            ): TextSelector(),
            probatio.Required(
                CONF_PORT, default=int(defaults.get(CONF_PORT, DEFAULT_PORT))
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=65535,
                    mode=NumberSelectorMode.BOX,
                    step=1,
                )
            ),
            probatio.Required(
                CONF_SCHEME, default=defaults.get(CONF_SCHEME, SCHEME_AUTO)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(SCHEMES),
                    translation_key="scheme",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            probatio.Required(
                CONF_VERIFY_SSL, default=bool(defaults.get(CONF_VERIFY_SSL, False))
            ): BooleanSelector(),
        }
    )


def _clean_connection(user_input: dict[str, Any]) -> dict[str, Any] | str:
    """Return a cleaned connection, or an error key."""
    host = str(user_input.get(CONF_HOST, "")).strip().rstrip(".")
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].strip("[]")
    if not host or any(char.isspace() for char in host):
        return "invalid_host"
    try:
        port = int(user_input[CONF_PORT])
    except KeyError, TypeError, ValueError:
        return "invalid_host"
    if port < 1 or port > 65535:
        return "invalid_host"
    scheme = str(user_input.get(CONF_SCHEME, SCHEME_AUTO))
    if scheme not in SCHEMES:
        return "invalid_host"
    return {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_SCHEME: scheme,
        CONF_VERIFY_SSL: bool(user_input.get(CONF_VERIFY_SSL, False)),
    }


def _kiosk_options(records: Mapping[str, Mapping[str, Any]]) -> list[SelectOptionDict]:
    """Return selector options for the stored kiosks."""
    options: list[SelectOptionDict] = []
    for kiosk_id, record in sorted(
        records.items(), key=lambda item: str(item[1].get("name") or "").lower()
    ):
        name = str(record.get("name") or record.get("host"))
        if record.get("ignored"):
            name = f"{name} (ignored)"
        options.append({"value": kiosk_id, "label": name})
    return options


class KioskMgmtConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the one Kiosk Mgmt entry."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Remember the advertisement waiting for confirmation."""
        self._candidate: Candidate | None = None
        self._reconfigure_id: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add the entry by typing the first tablet's host."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await self._async_connection_step(
            "user",
            user_input,
            create=True,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Start from a Kiosk Satellite advertisement, or add to the entry."""
        candidate = parse_zeroconf(discovery_info)
        if candidate is None:
            return self.async_abort(reason="not_kiosk_satellite")
        await self.async_set_unique_id(DOMAIN)
        if entries := self._async_current_entries():
            adopted = await _adopt_into_entry(self.hass, entries[0], candidate)
            if not adopted:
                _LOGGER.debug(
                    "Ignoring %s:%s until it answers on the admin port",
                    candidate.host,
                    candidate.port,
                )
            return self.async_abort(reason="already_configured")
        probe = await async_probe(
            self.hass,
            candidate.host,
            candidate.port,
            candidate.scheme_mode,
            verify_ssl=False,
        )
        if not probe.ok:
            return self.async_abort(reason="cannot_connect")
        self._candidate = candidate
        self.context["title_placeholders"] = {
            "name": probe.name or candidate.name or candidate.host
        }
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": probe.name or candidate.name or candidate.host,
                "host": candidate.host,
                "model": probe.model or "unknown",
            },
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the discovered tablet and create the single entry."""
        candidate = self._candidate
        if candidate is None:
            return self.async_abort(reason="cannot_connect")
        if user_input is None:
            return self.async_show_form(step_id="confirm")
        probe = await async_probe(
            self.hass,
            candidate.host,
            candidate.port,
            candidate.scheme_mode,
            verify_ssl=False,
        )
        if not probe.ok:
            return self.async_abort(reason="cannot_connect")
        records = apply_candidate({}, candidate, probe, verify_ssl=False)
        return self.async_create_entry(
            title="Kiosk Mgmt",
            data={},
            options={"kiosks": records},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which tablet's address to edit."""
        entry = self._get_reconfigure_entry()
        records = kiosks_from_options(entry.options)
        if not records:
            return self.async_abort(reason="no_kiosks")
        if len(records) == 1 and user_input is None:
            self._reconfigure_id = next(iter(records))
            return await self.async_step_reconfigure_connection()
        if user_input is not None:
            self._reconfigure_id = str(user_input[CONF_KIOSK_ID])
            return await self.async_step_reconfigure_connection()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_KIOSK_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=_kiosk_options(records),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_reconfigure_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit one tablet's host, port, protocol, and certificate check."""
        entry = self._get_reconfigure_entry()
        records = kiosks_from_options(entry.options)
        kiosk_id = self._reconfigure_id
        if kiosk_id is None or kiosk_id not in records:
            return self.async_abort(reason="no_kiosks")
        record = records[kiosk_id]
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _clean_connection(user_input)
            if isinstance(cleaned, str):
                errors["base"] = cleaned
            else:
                probe = await async_probe(
                    self.hass,
                    cleaned[CONF_HOST],
                    cleaned[CONF_PORT],
                    cleaned[CONF_SCHEME],
                    cleaned[CONF_VERIFY_SSL],
                )
                if not probe.ok:
                    errors["base"] = (
                        "ssl_error" if probe.ssl_error else "cannot_connect"
                    )
                else:
                    candidate = Candidate(
                        host=cleaned[CONF_HOST],
                        port=cleaned[CONF_PORT],
                        scheme_mode=cleaned[CONF_SCHEME],
                        name=record.get("name"),
                        mac=record.get("mac"),
                        fleet_id=record.get("fleet_id"),
                        node_name=record.get("node_name"),
                        link_url=record.get("link_url"),
                        source="reconfigure",
                    )
                    updated = apply_candidate(
                        records,
                        candidate,
                        probe,
                        verify_ssl=cleaned[CONF_VERIFY_SSL],
                        scheme_mode=cleaned[CONF_SCHEME],
                        kiosk_id=kiosk_id,
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        options={"kiosks": updated},
                    )
        defaults = {
            CONF_HOST: record.get("host", ""),
            CONF_PORT: record.get("port", DEFAULT_PORT),
            CONF_SCHEME: record.get("scheme_mode", SCHEME_AUTO),
            CONF_VERIFY_SSL: record.get("verify_ssl", False),
        }
        return self.async_show_form(
            step_id="reconfigure_connection",
            data_schema=_connection_schema(defaults),
            errors=errors or None,
            description_placeholders={
                "name": str(record.get("name") or record.get("host"))
            },
        )

    async def _async_connection_step(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        *,
        create: bool,
    ) -> ConfigFlowResult:
        """Show the manual-add form and create the entry when the probe works."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _clean_connection(user_input)
            if isinstance(cleaned, str):
                errors["base"] = cleaned
            else:
                probe = await async_probe(
                    self.hass,
                    cleaned[CONF_HOST],
                    cleaned[CONF_PORT],
                    cleaned[CONF_SCHEME],
                    cleaned[CONF_VERIFY_SSL],
                )
                if not probe.ok:
                    errors["base"] = (
                        "ssl_error" if probe.ssl_error else "cannot_connect"
                    )
                elif create:
                    candidate = Candidate(
                        host=cleaned[CONF_HOST],
                        port=cleaned[CONF_PORT],
                        scheme_mode=cleaned[CONF_SCHEME],
                        name=probe.name,
                        source="manual",
                    )
                    records = apply_candidate(
                        {},
                        candidate,
                        probe,
                        verify_ssl=cleaned[CONF_VERIFY_SSL],
                        scheme_mode=cleaned[CONF_SCHEME],
                    )
                    await self.async_set_unique_id(DOMAIN)
                    return self.async_create_entry(
                        title="Kiosk Mgmt",
                        data={},
                        options={"kiosks": records},
                    )
        return self.async_show_form(
            step_id=step_id,
            data_schema=_connection_schema({}),
            errors=errors or None,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KioskMgmtOptionsFlow:
        """Return the options flow."""
        return KioskMgmtOptionsFlow()


class KioskMgmtOptionsFlow(OptionsFlowWithReload):
    """Add, edit, ignore, and remove kiosks on the single entry."""

    def __init__(self) -> None:
        """Remember which kiosk the manage steps are about."""
        self._kiosk_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose add or manage."""
        return self.async_show_menu(step_id="init", menu_options=["add", "manage"])

    async def async_step_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a tablet by host."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _clean_connection(user_input)
            if isinstance(cleaned, str):
                errors["base"] = cleaned
            else:
                records = kiosks_from_options(self.config_entry.options)
                probe = await async_probe(
                    self.hass,
                    cleaned[CONF_HOST],
                    cleaned[CONF_PORT],
                    cleaned[CONF_SCHEME],
                    cleaned[CONF_VERIFY_SSL],
                )
                if not probe.ok:
                    errors["base"] = (
                        "ssl_error" if probe.ssl_error else "cannot_connect"
                    )
                else:
                    candidate = Candidate(
                        host=cleaned[CONF_HOST],
                        port=cleaned[CONF_PORT],
                        scheme_mode=cleaned[CONF_SCHEME],
                        name=probe.name,
                        source="manual",
                    )
                    updated = apply_candidate(
                        records,
                        candidate,
                        probe,
                        verify_ssl=cleaned[CONF_VERIFY_SSL],
                        scheme_mode=cleaned[CONF_SCHEME],
                    )
                    if updated == records:
                        errors["base"] = "already_tracked"
                    else:
                        return self.async_create_entry(data={"kiosks": updated})
        return self.async_show_form(
            step_id="add",
            data_schema=_connection_schema({}),
            errors=errors or None,
        )

    async def async_step_manage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a kiosk to edit, ignore, or remove."""
        records = kiosks_from_options(self.config_entry.options)
        if not records:
            return self.async_abort(reason="no_kiosks")
        if user_input is not None:
            self._kiosk_id = str(user_input[CONF_KIOSK_ID])
            return await self.async_step_kiosk()
        return self.async_show_form(
            step_id="manage",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_KIOSK_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=_kiosk_options(records),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_kiosk(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what to do with the selected kiosk."""
        records = kiosks_from_options(self.config_entry.options)
        record = records.get(self._kiosk_id or "")
        if record is None:
            return self.async_abort(reason="no_kiosks")
        include = "include" if record.get("ignored") else "ignore"
        return self.async_show_menu(
            step_id="kiosk",
            menu_options=["edit", include, "remove"],
            description_placeholders={
                "name": str(record.get("name") or record.get("host"))
            },
        )

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change one kiosk's connection."""
        records = kiosks_from_options(self.config_entry.options)
        kiosk_id = self._kiosk_id
        if kiosk_id is None or kiosk_id not in records:
            return self.async_abort(reason="no_kiosks")
        record = records[kiosk_id]
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _clean_connection(user_input)
            if isinstance(cleaned, str):
                errors["base"] = cleaned
            else:
                probe = await async_probe(
                    self.hass,
                    cleaned[CONF_HOST],
                    cleaned[CONF_PORT],
                    cleaned[CONF_SCHEME],
                    cleaned[CONF_VERIFY_SSL],
                )
                if not probe.ok:
                    errors["base"] = (
                        "ssl_error" if probe.ssl_error else "cannot_connect"
                    )
                else:
                    candidate = Candidate(
                        host=cleaned[CONF_HOST],
                        port=cleaned[CONF_PORT],
                        scheme_mode=cleaned[CONF_SCHEME],
                        name=record.get("name"),
                        mac=record.get("mac"),
                        fleet_id=record.get("fleet_id"),
                        node_name=record.get("node_name"),
                        link_url=record.get("link_url"),
                        source="options",
                    )
                    updated = apply_candidate(
                        records,
                        candidate,
                        probe,
                        verify_ssl=cleaned[CONF_VERIFY_SSL],
                        scheme_mode=cleaned[CONF_SCHEME],
                        kiosk_id=kiosk_id,
                    )
                    return self.async_create_entry(data={"kiosks": updated})
        defaults = {
            CONF_HOST: record.get("host", ""),
            CONF_PORT: record.get("port", DEFAULT_PORT),
            CONF_SCHEME: record.get("scheme_mode", SCHEME_AUTO),
            CONF_VERIFY_SSL: record.get("verify_ssl", False),
        }
        return self.async_show_form(
            step_id="edit",
            data_schema=_connection_schema(defaults),
            errors=errors or None,
            description_placeholders={
                "name": str(record.get("name") or record.get("host"))
            },
        )

    async def async_step_ignore(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stop managing a kiosk without forgetting that it should stay hidden."""
        return await self._async_flag_step("ignore", user_input, ignored=True)

    async def async_step_include(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage an ignored kiosk again."""
        return await self._async_flag_step("include", user_input, ignored=False)

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove a kiosk. A later discovery can add it again."""
        records = kiosks_from_options(self.config_entry.options)
        record = records.get(self._kiosk_id or "")
        if record is None or self._kiosk_id is None:
            return self.async_abort(reason="no_kiosks")
        if user_input is not None:
            return self.async_create_entry(
                data={"kiosks": without(records, self._kiosk_id)}
            )
        return self.async_show_form(
            step_id="remove",
            data_schema=probatio.Schema({}),
            description_placeholders={
                "name": str(record.get("name") or record.get("host"))
            },
        )

    async def _async_flag_step(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        *,
        ignored: bool,
    ) -> ConfigFlowResult:
        """Confirm ignoring or including a kiosk."""
        records = kiosks_from_options(self.config_entry.options)
        record = records.get(self._kiosk_id or "")
        if record is None or self._kiosk_id is None:
            return self.async_abort(reason="no_kiosks")
        if user_input is not None:
            records[self._kiosk_id]["ignored"] = ignored
            return self.async_create_entry(data={"kiosks": records})
        return self.async_show_form(
            step_id=step_id,
            data_schema=probatio.Schema({}),
            description_placeholders={
                "name": str(record.get("name") or record.get("host"))
            },
        )


async def _adopt_into_entry(
    hass: Any, entry: ConfigEntry, candidate: Candidate
) -> bool:
    """Probe and store a candidate on an entry that is already set up."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        return await coordinator.async_adopt(candidate)
    result = await async_probe(
        hass, candidate.host, candidate.port, candidate.scheme_mode, False
    )
    merged = apply_candidate(kiosks_from_options(entry.options), candidate, result)
    current = kiosks_from_options(entry.options)
    if merged == current:
        return False
    hass.config_entries.async_update_entry(entry, options={"kiosks": merged})
    return True
