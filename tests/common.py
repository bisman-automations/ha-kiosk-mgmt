"""Shared fixtures for Kiosk Mgmt tests."""

from __future__ import annotations

from ipaddress import IPv4Address
from typing import Any

from custom_components.kiosk_mgmt.client import ProbeResult
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

HOST = "192.0.2.10"
OTHER_HOST = "192.0.2.11"
MAC = "aa:bb:cc:dd:ee:ff"

HEALTH: dict[str, Any] = {
    "name": "Kitchen tablet",
    "model": "KFTUWI",
    "brand": "Amazon",
    "androidVersion": "11",
    "sdkInt": 30,
    "appVersion": "2026.10.42",
    "ip": HOST,
    "leader": False,
    "follows": None,
    "uptime": {"app": 10, "device": 20, "network": 5},
}


def probe_ok(scheme: str = "http", **overrides: Any) -> ProbeResult:
    """Return a successful probe shaped like /api/health."""
    return ProbeResult(
        ok=True,
        scheme=scheme,
        ssl_error=False,
        name=overrides.get("name", HEALTH["name"]),
        model=overrides.get("model", HEALTH["model"]),
        brand=overrides.get("brand", HEALTH["brand"]),
        app_version=overrides.get("app_version", HEALTH["appVersion"]),
        ip=overrides.get("ip", HOST),
        cert_sha256=overrides.get("cert_sha256"),
        cert_mismatch=bool(overrides.get("cert_mismatch", False)),
    )


def probe_fail(
    *, ssl_error: bool = False, cert_sha256: str | None = None
) -> ProbeResult:
    """Return a failed probe."""
    return ProbeResult(
        ok=False,
        scheme=None,
        ssl_error=ssl_error,
        cert_sha256=cert_sha256,
    )


def fleet_service(**properties: str) -> ZeroconfServiceInfo:
    """Build a _kiosk-satellite._tcp discovery."""
    props = {
        "id": "android-1",
        "name": "Kitchen tablet",
        "version": "2026.10.42",
        "port": "2324",
    }
    props.update(properties)
    return ZeroconfServiceInfo(
        ip_address=IPv4Address(HOST),
        ip_addresses=[IPv4Address(HOST)],
        port=int(props["port"]),
        hostname="ks-kitchen.local.",
        type="_kiosk-satellite._tcp.local.",
        name="ks-android-1._kiosk-satellite._tcp.local.",
        properties=props,
    )


def esphome_service(
    *,
    project_name: str = "kiosk_satellite.bluetooth_proxy",
    name: str = "ks-amazon-kftuwi._esphomelib._tcp.local.",
    mac: str = "aabbccddeeff",
    host: str = HOST,
) -> ZeroconfServiceInfo:
    """Build an _esphomelib._tcp discovery."""
    return ZeroconfServiceInfo(
        ip_address=IPv4Address(host),
        ip_addresses=[IPv4Address(host)],
        port=6053,
        hostname="ks-amazon-kftuwi.local.",
        type="_esphomelib._tcp.local.",
        name=name,
        properties={
            "project_name": project_name,
            "project_version": "2026.10.42",
            "friendly_name": "Kitchen tablet",
            "mac": mac,
            "board": "android",
            "platform": "ESP32",
        },
    )
