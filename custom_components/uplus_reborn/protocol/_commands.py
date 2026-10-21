"""Immutable target-program drafts and evidence-backed grouped Start encoding.

The U+ 2.6.0 c8hu12w1org1 builders define source arithmetic; EPP 4.0.3 defines
the word layout. Ordinary Wine uses paired panel stage references. Source
support and reference transfer are not exhaustive hardware validation.
This module performs no I/O and does not authorize command dispatch.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final

from ._capabilities import (
    DRYING_MODES,
    ORDINARY_STAIN_VALUES,
    STAIN_CAPABILITIES,
    WASH_OPTION_BITS,
    Drum,
    DryingModeName,
    OrdinaryStainCapability,
    ProgramCapabilities,
    ProgramFunctionName,
    ProgramSettings,
    fabric_capability,
    ordinary_stain_choices,
    program_capabilities,
    program_delay_capabilities,
    program_drying_modes,
    program_function_availability,
)


@dataclass(frozen=True, slots=True)
class ProgramIntent:
    """One complete local draft, never authoritative appliance state."""

    drum: Drum
    program: str
    wash_minutes: int | None
    temperature_c: int | None
    rinses: int | None
    spin_rpm: int | None
    drying_mode: DryingModeName | None = None
    delay_half_hour_step: int = 0
    enabled_functions: tuple[ProgramFunctionName, ...] = ()
    fabric: str | None = None
    stains: tuple[str, ...] = ()

    @property
    def smart(self) -> bool:
        """Return whether the fixed fabric recipe is selected."""
        return self.fabric is not None


@dataclass(frozen=True, slots=True)
class BuilderContext:
    """Internal state fields the source builder preserves, not user controls.

    Source builders mutate the latest data object and the command factory clones
    it. These profile fields are therefore retained rather than inferred from a
    program or cleared merely because a new local draft was selected.
    """

    load_level: int = 0
    wash_water_level: int = 0
    scrubbing_speed: int = 0
    washing_rotation_time: int = 0
    washing_stop_time: int = 0
    drying_value: int = 0


_EMPTY_CONTEXT: Final = BuilderContext()

# Heating levels are constants in c.a/b(String,int,int,int,int,Map,bool,...),
# not user load selections. A zero level means the builder bypasses heating.
_UPPER_HEATING_LEVEL: Final = MappingProxyType(
    {
        "baby_care": 3,
        "underwear": 2,
        "delicate_care": 2,
        "casual": 2,
        "allergy_care": 3,
        "colored_laundry": 2,
        "kids_wear": 3,
        "quick_wash": 2,
        "towels": 2,
        "rinse_spin": 0,
        "spin": 0,
        "drum_clean": 1,
    }
)
_LOWER_HEATING_LEVEL: Final = MappingProxyType(
    {
        "cotton": 2,
        "synthetic": 2,
        "wool": 1,
        "bulky": 3,
        "curtain": 3,
        "bedding": 3,
        "mix": 2,
        "jeans": 2,
        "sports_wear": 1,
        "outdoor_care": 2,
        "shirts": 1,
        "down": 1,
        "quick_wash": 1,
        "delicate_care": 1,
        "hygiene": 2,
        "rinse_spin": 0,
        "spin": 0,
        "drum_clean": 1,
        "dry": 0,
        "refresh": 0,
    }
)
# Ordered temperatures 20,30,40,50,60,70,80,90,95. The final value is the
# exact default branch (also used at cold/0 and stain-derived 55 degrees).
_UPPER_HEATING: Final = (
    (),
    (5, 10, 15, 20, 25, 30, 35, 50, 5, 5),
    (5, 10, 20, 25, 30, 35, 45, 60, 5, 5),
    (5, 15, 20, 30, 35, 45, 50, 70, 80, 5),
)
_LOWER_HEATING: Final = (
    (),
    (5, 10, 20, 25, 30, 35, 45, 80, 5, 5),
    (10, 15, 25, 35, 40, 50, 55, 100, 120, 5),
    (10, 20, 30, 40, 50, 60, 70, 120, 10, 10),
)
_HEATING_TEMPERATURES: Final = (20, 30, 40, 50, 60, 70, 80, 90, 95)
# Z.g/h (upper soak/intensive) and Z.i/j (lower soak/intensive).
_UPPER_OPTION_MINUTES: Final = MappingProxyType(
    {
        "baby_care": (15, 20),
        "underwear": (15, 20),
        "casual": (10, 15),
        "allergy_care": (10, 0),
        "kids_wear": (20, 15),
        "towels": (15, 10),
    }
)
_LOWER_OPTION_MINUTES: Final = MappingProxyType(
    {
        "cotton": (30, 50),
        "synthetic": (20, 20),
        "bulky": (20, 20),
        "curtain": (20, 20),
        "bedding": (20, 20),
        "mix": (20, 20),
        "jeans": (0, 15),
        "sports_wear": (15, 0),
        "outdoor_care": (15, 15),
        "shirts": (15, 15),
        "hygiene": (10, 0),
    }
)
_DRY_MINUTES: Final = MappingProxyType(
    {
        "auto": 150,
        "30_min": 30,
        "60_min": 60,
        "90_min": 90,
        "120_min": 120,
    }
)

# Five effects agree with the panel references after program-specific heating.
# Wine differs from the smart W12/W13 recipe and uses paired panel references.
_ORDINARY_STAIN_ADJUSTMENTS: Final = MappingProxyType(
    {
        "blood": (60, 9),
        "grass": (60, 10),
        "juice": (30, 10),
        "soil": (60, 40),
        "milk": (30, 40),
    }
)
_STAIN_CONFLICTS: Final = frozenset({"eco", "night", "soak"})


def _heating_minutes(drum: Drum, program: str, temperature: int | None) -> int:
    """Follow exact DEX branches, including their non-interpolated fallback."""
    upper = drum == "upper"
    level = (_UPPER_HEATING_LEVEL if upper else _LOWER_HEATING_LEVEL)[program]
    if not level:
        return 0
    if program == "drum_clean":
        return 50 if upper else 70
    if program == "quick_wash":
        return {20: 5, 30: 10, 40: 20}.get(temperature, 0)
    if not upper and program == "wool":
        return {20: 15, 30: 20, 40: 25}.get(temperature, 10)
    table = (_UPPER_HEATING if upper else _LOWER_HEATING)[level]
    if temperature not in _HEATING_TEMPERATURES:
        return table[-1]
    return table[_HEATING_TEMPERATURES.index(temperature)]


def ordinary_stain_capability(
    drum: Drum, program: str, stain: str
) -> OrdinaryStainCapability:
    """Resolve a treatment without claiming every program was hardware-tested.

    Lower Mix references cover all six; upper Casual covers Blood and Wine.
    Other combinations use source heating or the per-drum Wine reference.
    """
    if type(stain) is not str or stain not in ordinary_stain_choices(drum, program):
        raise ValueError("Unsupported ordinary stain for this program")
    if stain == "wine":
        temperature, duration = (55, 35) if drum == "upper" else (50, 25)
    else:
        temperature, main_minutes = _ORDINARY_STAIN_ADJUSTMENTS[stain]
        duration = main_minutes + _heating_minutes(drum, program, temperature)
    return OrdinaryStainCapability(
        stain, ORDINARY_STAIN_VALUES[stain], temperature, duration
    )


def _ordinary_stain(intent: ProgramIntent) -> OrdinaryStainCapability | None:
    if type(intent.stains) is not tuple:
        raise ValueError("stains must be an immutable tuple")
    if intent.smart or not intent.stains:
        return None
    if len(intent.stains) != 1:
        raise ValueError("An ordinary program accepts exactly one stain")
    return ordinary_stain_capability(intent.drum, intent.program, intent.stains[0])


def available_intent_functions(
    intent: ProgramIntent,
) -> tuple[ProgramFunctionName, ...]:
    """Return usable wash options, including recipe-dependent exclusions."""
    if intent.smart:
        return ()
    return tuple(
        item.name
        for item in program_function_availability(intent.drum, intent.program).available
        if item.name in WASH_OPTION_BITS
        and not (intent.stains and item.name in _STAIN_CONFLICTS)
    )


def intent_wash_stage_minutes(intent: ProgramIntent) -> int:
    """Return the encoded wash-stage duration, including heating and options."""
    stain = _ordinary_stain(intent)
    if stain is not None:
        duration = stain.wash_stage_minutes
    else:
        heating = _heating_minutes(intent.drum, intent.program, intent.temperature_c)
        if intent.wash_minutes is None:
            if intent.drum == "lower" and intent.program == "dry":
                return _DRY_MINUTES[intent.drying_mode]
            return heating
        duration = intent.wash_minutes + heating
    additions = (
        _UPPER_OPTION_MINUTES if intent.drum == "upper" else _LOWER_OPTION_MINUTES
    ).get(intent.program, (0, 0))
    if (intent.drum, intent.program, intent.temperature_c) == ("lower", "cotton", 90):
        additions = (10, 10)
    return (
        duration
        + (additions[0] if "soak" in intent.enabled_functions else 0)
        + (additions[1] if "intensive" in intent.enabled_functions else 0)
    )


def default_intent(drum: Drum, program: str) -> ProgramIntent:
    """Create a source-default draft without retaining a previous selection."""
    settings = program_capabilities(drum, program).default
    drying = program_drying_modes(program) if drum == "lower" else ()
    return ProgramIntent(
        drum=drum,
        program=program,
        wash_minutes=settings.wash_minutes,
        temperature_c=settings.temperature_c,
        rinses=settings.rinses,
        spin_rpm=settings.spin_rpm,
        drying_mode=drying[0].name if drying else None,
    )


def fabric_intent(
    drum: Drum, fabric: str, stains: tuple[str, ...] = ()
) -> ProgramIntent:
    """Build a fixed smart recipe, including source material-limit clamping.

    The app's confirmed warning path caps the strongest stain's temperature and
    the longer of its time and the recipe time. It does not combine separate
    maxima across stains, nor write a selected W row into the raw stain field.
    """
    capability = fabric_capability(drum, fabric)
    if type(stains) is not tuple:
        raise ValueError("stains must be an immutable tuple")
    by_id = {item.ui_id: item for item in STAIN_CAPABILITIES}
    if any(type(ui_id) is not str or ui_id not in by_id for ui_id in stains) or len(
        set(stains)
    ) != len(stains):
        raise ValueError("Unsupported or duplicate stains")
    canonical = tuple(item.ui_id for item in STAIN_CAPABILITIES if item.ui_id in stains)
    settings = capability.default
    if canonical:
        strongest = max(
            (by_id[ui_id] for ui_id in canonical), key=lambda item: item.priority_level
        )
        settings = replace(
            settings,
            wash_minutes=min(
                max(settings.wash_minutes, strongest.wash_minutes),
                capability.max_wash_minutes,
            ),
            temperature_c=min(strongest.temperature_c, capability.max_temperature_c),
        )
    return replace(
        default_intent(drum, capability.program),
        wash_minutes=settings.wash_minutes,
        temperature_c=settings.temperature_c,
        rinses=settings.rinses,
        spin_rpm=settings.spin_rpm,
        fabric=fabric,
        stains=canonical,
    )


def intent_capabilities(intent: ProgramIntent) -> ProgramCapabilities:
    """Return adjustable choices or fixed recipe-owned scalar coordinates."""
    capability = program_capabilities(intent.drum, intent.program)
    if not intent.smart:
        if stain := _ordinary_stain(intent):
            return replace(
                capability,
                default=replace(
                    capability.default,
                    wash_minutes=None,
                    temperature_c=stain.temperature_c,
                ),
                allowed_wash_minutes=(),
                allowed_temperatures_c=(),
            )
        return capability
    expected = fabric_intent(intent.drum, intent.fabric, intent.stains)
    return replace(
        capability,
        default=ProgramSettings(
            expected.wash_minutes,
            expected.temperature_c,
            expected.rinses,
            expected.spin_rpm,
        ),
        allowed_wash_minutes=(),
        allowed_temperatures_c=(),
        allowed_rinses=(),
        allowed_spin_rpm=(),
    )


def _validate_setting(
    name: str, value: int | None, allowed: tuple[int, ...], default: int | None
) -> None:
    if value is not None and type(value) is not int:
        raise ValueError(f"{name} must be an integer or None")
    if (allowed and value not in allowed) or (not allowed and value != default):
        raise ValueError(f"Unsupported {name}: {value}")


def _validate_context(context: BuilderContext) -> None:
    for name, maximum in (
        ("load_level", 8),
        ("wash_water_level", 65535),
        ("scrubbing_speed", 255),
        ("washing_rotation_time", 255),
        ("washing_stop_time", 255),
        ("drying_value", 10),
    ):
        value = getattr(context, name)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"Invalid preserved {name}: {value}")


def validate_intent(intent: ProgramIntent) -> None:
    """Reject incomplete settings and values absent from the target source UI."""
    if not isinstance(intent, ProgramIntent):
        raise TypeError("A complete ProgramIntent is required")
    capability = intent_capabilities(intent)
    for name, allowed in (
        ("wash_minutes", capability.allowed_wash_minutes),
        ("temperature_c", capability.allowed_temperatures_c),
        ("rinses", capability.allowed_rinses),
        ("spin_rpm", capability.allowed_spin_rpm),
    ):
        _validate_setting(
            name, getattr(intent, name), allowed, getattr(capability.default, name)
        )
    if type(intent.enabled_functions) is not tuple:
        raise ValueError("enabled_functions must be an immutable tuple")
    available = available_intent_functions(intent)
    if any(
        type(name) is not str or name not in available
        for name in intent.enabled_functions
    ) or len(set(intent.enabled_functions)) != len(intent.enabled_functions):
        raise ValueError("Unsupported or duplicate enabled_functions")
    drying = program_drying_modes(intent.program) if intent.drum == "lower" else ()
    if intent.drying_mode not in (
        tuple(mode.name for mode in drying) if drying else (None,)
    ):
        raise ValueError(f"Unsupported drying_mode: {intent.drying_mode}")
    delay = program_delay_capabilities(
        intent.drum, intent.program
    ).available_half_hour_steps
    if type(
        intent.delay_half_hour_step
    ) is not int or intent.delay_half_hour_step not in (delay or (0,)):
        raise ValueError(
            f"Unsupported delay_half_hour_step: {intent.delay_half_hour_step}"
        )
    if intent.smart:
        if intent != fabric_intent(intent.drum, intent.fabric, intent.stains):
            raise ValueError(
                "Smart settings and options must match the fixed fabric recipe"
            )
    else:
        _ordinary_stain(intent)
    if not 0 <= intent_wash_stage_minutes(intent) <= 255:
        raise ValueError("Computed wash duration exceeds the profile byte")


def update_intent(intent: ProgramIntent, **changes: object) -> ProgramIntent:
    """Apply one atomic edit, rebuilding recipes and discarding stale options."""
    validate_intent(intent)
    if unknown := changes.keys() - ProgramIntent.__dataclass_fields__.keys():
        raise ValueError(f"Unsupported intent fields: {', '.join(sorted(unknown))}")
    drum = changes.get("drum", intent.drum)
    program = changes.get("program", intent.program)
    program_changed = (drum, program) != (intent.drum, intent.program) or (
        intent.smart and "program" in changes and "fabric" not in changes
    )
    base = default_intent(drum, program) if program_changed else intent
    fabric = changes.get("fabric", base.fabric)
    if fabric is not None:
        forbidden = changes.keys() - {"drum", "program", "fabric", "stains"}
        if forbidden:
            raise ValueError(
                "Smart recipe scalar settings and options cannot be edited"
            )
        stains = changes.get("stains", base.stains if fabric == base.fabric else ())
        updated = fabric_intent(drum, fabric, stains)
        if "program" in changes and program != updated.program:
            raise ValueError("Program does not match the selected fabric recipe")
    else:
        if base.smart:
            base = default_intent(drum, program)
        updated = replace(base, **changes)
        if stain := _ordinary_stain(updated):
            if {"temperature_c", "wash_minutes"} & changes.keys():
                raise ValueError("Ordinary stain temperature and wash time are fixed")
            functions = updated.enabled_functions
            if "enabled_functions" not in changes:
                functions = tuple(
                    name for name in functions if name not in _STAIN_CONFLICTS
                )
            updated = replace(
                updated,
                temperature_c=stain.temperature_c,
                wash_minutes=None,
                enabled_functions=functions,
            )
        elif base.stains:
            defaults = program_capabilities(drum, program).default
            updated = replace(
                updated,
                temperature_c=changes.get("temperature_c", defaults.temperature_c),
                wash_minutes=changes.get("wash_minutes", defaults.wash_minutes),
            )
    validate_intent(updated)
    return updated


def encode_start(
    intent: ProgramIntent,
    *,
    buzzer_enabled: bool = True,
    child_lock: bool = False,
    context: BuilderContext = _EMPTY_CONTEXT,
) -> bytes:
    """Encode a complete 24-byte group without transport framing or dispatch."""
    validate_intent(intent)
    if type(buzzer_enabled) is not bool or type(child_lock) is not bool:
        raise ValueError("Shared appliance flags must be booleans")
    _validate_context(context)
    upper = intent.drum == "upper"
    program = program_capabilities(intent.drum, intent.program).program_value
    drying = DRYING_MODES[intent.drying_mode] if intent.drying_mode is not None else 0
    if not upper and intent.program == "refresh":
        drying = context.drying_value
    dehydration_minutes = 0 if not upper and intent.program in ("dry", "refresh") else 2
    functions = sum(1 << WASH_OPTION_BITS[name] for name in intent.enabled_functions)
    stain = _ordinary_stain(intent)
    return struct.pack(
        ">12H",
        1 if upper else 2,
        (program << 8) | intent.delay_half_hour_step,
        (drying << 8) | (int(intent.smart) << 1) | int(not buzzer_enabled),
        (int(child_lock) << 10) | context.load_level,
        ((intent.temperature_c or 0) << 8) | intent_wash_stage_minutes(intent),
        intent.spin_rpm or 0,
        ((intent.rinses or 0) << 8) | dehydration_minutes,
        functions,
        (stain.wire_value if stain is not None else 0) << 8,
        context.wash_water_level,
        (context.scrubbing_speed << 8) | context.washing_rotation_time,
        context.washing_stop_time << 8,
    )
