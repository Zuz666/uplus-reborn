"""Entry-bound saved intents, independent of transient next-run drafts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, fields
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .protocol import Drum, ProgramIntent, validate_intent

_INTENT_VERSION = 1
_INTENT_FIELDS = frozenset(item.name for item in fields(ProgramIntent))
_RESERVED_NAMES = frozenset({"none", "unknown", "unavailable"})


def _name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 100 or value.casefold() in _RESERVED_NAMES:
        raise ValueError(
            "Use a profile name of 1–100 characters, other than None, Unknown or Unavailable"
        )
    return value


def _decode_intent(data: dict[str, Any]) -> ProgramIntent:
    """Require a complete stored intent; never fill gaps with current defaults."""
    if not isinstance(data, dict) or data.keys() != _INTENT_FIELDS:
        raise ValueError("The stored intent has unsupported or missing fields")
    values = dict(data)
    for key in ("enabled_functions", "stains"):
        if not isinstance(values[key], list) or any(
            type(item) is not str for item in values[key]
        ):
            raise ValueError(f"Invalid stored {key}")
        values[key] = tuple(values[key])
    if values["drum"] not in ("upper", "lower") or type(values["program"]) is not str:
        raise ValueError("Invalid stored program or drum")
    if values["fabric"] is not None and type(values["fabric"]) is not str:
        raise ValueError("Invalid stored fabric")
    if values["drying_mode"] is not None and type(values["drying_mode"]) is not str:
        raise ValueError("Invalid stored drying mode")
    intent = ProgramIntent(**values)
    validate_intent(intent)
    return intent


@dataclass(frozen=True, slots=True)
class SavedProfile:
    """One immutable revision; incompatible records remain manageable."""

    id: str
    name: str
    drum: Drum
    revision: int
    intent: ProgramIntent | None


class _ProfileStorage(Store[dict[str, Any]]):
    """Keep schema migrations explicit; never silently downgrade future data."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        # Schema 1 is the first shipped format. Add lossless migrations here
        # only when a real stored schema changes; no historical defaults exist.
        raise ValueError(
            "Unsupported saved-profile storage version; upgrade the integration "
            "or restore a compatible backup. Storage was not changed"
        )


