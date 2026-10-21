"""Generate the U+ Reborn Haier Duo brand icons.

Run from the repository root:
    uv run --no-project --with pillow==12.0.0 python tools/generate_brand_icon.py

Rendering needs no photographs, fonts, textures or network access.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

SCALE = 4
CANVAS_SIZE = 512
BRAND_DIR = Path("custom_components/uplus_reborn/brand")
NAVY = "#0B2239"
TEAL = "#38D6C4"


def _box(values: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """Scale drawing bounds for antialiased 4x supersampling."""
    return tuple(round(value * SCALE) for value in values)


def _width(value: float) -> int:
    """Scale one stroke width."""
    return round(value * SCALE)


def _top_rounded_rectangle(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[float, float, float, float],
    radius: float,
    *,
    fill: int,
) -> None:
    """Round only the top corners so the lower edge meets its adjoining arc."""
    left, top, right, bottom = bounds
    draw.rounded_rectangle(_box(bounds), radius=_width(radius), fill=fill)
    draw.rectangle(_box((left, top + radius, right, bottom)), fill=fill)


def _u_frame_mask(size: tuple[int, int]) -> Image.Image:
    """Keep the U's 42px stroke and 12px cabinet gap constant around the base."""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    _top_rounded_rectangle(draw, (86, 66, 128, 380), 21, fill=255)
    _top_rounded_rectangle(draw, (360, 192, 402, 380), 21, fill=255)
    for offset, fill in ((54, 255), (12, 0)):
        radius = 38 + offset
        left, right, bottom = 140 - offset, 348 + offset, 418 + offset
        draw.rectangle(_box((left + radius, 380, right - radius, bottom)), fill=fill)
        draw.pieslice(
            _box((left, 380 - radius, left + 2 * radius, bottom)),
            start=90,
            end=180,
            fill=fill,
        )
        draw.pieslice(
            _box((right - 2 * radius, 380 - radius, right, bottom)),
            start=0,
            end=90,
            fill=fill,
        )
    return mask


def _chassis_mask(size: tuple[int, int]) -> Image.Image:
    """Share both lower corner centres with the U cradle."""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    _top_rounded_rectangle(draw, (140, 60, 348, 380), 14, fill=255)
    draw.rectangle(_box((178, 380, 310, 418)), fill=255)
    draw.pieslice(_box((140, 342, 216, 418)), start=90, end=180, fill=255)
    draw.pieslice(_box((272, 342, 348, 418)), start=0, end=90, fill=255)
    return mask


def _draw_drum(
    image: Image.Image,
    cx: float,
    cy: float,
    radius: float,
    *,
    upper_drum: bool = False,
) -> None:
    """Render dark glass, a thin silver rim and three shaded steel folds."""
    r = _width(radius)
    layer = Image.new("RGBA", (2 * r + 1, 2 * r + 1), (0, 0, 0, 0))
    pixels = layer.load()
    for py in range(2 * r + 1):
        ny = (py - r) / r
        for px in range(2 * r + 1):
            nx = (px - r) / r
            rho = math.hypot(nx, ny)
            if rho > 1:
                continue

            # One softbox reflection crosses all materials.
            sheen = math.exp(
                -1.55 * (((nx + 0.48) / 0.68) ** 2 + ((ny + 0.55) / 0.78) ** 2)
            )
            if rho >= 0.978:
                light = max(0.0, -0.61 * nx - 0.79 * ny)
                value = 153 + 96 * light
                color = (value - 4, value, value + 3)
            elif rho >= 0.960:
                color = (16 + 54 * sheen, 22 + 54 * sheen, 29 + 54 * sheen)
            elif rho > 0.705:
                bottom = 7 * math.exp(-((ny - 0.72) / 0.17) ** 2)
                value = 150 * sheen + bottom
                color = (9 + value, 15 + value, 23 + value)
            elif rho > 0.670:
                glint = math.exp(-((rho - 0.695) / 0.009) ** 2)
                value = 63 * sheen + 35 * glint * sheen
                color = (41 + value, 62 + value, 86 + value)
            else:
                q = rho / 0.670
                theta = math.atan2(ny, nx)
                # The nearest 120-degree sector defines one folded metal rib.
                orientation = math.pi / 2 + (0.30 if upper_drum else 0.0)
                sector = (
                    (theta - orientation + math.pi / 3) % (2 * math.pi / 3)
                    - math.pi / 3
                )
                along = q * math.cos(sector)
                across = q * math.sin(sector)
                taper = max(0.0, min(1.0, (0.87 - along) / 0.15))
                half_width = (0.14 + 0.09 * along) * taper
                rib = 0.0
                if half_width > 0:
                    edge = half_width - abs(across)
                    cover = max(0.0, min(1.0, edge / 0.018 + 0.5))
                    face = max(-1.0, min(1.0, across / half_width))
                    metal = 32 + 16 * sheen - 24 * face
                    edge_shadow = 10 * math.exp(-((edge + 0.023) / 0.020) ** 2)
                    rib = (cover * metal - edge_shadow) * (
                        0.22 if upper_drum else 0.40
                    )
                hub = (3 if upper_drum else 6) * max(
                    0.0, min(1.0, (0.205 - q) / 0.025)
                )
                lip = 19 * math.exp(-((q - 0.88) / 0.07) ** 2)
                cavity = 12 * math.exp(-((q - 0.38) / 0.30) ** 2)
                reflection = (
                    52
                    * math.exp(-((q - 0.77) / 0.037) ** 2)
                    * math.exp(-((theta + 1.94) / 0.68) ** 2)
                )
                value = 60 * sheen + lip - cavity + rib - hub + reflection
                color = (83 + value, 99 + value, 113 + value)
            pixels[px, py] = tuple(max(0, min(255, round(c))) for c in color) + (255,)
    image.alpha_composite(layer, dest=(_width(cx) - r, _width(cy) - r))


