"""Certificate pinning: first use, a match, a mismatch, and migration."""

from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_mgmt.client import async_probe
from custom_components.kiosk_mgmt.const import DOMAIN
from custom_components.kiosk_mgmt.tls import format_fingerprint
from homeassistant.config_entries import SOURCE_USER
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from tests.common import HEALTH, probe_ok
from tests.fake_kiosk import make_certificate, serve
from tests.test_config_flow import _patch_probe


def _record(host: str, port: int, **overrides: Any) -> dict[str, Any]:
    """Return one stored kiosk aimed at a test server."""
    record = {
        "name": "Kitchen tablet",
        "host": host,
        "port": port,
        "scheme": "https",
        "scheme_mode": "https",
        "verify_ssl": False,
        "cert_sha256": None,
        "model": "KFTUWI",
        "brand": "Amazon",
        "mac": None,
        "fleet_id": None,
        "node_name": None,
        "app_version": "2026.10.42",
        "ignored": False,
        "link_url": None,
        "source": "manual",
    }
    record.update(overrides)
    return record


async def test_probe_pins_on_first_use_and_rejects_a_different_certificate(
    hass: Any, socket_enabled: None
) -> None:
    """First contact returns the leaf. The same leaf matches. Another does not."""
    async with serve() as first:
        observed = await async_probe(
            hass, first.host, first.port, "https", verify_ssl=False
        )
        assert observed.ok is True
        assert observed.cert_sha256 == first.fingerprint
        assert observed.ssl_error is False
        assert observed.name == HEALTH["name"]

        matched = await async_probe(
            hass,
            first.host,
            first.port,
            "https",
            verify_ssl=False,
            pinned_sha256=first.fingerprint,
        )
        assert matched.ok is True
        assert matched.cert_mismatch is False
        assert matched.cert_sha256 == first.fingerprint

    other_cert = make_certificate()
    async with serve(other_cert) as second:
        mismatched = await async_probe(
            hass,
            second.host,
            second.port,
            "https",
            verify_ssl=False,
            pinned_sha256=first.fingerprint,
        )
        assert mismatched.ok is False
        assert mismatched.cert_mismatch is True
        assert mismatched.ssl_error is True
        assert mismatched.cert_sha256 == second.fingerprint
        assert mismatched.name == HEALTH["name"]


async def test_authority_check_reports_the_self_signed_leaf(
    hass: Any, socket_enabled: None
) -> None:
    """Verify SSL certificate still fails a self-signed leaf and shows its pin."""
    async with serve() as kiosk:
        result = await async_probe(
            hass, kiosk.host, kiosk.port, "https", verify_ssl=True
        )
        assert result.ok is False
        assert result.ssl_error is True
        assert result.cert_mismatch is False
        assert result.cert_sha256 == kiosk.fingerprint


async def test_migration_pins_on_the_next_successful_connect(
    hass: Any, socket_enabled: None
) -> None:
    """A 1.1 entry with verify_ssl false records the leaf on the next probe."""
    async with serve() as kiosk:
        entry = MockConfigEntry(
            domain=DOMAIN,
            version=1,
            minor_version=1,
            data={},
            options={
                "kiosks": {
                    "manual:1": _record(kiosk.host, kiosk.port, cert_sha256=None)
                }
            },
        )
        # The stored record from 1.1 has no cert_sha256 key at all.
        entry.options["kiosks"]["manual:1"].pop("cert_sha256")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.version == 1
        assert entry.minor_version == 2
        record = entry.options["kiosks"]["manual:1"]
        assert record["verify_ssl"] is False
        assert record["cert_sha256"] == kiosk.fingerprint
        state = hass.states.get("binary_sensor.kitchen_tablet_connectivity")
        assert state is not None
        assert state.state == "on"
        issues = [
            issue
            for issue in ir.async_get(hass).issues.values()
            if issue.domain == DOMAIN
        ]
        assert issues == []


