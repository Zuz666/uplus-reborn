"""Binary sensors for authoritative U+ Reborn state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UplusRebornConfigEntry
from .coordinator import UplusRebornCoordinator
from .entity import UplusRebornEntity
from .protocol import WASH_OPTION_BITS, DeviceSnapshot, Drum
from .reported_settings import contextual_report


@dataclass(frozen=True, kw_only=True)
class UplusRebornBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a decoded appliance flag."""

    value_fn: Callable[[DeviceSnapshot], bool | None]


def _reported_flag(snapshot: DeviceSnapshot, drum: Drum, option: str) -> bool | None:
    """Read a contextual flag without imposing next-run UI exclusions."""
    report = contextual_report(snapshot, drum)
    if report is None:
        return None
    if option == "smart_mode":
        return report.smart
    if option == "delay_active":
        target = snapshot.upper if drum == "upper" else snapshot.lower
        return target.delay_active
    return bool(report.option_bits & (1 << WASH_OPTION_BITS[option]))


BINARY_SENSORS: tuple[UplusRebornBinarySensorEntityDescription, ...] = (
    UplusRebornBinarySensorEntityDescription(
        key="power",
        translation_key="power",
        icon="mdi:power",
        device_class=BinarySensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.power,
    ),
    UplusRebornBinarySensorEntityDescription(
        key="remote_control",
        translation_key="remote_control",
        icon="mdi:remote",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.remote_control,
    ),
    UplusRebornBinarySensorEntityDescription(
        key="local_control_lock",
        translation_key="local_control_lock",
        icon="mdi:gesture-tap-button",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.local_lock,
    ),
    UplusRebornBinarySensorEntityDescription(
        key="upper_door_lock",
        translation_key="upper_door_lock",
        icon="mdi:lock",
        value_fn=lambda snapshot: snapshot.upper.door_locked,
    ),
    UplusRebornBinarySensorEntityDescription(
        key="lower_door_lock",
        translation_key="lower_door_lock",
        icon="mdi:lock",
        value_fn=lambda snapshot: snapshot.lower.door_locked,
    ),
    *(
        UplusRebornBinarySensorEntityDescription(
            key=f"{drum}_reported_{option}",
            translation_key=f"{drum}_reported_{option}",
            icon=icon,
            value_fn=lambda snapshot, drum=drum, option=option: _reported_flag(
                snapshot, drum, option
            ),
        )
        for drum in ("upper", "lower")
        for option, icon in (
            ("high_water", "mdi:water-plus"),
            ("easy_iron", "mdi:iron-outline"),
            ("intensive", "mdi:washing-machine"),
            ("eco", "mdi:leaf"),
            ("soak", "mdi:water"),
            ("night", "mdi:weather-night"),
            ("smart_mode", "mdi:auto-fix"),
            ("delay_active", "mdi:timer-outline"),
        )
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UplusRebornConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up decoded appliance flags."""
    coordinator = entry.runtime_data
    async_add_entities(
        UplusRebornBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )


class UplusRebornBinarySensor(UplusRebornEntity, BinarySensorEntity):
    """Expose one checksum-validated appliance flag."""

    entity_description: UplusRebornBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: UplusRebornCoordinator,
        description: UplusRebornBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the current flag value."""
        return self.entity_description.value_fn(self.coordinator.data)
