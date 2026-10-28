"""Volatile per-entry captures sharing native integration-wide logging.

Public logger helpers update both the integration logger and Core's native-menu
logger. No Core logger settings/storage is accessed. Notifications are edge
observations, not persistent authorization to resume a capture after setup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING

from homeassistant.components.logger.helpers import set_log_levels
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_LOGGING_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.setup import async_setup_component
from homeassistant.util.hass_dict import HassKey

from .capture_options import CapturePreferences, capture_preferences
from .const import CONF_ENABLE_VERIFIED_CONTROLS, DOMAIN, configured_poll_interval

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import UplusRebornCoordinator

_LOGGERS = (
    logging.getLogger(f"custom_components.{DOMAIN}"),
    logging.getLogger(f"homeassistant.components.{DOMAIN}"),
)
_MANAGER: HassKey[CaptureManager] = HassKey(f"{DOMAIN}.capture")


@dataclass(slots=True)
class _CaptureStart:
    """One save's immutable preferences and cancellable, nonpersistent consent."""

    owner: DiagnosticCapture
    preferences: CapturePreferences
    stop_revision: int
    reload_required: bool = False


@dataclass(frozen=True, slots=True)
class _LoggingState:
    """Explicit levels identify native NOTSET even when root DEBUG is inherited."""

    package_level: int
    menu_level: int
    package_debug: bool
    menu_debug: bool


