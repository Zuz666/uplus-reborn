"""Source-derived HWD120-B1558U program capabilities.

The tables come from the exact appliance profile bundled with Haier U+ 2.6.0.
They describe selectable UI values, not independently verified wire writes.
Hardware coverage is recorded separately and never restricts the encoder.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

type Drum = Literal["upper", "lower"]

_CODE_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True, slots=True)
class ProgramSettings:
    """One program's source-defined setting values."""

    wash_minutes: int | None
    temperature_c: int | None
    rinses: int | None
    spin_rpm: int | None


@dataclass(frozen=True, slots=True)
class ProgramCapabilities:
    """Defaults and selectable values for one exact profile program."""

    program_value: int
    default: ProgramSettings
    allowed_wash_minutes: tuple[int, ...]
    allowed_temperatures_c: tuple[int, ...]
    allowed_rinses: tuple[int, ...]
    allowed_spin_rpm: tuple[int, ...]

    @property
    def profile_code(self) -> str:
        """Return the profile attribute code for this program."""
        return f"30500{_CODE_ALPHABET[self.program_value]}"


@dataclass(frozen=True, slots=True)
class FabricCapability:
    """One source fabric choice and its fixed smart recipe/material limits."""

    ui_id: str
    name: str
    program: str
    default: ProgramSettings
    max_wash_minutes: int
    max_temperature_c: int


@dataclass(frozen=True, slots=True)
class VerifiedStart:
    """One grouped Start combination proven on physical hardware."""

    drum: Drum
    program: str
    spin_rpm: int


type ProgramFunctionName = Literal[
    "delay",
    "drying",
    "high_water",
    "easy_iron",
    "intensive",
    "eco",
    "soak",
    "night",
]


@dataclass(frozen=True, slots=True)
class ProgramFunction:
    """One source-defined program function and its profile attribute."""

    name: ProgramFunctionName
    attribute_id: str


@dataclass(frozen=True, slots=True)
class ProgramFunctionAvailability:
    """Individual UI functions included or excluded for one program."""

    available: tuple[ProgramFunction, ...]
    excluded: tuple[ProgramFunction, ...]


type DryingModeName = Literal["off", "auto", "30_min", "60_min", "90_min", "120_min"]


@dataclass(frozen=True, slots=True)
class DryingMode:
    """One source-defined lower-drum drying choice."""

    name: DryingModeName
    value: int
    profile_code: str


@dataclass(frozen=True, slots=True)
class ProgramDelayCapabilities:
    """Source UI end-time choices for one program."""

    cycle_minutes: int
    available_half_hour_steps: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class StainCapability:
    """One source UI stain row, retaining its label and numeric adjustments."""

    ui_id: str
    temperature_c: int
    wash_minutes: int
    priority_level: int
    name: str


@dataclass(frozen=True, slots=True)
class OrdinaryStainCapability:
    """One ordinary treatment, including its complete wash-stage duration."""

    name: str
    wire_value: int
    temperature_c: int
    wash_stage_minutes: int


# Panel references identify these enums independently of smart W-row priorities.
ORDINARY_STAIN_VALUES: Final = MappingProxyType(
    {"blood": 1, "grass": 2, "wine": 3, "juice": 7, "soil": 5, "milk": 6}
)
_ORDINARY_STAIN_CHOICES: Final = tuple(ORDINARY_STAIN_VALUES)
_ORDINARY_STAIN_PROGRAMS: Final = MappingProxyType(
    {
        "upper": frozenset({"kids_wear", "casual"}),
        "lower": frozenset({"cotton", "mix", "synthetic"}),
    }
)


def ordinary_stain_choices(drum: Drum, program: str) -> tuple[str, ...]:
    """Return the six treatments where the target manual offers stain removal."""
    program_capabilities(drum, program)
    return _ORDINARY_STAIN_CHOICES if program in _ORDINARY_STAIN_PROGRAMS[drum] else ()


def _optional(value: int) -> int | None:
    return None if value < 0 else value