async def test_changed_certificate_raises_a_fixable_repair(
    hass: Any, socket_enabled: None
) -> None:
    """A new leaf does not replace the pin, and the repair can accept it."""
    first_cert = make_certificate()
    second_cert = make_certificate()
    assert first_cert[2] != second_cert[2]
    async with serve(first_cert) as original, serve(second_cert) as changed:
        entry = MockConfigEntry(
            domain=DOMAIN,
            version=1,
            minor_version=2,
            data={},
            options={
                "kiosks": {
                    "manual:1": _record(
                        original.host,
                        original.port,
                        cert_sha256=original.fingerprint,
                    )
                }
            },
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert (
            entry.options["kiosks"]["manual:1"]["cert_sha256"] == original.fingerprint
        )

        entry.options["kiosks"]["manual:1"]["port"] = changed.port
        coordinator = entry.runtime_data
        coordinator.records = {
            "manual:1": dict(entry.options["kiosks"]["manual:1"]),
        }
        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert (
            entry.options["kiosks"]["manual:1"]["cert_sha256"] == original.fingerprint
        )
        state = coordinator.data["manual:1"]
        assert state.online is False
        assert state.cert_mismatch is True
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, "certificate_changed_manual_1"
        )
        assert issue is not None
        assert issue.is_fixable is True
        assert issue.translation_key == "certificate_changed"
        assert format_fingerprint(original.fingerprint) in (
            issue.translation_placeholders or {}
        ).get("fingerprint", "")
        assert format_fingerprint(changed.fingerprint) in (
            issue.translation_placeholders or {}
        ).get("new_fingerprint", "")

        assert await async_setup_component(hass, "repairs", {})
        await hass.async_block_till_done()
        result = await hass.data["repairs"]["flow_manager"].async_init(
            DOMAIN,
            context={"issue_id": "certificate_changed_manual_1"},
        )
        assert result["type"] == "form", result
        assert result["step_id"] == "init"
        assert (
            format_fingerprint(changed.fingerprint)
            in result["description_placeholders"]["new_fingerprint"]
        )
        result = await hass.data["repairs"]["flow_manager"].async_configure(
            result["flow_id"], {}
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
        assert entry.options["kiosks"]["manual:1"]["cert_sha256"] == changed.fingerprint
        assert coordinator.data["manual:1"].online is True
        assert (
            ir.async_get(hass).async_get_issue(DOMAIN, "certificate_changed_manual_1")
            is None
        )


async def test_config_flow_shows_the_fingerprint_before_storing_it(
    hass: Any, monkeypatch: Any
) -> None:
    """Manual setup of a self-signed tablet shows the pin and then stores it."""
    fingerprint = "ab" * 32
    _patch_probe(monkeypatch, probe_ok(scheme="https", cert_sha256=fingerprint))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "host": "192.0.2.10",
            "port": 2324,
            "scheme": "https",
            "verify_ssl": False,
        },
    )
    assert result["type"] == "form"
    assert result["step_id"] == "pin_certificate"
    assert result["description_placeholders"]["fingerprint"] == format_fingerprint(
        fingerprint
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    record = next(iter(result["options"]["kiosks"].values()))
    assert record["cert_sha256"] == fingerprint
    assert record["verify_ssl"] is False
    assert result["minor_version"] == 2


async def test_reconfigure_accepts_a_new_fingerprint(
    hass: Any, monkeypatch: Any
) -> None:
    """Reconfigure offers the new fingerprint instead of the old ssl_error."""
    old = "cd" * 32
    new = "ef" * 32
    from custom_components.kiosk_mgmt.client import ProbeResult

    mismatch = ProbeResult(
        ok=False,
        scheme="https",
        ssl_error=True,
        cert_sha256=new,
        cert_mismatch=True,
        name="Kitchen tablet",
        model="KFTUWI",
    )
    _patch_probe(monkeypatch, mismatch)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        data={},
        options={
            "kiosks": {
                "manual:1": _record("192.0.2.10", 2324, cert_sha256=old),
            }
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.config_entries import SOURCE_RECONFIGURE

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["step_id"] == "reconfigure_connection"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "host": "192.0.2.10",
            "port": 2324,
            "scheme": "https",
            "verify_ssl": False,
        },
    )
    assert result["type"] == "form"
    assert result["step_id"] == "accept_certificate"
    placeholders = result["description_placeholders"]
    assert placeholders["fingerprint"] == format_fingerprint(new)
    assert placeholders["old_fingerprint"] == format_fingerprint(old)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.options["kiosks"]["manual:1"]["cert_sha256"] == new
