"""Constants for Kiosk Mgmt.

The values here are taken from the Kiosk Satellite app (jxlarrea/kiosk-satellite):

- Fleet service ``_kiosk-satellite._tcp`` and its TXT keys are built in
  ``app/android/.../fleet/FleetDiscovery.kt`` (``SERVICE``, ``buildAnnouncement``).
- The ESPHome service ``_esphomelib._tcp`` TXT record is built in
  ``app/android/.../btproxy/MdnsAnnouncer.kt`` (``txtEntries``).
- ``project_name`` is ``kiosk_satellite.bluetooth_proxy`` in
  ``BluetoothProxyRuntime.buildIdentity``.
- The admin port default is 2324 in ``app/lib/managers/settings/definitions.dart``
  (``remotePort``).
- ``GET /api/health`` is the unauthenticated document in
  ``app/lib/managers/remote/remote_manager.dart`` (``_health``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "kiosk_mgmt"
# Core integration whose device-registry rows we read. Not imported.
ESPHOME_DOMAIN: Final = "esphome"

# Sidebar. The panel title is the product name, "Kiosk Mgmt".
PANEL_TITLE: Final = "Kiosk Mgmt"
PANEL_URL: Final = "kiosk-mgmt"
PANEL_ICON: Final = "mdi:tablet"
PANEL_JS_URL: Final = f"/{DOMAIN}/kiosk-mgmt-panel.js"
PANEL_ELEMENT: Final = "kiosk-mgmt-panel"

# Dispatcher signal so a websocket subscription survives an entry reload.
SIGNAL_KIOSKS_UPDATED: Final = f"{DOMAIN}_kiosks_updated"

# Kiosk Satellite's own advertisements.
FLEET_SERVICE: Final = "_kiosk-satellite._tcp.local."
ESPHOME_SERVICE: Final = "_esphomelib._tcp.local."
# BluetoothProxyRuntime.buildIdentity.
PROJECT_NAME: Final = "kiosk_satellite.bluetooth_proxy"
# What Home Assistant stores after splitting project_name on ".".
REGISTRY_MANUFACTURER: Final = "kiosk_satellite"
REGISTRY_MODEL: Final = "bluetooth_proxy"
# node_name.dart and the empty-name fallback in BluetoothProxyRuntime.
NODE_PREFIXES: Final = ("ks-", "kiosk-satellite-")

# definitions.dart remotePort.defaultValue. The ESPHome advertisement's port
# is the API port (6053), not this one.
DEFAULT_PORT: Final = 2324
HEALTH_PATH: Final = "/api/health"

# Short, so a dark tablet does not stall the coordinator.
PROBE_TIMEOUT: Final = 5.0
UPDATE_INTERVAL: Final = timedelta(seconds=30)
# The browser cannot tell a refused certificate from a slow tablet. Eight
# seconds is long enough for a cold WebView and short enough to offer the
# trust card instead of a blank frame.
FRAME_LOAD_TIMEOUT_MS: Final = 8000

SCHEME_AUTO: Final = "auto"
SCHEME_HTTP: Final = "http"
SCHEME_HTTPS: Final = "https"
SCHEMES: Final = (SCHEME_AUTO, SCHEME_HTTP, SCHEME_HTTPS)

CONF_SCHEME: Final = "scheme"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_KIOSK_ID: Final = "kiosk_id"

# options["kiosks"] is a dict of these records, keyed by stable id.
ATTR_FLEET_ID: Final = "fleet_id"
ATTR_MAC: Final = "mac"
ATTR_NODE_NAME: Final = "node_name"
ATTR_LINK_URL: Final = "link_url"
ATTR_SCHEME_MODE: Final = "scheme_mode"
ATTR_IGNORED: Final = "ignored"
ATTR_APP_VERSION: Final = "app_version"
ATTR_BRAND: Final = "brand"
ATTR_SOURCE: Final = "source"

SERVICE_REFRESH: Final = "refresh"
SERVICE_IGNORE: Final = "ignore_kiosk"
SERVICE_REMOVE: Final = "remove_kiosk"
