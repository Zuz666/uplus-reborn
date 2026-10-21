"""Saved intent persistence, native restoration and one-shot automation contracts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_get_all_descriptions
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.uplus_reborn.const import CONF_ENABLE_VERIFIED_CONTROLS, DOMAIN
from custom_components.uplus_reborn.profiles import ProfileStore
from custom_components.uplus_reborn.protocol import (
    ControlResult,
    DeviceSnapshot,
    OutcomeUnknown,
    UwtClient,
    default_intent,
    fabric_intent,
)

from .conftest import DEVICE_ID
from .test_integration import _entity_id, _entry


async def _action(hass, device, action, drum="upper", **data):
    return await hass.services.async_call(
        DOMAIN,
        action,
        {"device_id": device, "drum": drum, **data},
        blocking=True,
        return_response=action in ("save_profile", "update_profile", "rename_profile"),
    )


async def _set_name(hass, drum, value):
    await hass.services.async_call(
        "text",
        "set_value",
        {
            "entity_id": _entity_id(er.async_get(hass), "text", f"{drum}_profile_name"),
            "value": value,
        },
        blocking=True,
    )


async def _save_button(hass, drum):
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity_id(er.async_get(hass), "button", f"{drum}_save_profile")},
        blocking=True,
    )


@pytest.fixture
async def profile_entry(hass: HomeAssistant, standby_snapshot: DeviceSnapshot):
    entry = _entry(controls=True)
    entry.add_to_hass(hass)
    with patch.object(
        UwtClient, "read_state", AsyncMock(return_value=standby_snapshot)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, DEVICE_ID)})
        yield entry, device.id


@pytest.mark.parametrize(
    "drum,kind",
    [
        ("upper", "settings"),
        ("lower", "settings"),
        ("upper", "stain"),
        ("lower", "stain"),
        ("upper", "smart"),
        ("lower", "smart"),
        ("upper", "empty_options"),
        ("lower", "empty_options"),
        ("lower", "dry"),
    ],
)
async def test_complete_native_restore_is_atomic_and_local(
    hass, profile_entry, drum, kind
):
    entry, device = profile_entry
    c = entry.runtime_data
    program = "casual" if drum == "upper" else "cotton"
    c.stage_program(drum, program)
    if kind == "settings":
        c.stage_settings(
            drum,
            spin_rpm=600,
            enabled_functions=("high_water",),
            delay_half_hour_step=48,
            **({"drying_mode": "120_min"} if drum == "lower" else {}),
        )
    elif kind == "stain":
        c.stage_settings(drum, stains=("wine",))
    elif kind == "smart":
        c.stage_fabric(drum, "B1")
        c.stage_settings(drum, stains=("W1", "W30"))
    elif kind == "empty_options":
        c.stage_program(drum, "spin")
    elif kind == "dry":
        c.stage_program(drum, "dry")
        c.stage_settings(drum, drying_mode="30_min")
    expected = c.draft(drum)
    saved = await _action(hass, device, "save_profile", drum, name="My laundry")
    c.stage_program(drum, program)
    c.stage_settings(drum, enabled_functions=("easy_iron",))
    other = "lower" if drum == "upper" else "upper"
    c.stage_program(other, "spin")
    other_draft = c.draft(other)
    before = c.data
    with (
        patch.object(UwtClient, "read_state", AsyncMock()) as read,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
    ):
        await hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": _entity_id(
                    er.async_get(hass), "select", f"{drum}_profile"
                ),
                "option": "My laundry",
            },
            blocking=True,
        )
        await hass.async_block_till_done()
        assert c.draft(drum) == expected
        assert c.draft(other) is other_draft
        assert c.data is before
        assert c.selected_profile(drum) == saved["profile_id"]
        read.assert_not_awaited()
        start.assert_not_awaited()
    c.stage_program(drum, "spin")
    assert c.selected_profile(drum) is None
    assert c.profiles.resolve(saved["profile_id"], drum).intent == expected


async def test_device_page_save_restores_both_complete_drafts_while_offline(
    hass, profile_entry
):
    entry, _ = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "casual")
    c.stage_settings(
        "upper",
        spin_rpm=600,
        enabled_functions=("high_water",),
        delay_half_hour_step=48,
    )
    c.stage_program("lower", "dry")
    c.stage_settings("lower", drying_mode="30_min")
    drafts = {drum: c.draft(drum) for drum in ("upper", "lower")}
    report = c.data
    c.async_set_update_error(UpdateFailed("Offline"))
    with (
        patch.object(UwtClient, "read_state", AsyncMock()) as read,
        patch.object(UwtClient, "start_program", AsyncMock()) as start,
    ):
        for drum in ("upper", "lower"):
            await _set_name(hass, drum, " Daily ")
            assert c.draft(drum) is drafts[drum]
            await _save_button(hass, drum)
            profile = c.profiles.profiles(drum)[0]
            assert profile.name == "Daily"
            assert profile.intent == drafts[drum]
            assert c.draft(drum) is drafts[drum]
            assert c.selected_profile(drum) is None
            assert c.profile_name(drum) == " Daily "
            descriptions = await async_get_all_descriptions(hass)
            choices = descriptions[DOMAIN]["start_profile"]["fields"]["profile_id"][
                "selector"
            ]["select"]["options"]
            assert any(row["value"] == profile.id for row in choices)
        for drum in ("upper", "lower"):
            c.stage_program(drum, "spin")
            await hass.services.async_call(
                "select",
                "select_option",
                {
                    "entity_id": _entity_id(
                        er.async_get(hass), "select", f"{drum}_profile"
                    ),
                    "option": "Daily",
                },
                blocking=True,
            )
            assert c.draft(drum) == drafts[drum]
        assert c.data is report
        read.assert_not_awaited()
        start.assert_not_awaited()


@pytest.mark.parametrize(
    "name", ["", "   ", "None", "UNKNOWN", "unavailable", "x" * 101]
)
async def test_device_page_invalid_name_never_saves_or_changes_drafts(
    hass, profile_entry, name
):
    entry, _ = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "spin")
    draft = c.draft("upper")
    if len(name) > 100:
        with pytest.raises((HomeAssistantError, ValueError)):
            await _set_name(hass, "upper", name)
        assert c.profile_name("upper") == ""
    else:
        await _set_name(hass, "upper", name)
        with pytest.raises(ServiceValidationError, match="profile name"):
            await _save_button(hass, "upper")
        assert c.profile_name("upper") == name
    assert c.profiles.profiles() == ()
    assert c.draft("upper") is draft
    assert c.draft("lower") is None


async def test_device_page_missing_draft_and_duplicate_are_explicit_and_non_destructive(
    hass, profile_entry
):
    entry, _ = profile_entry
    c = entry.runtime_data
    await _set_name(hass, "upper", "Daily")
    with pytest.raises(ServiceValidationError, match="draft first"):
        await _save_button(hass, "upper")
    assert c.profiles.profiles() == ()
    c.stage_program("upper", "spin")
    await _save_button(hass, "upper")
    saved = c.profiles.profiles("upper")[0]
    c.stage_settings("upper", spin_rpm=400)
    changed = c.draft("upper")
    await _set_name(hass, "upper", " daily ")
    with pytest.raises(ServiceValidationError, match="already exists"):
        await _save_button(hass, "upper")
    assert c.profiles.profiles() == (saved,)
    assert saved.intent.spin_rpm == 1000
    assert c.draft("upper") is changed
    assert c.profile_name("upper") == " daily "
    assert c.profile_name("lower") == ""


async def test_device_page_storage_failure_keeps_input_draft_and_existing_records(
    hass, profile_entry
):
    entry, _ = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "spin")
    await _set_name(hass, "upper", "Existing")
    await _save_button(hass, "upper")
    existing = c.profiles.profiles()
    await _set_name(hass, "upper", "Daily")
    draft = c.draft("upper")
    with (
        patch.object(
            c.profiles._store,
            "async_save",
            side_effect=HomeAssistantError("Storage unavailable"),
        ),
        pytest.raises(HomeAssistantError, match="Storage unavailable"),
    ):
        await _save_button(hass, "upper")
    assert c.profiles.profiles() == existing
    assert c.draft("upper") is draft
    assert c.profile_name("upper") == "Daily"


async def test_device_page_save_snapshots_name_and_draft_without_losing_concurrent_edits(
    hass, profile_entry
):
    entry, _ = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "spin")
    c.stage_settings("upper", spin_rpm=400)
    original = c.draft("upper")
    await _set_name(hass, "upper", "Original")
    entered, release = asyncio.Event(), asyncio.Event()
    persist = c.profiles._store.async_save

    async def delayed_persist(data):
        entered.set()
        await release.wait()
        await persist(data)

    with patch.object(c.profiles._store, "async_save", side_effect=delayed_persist):
        saving = asyncio.create_task(_save_button(hass, "upper"))
        await entered.wait()
        await _set_name(hass, "upper", "Next profile")
        c.stage_settings("upper", spin_rpm=600)
        release.set()
        await saving
    saved = c.profiles.profiles("upper")[0]
    assert saved.name == "Original"
    assert saved.intent is not None and saved.intent == original
    assert c.profile_name("upper") == "Next profile"
    assert c.draft("upper").spin_rpm == 600


async def test_device_page_opt_out_revokes_local_edits_without_losing_profiles(
    hass, profile_entry
):
    entry, _ = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "spin")
    await _set_name(hass, "upper", "Retained")
    await _save_button(hass, "upper")
    saved = c.profiles.profiles("upper")[0]
    hass.config_entries.async_update_entry(
        entry, options={CONF_ENABLE_VERIFIED_CONTROLS: False}
    )
    # HA skips unavailable entity targets; the contract is no mutation.
    await _save_button(hass, "upper")
    await _set_name(hass, "upper", "Not permitted")
    await hass.async_block_till_done()
    fresh = ProfileStore(hass, entry.entry_id, DEVICE_ID)
    await fresh.async_load()
    assert fresh.profiles() == (saved,)
    assert entry.runtime_data.profile_name("upper") == ""
    assert entry.runtime_data.draft("upper") is None


async def test_management_keeps_ids_explicit_updates_and_independent_drafts(
    hass, profile_entry
):
    entry, device = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "spin")
    saved = await _action(hass, device, "save_profile", name="Daily")
    profile_id = saved["profile_id"]
    with pytest.raises(ServiceValidationError, match="already exists"):
        await _action(hass, device, "save_profile", name=" daily ")
    await _action(hass, device, "apply_profile", profile_id=profile_id)
    await _action(hass, device, "rename_profile", profile_id=profile_id, name="Renamed")
    selector = _entity_id(er.async_get(hass), "select", "upper_profile")
    await hass.async_block_till_done()
    assert hass.states.get(selector).state == "Renamed"
    assert hass.states.get(selector).attributes["profile_id"] == profile_id
    descriptions = await async_get_all_descriptions(hass)
    choices = descriptions[DOMAIN]["start_profile"]["fields"]["profile_id"]["selector"][
        "select"
    ]["options"]
    assert any(
        row["value"] == profile_id and row["label"].endswith("/ Renamed")
        for row in choices
    )
    c.stage_settings("upper", spin_rpm=400)
    assert c.selected_profile("upper") is None
    assert c.profiles.resolve(profile_id, "upper").intent.spin_rpm == 1000
    updated = await _action(hass, device, "update_profile", profile_id=profile_id)
    assert updated["profile_id"] == profile_id
    assert c.profiles.resolve(profile_id, "upper").intent.spin_rpm == 400
    await _action(hass, device, "apply_profile", profile_id=profile_id)
    draft = c.draft("upper")
    await _action(hass, device, "delete_profile", profile_id=profile_id)
    assert c.draft("upper") is draft
    assert c.selected_profile("upper") is None
    with pytest.raises(ServiceValidationError, match="not found"):
        await _action(hass, device, "apply_profile", profile_id=profile_id)


async def test_reload_and_fresh_store_keep_profiles_but_never_restore_drafts(
    hass, profile_entry
):
    entry, device = profile_entry
    saved = {}
    for drum in ("upper", "lower"):
        await _set_name(hass, drum, "Unsaved next name")
        entry.runtime_data.stage_fabric(drum, "B1")
        entry.runtime_data.stage_settings(drum, stains=("W30",))
        saved[drum] = await _action(
            hass, device, "save_profile", drum, name="Same label"
        )
        await _action(
            hass, device, "apply_profile", drum, profile_id=saved[drum]["profile_id"]
        )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    fresh = ProfileStore(hass, entry.entry_id, DEVICE_ID)
    await fresh.async_load()
    for drum in ("upper", "lower"):
        assert entry.runtime_data.draft(drum) is None
        assert entry.runtime_data.selected_profile(drum) is None
        assert entry.runtime_data.profile_name(drum) == ""
        name_entity = _entity_id(er.async_get(hass), "text", f"{drum}_profile_name")
        assert hass.states.get(name_entity).state == ""
        profile_id = saved[drum]["profile_id"]
        assert fresh.resolve(profile_id, drum).intent == fabric_intent(
            drum, "B1", ("W30",)
        )
        await _action(hass, device, "apply_profile", drum, profile_id=profile_id)
        assert entry.runtime_data.draft(drum) == fresh.resolve(profile_id, drum).intent


@pytest.mark.parametrize(
    "failure", ["missing", "wrong_drum", "wrong_device", "opt_out"]
)
async def test_invalid_profile_start_cannot_dispatch_or_change_drafts(
    hass, profile_entry, failure
):
    entry, device = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "spin")
    saved = await _action(hass, device, "save_profile", name="Upper")
    drum = "lower" if failure == "wrong_drum" else "upper"
    profile_id = "missing" if failure == "missing" else saved["profile_id"]
    if failure == "opt_out":
        # Revoke live authorization before the queued operation, without waiting for reload.
        hass.config_entries.async_update_entry(
            entry, options={CONF_ENABLE_VERIFIED_CONTROLS: False}
        )
    if failure == "wrong_device":
        other = _entry(controls=True, device_id="ANOTHER12345")
        other.add_to_hass(hass)
        assert await hass.config_entries.async_setup(other.entry_id)
        await hass.async_block_till_done()
        device = (
            dr.async_get(hass)
            .async_get_device(identifiers={(DOMAIN, "ANOTHER12345")})
            .id
        )
    with patch.object(UwtClient, "start_program", AsyncMock()) as start:
        with pytest.raises(ServiceValidationError):
            await _action(hass, device, "start_profile", drum, profile_id=profile_id)
        start.assert_not_awaited()


async def test_start_freezes_saved_revision_before_waiting_for_connection(
    hass, profile_entry, standby_snapshot, active_snapshot
):
    entry, device = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "spin")
    c.stage_settings("upper", spin_rpm=400)
    saved = await _action(hass, device, "save_profile", name="Spin 400")
    intent = c.draft("upper")
    c.stage_program("lower", "cotton")
    resolved = asyncio.Event()
    original_resolve = c.profiles.resolve

    def resolve_revision(*args):
        profile = original_resolve(*args)
        resolved.set()
        return profile

    async with c._connection_lock:
        with patch.object(
            c.profiles, "resolve", side_effect=resolve_revision
        ) as resolve:
            command = asyncio.create_task(
                _action(hass, device, "start_profile", profile_id=saved["profile_id"])
            )
            await asyncio.wait_for(resolved.wait(), 5)
            assert resolve.call_count == 1
        c.stage_settings("upper", spin_rpm=600)
        await _action(hass, device, "update_profile", profile_id=saved["profile_id"])
        await _action(hass, device, "delete_profile", profile_id=saved["profile_id"])
        drafts = {drum: c.draft(drum) for drum in ("upper", "lower")}
        with patch.object(
            UwtClient,
            "start_program",
            AsyncMock(return_value=ControlResult(standby_snapshot, active_snapshot)),
        ) as start:
            c._connection_lock.release()
            try:
                await command
            finally:
                await c._connection_lock.acquire()
            start.assert_awaited_once_with(
                intent, allow_live=True, expected=standby_snapshot
            )
    assert {drum: c.draft(drum) for drum in drafts} == drafts
    assert c.health.latest_command.outcome == "confirmed"


async def test_profile_unknown_outcome_is_not_retried_or_relabelled(
    hass, profile_entry
):
    entry, device = profile_entry
    c = entry.runtime_data
    c.stage_program("upper", "spin")
    saved = await _action(hass, device, "save_profile", name="Spin")
    draft = c.draft("upper")
    with patch.object(
        UwtClient,
        "start_program",
        AsyncMock(side_effect=OutcomeUnknown("Unconfirmed write")),
    ) as start:
        with pytest.raises(HomeAssistantError) as raised:
            await _action(hass, device, "start_profile", profile_id=saved["profile_id"])
        assert raised.value.translation_key == "outcome_unknown"
        assert c.health.latest_command.outcome == "unknown"
        assert c.draft("upper") is draft
        start.assert_awaited_once()


@pytest.mark.parametrize(
    "change", ["new_intent_version", "unknown_program", "missing_field", "wrong_drum"]
)
async def test_incompatible_stored_intents_are_retained_manageable_and_never_defaulted(
    hass, change
):
    intent = asdict(default_intent("upper", "spin"))
    intent["enabled_functions"] = []
    intent["stains"] = []
    row = {
        "name": "Old profile",
        "drum": "upper",
        "revision": 7,
        "intent_version": 1,
        "intent": intent,
    }
    if change == "new_intent_version":
        row["intent_version"] = 2
        intent["future_field"] = "preserve me"
    elif change == "unknown_program":
        intent["program"] = "future_program"
    elif change == "missing_field":
        del intent["spin_rpm"]
    else:
        intent["drum"] = "lower"
    store = Store(hass, 1, f"{DOMAIN}.profiles.entry")
    await store.async_save({"device_id": DEVICE_ID, "profiles": {"stable-id": row}})
    profiles = ProfileStore(hass, "entry", DEVICE_ID)
    await profiles.async_load()
    with pytest.raises(ValueError, match="incompatible"):
        profiles.resolve("stable-id", "upper")
    renamed = await profiles.async_rename("stable-id", "upper", "Still saved")
    assert renamed.id == "stable-id"
    loaded = await store.async_load()
    assert loaded["profiles"]["stable-id"]["intent"] == intent
    repaired = await profiles.async_update(
        "stable-id", "upper", default_intent("upper", "spin")
    )
    assert repaired.revision == 9
    assert profiles.resolve("stable-id", "upper").intent == default_intent(
        "upper", "spin"
    )


async def test_storage_binding_and_removal_do_not_cross_entries(hass):
    first = ProfileStore(hass, "first", DEVICE_ID)
    saved = await first.async_save("First", default_intent("upper", "spin"))
    second = ProfileStore(hass, "second", DEVICE_ID)
    await second.async_load()
    with pytest.raises(ValueError, match="not found"):
        second.resolve(saved.id, "upper")
    rebound = ProfileStore(hass, "first", "DIFFERENT123")
    with pytest.raises(ValueError, match="belong"):
        await rebound.async_load()
    await second.async_save("Second", default_intent("lower", "spin"))
    await first.async_remove()
    reloaded = ProfileStore(hass, "first", DEVICE_ID)
    await reloaded.async_load()
    assert reloaded.profiles() == ()
    fresh_second = ProfileStore(hass, "second", DEVICE_ID)
    await fresh_second.async_load()
    assert fresh_second.profiles()[0].name == "Second"


@pytest.mark.parametrize("major,minor", [(2, 1), (1, 2)])
async def test_unknown_storage_schema_cannot_be_downgraded(hass, major, minor):
    """A downgrade must preserve future data rather than rewrite it as schema 1."""
    store = Store(hass, major, f"{DOMAIN}.profiles.future", minor_version=minor)
    future = {"device_id": DEVICE_ID, "profiles": {}, "future_metadata": ["retained"]}
    await store.async_save(future)
    profiles = ProfileStore(hass, "future", DEVICE_ID)
    with pytest.raises(ValueError, match="Unsupported saved-profile storage version"):
        await profiles.async_load()
    assert await store.async_load() == future
