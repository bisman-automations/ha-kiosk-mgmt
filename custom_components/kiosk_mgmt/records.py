"""The kiosk list stored on the config entry.

``options["kiosks"]`` maps a stable id to one JSON object. The id is the
fleet id when the ``_kiosk-satellite._tcp`` TXT record has one (that id is
the Android id, or the fleet id the user set), otherwise the ESPHome MAC,
otherwise an id minted for a host typed by hand. A later advertisement that
shares the host fills in the fleet id and MAC without changing the id, so
entity unique ids stay put when a tablet changes address.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from typing import Any
from uuid import uuid4

from .client import ProbeResult, admin_url
from .const import (
    ATTR_APP_VERSION,
    ATTR_BRAND,
    ATTR_FLEET_ID,
    ATTR_IGNORED,
    ATTR_LINK_URL,
    ATTR_MAC,
    ATTR_NODE_NAME,
    ATTR_SCHEME_MODE,
    ATTR_SOURCE,
    CONF_SCHEME,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    SCHEME_AUTO,
    SCHEME_HTTP,
)
from .discovery import Candidate

RECORD_KEYS = (
    "name",
    "host",
    "port",
    CONF_SCHEME,
    ATTR_SCHEME_MODE,
    CONF_VERIFY_SSL,
    "model",
    ATTR_BRAND,
    ATTR_MAC,
    ATTR_FLEET_ID,
    ATTR_NODE_NAME,
    ATTR_APP_VERSION,
    ATTR_IGNORED,
    ATTR_LINK_URL,
    ATTR_SOURCE,
)


def empty_options() -> dict[str, Any]:
    """Return options for a new config entry."""
    return {"kiosks": {}}


def kiosks_from_options(options: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a mutable copy of the stored kiosk records."""
    raw = options.get("kiosks", {})
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(kiosk_id): _clean(record)
        for kiosk_id, record in raw.items()
        if isinstance(record, Mapping)
    }


def _clean(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a record with the keys we persist."""
    cleaned = {key: record.get(key) for key in RECORD_KEYS}
    cleaned["port"] = int(cleaned["port"] or DEFAULT_PORT)
    cleaned[CONF_SCHEME] = cleaned[CONF_SCHEME] or SCHEME_HTTP
    cleaned[ATTR_SCHEME_MODE] = cleaned[ATTR_SCHEME_MODE] or SCHEME_AUTO
    cleaned[CONF_VERIFY_SSL] = bool(cleaned[CONF_VERIFY_SSL])
    cleaned[ATTR_IGNORED] = bool(cleaned[ATTR_IGNORED])
    cleaned["name"] = cleaned["name"] or cleaned["host"]
    return cleaned


def find_kiosk(
    records: Mapping[str, Mapping[str, Any]],
    candidate: Candidate,
) -> str | None:
    """Return the id of the record this candidate already is, if any."""
    for kiosk_id, record in records.items():
        if candidate.fleet_id and record.get(ATTR_FLEET_ID) == candidate.fleet_id:
            return kiosk_id
        if candidate.mac and record.get(ATTR_MAC) == candidate.mac:
            return kiosk_id
    for kiosk_id, record in records.items():
        if (
            record.get("host") != candidate.host
            or int(record.get("port") or 0) != candidate.port
        ):
            continue
        fleet = record.get(ATTR_FLEET_ID)
        if candidate.fleet_id and fleet and fleet != candidate.fleet_id:
            continue
        mac = record.get(ATTR_MAC)
        if candidate.mac and mac and mac != candidate.mac:
            continue
        return kiosk_id
    return None


def _new_id(candidate: Candidate) -> str:
    """Mint a stable id. The value is kept for the life of the record."""
    if candidate.fleet_id:
        return f"fleet:{candidate.fleet_id}"
    if candidate.mac:
        return f"mac:{candidate.mac}"
    return f"manual:{uuid4().hex}"


def apply_candidate(
    records: Mapping[str, Mapping[str, Any]],
    candidate: Candidate,
    probe: ProbeResult,
    *,
    verify_ssl: bool = False,
    scheme_mode: str | None = None,
    kiosk_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return records with this candidate merged in.

    A failed probe does not add a new kiosk and does not move an existing
    one's address. An ignored kiosk is left ignored. ``scheme_mode`` set by
    the user (anything but the candidate's own guess) is kept.
    """
    updated = kiosks_from_options({"kiosks": records})
    if kiosk_id not in updated:
        kiosk_id = find_kiosk(updated, candidate)
    if kiosk_id is not None and updated[kiosk_id][ATTR_IGNORED]:
        return updated
    if not probe.ok:
        return updated
    if kiosk_id is None:
        kiosk_id = _new_id(candidate)
        updated[kiosk_id] = _clean(
            {
                "host": candidate.host,
                "port": candidate.port,
                "name": candidate.host,
                ATTR_SCHEME_MODE: scheme_mode or candidate.scheme_mode,
                CONF_VERIFY_SSL: verify_ssl,
                ATTR_SOURCE: candidate.source,
            }
        )
    record = updated[kiosk_id]
    record["host"] = candidate.host
    record["port"] = candidate.port
    record[CONF_SCHEME] = probe.scheme or record[CONF_SCHEME]
    if scheme_mode is not None:
        record[ATTR_SCHEME_MODE] = scheme_mode
    elif record[ATTR_SCHEME_MODE] == SCHEME_AUTO:
        record[ATTR_SCHEME_MODE] = candidate.scheme_mode
    record[CONF_VERIFY_SSL] = (
        verify_ssl if scheme_mode is not None else record[CONF_VERIFY_SSL]
    )
    record["name"] = probe.name or candidate.name or record["name"]
    if probe.model:
        record["model"] = probe.model
    if probe.brand:
        record[ATTR_BRAND] = probe.brand
    if candidate.mac:
        record[ATTR_MAC] = candidate.mac
    if candidate.fleet_id:
        record[ATTR_FLEET_ID] = candidate.fleet_id
    if candidate.node_name:
        record[ATTR_NODE_NAME] = candidate.node_name
    if probe.app_version or candidate.app_version:
        record[ATTR_APP_VERSION] = probe.app_version or candidate.app_version
    if candidate.link_url:
        record[ATTR_LINK_URL] = candidate.link_url
    record[ATTR_SOURCE] = candidate.source
    return updated


def record_admin_url(record: Mapping[str, Any]) -> str:
    """Return the page the panel should open."""
    link = record.get(ATTR_LINK_URL)
    if isinstance(link, str) and link.startswith(("http://", "https://")):
        return link if link.endswith("/") else f"{link}/"
    return admin_url(
        str(record.get(CONF_SCHEME) or SCHEME_HTTP),
        str(record["host"]),
        int(record["port"]),
    )


def without(
    records: Mapping[str, Mapping[str, Any]], kiosk_id: str
) -> dict[str, dict[str, Any]]:
    """Return records without one kiosk."""
    updated = copy.deepcopy(kiosks_from_options({"kiosks": records}))
    updated.pop(kiosk_id, None)
    return updated
