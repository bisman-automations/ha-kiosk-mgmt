"""Pytest fixtures for Kiosk Mgmt."""

from __future__ import annotations

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: Generator[None],
) -> Generator[None]:
    """Load custom integrations for every test."""
    yield
