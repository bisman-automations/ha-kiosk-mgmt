"""Turn zeroconf records and device-registry rows into kiosk candidates.

Two advertisements are real, and they are not interchangeable:

- ``_kiosk-satellite._tcp`` (FleetDiscovery.kt) carries the admin port, the
  fleet id, the display name, the app version, and ``tls=1`` when the admin
  page is HTTPS. It is only sent while Find other kiosks is on.
- ``_esphomelib._tcp`` (MdnsAnnouncer.kt) is the ESPHome API. Its port is
  6053, not 2324. ``project_name`` is ``kiosk_satellite.bluetooth_proxy``.
  ``mac`` is the identity Home Assistant already uses. There is no TLS flag;
  the app omits the Visit link entirely when the admin page is HTTPS
  (bt_proxy_manager.dart ``_webserverPort``).

Device-registry rows are read and never written here. ESPHome copies the
project name onto the device as manufacturer ``kiosk_satellite`` and model
``bluetooth_proxy``, and the device name is the friendly name, so a prefix
check on the name alone misses a renamed tablet. The MAC connection is how
our own device later merges with that row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    DEFAULT_PORT,
    ESPHOME_DOMAIN,
    ESPHOME_SERVICE,
    FLEET_SERVICE,
    NODE_PREFIXES,
    PROJECT_NAME,
    REGISTRY_MANUFACTURER,
    REGISTRY_MODEL,
    SCHEME_AUTO,
    SCHEME_HTTP,
    SCHEME_HTTPS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Candidate:
    """A tablet we might manage, before it has been probed."""

    host: str
    port: int
    scheme_mode: str
    name: str | None = None
    mac: str | None = None
    fleet_id: str | None = None
    node_name: str | None = None
    app_version: str | None = None
    link_url: str | None = None
    source: str = "manual"


def normalize_mac(value: str | None) -> str | None:
    """Return a device-registry MAC, or None when the value is not one."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    formatted = format_mac(cleaned)
    compact = formatted.replace(":", "")
    if len(compact) != 12 or any(char not in "0123456789abcdef" for char in compact):
        return None
    return formatted


def _prop(properties: dict[str, Any], key: str) -> str | None:
    """Return one TXT value as text."""
    value = properties.get(key)
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = text.strip()
    return text or None


def _node_from_service_name(name: str) -> str | None:
    """Return the instance label of ``ks-tablet._esphomelib._tcp.local.``."""
    label = name.split(".", 1)[0].strip().lower()
    if label.startswith(NODE_PREFIXES):
        return label
    return label or None


def parse_zeroconf(info: ZeroconfServiceInfo) -> Candidate | None:
    """Return a candidate for a Kiosk Satellite advertisement, or None."""
    return candidate_from_mdns(
        info.type,
        info.name,
        str(info.host).strip("[]"),
        info.port,
        info.properties,
    )


def candidate_from_mdns(
    service_type: str,
    name: str,
    host: str,
    port: int | None,
    properties: dict[str, Any],
) -> Candidate | None:
    """Return a candidate from the raw mDNS fields, or None."""
    service = service_type if service_type.endswith(".") else f"{service_type}."
    properties = {str(key).lower(): value for key, value in properties.items()}
    host = host.strip("[]")
    if not host:
        return None

    if service == FLEET_SERVICE or service.startswith("_kiosk-satellite._tcp"):
        port_text = _prop(properties, "port")
        resolved_port = port or DEFAULT_PORT
        if port_text and port_text.isdigit():
            resolved_port = int(port_text)
        tls = _prop(properties, "tls") == "1"
        return Candidate(
            host=host,
            port=resolved_port,
            scheme_mode=SCHEME_HTTPS if tls else SCHEME_HTTP,
            name=_prop(properties, "name"),
            fleet_id=_prop(properties, "id"),
            node_name=_prop(properties, "host"),
            app_version=_prop(properties, "version"),
            link_url=_prop(properties, "admin"),
            source="zeroconf",
        )

    if service != ESPHOME_SERVICE and not service.startswith("_esphomelib._tcp"):
        return None

    project = _prop(properties, "project_name") or ""
    node = _node_from_service_name(name)
    project_match = project == PROJECT_NAME or project.startswith("kiosk_satellite.")
    name_match = bool(node and node.startswith(NODE_PREFIXES))
    if not project_match and not name_match:
        return None
    # This record's port is the ESPHome API. The admin port is not in it.
    return Candidate(
        host=host,
        port=DEFAULT_PORT,
        scheme_mode=SCHEME_AUTO,
        name=_prop(properties, "friendly_name"),
        mac=normalize_mac(_prop(properties, "mac")),
        node_name=node,
        app_version=_prop(properties, "project_version"),
        source="zeroconf",
    )


