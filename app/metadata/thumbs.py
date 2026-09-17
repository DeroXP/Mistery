"""Seek-bar preview thumbnails.

Extraction strategy matters enormously here. Measured on a 2h21m 4K HEVC file:

    one fast-seek ffmpeg per thumbnail (120 procs)   456 s CPU
    one pass, keyframes only, software                23 s CPU
    one pass, keyframes only, d3d11va                 17 s CPU
    one pass + fps filter, d3d11va                    13 s CPU

The per-seek approach loses because every process re-opens and indexes a 6 GB
MKV. So we make a single pass, decode only keyframes, and let the GPU do it —
roughly 35x cheaper. The whole thing runs below normal priority.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import time
from pathlib import Path

from ..config import find_ffmpeg, subprocess_flags, thumbs_dir
from .artwork import tonemap_prefix

TILE_WIDTH = 208
COLUMNS = 10

# Frames land on keyframe boundaries, so precision is the file's GOP length
# (a few seconds) rather than exact. That is plenty for scrubbing.
_HWACCEL = "auto"


def sprite_paths(video_path: str) -> tuple[Path, Path]:
    digest = hashlib.sha1(video_path.encode("utf-8")).hexdigest()[:16]
    folder = thumbs_dir()
    return folder / f"{digest}.jpg", folder / f"{digest}.json"


def load_index(index_path: str | Path) -> dict | None:
    """Read the sprite descriptor written by `generate`."""
    try:
        data = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not data.get("sprite") or not Path(data["sprite"]).is_file():
        return None
    return data


def _extract_frames(
    video_path: str,
    destination: Path,
    interval: float,
    count: int,
    hdr: str | None,
    cancel=None,
    timeout: float = 900.0,
) -> int:
    """One ffmpeg pass writing numbered JPEGs. Returns how many were written."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return 0

    # Scale before tone mapping: mapping at full 4K then shrinking is wasted work.
    chain = f"fps=1/{interval:.4f},scale={TILE_WIDTH}:-2:flags=bilinear"
    chain += tonemap_prefix(hdr)

    command = [
        ffmpeg, "-nostdin", "-v", "error",
        "-hwaccel", _HWACCEL,
        "-skip_frame", "nokey",      # decode I-frames only
        "-i", video_path,
        "-vf", chain,
        "-frames:v", str(count),
        "-q:v", "4",
        str(destination / "%05d.jpg"),
    ]

    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess_flags(low_priority=True),
        )
    except OSError:
        return 0

    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if (cancel is not None and cancel()) or time.monotonic() > deadline:
            process.kill()
            process.wait(timeout=5)
            return 0
        time.sleep(0.1)

    return len(list(destination.glob("*.jpg")))


def _built_from(index: dict, index_path: Path, size: int, mtime: float) -> bool:
    """Was this sprite made from the file as it is now?

    Size alone can't say. A torrent writes a film at its final size before the
    first piece lands, so a sprite built mid-download (every tile past what had
    arrived a copy of the last readable frame) matched the finished film and was
    kept for good: 77 of 120 tiles frozen. The modified time moves when the last
    piece is written, as the scanner already relies on.
    """
    if index.get("source_size", size) != size:
        return False
    if "source_mtime" in index:
        return abs(float(index["source_mtime"]) - mtime) < 1.0
    # Made before the time was recorded. Trusted rather than needlessly rebuilt,
    # unless the file was written to after the sprite was.
    try:
        return index_path.stat().st_mtime >= mtime
    except OSError:
        return False


def generate(
    video_path: str,
    duration: float,
    hdr: str | None = None,
    count: int = 120,
    cancel=None,
) -> dict:
    """Build the sprite sheet. Returns media-row columns to persist."""
    if duration <= 5:
        return {"thumbs_state": "error"}

    try:
        # Taken before extraction: a download that writes its last piece while
        # frames are being pulled must not look like the file the sprite shows.
        source = Path(video_path).stat()
    except OSError:
        return {"thumbs_state": "error"}
    source_size, source_mtime = source.st_size, source.st_mtime

    sprite_path, index_path = sprite_paths(video_path)
    existing = load_index(index_path)
    if existing and _built_from(existing, index_path, source_size, source_mtime):
        return {"thumbs": str(index_path), "thumbs_state": "done"}

    count = max(12, min(int(count), 400))
    interval = duration / (count + 1)

    workdir = thumbs_dir() / f"tmp-{sprite_path.stem}"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        written = _extract_frames(
            video_path, workdir, interval, count, hdr, cancel=cancel
        )
        if cancel is not None and cancel():
            return {"thumbs_state": "pending"}
        if written < 4:
            return {"thumbs_state": "error"}

        try:
            from PIL import Image
        except ImportError:
            return {"thumbs_state": "error"}

        files = sorted(workdir.glob("*.jpg"))
        tiles = []
        for path in files:
            try:
                tiles.append(Image.open(path).convert("RGB"))
            except Exception:
                continue
        if len(tiles) < 4:
            return {"thumbs_state": "error"}

        tile_w, tile_h = tiles[0].size
        columns = COLUMNS
        rows = math.ceil(len(tiles) / columns)
        sheet = Image.new("RGB", (columns * tile_w, rows * tile_h), (0, 0, 0))
        for index, tile in enumerate(tiles):
            if tile.size != (tile_w, tile_h):
                tile = tile.resize((tile_w, tile_h), Image.LANCZOS)
            sheet.paste(tile, ((index % columns) * tile_w, (index // columns) * tile_h))

        # Frames come out on the fps grid, so tile N covers N * interval seconds.
        sheet.save(sprite_path, "JPEG", quality=76, optimize=True)
        index_path.write_text(
            json.dumps({
                "sprite": str(sprite_path),
                "count": len(tiles),
                "columns": columns,
                "rows": rows,
                "tile_width": tile_w,
                "tile_height": tile_h,
                "interval": interval,
                "duration": duration,
                "source_size": source_size,
                "source_mtime": source_mtime,
            }),
            encoding="utf-8",
        )
    except OSError:
        return {"thumbs_state": "error"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return {"thumbs": str(index_path), "thumbs_state": "done"}


def tile_rect(index_data: dict, position: float) -> tuple[int, int, int, int]:
    """Pixel rect of the tile covering `position` seconds within the sprite."""
    interval = index_data.get("interval") or 1.0
    count = index_data.get("count") or 1
    columns = index_data.get("columns") or COLUMNS
    tile_w = index_data.get("tile_width") or TILE_WIDTH
    tile_h = index_data.get("tile_height") or int(TILE_WIDTH * 9 / 16)

    index = int(max(0.0, position) / interval)
    index = max(0, min(count - 1, index))
    return (index % columns) * tile_w, (index // columns) * tile_h, tile_w, tile_h
