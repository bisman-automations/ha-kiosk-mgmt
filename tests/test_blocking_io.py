"""No blocking I/O on the event loop (hacs/default#14090).

The review found ``KioskProxyView.get`` reading ``shim.mjs`` with
``Path.read_text`` on the event loop every time the iframe asked for the
hook script. The file is now read once, in the executor, during setup (or
on first use if that failed), and the hook is built from the cached string.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_mgmt import proxy
from custom_components.kiosk_mgmt.const import DOMAIN
from custom_components.kiosk_mgmt.proxy import async_load_shim
from custom_components.kiosk_mgmt.tls import unverified_context
from homeassistant.util.ssl import get_default_no_verify_context
from tests.fake_kiosk import serve
from tests.test_pinning import _record
from tests.test_proxy import admin_prefix

_REAL_READ = proxy._read_shim_source


class _ReadSpy:
    """Wrap ``_read_shim_source``: count calls and record which thread ran them."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.threads: list[int] = []
        self.fail = fail

    def __call__(self) -> str:
        self.calls += 1
        self.threads.append(threading.get_ident())
        if self.fail:
            raise OSError("shim.mjs is missing")
        return _REAL_READ()


async def _setup(hass: Any, host: str, port: int, fingerprint: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        data={},
        options={"kiosks": {"manual:1": _record(host, port, cert_sha256=fingerprint)}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_shim_is_read_once_in_the_executor_and_cached(
    hass: Any, hass_client: Any
) -> None:
    """Setup reads the file off the loop; serving the hook never reads it again."""
    spy = _ReadSpy()
    loop_thread = threading.get_ident()
    with patch.object(proxy, "_read_shim_source", spy):
        async with serve() as kiosk:
            entry = await _setup(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
            assert spy.calls == 1, "the shim is read during setup"
            assert loop_thread not in spy.threads, "and not on the event loop"

            prefix = await admin_prefix(hass, entry.entry_id)
            admin = await hass_client()
            for _ in range(3):
                hook = await admin.get(f"{prefix}/__kiosk_mgmt__/hook.js")
                assert hook.status == 200
                text = await hook.text()
                assert "installNetworkShim(window, __KM_CTX__)" in text
                assert prefix in text
    assert spy.calls == 1, "later requests use the cached source"


async def test_concurrent_first_use_shares_one_read(hass: Any) -> None:
    """Several requests racing on a cold cache start a single executor read."""
    hass.data.setdefault(DOMAIN, {})
    spy = _ReadSpy()
    with patch.object(proxy, "_read_shim_source", spy):
        results = await asyncio.gather(*(async_load_shim(hass) for _ in range(5)))
    assert spy.calls == 1
    assert len(set(results)) == 1
    assert "function installNetworkShim" in results[0]
    assert "export function" not in results[0]


async def test_unreadable_shim_does_not_break_setup_and_is_retried(
    hass: Any, hass_client: Any
) -> None:
    """A broken install shows a clear page, and a later good read recovers."""
    async with serve() as kiosk:
        with patch.object(proxy, "_read_shim_source", _ReadSpy(fail=True)):
            entry = await _setup(hass, kiosk.host, kiosk.port, kiosk.fingerprint)
            prefix = await admin_prefix(hass, entry.entry_id)
            admin = await hass_client()
            broken = await admin.get(f"{prefix}/__kiosk_mgmt__/hook.js")
            assert broken.status == 500
            assert "Reinstall Kiosk Mgmt" in await broken.text()

        spy = _ReadSpy()
        with patch.object(proxy, "_read_shim_source", spy):
            fixed = await admin.get(f"{prefix}/__kiosk_mgmt__/hook.js")
            assert fixed.status == 200
            assert "installNetworkShim" in await fixed.text()
        assert spy.calls == 1


def test_unverified_context_is_home_assistants_shared_one() -> None:
    """No SSLContext is built on the event loop: the prebuilt one is reused."""
    context = unverified_context()
    assert context is get_default_no_verify_context()
    assert context is unverified_context()
    assert context.check_hostname is False
