"""Offline display facts, independent of discovery validity or control support.

Category-only facts: U+ Euro 2.6.0 factory and English resources, APK SHA256
30fd548089468cddd9b5ccce04643f9529d70016e748141d04c70f593a0ced9e.
Model facts: manufacturer's /uplusi/products catalog, categories 1/2/3,
retrieved 2026-10-20. The target also has inspected-profile/hardware evidence.
Names are catalog candidates, not model strings advertised by UDP responders.
Only factual correspondences are recorded here; no APK implementation/artwork.
"""

from dataclasses import dataclass
from types import MappingProxyType

from .const import MODEL, SUPPORTED_WIFI_TYPE

CATALOG_DATE = "2026-10-20"
CATALOG_SOURCE = "https://uhome.haieriot.net/uplusi/products"


@dataclass(frozen=True, slots=True)
class TypeIdentification:
    """Immutable display evidence; empty models never imply the target model."""

    category: str = "unknown"
    models: tuple[str, ...] = ()
    category_source: str = "apk"
    model_source: str = "none"


_UNKNOWN = TypeIdentification(category_source="none")
_TYPES = MappingProxyType({
    "200861051c408504160400718001990000000000000071800162420000000040":
        TypeIdentification("freezer"),
    "201c51890c31c308050100218000674b00000000000000000000000000000040":
        TypeIdentification("washing_machine", ("HWD100-BD1499U1",), model_source="catalog"),
    "201c51890c31c308050100218002384500000000000000000000000000000040":
        TypeIdentification("washing_machine"),
    "201c51890c31c308050100218002384400000001000000000000000000000040":
        TypeIdentification("washing_machine"),
    "201c12002400081005010021800067414800bc00000000000000000000000040":
        TypeIdentification("washing_machine"),
    "201c51890c31c30805010021800067414800bc00000000000000000000000040":
        TypeIdentification("washing_machine", ("HW120_BD14889U1",), model_source="catalog"),
    "200861051c408504160400718001994100000000000071800162420000000040":
        TypeIdentification("freezer", ("H3F-320WTAAU1",), model_source="catalog"),
    "2010f0c2107387200121458c47df2b000000bb751c529fcd112a7835461dbec0":
        TypeIdentification("refrigerator", ("HTF-552DGS6U1",), model_source="catalog"),
    "200861051c408504081200718001324200000000000071800223a00000000040":
        TypeIdentification("wine_cellar"),
    "201c51890c31c308050100218002384400000000000000000000000000000040":
        TypeIdentification("washing_machine"),
    "201c51890c31c308050100218002384500000001000000000000000000000040":
        TypeIdentification("washing_machine"),
    "200861051c408504081100718002124200000000000071800223000000000040":
        TypeIdentification("wine_cellar"),
    SUPPORTED_WIFI_TYPE:
        TypeIdentification("washing_machine", (MODEL,), model_source="target"),
    "200861051c408504012300618002960000000000000060841164410000000040":
        TypeIdentification(
            "refrigerator",
            ("A3FE742CGBJU1(EE)", "A3FE742CGWJU1(EE)", "A3FE742CPJU1(EE)"),
            model_source="catalog",
        ),
    "200861051c408504012700718001990000000000000071800162b00000000040":
        TypeIdentification(
            "freezer", ("H3F-280WSAAU1", "H3F-320FSAAU1", "H3F-320WSAAU1"),
            category_source="catalog", model_source="catalog",
        ),
})


def identify_type(type_id: str) -> TypeIdentification:
    """Resolve exact source-backed candidates, never selecting within a family."""
    return _TYPES.get(type_id, _UNKNOWN)