def _url_host(configuration_url: str | None) -> tuple[str | None, int, str]:
    """Return host, port, and scheme from a Visit link, when it has a host."""
    if not configuration_url:
        return None, DEFAULT_PORT, SCHEME_HTTP
    parts = urlsplit(configuration_url)
    if parts.hostname is None:
        return None, DEFAULT_PORT, SCHEME_HTTP
    scheme = (
        parts.scheme if parts.scheme in (SCHEME_HTTP, SCHEME_HTTPS) else SCHEME_HTTP
    )
    return parts.hostname, parts.port or DEFAULT_PORT, scheme


def _device_mac(device: dr.DeviceEntry) -> str | None:
    """Return the MAC connection on a device registry row."""
    for connection, value in device.connections:
        if connection == dr.CONNECTION_NETWORK_MAC:
            return normalize_mac(value)
    return None


def _is_kiosk_device(device: dr.DeviceEntry) -> bool:
    """Return True when an ESPHome device row is a Kiosk Satellite.

    Manufacturer and model come from the project name Home Assistant splits.
    The name prefix covers a tablet whose friendly name was left as the node
    name (``ks-amazon-kftuwi`` or the generated ``kiosk-satellite-`` name).
    """
    manufacturer = (device.manufacturer or "").lower()
    model = (device.model or "").lower()
    if manufacturer == REGISTRY_MANUFACTURER and model == REGISTRY_MODEL:
        return True
    name = (device.name or "").lower()
    return name.startswith(NODE_PREFIXES)


def candidates_from_registry(hass: HomeAssistant) -> list[Candidate]:
    """Return kiosk candidates visible on ESPHome device-registry rows.

    Only rows linked to an ESPHome config entry are considered, and only the
    row that owns the MAC connection (sub-devices share the project
    manufacturer and model but have no MAC). The host is taken from the
    Visit link on that row. HTTPS tablets have no Visit link, so they are
    not returned here; the fleet or ESPHome advertisement has their address.
    """
    registry = dr.async_get(hass)
    esphome_entries = hass.config_entries.async_entries(ESPHOME_DOMAIN)
    if not esphome_entries:
        return []
    found: list[Candidate] = []
    for entry in esphome_entries:
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            if not _is_kiosk_device(device):
                continue
            mac = _device_mac(device)
            if mac is None:
                continue
            host, port, scheme = _url_host(device.configuration_url)
            if host is None:
                _LOGGER.debug(
                    "ESPHome device %s looks like a Kiosk Satellite but has no Visit link",
                    device.name,
                )
                continue
            found.append(
                Candidate(
                    host=host,
                    port=port,
                    scheme_mode=scheme,
                    name=device.name,
                    mac=mac,
                    source="device_registry",
                )
            )
    return found


async def async_watch_mdns(
    hass: HomeAssistant,
    adopt: Callable[[Candidate], Any],
) -> Callable[[], None]:
    """Listen for Kiosk Satellite advertisements after the entry exists.

    ``single_config_entry`` makes Home Assistant drop later zeroconf flows
    before this integration can see them. The shared zeroconf instance is
    only used when that component is already running, so tests that never
    start it do not open a multicast socket.
    """
    if "zeroconf" not in hass.config.components:
        return lambda: None
    from zeroconf import ServiceBrowser, ServiceStateChange

    from homeassistant.components.zeroconf import async_get_instance

    zc = await async_get_instance(hass)

    def _handler(
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change not in (ServiceStateChange.Added, ServiceStateChange.Updated):
            return
        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            return
        addresses = info.parsed_addresses()
        if not addresses:
            return
        properties: dict[str, Any] = {}
        for key, value in info.properties.items():
            decoded_key = key.decode() if isinstance(key, bytes) else str(key)
            if isinstance(value, bytes):
                properties[decoded_key] = value.decode("utf-8", errors="replace")
            elif value is None:
                properties[decoded_key] = ""
            else:
                properties[decoded_key] = value
        candidate = candidate_from_mdns(
            service_type, name, addresses[0], info.port, properties
        )
        if candidate is None:
            return
        hass.loop.call_soon_threadsafe(
            lambda found=candidate: hass.async_create_task(adopt(found))
        )

    browser = ServiceBrowser(zc, [FLEET_SERVICE, ESPHOME_SERVICE], handlers=[_handler])

    def _cancel() -> None:
        browser.cancel()

    return _cancel