def _program(
    value: int,
    wash_minutes: int,
    temperature_c: int,
    rinses: int,
    spin_rpm: int,
    allowed_wash_minutes: tuple[int, ...],
    allowed_temperatures_c: tuple[int, ...],
    allowed_rinses: tuple[int, ...],
    allowed_spin_rpm: tuple[int, ...],
) -> ProgramCapabilities:
    return ProgramCapabilities(
        program_value=value,
        default=ProgramSettings(
            wash_minutes=_optional(wash_minutes),
            temperature_c=_optional(temperature_c),
            rinses=_optional(rinses),
            spin_rpm=_optional(spin_rpm),
        ),
        allowed_wash_minutes=allowed_wash_minutes,
        allowed_temperatures_c=allowed_temperatures_c,
        allowed_rinses=allowed_rinses,
        allowed_spin_rpm=allowed_spin_rpm,
    )


UPPER_PROGRAM_CAPABILITIES: Final = MappingProxyType(
    {
        "baby_care": _program(
            20,
            10,
            40,
            3,
            1000,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30, 40, 60, 90),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200),
        ),
        "underwear": _program(
            14,
            10,
            40,
            2,
            1000,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6),
            (0, 400, 600, 800, 1000),
        ),
        "delicate_care": _program(
            19,
            10,
            30,
            2,
            600,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30),
            (0, 1, 2, 3),
            (0, 400, 600),
        ),
        "casual": _program(
            21,
            10,
            30,
            2,
            1000,
            (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200),
        ),
        "allergy_care": _program(
            9,
            30,
            90,
            2,
            1000,
            (),
            (90,),
            (2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000),
        ),
        "colored_laundry": _program(
            24,
            25,
            0,
            2,
            600,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30),
            (0, 1, 2, 3),
            (0, 400, 600),
        ),
        "kids_wear": _program(
            7,
            15,
            40,
            3,
            1000,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30, 40, 60, 90),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200),
        ),
        "quick_wash": _program(
            15,
            3,
            0,
            1,
            1000,
            (3, 4, 5, 6, 7, 8, 9, 10),
            (0, 20, 30, 40),
            (0, 1, 2, 3, 4, 5, 6),
            (0, 400, 600, 800, 1000),
        ),
        "towels": _program(
            26,
            5,
            60,
            2,
            1200,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200),
        ),
        "rinse_spin": _program(
            27,
            -1,
            -1,
            2,
            1000,
            (),
            (),
            (1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200),
        ),
        "spin": _program(28, -1, -1, -1, 1000, (), (), (), (400, 600, 800, 1000, 1200)),
        "drum_clean": _program(13, 5, 90, 1, 600, (), (90,), (), (600,)),
    }
)

LOWER_PROGRAM_CAPABILITIES: Final = MappingProxyType(
    {
        "cotton": _program(
            1,
            60,
            30,
            2,
            1500,
            (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60),
            (0, 20, 30, 40, 60, 90),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200, 1500),
        ),
        "synthetic": _program(
            2,
            40,
            30,
            2,
            1200,
            (5, 10, 15, 20, 25, 30, 35, 40, 45, 50),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200),
        ),
        "wool": _program(
            10,
            15,
            20,
            1,
            800,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30, 40),
            (0, 1, 2, 3),
            (0, 400, 600, 800),
        ),
        "bulky": _program(
            17,
            20,
            30,
            3,
            1000,
            (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000),
        ),
        "curtain": _program(
            4,
            5,
            30,
            2,
            800,
            (5, 10, 15, 20, 25, 30, 35, 40, 45, 50),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000),
        ),
        "bedding": _program(
            31,
            15,
            40,
            3,
            1000,
            (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000),
        ),
        "mix": _program(
            5,
            15,
            30,
            2,
            1000,
            (5, 10, 15, 20, 25, 30, 35, 40, 45, 50),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200),
        ),
        "jeans": _program(
            11,
            20,
            30,
            2,
            1000,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30, 40, 60),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000),
        ),
        "sports_wear": _program(
            18,
            5,
            20,
            1,
            800,
            (5, 10, 15, 20),
            (0, 20, 30, 40),
            (0, 1, 2, 3, 4, 5, 6),
            (0, 400, 600, 800, 1000),
        ),
        "outdoor_care": _program(
            32,
            15,
            30,
            2,
            800,
            (5, 10, 15, 20, 25, 30, 35, 40),
            (0, 20, 30, 40),
            (0, 1, 2, 3, 4, 5, 6),
            (0, 400, 600, 800, 1000),
        ),
        "shirts": _program(
            6,
            10,
            40,
            3,
            1000,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30, 40),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000),
        ),
        "down": _program(
            3,
            25,
            40,
            2,
            600,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30, 40),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800),
        ),
        "quick_wash": _program(
            15,
            3,
            0,
            1,
            1000,
            (3, 4, 5, 6, 7, 8, 9, 10),
            (0, 20, 30, 40),
            (0, 1, 2, 3, 4, 5, 6),
            (0, 400, 600, 800, 1000),
        ),
        "delicate_care": _program(
            19,
            10,
            30,
            2,
            600,
            (5, 10, 15, 20, 25, 30),
            (0, 20, 30),
            (0, 1, 2, 3),
            (0, 400, 600),
        ),
        "hygiene": _program(
            9,
            30,
            90,
            2,
            1000,
            (),
            (90,),
            (2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000),
        ),
        "rinse_spin": _program(
            27,
            -1,
            -1,
            2,
            1000,
            (),
            (),
            (1, 2, 3, 4, 5, 6, 7, 8),
            (0, 400, 600, 800, 1000, 1200, 1500),
        ),
        "spin": _program(
            28, -1, -1, -1, 1000, (), (), (), (400, 600, 800, 1000, 1200, 1500)
        ),
        "drum_clean": _program(13, 5, 90, 1, 600, (), (90,), (), (600,)),
        "dry": _program(29, -1, -1, -1, -1, (), (), (), ()),
        "refresh": _program(12, -1, -1, -1, -1, (), (), (), ()),
    }
)

