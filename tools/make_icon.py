"""Generate the application icon.

Draws a dark rounded tile with an amber play mark at 4x, then downsamples into
a multi-resolution .ico so it stays crisp from 256px down to the 16px used in
the taskbar and title bar.

    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

BG_TOP = (26, 31, 40)
BG_BOTTOM = (10, 13, 18)
AMBER = (240, 180, 41)
AMBER_LIGHT = (255, 205, 90)

MASTER = 1024
SS = 4                      # supersampling factor for the vector shapes


def _rounded_mask(size: int, radius_ratio: float) -> Image.Image:
    mask = Image.new("L", (size * SS, size * SS), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, size * SS - 1, size * SS - 1),
        radius=int(size * SS * radius_ratio),
        fill=255,
    )
    return mask.resize((size, size), Image.LANCZOS)


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    gradient = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        gradient.putpixel((0, y), tuple(
            round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)
        ))
    return gradient.resize((size, size), Image.BICUBIC)


def build_master() -> Image.Image:
    size = MASTER
    base = _vertical_gradient(size, BG_TOP, BG_BOTTOM).convert("RGBA")

    # Warm glow behind the play mark so the tile doesn't read as flat black.
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (size * 0.16, size * 0.16, size * 0.84, size * 0.84),
        fill=AMBER + (58,),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.10))
    base = Image.alpha_composite(base, glow)

    # Play triangle, nudged right so it looks optically centred.
    mark = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    cx, cy = size * SS * 0.53, size * SS * 0.5
    half_h = size * SS * 0.215
    width = size * SS * 0.375
    draw.polygon(
        [
            (cx - width * 0.48, cy - half_h),
            (cx + width * 0.52, cy),
            (cx - width * 0.48, cy + half_h),
        ],
        fill=AMBER_LIGHT + (255,),
    )
    base = Image.alpha_composite(base, mark.resize((size, size), Image.LANCZOS))

    # Hairline inner edge for definition against dark backgrounds.
    edge = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        (SS * 2, SS * 2, size * SS - SS * 2, size * SS - SS * 2),
        radius=int(size * SS * 0.22),
        outline=(255, 255, 255, 30),
        width=SS * 3,
    )
    base = Image.alpha_composite(base, edge.resize((size, size), Image.LANCZOS))

    base.putalpha(_rounded_mask(size, 0.22))
    return base


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = build_master()

    master.save(ASSETS / "icon.png")
    frames = [master.resize((s, s), Image.LANCZOS) for s in SIZES]
    frames[-1].save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=frames[:-1],
    )
    print(f"wrote {ASSETS / 'icon.ico'} ({', '.join(str(s) for s in SIZES)})")
    print(f"wrote {ASSETS / 'icon.png'} (1024)")


if __name__ == "__main__":
    main()
