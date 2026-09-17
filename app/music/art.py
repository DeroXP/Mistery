"""Album covers, and the colours the interface borrows from them.

Covers come from the files themselves (a FLAC PICTURE block, an MP3 APIC frame,
an MP4 'covr' atom), then from the usual image files sitting next to the tracks.
Nothing is fetched: a cover is a fact about the album you already have.

The palette is what makes the album page and the Now Playing view take on the
look of the record. Two constraints shape it: the background colours are pulled
dark enough that white text stays readable on *any* cover (WCAG contrast, not
eyeballing), and the accent is lifted bright enough to be visible against them.
"""

from __future__ import annotations

import colorsys
import hashlib
import io
import json
import logging
import os
import re
from pathlib import Path

from ..config import music_art_dir

_log = logging.getLogger("music")

_LARGE = 1000
_SMALL = 360
_FOLDER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumartsmall", "art")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_ART_FOLDERS = ("artwork", "scans", "covers", "cover", "art", "images")
_DISC_FOLDER_RE = re.compile(r"^(?:cd|disc|disk)[\s._-]*\d{1,2}(?!\d)", re.IGNORECASE)


def cover_paths(album_key: str) -> tuple[Path, Path]:
    digest = hashlib.sha1(album_key.encode("utf-8")).hexdigest()[:16]
    folder = music_art_dir()
    return folder / f"{digest}.jpg", folder / f"{digest}-sm.jpg"


def embedded_cover(track_path: str | Path) -> bytes | None:
    """Raw image bytes from inside an audio file, preferring the front cover."""
    try:
        import mutagen
    except ImportError:
        return None
    try:
        audio = mutagen.File(track_path)
    except Exception:
        return None
    if audio is None:
        return None

    pictures = list(getattr(audio, "pictures", None) or [])          # FLAC
    if pictures:
        front = next((p for p in pictures if getattr(p, "type", 0) == 3), pictures[0])
        return bytes(front.data)

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None
    try:
        apic = [frame for key, frame in tags.items() if str(key).startswith("APIC")]
    except Exception:
        apic = []
    if apic:                                                           # MP3
        front = next((f for f in apic if getattr(f, "type", 0) == 3), apic[0])
        return bytes(front.data)

    covr = tags.get("covr") if hasattr(tags, "get") else None          # MP4
    if covr:
        return bytes(covr[0])

    blocks = tags.get("metadata_block_picture") if hasattr(tags, "get") else None
    if blocks:                                                         # Ogg / Opus
        import base64

        from mutagen.flac import Picture
        try:
            return bytes(Picture(base64.b64decode(blocks[0])).data)
        except Exception:
            return None
    return None


def folder_cover(folder: str | Path) -> Path | None:
    """cover.jpg, folder.png and friends; failing those, a lone image file.

    Rips often keep their images one level down, in Artwork/ or Scans/, and a
    multi-disc album keeps its cover one level up from CD1/ and CD2/. Both are
    looked at, but only for a file with a cover's name: a lone image in Scans/
    is as likely to be the back or the disc, and a lone image above a disc
    folder may be something else entirely.
    """
    folder = Path(folder)
    cover = _cover_in(folder, lone_image=True, subfolders=True)
    if cover is None and _DISC_FOLDER_RE.match(folder.name):
        cover = _cover_in(folder.parent, lone_image=False, subfolders=True)
    return cover


def _cover_in(folder: Path, lone_image: bool, subfolders: bool) -> Path | None:
    # os.scandir rather than iterdir + is_file: on Windows the directory listing
    # already says what each entry is, so this is one call per folder. It runs
    # on every watcher pass for albums still without a cover.
    try:
        with os.scandir(folder) as listing:
            entries = list(listing)
    except OSError:
        return None
    images, art_folders = [], []
    for entry in entries:
        try:
            if entry.is_file():
                if os.path.splitext(entry.name)[1].lower() in _IMAGE_EXTS:
                    images.append(Path(entry.path))
            elif subfolders and entry.name.lower() in _ART_FOLDERS and entry.is_dir():
                art_folders.append(Path(entry.path))
        except OSError:
            continue
    by_stem = {p.stem.lower(): p for p in images}
    for name in _FOLDER_NAMES:
        if name in by_stem:
            return by_stem[name]
    for art_folder in art_folders:
        cover = _cover_in(art_folder, lone_image=False, subfolders=False)
        if cover is not None:
            return cover
    return images[0] if lone_image and len(images) == 1 else None