PROGRAM_CAPABILITIES: Final = MappingProxyType(
    {
        "upper": UPPER_PROGRAM_CAPABILITIES,
        "lower": LOWER_PROGRAM_CAPABILITIES,
    }
)

DRYING_MODE_OPTIONS: Final = (
    DryingMode("off", 0, "305000"),
    DryingMode("auto", 10, "30500a"),
    DryingMode("30_min", 7, "305007"),
    DryingMode("60_min", 6, "305006"),
    DryingMode("90_min", 5, "305005"),
    DryingMode("120_min", 4, "305004"),
)
DRYING_MODES: Final = MappingProxyType(
    {mode.name: mode.value for mode in DRYING_MODE_OPTIONS}
)
DELAY_HALF_HOUR_STEPS: Final = range(49)
WASH_OPTION_BITS: Final = MappingProxyType(
    {
        "high_water": 0,
        "easy_iron": 1,
        "intensive": 2,
        "eco": 3,
        "soak": 5,
        "night": 6,
    }
)
_UPPER_PROGRAM_FUNCTIONS: Final = (
    ProgramFunction("delay", "20500O"),
    ProgramFunction("high_water", "20501b"),
    ProgramFunction("easy_iron", "20501c"),
    ProgramFunction("intensive", "20501d"),
    ProgramFunction("eco", "20501e"),
    ProgramFunction("soak", "20501f"),
    ProgramFunction("night", "20501g"),
)
_LOWER_PROGRAM_FUNCTIONS: Final = (
    ProgramFunction("delay", "20500Z"),
    ProgramFunction("drying", "205018"),
    ProgramFunction("high_water", "20501j"),
    ProgramFunction("easy_iron", "20501k"),
    ProgramFunction("intensive", "20501l"),
    ProgramFunction("eco", "20501m"),
    ProgramFunction("soak", "20501n"),
    ProgramFunction("night", "20501o"),
)
_PROGRAM_FUNCTIONS: Final = MappingProxyType(
    {
        "upper": _UPPER_PROGRAM_FUNCTIONS,
        "lower": _LOWER_PROGRAM_FUNCTIONS,
    }
)
_UPPER_FUNCTION_EXCLUSIONS: Final = MappingProxyType(
    {
        "delicate_care": ("high_water", "eco", "soak", "intensive"),
        "allergy_care": ("high_water", "eco", "intensive"),
        "colored_laundry": ("soak", "intensive"),
        "quick_wash": ("high_water", "eco", "soak", "intensive"),
        "rinse_spin": ("eco", "soak", "intensive"),
        "spin": ("high_water", "eco", "easy_iron", "soak", "intensive", "delay"),
        "drum_clean": ("high_water", "eco", "easy_iron", "soak", "intensive", "delay"),
    }
)
_LOWER_FUNCTION_EXCLUSIONS: Final = MappingProxyType(
    {
        "wool": ("drying", "high_water", "eco", "soak", "intensive"),
        "curtain": ("drying",),
        "jeans": ("soak",),
        "sports_wear": ("eco", "intensive"),
        "down": ("drying", "soak", "intensive"),
        "quick_wash": ("high_water", "eco", "soak", "intensive"),
        "delicate_care": ("drying", "high_water", "eco", "soak", "intensive"),
        "hygiene": ("high_water", "eco", "intensive"),
        "rinse_spin": ("eco", "soak", "intensive"),
        "spin": ("high_water", "eco", "easy_iron", "soak", "intensive", "delay"),
        "drum_clean": (
            "drying",
            "high_water",
            "eco",
            "easy_iron",
            "soak",
            "intensive",
            "delay",
        ),
        "dry": ("high_water", "eco", "night", "easy_iron", "soak", "intensive"),
        "refresh": ("high_water", "eco", "night", "easy_iron", "soak", "intensive"),
    }
)
_PROGRAM_FUNCTION_EXCLUSIONS: Final = MappingProxyType(
    {
        "upper": _UPPER_FUNCTION_EXCLUSIONS,
        "lower": _LOWER_FUNCTION_EXCLUSIONS,
    }
)
_LOWER_DRYING_MODE_EXCLUSIONS: Final = MappingProxyType(
    {
        "dry": ("off",),
        "refresh": ("off", "60_min", "90_min", "120_min", "auto"),
    }
)
_UPPER_CYCLE_MINUTES: Final = MappingProxyType(
    {
        "baby_care": 85,
        "underwear": 63,
        "delicate_care": 41,
        "casual": 51,
        "allergy_care": 131,
        "colored_laundry": 59,
        "kids_wear": 90,
        "quick_wash": 15,
        "towels": 68,
        "rinse_spin": 26,
        "spin": 7,
        "drum_clean": 70,
    }
)
_LOWER_CYCLE_MINUTES: Final = MappingProxyType(
    {
        "cotton": 118,
        "synthetic": 93,
        "wool": 46,
        "bulky": 92,
        "curtain": 58,
        "bedding": 97,
        "mix": 67,
        "jeans": 72,
        "sports_wear": 25,
        "outdoor_care": 55,
        "shirts": 70,
        "down": 72,
        "quick_wash": 15,
        "delicate_care": 41,
        "hygiene": 161,
        "rinse_spin": 26,
        "spin": 7,
        "drum_clean": 90,
        "dry": 150,
        "refresh": 30,
    }
)
_PROGRAM_CYCLE_MINUTES: Final = MappingProxyType(
    {
        "upper": _UPPER_CYCLE_MINUTES,
        "lower": _LOWER_CYCLE_MINUTES,
    }
)
STAIN_CAPABILITIES: Final = (
    StainCapability("W1", 30, 10, 1, "Fruit juice"),
    StainCapability("W2", 30, 10, 1, "Vegetable juice"),
    StainCapability("W3", 30, 25, 2, "Jam"),
    StainCapability("W4", 30, 25, 2, "Ketchup"),
    StainCapability("W5", 30, 25, 2, "Orange jam"),
    StainCapability("W6", 30, 25, 2, "Strawberry jam"),
    StainCapability("W7", 30, 25, 2, "Blueberry jam"),
    StainCapability("W8", 30, 25, 2, "Seesam jam"),
    StainCapability("W9", 30, 40, 3, "Milk stain"),
    StainCapability("W10", 30, 40, 3, "Milk tea"),
    StainCapability("W11", 50, 15, 4, "Chocolate"),
    StainCapability("W12", 55, 10, 5, "Wine stain"),
    StainCapability("W13", 55, 10, 5, "Wine"),
    StainCapability("W14", 55, 35, 6, "Tea stain"),
    StainCapability("W15", 60, 9, 7, "Blood stain"),
    StainCapability("W16", 60, 10, 8, "Grass stain"),
    StainCapability("W17", 60, 15, 9, "Lipstick"),
    StainCapability("W18", 60, 15, 9, "Lip gloss"),
    StainCapability("W19", 60, 20, 10, "Curry"),
    StainCapability("W20", 60, 20, 10, "Fermented bean curd"),
    StainCapability("W21", 60, 25, 11, "Coloured pencil"),
    StainCapability("W22", 60, 25, 11, "Shoe cream"),
    StainCapability("W23", 60, 30, 12, "Chili sauce"),
    StainCapability("W24", 60, 30, 12, "Chili oil"),
    StainCapability("W25", 60, 35, 13, "Coffee"),
    StainCapability("W26", 60, 35, 13, "Cooking oil"),
    StainCapability("W27", 60, 35, 13, "Chicken soup"),
    StainCapability("W28", 60, 35, 13, "Trotter soup"),
    StainCapability("W29", 60, 35, 13, "Oyster sauce"),
    StainCapability("W30", 60, 40, 14, "Soil stain"),
)
STAIN_LABEL_COUNT: Final = len(STAIN_CAPABILITIES)
STAIN_PRIORITY_LEVEL_COUNT: Final = len(
    {capability.priority_level for capability in STAIN_CAPABILITIES}
)
# Captured configurations, not an executable-program allowlist.
VERIFIED_STARTS: Final = (
    VerifiedStart("upper", "spin", 400),
    VerifiedStart("upper", "spin", 600),
    VerifiedStart("upper", "spin", 1000),
    VerifiedStart("upper", "spin", 1200),
    VerifiedStart("lower", "spin", 400),
)