class DiagnosticCapture:
    """Own a store's finite collection lifetime, not its appliance operation."""

    def __init__(self, coordinator: UplusRebornCoordinator) -> None:
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.started_at: datetime | None = None
        self.end_at: datetime | None = None
        self.stopped_at: datetime | None = None
        self.stop_reason: str | None = None
        self._deadline: float | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._generation = 0
        self._closed = False
        manager = self.hass.data.get(_MANAGER)
        if manager is None:
            manager = self.hass.data[_MANAGER] = CaptureManager(self.hass)
        self.manager = manager
        manager.captures[coordinator.config_entry.entry_id] = self

    @callback
    def start(self, preferences: CapturePreferences) -> None:
        """Replace previous evidence and reject a stale timer's later expiry."""
        if self._closed:
            raise HomeAssistantError("Diagnostic entry is no longer loaded")
        self._cancel_deadline()
        self._generation += 1
        generation = self._generation
        self.started_at = datetime.now(UTC)
        self.end_at = self.started_at + timedelta(seconds=preferences.duration)
        self.stopped_at = None
        self.stop_reason = None
        self._deadline = self.hass.loop.time() + preferences.duration
        self.coordinator.trace.start(preferences)

        @callback
        def expire(_now: datetime) -> None:
            if not self._closed and generation == self._generation:
                self.stop(reason="deadline")

        self._cancel_timer = async_call_later(self.hass, preferences.duration, expire)

    @callback
    def _cancel_deadline(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    @callback
    def stop(self, *, reason: str | None = None) -> None:
        """Freeze data and the first actual stop, without guessing its origin."""
        if self._closed or not self.coordinator.trace.active:
            return
        self.stopped_at = datetime.now(UTC)
        self.stop_reason = reason
        self._cancel_deadline()
        self._generation += 1
        self.coordinator.trace.stop()
        self.manager.restore_if_idle()

    @callback
    def close(self) -> None:
        """Permanently discard volatile consent, evidence and timer ownership."""
        if self._closed:
            return
        self._closed = True
        self._cancel_deadline()
        self._generation += 1
        self.coordinator.trace.close()
        self.started_at = None
        self.end_at = None
        self.stopped_at = None
        self.stop_reason = None
        self._deadline = None
        entry_id = self.coordinator.config_entry.entry_id
        if self.manager.captures.get(entry_id) is self:
            self.manager.captures.pop(entry_id)
        self.manager.restore_if_idle()
        if not self.manager.captures and not self.manager.saving_options:
            self.manager.close()

    @property
    def status(self) -> dict[str, object]:
        """Snapshot capacity/timing without exporting events or raw buffers."""
        remaining = (
            max(0, ceil(self._deadline - self.hass.loop.time()))
            if self.coordinator.trace.active and self._deadline is not None
            else 0 if self.started_at is not None else None
        )
        return {
            **self.coordinator.trace.summary(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "stop_reason": self.stop_reason,
            "debug_enabled": self.coordinator.logger.isEnabledFor(logging.DEBUG),
            "remaining_seconds": remaining,
            "snapshot_at": datetime.now(UTC).isoformat(),
        }


class CaptureManager:
    """Own only shared logging; never merge entries' evidence or deadlines."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.captures: dict[str, DiagnosticCapture] = {}
        self.saving_options: dict[str, _CaptureStart] = {}
        self._owned_levels: dict[str, int] | None = None
        self._last_logging = self._logging_state()
        self._stop_revision = 0
        self._changing_logging = False
        self._own_notification_seen = False
        self._deferred_own_state: _LoggingState | None = None
        self._remove_logger: Callable[[], None] | None = hass.bus.async_listen(
            EVENT_LOGGING_CHANGED, self._logging_changed
        )
        self._remove_stop: Callable[[], None] | None = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._shutdown
        )

    @staticmethod
    def _logging_state() -> _LoggingState:
        package, menu = _LOGGERS
        return _LoggingState(
            package.level, menu.level,
            package.isEnabledFor(logging.DEBUG), menu.isEnabledFor(logging.DEBUG),
        )

    @callback
    def _set_levels(self, levels: dict[str, int]) -> None:
        # HA may invoke callback listeners synchronously inside async_fire.
        self._changing_logging = True
        self._own_notification_seen = False
        try:
            set_log_levels(self.hass, levels)
        finally:
            self._last_logging = self._logging_state()
            if not self._own_notification_seen:
                self._deferred_own_state = self._last_logging
            self._changing_logging = False

    def check_consent(self, entry_id: str, consent: _CaptureStart) -> None:
        """Reject a superseded request or any native stop since its confirmation."""
        if (
            consent.stop_revision != self._stop_revision
            or self.saving_options.get(entry_id) is not consent
        ):
            raise HomeAssistantError("Diagnostic start consent was cancelled or superseded")

    async def async_prepare_start(self, entry_id: str, consent: _CaptureStart) -> None:
        """Expose explicit native DEBUG before any awaited reload can be stopped."""
        if not await async_setup_component(self.hass, "logger", {}):
            raise HomeAssistantError("Native logger could not be initialized")
        if self.hass.is_stopping or consent.owner._closed:
            raise HomeAssistantError("Diagnostic entry is no longer loaded")
        self.check_consent(entry_id, consent)
        changes = {
            logger.name: logger.level
            for logger in _LOGGERS
            if logger.level == logging.NOTSET or not logger.isEnabledFor(logging.DEBUG)
        }
        if changes:
            self._owned_levels = {**(self._owned_levels or {}), **changes}
            self._set_levels(dict.fromkeys(changes, logging.DEBUG))
        if not _LOGGERS[0].isEnabledFor(logging.DEBUG):
            self._owned_levels = None
            raise HomeAssistantError("Logging was explicitly stopped during capture start")

    @callback
    def start(self, capture: DiagnosticCapture, consent: _CaptureStart) -> None:
        """No await separates final consent/logging checks from recording."""
        self.check_consent(capture.coordinator.config_entry.entry_id, consent)
        if (
            self.hass.is_stopping or capture._closed or consent.reload_required
            or capture.coordinator.config_entry.state is not ConfigEntryState.LOADED
            or capture.coordinator.config_entry.disabled_by is not None
        ):
            raise HomeAssistantError("Diagnostic entry is no longer ready")
        if any(
            logger.level == logging.NOTSET or not logger.isEnabledFor(logging.DEBUG)
            for logger in _LOGGERS
        ):
            raise HomeAssistantError("Logging was explicitly stopped during capture start")
        capture.start(consent.preferences)

    @callback
    def _logging_changed(self, event: Event) -> None:
        previous, current = self._last_logging, self._logging_state()
        self._last_logging = current
        if self._changing_logging:
            self._own_notification_seen = True
            return
        if self._deferred_own_state is not None:
            expected, self._deferred_own_state = self._deferred_own_state, None
            if current == expected:
                return
        # Core omits provenance, including same-level native persistence choices.
        # External ambiguity relinquishes restoration, never capture/timer state.
        self._owned_levels = None
        if previous == current:
            return
        cleared_overrides = (
            current.package_level == current.menu_level == logging.NOTSET
            and (previous.package_level != logging.NOTSET or previous.menu_level != logging.NOTSET)
        )
        if (
            not current.package_debug
            or (previous.menu_debug and not current.menu_debug)
            or cleared_overrides
        ):
            self._stop_revision += 1
            for capture in tuple(self.captures.values()):
                capture.stop(reason="debug_disabled")
        elif (
            (current.package_level != logging.NOTSET and current.package_debug)
            or (current.menu_level != logging.NOTSET and current.menu_debug)
        ) and (
            not previous.package_debug
            or (not previous.menu_debug and current.menu_debug)
            or (current.package_level == logging.DEBUG and previous.package_level != logging.DEBUG)
            or (current.menu_level == logging.DEBUG and previous.menu_level != logging.DEBUG)
        ):
            for capture in tuple(self.captures.values()):
                if capture.coordinator.config_entry.state is ConfigEntryState.LOADED:
                    capture.start(capture_preferences(capture.coordinator.config_entry.options))

    @callback
    def restore_if_idle(self) -> None:
        if self._owned_levels is None or self.saving_options or any(
            capture.coordinator.trace.active for capture in self.captures.values()
        ):
            return
        previous, self._owned_levels = self._owned_levels, None
        # Do not overwrite a newer explicit setting, even before its bus callback.
        if all(logging.getLogger(name).level == logging.DEBUG for name in previous):
            self._set_levels(previous)

    @callback
    def _shutdown(self, event: Event) -> None:
        # Core already consumed this one-shot listener before calling us.
        self._remove_stop = None
        self._stop_revision += 1
        self.saving_options.clear()
        for capture in tuple(self.captures.values()):
            capture.close()
        self.restore_if_idle()
        if not self.captures:
            self.close()

    @callback
    def close(self) -> None:
        if self._remove_logger is not None:
            remove, self._remove_logger = self._remove_logger, None
            remove()
        if self._remove_stop is not None:
            remove, self._remove_stop = self._remove_stop, None
            remove()
        if self.hass.data.get(_MANAGER) is self:
            self.hass.data.pop(_MANAGER)


async def async_save_capture_options(
    hass: HomeAssistant,
    entry: ConfigEntry[UplusRebornCoordinator],
    options: dict[str, object],
    start: bool,
) -> None:
    """Save preferences; explicitly carry a checked start across a required reload.

    A checked start owns this save's reload rather than racing the ordinary entry
    listener. Consent remains on this call's stack, never in persisted options.
    """
    if not start:
        hass.config_entries.async_update_entry(entry, options=options)
        return
    if entry.state is not ConfigEntryState.LOADED or entry.disabled_by is not None:
        raise HomeAssistantError("Load the integration before starting capture")
    coordinator = entry.runtime_data
    if coordinator.capture is None:
        raise HomeAssistantError("Diagnostic entry is not ready")
    manager = coordinator.capture.manager
    needs_reload = coordinator.controls_enabled != (
        options.get(CONF_ENABLE_VERIFIED_CONTROLS) is True
    )
    consent = _CaptureStart(
        coordinator.capture, capture_preferences(options), manager._stop_revision,
        reload_required=needs_reload,
    )
    manager.saving_options[entry.entry_id] = consent
    try:
        await manager.async_prepare_start(entry.entry_id, consent)
        if entry.state is not ConfigEntryState.LOADED or entry.disabled_by is not None:
            raise HomeAssistantError("Integration is not loaded; capture did not start")
        if needs_reload:
            coordinator.clear_drafts()
        hass.config_entries.async_update_entry(entry, options=options)
        while consent.reload_required:
            manager.check_consent(entry.entry_id, consent)
            consent.reload_required = False
            if not await hass.config_entries.async_reload(entry.entry_id):
                raise HomeAssistantError("Integration reload failed; capture did not start")
        if entry.state is not ConfigEntryState.LOADED or entry.disabled_by is not None:
            raise HomeAssistantError("Integration is not loaded; capture did not start")
        entry.runtime_data.update_interval = timedelta(
            seconds=configured_poll_interval(entry.options)
        )
        capture = entry.runtime_data.capture
        if capture is None:
            raise HomeAssistantError("Diagnostic entry is not ready")
        capture.manager.start(capture, consent)
    finally:
        if manager.saving_options.get(entry.entry_id) is consent:
            manager.saving_options.pop(entry.entry_id)
            if consent.reload_required and entry.state in (
                ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS,
            ):
                hass.config_entries.async_schedule_reload(entry.entry_id)
        manager.restore_if_idle()
        if not manager.captures and not manager.saving_options:
            manager.close()
