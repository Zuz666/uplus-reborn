"""Tests for local MAC-to-IP discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiodiscover.discovery import IP_ADDRESS, MAC_ADDRESS

from custom_components.uplus_reborn.discovery import (
    DeviceNotFound,
    async_find_device_host,
    device_id_from_mac,
)


def test_device_id_from_mac_normalizes_common_formats() -> None:
    """DHCP and cloud identities converge on one uppercase UWT identifier."""
    assert device_id_from_mac("aa:bb:cc:dd:ee:ff") == "AABBCCDDEEFF"
    assert device_id_from_mac("AA-BB-CC-DD-EE-FF") == "AABBCCDDEEFF"


async def test_find_device_host_matches_discovered_mac() -> None:
    """LAN discovery returns only the address belonging to the selected device."""
    discover = AsyncMock(
        return_value=[
            {MAC_ADDRESS: "00:11:22:33:44:55", IP_ADDRESS: "192.0.2.10"},
            {MAC_ADDRESS: "aa:bb:cc:dd:ee:ff", IP_ADDRESS: "192.0.2.20"},
        ]
    )
    with patch(
        "custom_components.uplus_reborn.discovery.DiscoverHosts"
    ) as discover_hosts:
        discover_hosts.return_value.async_discover = discover
        host = await async_find_device_host("AABBCCDDEEFF")

    assert host == "192.0.2.20"
    discover.assert_awaited_once_with()


@pytest.mark.parametrize(
    "scanner_error",
    [
        RuntimeError("scanner failed"),
        UnicodeDecodeError("utf-8", b"\x8e", 0, 1, "invalid start byte"),
    ],
)
async def test_find_device_host_maps_scanner_failure(
    scanner_error: Exception,
) -> None:
    """Third-party scanner failures become a recoverable config-flow outcome."""
    with patch(
        "custom_components.uplus_reborn.discovery.DiscoverHosts"
    ) as discover_hosts:
        discover_hosts.return_value.async_discover = AsyncMock(
            side_effect=scanner_error
        )
        with pytest.raises(DeviceNotFound, match="discovery failed"):
            await async_find_device_host("AABBCCDDEEFF")