# Target c.y/z fabric lists and wash.k.a/c(int) recipes, independently checked
# against DEX constructor registers. These are distinct from ordinary defaults.
_FABRIC_IDENTITIES: Final = MappingProxyType(
    {
        "A1": ("Outdoor jacket", 11),
        "A2": ("Silk clothes", 12),
        "A3": ("Gauze curtain", 13),
        "A4": ("Cotton dress", 13),
        "A5": ("Gauze dress", 13),
        "A6": ("Woolen sweater", 14),
        "A7": ("Down feather garment", 15),
        "A8": ("Synthetic fabric shirt", 16),
        "A9": ("Cotton & linen shirt", 16),
        "B1": ("Synthetic fabric coat", 2),
        "B2": ("Synthetic fabric trouses", 2),
        "C1": ("Jeans coat", 3),
        "C2": ("Jeans trouses", 3),
        "D1": ("Bed sheet", 4),
        "D2": ("Duvet cover", 4),
        "D3": ("Cotton curtain", 4),
        "D4": ("Blanket", 4),
        "E1": ("Cotton & linen coat", 51),
        "E2": ("Cotton & linen trouses", 51),
        "E3": ("Cotton underwear", 52),
        "E4": ("Kid coat", 52),
        "E5": ("Kid underwear", 52),
        "E6": ("Baby underwear", 51),
        "F1": ("School uniform", 6),
        "F2": ("Workwear", 6),
    }
)
_UPPER_FABRIC_RECIPES: Final = MappingProxyType(
    {
        11: ("delicate_care", ProgramSettings(10, 0, 2, None), 15, 0),
        # The source really has max wash zero for upper silk, not fifteen.
        12: ("delicate_care", ProgramSettings(10, 0, 2, None), 0, 30),
        13: ("delicate_care", ProgramSettings(10, 30, 2, 600), 15, 30),
        14: ("delicate_care", ProgramSettings(15, 0, 1, 600), 15, 40),
        16: ("casual", ProgramSettings(15, 40, 3, 1000), 30, 40),
        2: ("casual", ProgramSettings(20, 30, 2, 1000), 40, 60),
        3: ("casual", ProgramSettings(20, 30, 2, 1000), 60, 60),
        51: ("baby_care", ProgramSettings(15, 30, 2, 1000), 30, 60),
        52: ("underwear", ProgramSettings(10, 40, 3, 1000), 30, 60),
        6: ("baby_care", ProgramSettings(30, 30, 2, 1000), 45, 90),
    }
)
_LOWER_FABRIC_RECIPES: Final = MappingProxyType(
    {
        11: ("delicate_care", ProgramSettings(10, 0, 2, None), 15, 0),
        12: ("delicate_care", ProgramSettings(10, 0, 3, None), 15, 30),
        13: ("delicate_care", ProgramSettings(10, 30, 2, 600), 15, 30),
        14: ("wool", ProgramSettings(20, 0, 1, 600), 20, 40),
        15: ("down", ProgramSettings(20, 40, 2, 800), 25, 40),
        16: ("synthetic", ProgramSettings(10, 40, 3, 1000), 30, 40),
        2: ("synthetic", ProgramSettings(10, 30, 2, 1000), 60, 60),
        3: ("jeans", ProgramSettings(20, 30, 2, 1000), 40, 60),
        4: ("cotton", ProgramSettings(20, 30, 3, 1000), 60, 60),
        51: ("cotton", ProgramSettings(10, 30, 2, 1000), 60, 90),
        52: ("cotton", ProgramSettings(10, 40, 2, 1000), 60, 60),
        6: ("cotton", ProgramSettings(60, 30, 2, 1500), 120, 90),
    }
)
FABRIC_CAPABILITIES: Final = MappingProxyType(
    {
        "upper": tuple(
            FabricCapability(ui_id, name, *_UPPER_FABRIC_RECIPES[level])
            for ui_id, (name, level) in _FABRIC_IDENTITIES.items()
            if ui_id not in ("A3", "A7", "D1", "D2", "D3", "D4")
        ),
        "lower": tuple(
            FabricCapability(ui_id, name, *_LOWER_FABRIC_RECIPES[level])
            for ui_id, (name, level) in _FABRIC_IDENTITIES.items()
            if ui_id != "E6"
        ),
    }
)


