"""Coordinate serialized local access to one U+ appliance."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from itertools import count

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_VERIFIED_CONTROLS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    configured_poll_interval,
)
from .profiles import ProfileStore, SavedProfile
from .protocol import (
    CannotConnect,
    ControlResult,
    DeviceSnapshot,
    Drum,
    InvalidFrame,
    OutcomeUnknown,
    ProgramIntent,
    ProtocolError,
    UnsafeState,
    UwtClient,
    classify_error,
    default_intent,
    fabric_intent,
    update_intent,
    validate_intent,
)

_LOGGER = logging.getLogger(__name__)
_REQUEST_IDS = count(1)


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    """One bounded, identifier-free failure record."""

    at: datetime
    cause: str
    context: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Latest command only; a failed dispatch is never retried."""

    at: datetime
    kind: str
    outcome: str
    cause: str | None = None
    retried: bool = False
    unconfirmed_settings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CoordinatorHealth:
    """Read-only cached health shared by diagnostics and System Health.

    No network access. Age is calculated at access; a failed read makes the
    retained snapshot unavailable even when its age is below the stale limit.
    """

    available: bool
    freshness: str
    last_success: datetime | None
    age_seconds: float | None
    last_error: ClassifiedError | None
    latest_command: CommandOutcome | None


class UplusRebornCoordinator(DataUpdateCoordinator[DeviceSnapshot]):
    """Own polling and one-at-a-time control access for an appliance."""

    config_entry: ConfigEntry[UplusRebornCoordinator]

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry[UplusRebornCoordinator],
        client: UwtClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=configured_poll_interval(config_entry.options)
            ),
        )
        self.client = client
        self._connection_lock = asyncio.Lock()
        self._drafts: dict[Drum, ProgramIntent | None] = {"upper": None, "lower": None}
        self.profiles = ProfileStore(hass, config_entry.entry_id, self.device_id)
        self._selected_profiles: dict[Drum, str | None] = {"upper": None, "lower": None}
        self._profile_names: dict[Drum, str] = {"upper": "", "lower": ""}
        self._last_logged_snapshot: DeviceSnapshot | None = None
        self._last_success: datetime | None = None
        self._last_error: ClassifiedError | None = None
        self._latest_command: CommandOutcome | None = None
        self._read_available = False

    @property
    def health(self) -> CoordinatorHealth:
        """Return immutable health without probing or exposing exception text."""
        age = (
            max(0.0, (datetime.now(UTC) - self._last_success).total_seconds())
            if self._last_success is not None
            else None
        )
        interval = self.update_interval or DEFAULT_UPDATE_INTERVAL
        freshness = (
            "never_read"
            if age is None
            else "unavailable"
            if not self._read_available
            else "stale"
            if age > interval.total_seconds() * 2
            else "fresh"
        )
        return CoordinatorHealth(
            available=self._read_available,
            freshness=freshness,
            last_success=self._last_success,
            age_seconds=age,
            last_error=self._last_error,
            latest_command=self._latest_command,
        )

    def _record_snapshot(self, snapshot: DeviceSnapshot, request: int) -> None:
        """Retain only the latest immutable state; build deltas only at DEBUG."""
        debug_enabled = _LOGGER.isEnabledFor(logging.DEBUG)
        previous = self._last_logged_snapshot
        if debug_enabled:
            for drum in ("upper", "lower"):
                current = getattr(snapshot, drum)
                old = getattr(previous, drum) if previous is not None else None
                delta = {
                    item.name: getattr(current, item.name)
                    for item in fields(current)
                    if old is None
                    or getattr(current, item.name) != getattr(old, item.name)
                }
                if delta:
                    _LOGGER.debug(
                        "request=%s event=%s drum=%s fields=%s",
                        request,
                        "initial_snapshot" if old is None else "delta",
                        drum,
                        delta,
                    )
            shared = {
                item.name: getattr(snapshot, item.name)
                for item in fields(snapshot)
                if item.name not in ("observed_at", "upper", "lower")
                and (
                    previous is None
                    or getattr(snapshot, item.name) != getattr(previous, item.name)
                )
            }
            if shared:
                _LOGGER.debug(
                    "request=%s event=shared_delta fields=%s", request, shared
                )
        if not self._read_available and self._last_error is not None:
            # DUC owns poll failure/recovery INFO/ERROR; this adds safe context.
            _LOGGER.debug(
                "request=%s event=recovered previous_cause=%s last_success=%s",
                request,
                self._last_error.cause,
                self._last_success,
            )
        self._last_logged_snapshot = snapshot if debug_enabled else None
        self._last_success = datetime.now(UTC)
        self._read_available = True

    def _record_failure(self, error: BaseException, request: int) -> str:
        cause = classify_error(error)
        self._last_error = ClassifiedError(
            datetime.now(UTC),
            cause,
            error.context if isinstance(error, ProtocolError) else (),
        )
        self._read_available = False
        _LOGGER.debug(
            "request=%s event=read_failed cause=%s last_success=%s",
            request,
            cause,
            self._last_success,
        )
        return cause

    @property
    def device_id(self) -> str:
        """Return the stable appliance identifier."""
        return self.config_entry.data[CONF_DEVICE_ID]

    @property
    def controls_enabled(self) -> bool:
        """Return whether the owner explicitly opted into live controls."""
        return self.config_entry.options.get(CONF_ENABLE_VERIFIED_CONTROLS) is True

    def draft(self, drum: Drum) -> ProgramIntent | None:
        """Return one immutable local selection, never reported device state."""
        if drum not in self._drafts:
            raise ValueError("Unknown drum")
        return self._drafts[drum]

    def stage_program(self, drum: Drum, program: str) -> None:
        """Restore exact program defaults without appliance traffic."""
        self.draft(drum)
        self._drafts[drum] = (
            None if program == "none" else default_intent(drum, program)
        )
        self._selected_profiles[drum] = None
        self.async_update_listeners()

    def stage_fabric(self, drum: Drum, fabric: str) -> None:
        """Stage an exact smart recipe or clear the selected drum."""
        self.draft(drum)
        self._drafts[drum] = None if fabric == "none" else fabric_intent(drum, fabric)
        self._selected_profiles[drum] = None
        self.async_update_listeners()

    def stage_settings(self, drum: Drum, **changes: object) -> None:
        """Atomically validate and replace one local draft."""
        draft = self.draft(drum)
        if draft is None:
            raise ValueError("Select a program first")
        self._drafts[drum] = update_intent(draft, **changes)
        self._selected_profiles[drum] = None
        self.async_update_listeners()

    def clear_drafts(self) -> None:
        """Discard both local drafts on reload or permission changes."""
        self._drafts = {"upper": None, "lower": None}
        self._selected_profiles = {"upper": None, "lower": None}
        self._profile_names = {"upper": "", "lower": ""}
        self.async_update_listeners()

    def selected_profile(self, drum: Drum) -> str | None:
        """Return identity only while the draft exactly matches its saved intent."""
        profile_id = self._selected_profiles[drum]
        if profile_id is None:
            return None
        try:
            profile = self.profiles.resolve(profile_id, drum)
        except ValueError:
            return None
        return profile_id if self.draft(drum) == profile.intent else None

    def apply_profile(self, drum: Drum, profile_id: str) -> None:
        """Atomically restore every field, without reads or control dispatch."""
        self.draft(drum)
        profile = self.profiles.resolve(profile_id, drum)
        if not self.controls_enabled:
            raise ValueError("Remote controls are disabled")
        self._drafts[drum] = profile.intent
        self._selected_profiles[drum] = profile.id
        self.async_update_listeners()

    def detach_profile(self, drum: Drum) -> None:
        """Detach a saved selection without discarding the independent draft."""
        self.draft(drum)
        self._selected_profiles[drum] = None
        self.async_update_listeners()

    def profile_name(self, drum: Drum) -> str:
        """Return transient name input, separate from profiles and drafts."""
        return self._profile_names[drum]

    def set_profile_name(self, drum: Drum, name: str) -> None:
        """Edit only the local name input; validation of a saved name is explicit."""
        self.draft(drum)
        if not self.controls_enabled:
            raise ValueError("Remote controls are disabled")
        if len(name) > 100:
            raise ValueError("Use a profile name of at most 100 characters")
        self._profile_names[drum] = name
        self.async_update_listeners()

    async def async_save_profile(self, drum: Drum, name: str) -> SavedProfile:
        """Snapshot one complete draft before awaiting durable create-only save."""
        if not self.controls_enabled:
            raise ValueError("Remote controls are disabled")
        intent = self.draft(drum)
        if intent is None:
            raise ValueError("Select a complete program or smart fabric draft first")
        profile = await self.profiles.async_save(name, intent)
        self.async_update_listeners()
        return profile

    async def async_start_profile(self, drum: Drum, profile_id: str) -> ControlResult:
        """Resolve one revision before queuing; UI/store edits cannot change it."""
        profile = self.profiles.resolve(profile_id, drum)
        return await self.async_start_program(drum, profile.intent)

    async def _async_update_data(self) -> DeviceSnapshot:
        """Fetch one fresh, checksum-validated appliance snapshot."""
        request = next(_REQUEST_IDS)
        async with self._connection_lock:
            try:
                snapshot = await self.client.read_state()
            except ProtocolError as error:
                cause = self._record_failure(error, request)
                # DUC logs this message: never forward arbitrary exception text.
                raise UpdateFailed(
                    f"Unable to read appliance state ({cause})"
                ) from None
            self._record_snapshot(snapshot, request)
            return snapshot

    async def async_start_program(
        self, drum: Drum, intent: ProgramIntent | None = None
    ) -> ControlResult:
        """Start an atomic supplied intent or consume the selected drum draft."""
        self.draft(drum)
        if intent is not None:
            validate_intent(intent)
            if intent.drum != drum:
                raise ValueError("Intent drum does not match the target")

        async def execute() -> ControlResult:
            selected = intent if intent is not None else self.draft(drum)
            if selected is None:
                raise UnsafeState("Select a program first", reason="program_not_staged")
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "kind=%s_start event=intent fields=%s",
                    drum,
                    {
                        item.name: getattr(selected, item.name)
                        for item in fields(selected)
                    },
                )
            if intent is None:
                self._drafts[drum] = None
                self.async_update_listeners()
            return await self.client.start_program(
                selected, allow_live=True, expected=self.data
            )

        return await self._async_control(f"{drum}_start", execute)

    async def async_control_drum(self, drum: Drum, action: str) -> ControlResult:
        """Execute an explicit per-drum lifecycle action once."""
        self.draft(drum)
        return await self._async_control(
            f"{drum}_{action}",
            lambda: self.client.control_drum(
                drum, action, allow_live=True, expected=self.data
            ),
        )

    async def async_set_appliance_option(
        self, option: str, enabled: bool
    ) -> ControlResult:
        """Change an actual appliance setting, not a staged cycle option."""
        return await self._async_control(
            option,
            lambda: self.client.set_appliance_option(
                option, enabled, allow_live=True, expected=self.data
            ),
        )

    async def _async_control(
        self, kind: str, execute: Callable[[], Awaitable[ControlResult]]
    ) -> ControlResult:
        request = next(_REQUEST_IDS)
        async with self._connection_lock:
            try:
                if not self.controls_enabled:
                    raise UnsafeState(
                        "Remote controls are disabled", reason="controls_disabled"
                    )
                if self.data is None:
                    raise UnsafeState("No appliance snapshot", reason="no_snapshot")
                result = await execute()
            except (ProtocolError, asyncio.CancelledError) as error:
                cause = classify_error(error)
                unknown = isinstance(error, OutcomeUnknown) or (
                    isinstance(error, asyncio.CancelledError)
                    and bool(getattr(error, "control_dispatched", False))
                )
                outcome = "unknown" if unknown else "not_dispatched"
                self._latest_command = CommandOutcome(
                    datetime.now(UTC), kind, outcome, cause
                )
                _LOGGER.debug(
                    "request=%s kind=%s event=%s cause=%s retried=false",
                    request,
                    kind,
                    outcome,
                    cause,
                )
                if unknown or isinstance(error, (CannotConnect, InvalidFrame)):
                    self._record_failure(error, request)
                    self.async_set_update_error(
                        UpdateFailed(f"Authoritative state unavailable ({cause})")
                    )
                raise
            self._latest_command = CommandOutcome(
                datetime.now(UTC),
                kind,
                "confirmed",
                unconfirmed_settings=result.unconfirmed_settings,
            )
            _LOGGER.debug(
                "request=%s kind=%s event=confirmed unconfirmed_settings=%s",
                request,
                kind,
                result.unconfirmed_settings,
            )
            self._record_snapshot(result.after, request)
            self.async_set_updated_data(result.after)
            return result
