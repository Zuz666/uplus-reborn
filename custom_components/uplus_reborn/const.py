"""Constants for the U+ Reborn integration."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "uplus_reborn"
MANUFACTURER: Final = "Haier"
MODEL: Final = "HWD120-B1558U"
SUPPORTED_WIFI_TYPE: Final = (
    "111c12002400081005010021800088410000d500000000000000000000000000"
)
DEFAULT_PORT: Final = 56800
MIN_POLL_INTERVAL: Final = 15
MAX_POLL_INTERVAL: Final = 30
DEFAULT_UPDATE_INTERVAL: Final = timedelta(seconds=15)

CONF_DEVICE_ID: Final = "device_id"
CONF_ENABLE_VERIFIED_CONTROLS: Final = "enable_verified_controls"
CONF_POLL_INTERVAL: Final = "poll_interval"

PLATFORMS: Final = (
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
)


def configured_poll_interval(options: Mapping[str, object]) -> int:
    """Return safe integer seconds without mutating malformed persisted options."""
    interval = options.get(CONF_POLL_INTERVAL)
    if type(interval) is int and MIN_POLL_INTERVAL <= interval <= MAX_POLL_INTERVAL:
        return interval
    return int(DEFAULT_UPDATE_INTERVAL.total_seconds())