def fabric_capabilities(drum: Drum) -> tuple[FabricCapability, ...]:
    """Return every fabric offered on the exact target drum."""
    try:
        return FABRIC_CAPABILITIES[drum]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Unsupported drum: {drum}") from error


def fabric_capability(drum: Drum, fabric: str) -> FabricCapability:
    """Resolve a fabric UI identity without accepting another drum's list."""
    for capability in fabric_capabilities(drum):
        if capability.ui_id == fabric:
            return capability
    raise ValueError(f"Unsupported {drum} fabric: {fabric}")


def fabric_stains(drum: Drum, fabric: str) -> tuple[StainCapability, ...]:
    """Return stain choices; smart recipes clamp them to material limits."""
    fabric_capability(drum, fabric)
    return STAIN_CAPABILITIES


def program_capabilities(drum: Drum, program: str) -> ProgramCapabilities:
    """Return one exact profile capability or reject an unknown program."""
    try:
        return PROGRAM_CAPABILITIES[drum][program]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Unsupported {drum} program: {program}") from error


def program_function_availability(
    drum: Drum, program: str
) -> ProgramFunctionAvailability:
    """Return source UI inclusion/exclusion without claiming combinations."""
    program_capabilities(drum, program)
    excluded_names = _PROGRAM_FUNCTION_EXCLUSIONS[drum].get(program, ())
    functions = _PROGRAM_FUNCTIONS[drum]
    return ProgramFunctionAvailability(
        available=tuple(item for item in functions if item.name not in excluded_names),
        excluded=tuple(item for item in functions if item.name in excluded_names),
    )


def program_drying_modes(program: str) -> tuple[DryingMode, ...]:
    """Return source-offered lower drying modes for one program."""
    availability = program_function_availability("lower", program)
    if any(item.name == "drying" for item in availability.excluded):
        return ()
    excluded = _LOWER_DRYING_MODE_EXCLUSIONS.get(program, ())
    return tuple(mode for mode in DRYING_MODE_OPTIONS if mode.name not in excluded)


def program_delay_capabilities(drum: Drum, program: str) -> ProgramDelayCapabilities:
    """Return source UI end-time steps, not the full raw coordinate space."""
    availability = program_function_availability(drum, program)
    cycle_minutes = _PROGRAM_CYCLE_MINUTES[drum][program]
    if any(item.name == "delay" for item in availability.excluded):
        steps: tuple[int, ...] = ()
    else:
        first_whole_hour_step = ((cycle_minutes + 59) // 60) * 2
        steps = (0, *range(first_whole_hour_step, DELAY_HALF_HOUR_STEPS.stop, 2))
    return ProgramDelayCapabilities(
        cycle_minutes=cycle_minutes,
        available_half_hour_steps=steps,
    )
