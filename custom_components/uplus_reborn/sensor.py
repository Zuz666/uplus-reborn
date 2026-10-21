"""Sensors for authoritative U+ Reborn state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UplusRebornConfigEntry
from .coordinator import UplusRebornCoordinator
from .entity import UplusRebornEntity
from .protocol import (
    DRYING_MODE_OPTIONS,
    LOWER_PROGRAM_OPTIONS,
    PHASE_OPTIONS,
    PROGRAM_CAPABILITIES,
    UNKNOWN_VALUE,
    UPPER_PROGRAM_OPTIONS,
    DeviceSnapshot,
    Drum,
    program_drying_modes,
)
from .reported_settings import contextual_report

type SensorValue = float | int | str | None


@dataclass(frozen=True, kw_only=True)
class UplusRebornSensorEntityDescription(SensorEntityDescription):
    """Describe one decoded appliance value."""

    value_fn: Callable[[DeviceSnapshot], SensorValue]


def _reported_value(
    snapshot: DeviceSnapshot, drum: Drum, report_field: str, default_field: str
) -> int | None:
    """Read an applicable scalar, including fixed non-editable settings."""
    report = contextual_report(snapshot, drum)
    if report is None:
        return None
    target = snapshot.upper if drum == "upper" else snapshot.lower
    defaults = PROGRAM_CAPABILITIES[drum][target.program].default
    if getattr(defaults, default_field) is None:
        return None
    return getattr(report, report_field)


def _reported_drying_mode(snapshot: DeviceSnapshot) -> str | None:
    """Map the actual lower drying byte only for a drying-capable program."""
    report = contextual_report(snapshot, "lower")
    if report is None or not program_drying_modes(snapshot.lower.program):
        return None
    return next(
        (mode.name for mode in DRYING_MODE_OPTIONS if mode.value == report.drying_value),
        None,
    )


def _execution_state(snapshot: DeviceSnapshot, drum: Drum) -> str | None:
    """Distinguish cycle execution from its retained program phase."""
    target = snapshot.upper if drum == "upper" else snapshot.lower
    if target.phase not in PHASE_OPTIONS:
        return None
    if target.phase in ("drying_finished", "completed"):
        return "completed"
    if target.phase in ("standby", "delayed_start"):
        if target.delay_active:
            return "delayed"
        if target.phase == "standby" and not target.cycle_active:
            return "standby"
        return None
    return "running" if target.cycle_active else "paused"


SENSORS: tuple[UplusRebornSensorEntityDescription, ...] = (
    UplusRebornSensorEntityDescription(
        key="upper_program",
        translation_key="upper_program",
        device_class=SensorDeviceClass.ENUM,
        options=[*UPPER_PROGRAM_OPTIONS, UNKNOWN_VALUE],
        value_fn=lambda snapshot: snapshot.upper.program,
    ),
    UplusRebornSensorEntityDescription(
        key="lower_program",
        translation_key="lower_program",
        device_class=SensorDeviceClass.ENUM,
        options=[*LOWER_PROGRAM_OPTIONS, UNKNOWN_VALUE],
        value_fn=lambda snapshot: snapshot.lower.program,
    ),
    UplusRebornSensorEntityDescription(
        key="upper_phase",
        translation_key="upper_phase",
        device_class=SensorDeviceClass.ENUM,
        options=[*PHASE_OPTIONS, UNKNOWN_VALUE],
        value_fn=lambda snapshot: snapshot.upper.phase,
    ),
    UplusRebornSensorEntityDescription(
        key="lower_phase",
        translation_key="lower_phase",
        device_class=SensorDeviceClass.ENUM,
        options=[*PHASE_OPTIONS, UNKNOWN_VALUE],
        value_fn=lambda snapshot: snapshot.lower.phase,
    ),
    *(
        UplusRebornSensorEntityDescription(
            key=f"{drum}_execution_state",
            translation_key=f"{drum}_execution_state",
            device_class=SensorDeviceClass.ENUM,
            options=["running", "paused", "standby", "completed", "delayed"],
            icon="mdi:state-machine",
            value_fn=lambda snapshot, drum=drum: _execution_state(snapshot, drum),
        )
        for drum in ("upper", "lower")
    ),
    UplusRebornSensorEntityDescription(
        key="upper_remaining_time",
        translation_key="upper_remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda snapshot: snapshot.upper.remaining_minutes,
    ),
    UplusRebornSensorEntityDescription(
        key="lower_remaining_time",
        translation_key="lower_remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda snapshot: snapshot.lower.remaining_minutes,
    ),
    UplusRebornSensorEntityDescription(
        key="upper_cycle_count",
        translation_key="upper_cycle_count",
        native_unit_of_measurement="cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.upper.cycle_count,
    ),
    UplusRebornSensorEntityDescription(
        key="lower_cycle_count",
        translation_key="lower_cycle_count",
        native_unit_of_measurement="cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.lower.cycle_count,
    ),
    UplusRebornSensorEntityDescription(
        key="session_water",
        translation_key="session_water",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.current_water_raw,
    ),
    UplusRebornSensorEntityDescription(
        key="cumulative_water",
        translation_key="cumulative_water",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.total_water_raw,
    ),
    UplusRebornSensorEntityDescription(
        key="session_energy",
        translation_key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: (
            float(snapshot.current_electricity_raw)
            if snapshot.current_electricity_raw is not None
            else None
        ),
    ),
    UplusRebornSensorEntityDescription(
        key="cumulative_energy",
        translation_key="cumulative_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: (
            float(snapshot.total_electricity_raw)
            if snapshot.total_electricity_raw is not None
            else None
        ),
    ),
    *(
        description
        for drum in ("upper", "lower")
        for description in (
            UplusRebornSensorEntityDescription(
                key=f"{drum}_reported_temperature",
                translation_key=f"{drum}_reported_temperature",
                device_class=SensorDeviceClass.TEMPERATURE,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                icon="mdi:thermometer",
                value_fn=lambda snapshot, drum=drum: _reported_value(
                    snapshot, drum, "temperature_c", "temperature_c"
                ),
            ),
            UplusRebornSensorEntityDescription(
                key=f"{drum}_reported_wash_stage_time",
                translation_key=f"{drum}_reported_wash_stage_time",
                device_class=SensorDeviceClass.DURATION,
                native_unit_of_measurement=UnitOfTime.MINUTES,
                icon="mdi:washing-machine",
                value_fn=lambda snapshot, drum=drum: _reported_value(
                    snapshot, drum, "wash_minutes_encoded", "wash_minutes"
                ),
            ),
            UplusRebornSensorEntityDescription(
                key=f"{drum}_reported_rinses",
                translation_key=f"{drum}_reported_rinses",
                icon="mdi:water-sync",
                value_fn=lambda snapshot, drum=drum: _reported_value(
                    snapshot, drum, "rinses", "rinses"
                ),
            ),
            UplusRebornSensorEntityDescription(
                key=f"{drum}_reported_spin_stage_time",
                translation_key=f"{drum}_reported_spin_stage_time",
                device_class=SensorDeviceClass.DURATION,
                native_unit_of_measurement=UnitOfTime.MINUTES,
                icon="mdi:timer-outline",
                value_fn=lambda snapshot, drum=drum: _reported_value(
                    snapshot, drum, "spin_minutes", "spin_rpm"
                ),
            ),
        )
    ),
    UplusRebornSensorEntityDescription(
        key="lower_reported_drying_mode",
        translation_key="lower_reported_drying_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[mode.name for mode in DRYING_MODE_OPTIONS],
        icon="mdi:tumble-dryer",
        value_fn=_reported_drying_mode,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UplusRebornConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up decoded appliance sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        UplusRebornSensor(coordinator, description) for description in SENSORS
    )


class UplusRebornSensor(UplusRebornEntity, SensorEntity):
    """Expose one decoded value from the shared appliance snapshot."""

    entity_description: UplusRebornSensorEntityDescription

    def __init__(
        self,
        coordinator: UplusRebornCoordinator,
        description: UplusRebornSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> SensorValue:
        """Return the current decoded value."""
        return self.entity_description.value_fn(self.coordinator.data)
