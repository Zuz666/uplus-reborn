"""Local network discovery helpers for U+ Reborn."""

from __future__ import annotations

from aiodiscover import DiscoverHosts
from aiodiscover.discovery import IP_ADDRESS, MAC_ADDRESS

_MAC_HEX_LENGTH = 12
_HEX_DIGITS = frozenset("0123456789ABCDEF")


class DeviceNotFound(Exception):
    """The requested appliance was not present in the LAN discovery result."""


def device_id_from_mac(macaddress: str) -> str:
    """Convert a network MAC address to the UWT wire identity."""
    device_id = macaddress.translate(str.maketrans("", "", ":-.")).strip().upper()
    if len(device_id) != _MAC_HEX_LENGTH or not set(device_id) <= _HEX_DIGITS:
        raise ValueError("Discovery returned an invalid MAC address")
    return device_id


async def async_find_device_host(device_id: str) -> str:
    """Find the current IP address for one UWT/MAC identity."""
    target = device_id_from_mac(device_id)
    try:
        hosts = await DiscoverHosts().async_discover()
    except (AttributeError, KeyError, OSError, RuntimeError, UnicodeError) as err:
        raise DeviceNotFound("Local network discovery failed") from err

    for host in hosts:
        macaddress = host.get(MAC_ADDRESS)
        if not isinstance(macaddress, str):
            continue
        try:
            candidate = device_id_from_mac(macaddress)
        except ValueError:
            continue
        if candidate == target:
            ip_address = host.get(IP_ADDRESS)
            if isinstance(ip_address, str) and ip_address:
                return ip_address
    raise DeviceNotFound("Appliance was not found on the local network")
