"""Programmatic application icon (no external assets required)."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ACCENT_TOP = (37, 99, 235)
ACCENT_BOTTOM = (29, 78, 216)
SHIELD = (255, 255, 255, 255)
GEAR = (30, 64, 175, 255)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _gradient(size: int) -> Image.Image:
    gradient = Image.new("RGB", (1, size))
    pixels = gradient.load()
    for y in range(size):
        t = y / max(1, size - 1)
        # Slight ease so the middle stays vivid.
        t = t * t * (3 - 2 * t)
        color = tuple(
            int(round(ACCENT_TOP[i] * (1 - t) + ACCENT_BOTTOM[i] * t)) for i in range(3)
        )
        pixels[0, y] = color
    return gradient.resize((size, size), Image.Resampling.BILINEAR)


def _bezier(p0, p1, p2, p3, steps: int = 30):
    points = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = (
            mt**3 * p0[0]
            + 3 * mt**2 * t * p1[0]
            + 3 * mt * t**2 * p2[0]
            + t**3 * p3[0]
        )
        y = (
            mt**3 * p0[1]
            + 3 * mt**2 * t * p1[1]
            + 3 * mt * t**2 * p2[1]
            + t**3 * p3[1]
        )
        points.append((x, y))
    return points


def _shield_points(cx: float, top: float, width: float, height: float) -> list[tuple[float, float]]:
    left = cx - width / 2
    right = cx + width / 2
    corner = width * 0.12
    shoulder = top + height * 0.58
    bottom = top + height
    points: list[tuple[float, float]] = []

    # Rounded top-left corner.
    for i in range(13):
        angle = math.pi + (math.pi / 2) * (i / 12)
        points.append(
            (left + corner + math.cos(angle) * corner, top + corner + math.sin(angle) * corner)
        )
    # Rounded top-right corner.
    for i in range(13):
        angle = -math.pi / 2 + (math.pi / 2) * (i / 12)
        points.append(
            (right - corner + math.cos(angle) * corner, top + corner + math.sin(angle) * corner)
        )
    points.append((right, shoulder))
    # Curved right flank down to the tip.
    points.extend(
        _bezier(
            (right, shoulder),
            (right - width * 0.02, top + height * 0.80),
            (cx + width * 0.20, top + height * 0.94),
            (cx, bottom),
            steps=24,
        )
    )
    # Mirror the flank back up.
    points.extend(
        _bezier(
            (cx, bottom),
            (cx - width * 0.20, top + height * 0.94),
            (left + width * 0.02, top + height * 0.80),
            (left, shoulder),
            steps=24,
        )
    )
    points.append((left, top + corner))
    return points


def _draw_gear(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, teeth: int = 8):
    inner = radius * 0.80
    tooth_half = math.pi / teeth * 0.46
    step = 2 * math.pi / teeth
    points: list[tuple[float, float]] = []
    for i in range(teeth):
        center = i * step - math.pi / 2
        for angle, r in (
            (center - tooth_half, inner),
            (center - tooth_half * 0.62, radius),
            (center + tooth_half * 0.62, radius),
            (center + tooth_half, inner),
            (center + step / 2, inner),
        ):
            points.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
    draw.polygon(points, fill=GEAR)
    # Center bore.
    hole = radius * 0.36
    draw.ellipse((cx - hole, cy - hole, cx + hole, cy + hole), fill=SHIELD)


def create_icon_image(size: int = 256) -> Image.Image:
    """Render the app icon at the requested pixel size."""
    supersample = 4
    s = size * supersample
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    mask = _rounded_mask(s, int(s * 0.225))
    canvas.paste(_gradient(s), (0, 0), mask)

    # Soft inner highlight for depth.
    highlight = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.ellipse(
        (-s * 0.35, -s * 0.55, s * 1.35, s * 0.55),
        fill=(255, 255, 255, 26),
    )
    canvas.alpha_composite(highlight)

    # Shield shadow.
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    shield = _shield_points(s / 2, s * 0.155, s * 0.60, s * 0.665)
    sd.polygon([(x + s * 0.012, y + s * 0.018) for x, y in shield], fill=(15, 23, 42, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=s * 0.012))
    canvas.alpha_composite(shadow)

    draw = ImageDraw.Draw(canvas)
    draw.polygon(shield, fill=SHIELD)
    _draw_gear(draw, s / 2, s * 0.455, s * 0.145)

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def save_ico(path: str | Path, size: int = 256) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = create_icon_image(size)
    image.save(
        path,
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return path


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "assets" / "app.ico"
    save_ico(target)
    print(target)
