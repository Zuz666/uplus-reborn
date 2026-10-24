"""One UI-owned discovery lifetime, independent of forms and presentation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .local_discovery import ScanActivity, ScanProgress, ScanResult, async_get_collector


class DiscoveryInterval:
    """Own participation, collection, progress publication and terminal cleanup."""

    def __init__(self, hass: HomeAssistant, user_id: str | None) -> None:
        self._hass = hass
        self._user_id = user_id
        self._activity = ScanActivity()
        self._owners: dict[object, Callable[[ScanProgress], None]] = {}
        self._scan_task: asyncio.Task[ScanResult] | None = None
        self._publisher: asyncio.Task[None] | None = None
        self._shutdown_unsubscribe: Callable[[], None] | None = None
        self._changed = asyncio.Event()
        self._terminal: ScanProgress | None = None

    @property
    def progress(self) -> ScanProgress:
        """Return immutable observations; a completed interval never changes."""
        return self._terminal or self._activity.snapshot()

    async def async_subscribe(
        self, user_id: str, send: Callable[[ScanProgress], None]
    ) -> Callable[[], None]:
        """Acquire one authorized owner; the first starts collection."""
        if self._terminal is not None or user_id != self._user_id:
            raise ValueError("This discovery interval is not active for this user")
        owner = object()
        self._owners[owner] = send
        if self._scan_task is None:
            @callback
            def shutdown(_event: Any) -> None:
                self._shutdown_unsubscribe = None
                self._finish("shutdown")

            self._shutdown_unsubscribe = self._hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, shutdown
            )
            self._scan_task = self._hass.async_create_background_task(
                self._collect(), f"{DOMAIN} discovery collection", eager_start=False
            )
            self._publisher = self._hass.async_create_background_task(
                self._publish(), f"{DOMAIN} discovery progress", eager_start=False
            )

        @callback
        def unsubscribe() -> None:
            if owner not in self._owners:
                return
            self._owners.pop(owner)
            if not self._owners:
                self._finish("disconnected")

        return unsubscribe

    async def async_wait(self) -> ScanResult:
        """Wait for completion without transferring task ownership to the caller."""
        if self._scan_task is None:
            if self._terminal is None:
                raise ValueError("Discovery has no preview owner")
            return ScanResult((), self.progress.issues)
        try:
            return await asyncio.shield(self._scan_task)
        except asyncio.CancelledError:
            if not self._scan_task.cancelled():
                raise
            return ScanResult((), self.progress.issues)

    async def async_stop(self) -> ScanResult:
        """Freeze explicit results and wait for the collector to release ownership."""
        self._finish("stopped")
        if self._publisher is not None:
            try:
                await asyncio.shield(self._publisher)
            except asyncio.CancelledError:
                if not self._publisher.cancelled():
                    raise
        self._owners.clear()
        return await self.async_wait()

    @callback
    def close(self) -> None:
        """Release a removed dialog immediately; it cannot acquire owners again."""
        self._finish("closed")
        self._owners.clear()
        for task in (self._scan_task, self._publisher):
            if task is not None:
                task.cancel()

    @callback
    def _finish(self, outcome: str) -> None:
        if self._terminal is None:
            self._activity.stop.set()
            self._activity.finish(outcome)
            self._terminal = self._activity.snapshot()
            self._changed.set()
        if self._shutdown_unsubscribe is not None:
            self._shutdown_unsubscribe()
            self._shutdown_unsubscribe = None

    async def _collect(self) -> ScanResult:
        try:
            return await async_get_collector(self._hass).scan(activity=self._activity)
        except asyncio.CancelledError:
            self._finish("closed")
            raise
        except (OSError, RuntimeError, ValueError):
            if self._terminal is None:
                self._activity.issues.append("scan_failed")
                self._finish("failed")
            return ScanResult((), self.progress.issues)
        finally:
            outcome = self._activity.outcome
            self._finish("stopped" if outcome == "searching" else outcome)

    async def _publish(self) -> None:
        """Publish observations at the existing cadence, waking on completion."""
        while True:
            state = self.progress
            for send in tuple(self._owners.values()):
                send(state)
            if self._terminal is not None:
                return
            try:
                async with asyncio.timeout(1):
                    await self._changed.wait()
            except TimeoutError:
                pass