def _draw_panel(draw: ImageDraw.ImageDraw) -> None:
    """Render the colour TFT and small right-hand power button without text."""
    draw.rounded_rectangle(
        _box((145, 65, 343, 105)), radius=_width(9), fill="#EEF1F4"
    )
    draw.line(_box((142, 108, 346, 108)), fill="#ABB5C0", width=_width(1.2))
    draw.line(_box((143, 110, 345, 110)), fill="#FFFFFF", width=_width(0.8))
    draw.rounded_rectangle(
        _box((213, 70, 275, 101)), radius=_width(2.5), fill="#1B252E"
    )
    draw.rectangle(_box((216, 73, 223, 98)), fill="#080F15")
    for left, color, shade in (
        (225, "#DACBC8", "#978D91"),
        (241, "#CACAD0", "#8C9099"),
        (257, "#E1D8BF", "#A39B85"),
    ):
        draw.rectangle(_box((left, 73, left + 14, 87)), fill=color)
        draw.rectangle(_box((left, 89, left + 14, 98)), fill=shade)
    draw.rounded_rectangle(
        _box((312, 81, 326, 87)),
        radius=_width(1.2),
        fill="#F9FAFB",
        outline="#A8B0B9",
        width=_width(0.8),
    )
    draw.line(_box((319, 82, 319, 85)), fill="#9DA8B4", width=_width(0.8))


def _draw_plus(draw: ImageDraw.ImageDraw) -> None:
    """Separate the teal plus from the chassis and U with a navy border."""
    cx, cy = 400, 112
    for length, thickness, radius, color in (
        (68, 34, 22.6, NAVY),
        (52, 18, 13, TEAL),
    ):
        draw.rounded_rectangle(
            _box((cx - thickness, cy - length, cx + thickness, cy + length)),
            radius=_width(radius),
            fill=color,
        )
        draw.rounded_rectangle(
            _box((cx - length, cy - thickness, cx + length, cy + thickness)),
            radius=_width(radius),
            fill=color,
        )


def generate() -> None:
    """Render the selected Haier Duo design as 256px and 512px RGBA icons."""
    image = Image.new(
        "RGBA",
        (CANVAS_SIZE * SCALE, CANVAS_SIZE * SCALE),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(_box((20, 20, 492, 492)), radius=_width(96), fill=NAVY)
    image.paste(TEAL, (0, 0, image.width, image.height), _u_frame_mask(image.size))

    # Clip a cool-white horizontal reflection to the cabinet mask.
    gradient = Image.new("RGB", (image.width, 1))
    colors = []
    for x in range(image.width):
        t = max(0.0, min(1.0, (x / SCALE - 140) / 208))
        colors.append((round(248 - 17 * t), round(250 - 15 * t), round(252 - 12 * t)))
    gradient.putdata(colors)
    image.paste(gradient.resize(image.size), (0, 0), _chassis_mask(image.size))

    _draw_panel(draw)
    _draw_drum(image, cx=244, cy=175, radius=61, upper_drum=True)
    _draw_drum(image, cx=244, cy=325, radius=76)
    draw.ellipse(
        _box((313, 390, 333, 410)),
        fill="#E7EBEF",
        outline="#A5AFB9",
        width=_width(1.2),
    )
    draw.line(_box((320, 392, 325, 392)), fill="#7C8895", width=_width(1))
    _draw_plus(draw)

    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    resampling = Image.Resampling.LANCZOS
    image.resize((512, 512), resampling).save(BRAND_DIR / "icon@2x.png", optimize=True)
    image.resize((256, 256), resampling).save(BRAND_DIR / "icon.png", optimize=True)


if __name__ == "__main__":
    generate()