class ProfileStore:
    """Serialize durable edits and resolve exact, validated profile revisions."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device_id: str) -> None:
        self._store = _ProfileStorage(
            hass, 1, f"{DOMAIN}.profiles.{entry_id}", atomic_writes=True
        )
        self._device_id = device_id
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load schema 1 without restoring any executable selection."""
        data = await self._store.async_load()
        if data is None:
            return
        if (
            not isinstance(data, dict)
            or data.get("device_id") != self._device_id
            or not isinstance(data.get("profiles"), dict)
        ):
            raise ValueError(
                "Saved profiles do not belong to this appliance or have invalid storage"
            )
        names: set[tuple[str, str]] = set()
        for profile_id, row in data["profiles"].items():
            if (
                type(profile_id) is not str
                or not profile_id
                or not isinstance(row, dict)
                or type(row.get("name")) is not str
                or row.get("drum") not in ("upper", "lower")
                or type(row.get("revision")) is not int
                or row["revision"] < 1
            ):
                raise ValueError(
                    "Invalid saved profile metadata; storage was not changed"
                )
            name = _name(row["name"])
            name_key = (row["drum"], name.casefold())
            if name != row["name"] or name_key in names:
                raise ValueError(
                    "Invalid or duplicate saved profile name; storage was not changed"
                )
            names.add(name_key)
        self._records = data["profiles"]

    def _profile(self, profile_id: str) -> SavedProfile:
        row = self._records.get(profile_id)
        if row is None:
            raise ValueError(
                "Profile not found for this appliance; it may have been deleted"
            )
        intent = None
        if row.get("intent_version") == _INTENT_VERSION:
            try:
                intent = _decode_intent(row.get("intent"))
                if intent.drum != row["drum"]:
                    intent = None
            except (ValueError, TypeError, KeyError):
                # Preserve the original bytes/fields on unrelated management edits.
                # A future/changed capability must not silently become defaults.
                intent = None
        return SavedProfile(
            profile_id, row["name"], row["drum"], row["revision"], intent
        )

    def profiles(self, drum: Drum | None = None) -> tuple[SavedProfile, ...]:
        """Return displayable records, including incompatible ones."""
        return tuple(
            self._profile(profile_id)
            for profile_id, row in self._records.items()
            if drum is None or row["drum"] == drum
        )

    def get(self, profile_id: str, drum: Drum) -> SavedProfile:
        """Check ownership and drum before any apply, update or start."""
        profile = self._profile(profile_id)
        if profile.drum != drum:
            raise ValueError(
                "Profile belongs to the other drum; select a matching profile and drum"
            )
        return profile

    def resolve(self, profile_id: str, drum: Drum) -> SavedProfile:
        """Return a validated immutable intent or an actionable incompatibility."""
        profile = self.get(profile_id, drum)
        if profile.intent is None:
            raise ValueError(
                "Profile is incompatible with this integration version; explicitly update it from a valid draft or delete it"
            )
        return profile

    def _check_name(self, name: str, drum: Drum, profile_id: str | None = None) -> str:
        name = _name(name)
        if any(
            key != profile_id
            and row["drum"] == drum
            and row["name"].casefold() == name.casefold()
            for key, row in self._records.items()
        ):
            raise ValueError(
                "A profile with this name already exists for this drum; use Update profile explicitly"
            )
        return name

    @staticmethod
    def _intent_data(intent: ProgramIntent) -> dict[str, Any]:
        validate_intent(intent)
        data = asdict(intent)
        for key in ("enabled_functions", "stains"):
            data[key] = list(data[key])
        return data

    async def _async_commit(self, records: dict[str, dict[str, Any]]) -> None:
        await self._store.async_save(
            {"device_id": self._device_id, "profiles": records}
        )
        self._records = records

    async def async_save(self, name: str, intent: ProgramIntent) -> SavedProfile:
        """Create only; a name collision never overwrites existing data."""
        data = self._intent_data(intent)
        async with self._lock:
            name = self._check_name(name, intent.drum)
            profile_id = uuid4().hex
            await self._async_commit(
                {
                    **self._records,
                    profile_id: {
                        "name": name,
                        "drum": intent.drum,
                        "revision": 1,
                        "intent_version": _INTENT_VERSION,
                        "intent": data,
                    },
                }
            )
            return self._profile(profile_id)

    async def async_update(
        self, profile_id: str, drum: Drum, intent: ProgramIntent
    ) -> SavedProfile:
        """Explicitly replace all saved settings, retaining stable identity/name."""
        data = self._intent_data(intent)
        if intent.drum != drum:
            raise ValueError("Intent drum does not match the target")
        async with self._lock:
            profile = self.get(profile_id, drum)
            await self._async_commit(
                {
                    **self._records,
                    profile_id: {
                        "name": profile.name,
                        "drum": drum,
                        "revision": profile.revision + 1,
                        "intent_version": _INTENT_VERSION,
                        "intent": data,
                    },
                }
            )
            return self._profile(profile_id)

    async def async_rename(
        self, profile_id: str, drum: Drum, name: str
    ) -> SavedProfile:
        """Change only the display label, including for an incompatible record."""
        async with self._lock:
            profile = self.get(profile_id, drum)
            name = self._check_name(name, drum, profile_id)
            await self._async_commit(
                {
                    **self._records,
                    profile_id: {
                        **self._records[profile_id],
                        "name": name,
                        "revision": profile.revision + 1,
                    },
                }
            )
            return self._profile(profile_id)

    async def async_delete(self, profile_id: str, drum: Drum) -> None:
        """Remove only an explicitly identified profile, never the current draft."""
        async with self._lock:
            self.get(profile_id, drum)
            await self._async_commit(
                {key: row for key, row in self._records.items() if key != profile_id}
            )

    async def async_remove(self) -> None:
        """Remove entry-owned storage only when the config entry is deleted."""
        await self._store.async_remove()