def save_cover(album_key: str, data: bytes) -> tuple[str, str] | None:
    """Write the large and grid-sized covers. Returns their paths, or None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    large, small = cover_paths(album_key)
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
        for target, edge in ((large, _LARGE), (small, _SMALL)):
            copy = image.copy()
            copy.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            copy.save(target, "JPEG", quality=90, optimize=True)
    except Exception as exc:
        _log.warning("could not save cover for %s: %s", album_key, exc)
        return None
    return str(large), str(small)


# --- palette ------------------------------------------------------------------

def _luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG relative luminance."""
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _to_hex(rgb: tuple[float, float, float]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(v))) for v in rgb))


def _with_lightness(rgb, lightness: float, saturation_scale: float = 1.0):
    h, _, s = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    r, g, b = colorsys.hls_to_rgb(h, lightness, min(1.0, s * saturation_scale))
    return r * 255, g * 255, b * 255


def _darken_until(rgb, max_luminance: float):
    """Pull a colour down until white text on it clears WCAG AA with room."""
    h, l, s = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    while l > 0.02 and _luminance(tuple(round(v * 255) for v in colorsys.hls_to_rgb(h, l, s))) > max_luminance:
        l -= 0.02
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, l), s)
    return r * 255, g * 255, b * 255


def _brighten_until(rgb, min_luminance: float):
    h, l, s = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    while l < 0.92 and _luminance(tuple(round(v * 255) for v in colorsys.hls_to_rgb(h, l, s))) < min_luminance:
        l += 0.02
    r, g, b = colorsys.hls_to_rgb(h, min(1.0, l), s)
    return r * 255, g * 255, b * 255


def palette(image_path: str | Path) -> dict | None:
    """{'dark', 'mid', 'accent'} hex colours drawn from a cover.

    'dark' and 'mid' are the background gradient: white text on either has a
    contrast ratio of at least 7:1. 'accent' is the cover's most vivid colour,
    raised until it reads clearly against them — or a neutral when the cover is
    black-and-white, rather than inventing a hue that isn't there.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
    except Exception:
        return None

    image.thumbnail((96, 96))
    quantized = image.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
    raw = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)
    swatches = []
    total = sum(count for count, _ in counts) or 1
    for count, index in counts:
        rgb = tuple(raw[index * 3: index * 3 + 3])
        if len(rgb) == 3:
            swatches.append((count / total, rgb))
    if not swatches:
        return None

    def vividness(rgb) -> float:
        _, l, s = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
        return s * (1 - abs(l - 0.5) * 2)

    dominant = swatches[0][1]
    # Weighted so a speck of saturated colour doesn't outvote the cover's mood.
    vivid_share, vivid = max(swatches, key=lambda item: vividness(item[1]) * (item[0] ** 0.35))
    second = next((rgb for _, rgb in swatches[1:] if rgb != dominant), dominant)

    # White text at 7:1 needs luminance <= 0.10.
    dark = _darken_until(_with_lightness(dominant, 0.18, 0.9), 0.035)
    mid = _darken_until(_with_lightness(second, 0.30, 0.95), 0.10)

    if vividness(vivid) < 0.12:
        accent = (235, 235, 235)                 # a monochrome cover gets no fake hue
    else:
        accent = _brighten_until(_with_lightness(vivid, 0.55, 1.1), 0.30)

    return {"dark": _to_hex(dark), "mid": _to_hex(mid), "accent": _to_hex(accent)}


def palette_json(image_path: str | Path) -> str | None:
    colours = palette(image_path)
    return json.dumps(colours) if colours else None


def contrast(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two hex colours — for tests and sanity."""
    def rgb(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    a, b = _luminance(rgb(foreground)), _luminance(rgb(background))
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)
