"""Base entities for U+ Reborn."""

from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import UplusRebornCoordinator
from .protocol import (
    ControlResult,
    OutcomeUnknown,
    ProtocolError,
    StateChanged,
    UnsafeState,
)

_PAUSE_RESUME_REASONS = frozenset(
    (
        "controls_disabled",
        "no_snapshot",
        "power_off",
        "remote_control_off",
        "local_lock",
        "child_lock",
        "unsupported_phase",
        "not_active",
        "not_paused",
        "state_changed",
    )
)


async def async_execute_control(
    operation: Awaitable[ControlResult], *, pause_resume: bool = False
) -> ControlResult:
    """Translate outcomes, using structured reasons for explicit Pause/Resume."""
    try:
        return await operation
    except (StateChanged, UnsafeState, ValueError) as error:
        if pause_resume:
            reason = (
                "state_changed"
                if isinstance(error, StateChanged)
                else error.reason
                if isinstance(error, UnsafeState)
                else None
            )
            if reason not in _PAUSE_RESUME_REASONS:
                reason = "invalid_state"
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=f"pause_resume_{reason}",
            ) from error
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsafe_state",
            translation_placeholders={"reason": str(error)},
        ) from error
    except OutcomeUnknown as error:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="outcome_unknown"
        ) from error
    except ProtocolError as error:
        if pause_resume:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="pause_resume_control_failed",
            ) from error
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="control_failed",
            translation_placeholders={"reason": type(error).__name__},
        ) from error


class UplusRebornEntity(CoordinatorEntity[UplusRebornCoordinator]):
    """Base class linking an entity to one appliance coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UplusRebornCoordinator, key: str) -> None:
        """Initialize common identity metadata."""
        super().__init__(coordinator)
        device_id = coordinator.device_id
        connections = (
            {(CONNECTION_NETWORK_MAC, format_mac(device_id))}
            if len(device_id) == 12
            and all(character in "0123456789abcdefABCDEF" for character in device_id)
            else set()
        )
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            connections=connections,
            identifiers={(DOMAIN, device_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=MODEL,
        )
