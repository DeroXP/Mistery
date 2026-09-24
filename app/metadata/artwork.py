"""Offline artwork: pull a representative frame out of the video with ffmpeg.

This is the fallback whenever TMDB has no key, no match, or no network. HDR
sources are tone-mapped on the way out, otherwise every frame comes back as
washed-out grey.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import time
from pathlib import Path

from ..config import art_dir, find_ffmpeg, subprocess_flags

POSTER_SIZE = (600, 900)
BACKDROP_WIDTH = 1600

# PQ/BT.2020 -> BT.709. Hable keeps highlight detail without crushing midtones.
_TONEMAP = (
    "zscale=transfer=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=primaries=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=transfer=bt709:matrix=bt709:range=tv,"
    "format=yuv420p"
)

# Sample points as a fraction of runtime; the opening and the credits are dull
# or black, so we stay well inside the feature. Each one costs a full ffmpeg
# open of a multi-gigabyte file, so keep the list short.
_SAMPLE_POINTS = (0.22, 0.38, 0.54, 0.70)


def tonemap_prefix(hdr: str | None) -> str:
    """Tone-map suffix for a filter chain, or nothing for SDR sources."""
    return ("," + _TONEMAP) if hdr else ""


def _filter_chain(width: int, hdr: str | None) -> str:
    # Scale first: tone mapping at full 4K and then shrinking to a thumbnail
    # does the expensive part on ~340x more pixels than necessary.
    return f"scale={width}:-2:flags=lanczos" + tonemap_prefix(hdr)


def extract_frame(
    video_path: str,
    timestamp: float,
    width: int = BACKDROP_WIDTH,
    hdr: str | None = None,
    timeout: float = 60.0,
    cancel=None,
) -> bytes | None:
    """Grab one frame as JPEG bytes. Seeks before input, so it stays fast.

    Writes to a temp file rather than a pipe: the process is polled so it can be
    killed the moment the app is quitting, and an unread pipe would deadlock on
    anything bigger than the 64 KB Windows pipe buffer.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    with tempfile.TemporaryDirectory(prefix="mistery-frame-") as workdir:
        destination = Path(workdir) / "frame.jpg"
        command = [
            ffmpeg, "-nostdin", "-v", "error", "-y",
            "-ss", f"{max(0.0, timestamp):.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", _filter_chain(width, hdr),
            "-q:v", "3", str(destination),
        ]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess_flags(low_priority=True),
            )
        except OSError:
            return None

        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if (cancel is not None and cancel()) or time.monotonic() > deadline:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return None
            time.sleep(0.03)

        if process.returncode != 0 or not destination.is_file():
            return None
        try:
            return destination.read_bytes()
        except OSError:
            return None


def _score_frame(data: bytes) -> float:
    """Rank a candidate frame for poster use.

    Rewards detail and colour and penalises the murky, near-monochrome frames
    that dominate a dark film — those technically have contrast but make for a
    miserable poster.
    """
    try:
        from PIL import Image, ImageStat

        image = Image.open(io.BytesIO(data)).convert("RGB")
        stat = ImageStat.Stat(image)
        brightness = sum(stat.mean) / 3.0
        detail = sum(stat.stddev) / 3.0
        saturation = ImageStat.Stat(image.convert("HSV")).mean[1]
    except Exception:
        return 0.0

    if brightness < 30 or brightness > 242:
        return 0.0            # black frame, fade, or a blown-out flash

    # Mid-range exposure looks best; drift toward either extreme costs a little.
    exposure = 1.0 - min(1.0, abs(brightness - 110.0) / 130.0) * 0.45
    colour = 1.0 + min(saturation, 120.0) / 150.0
    return detail * exposure * colour


def pick_best_frame(
    video_path: str, duration: float, hdr: str | None, cancel=None
) -> bytes | None:
    """Sample a few points in the runtime and keep the most interesting frame."""
    if duration <= 0:
        return extract_frame(video_path, 60.0, hdr=hdr, cancel=cancel)

    best: bytes | None = None
    best_score = -1.0
    fallback: bytes | None = None      # any readable frame, however unpromising

    for fraction in _SAMPLE_POINTS:
        if cancel is not None and cancel():
            return best or fallback
        data = extract_frame(video_path, duration * fraction, hdr=hdr, cancel=cancel)
        if not data:
            continue
        if fallback is None:
            fallback = data
        score = _score_frame(data)
        if score > best_score:
            best, best_score = data, score
        if best_score > 95:       # excellent already, stop sampling
            break

    # A uniformly dark film scores 0 everywhere. A murky poster still beats no
    # poster, so never come back empty-handed when a frame was readable.
    return best if best_score > 0 else fallback


def _art_path(video_path: str, kind: str, extension: str = "jpg") -> Path:
    """Artwork filename, keyed on the file's identity *and* its current size.

    A file that changes — most often a download finishing, or a better rip
    replacing an old one — gets a fresh name, so stale artwork can never be
    served for new content.
    """
    try:
        size = Path(video_path).stat().st_size
    except OSError:
        size = 0
    digest = hashlib.sha1(f"{video_path}|{size}".encode("utf-8")).hexdigest()[:16]
    return art_dir() / f"{digest}-{kind}.{extension}"


def compose_poster(frame_bytes: bytes, destination: Path) -> bool:
    """Fit a 16:9 frame into a 2:3 poster over a blurred, darkened fill.

    Cropping a widescreen frame to poster shape throws away most of the image,
    so the frame is letterboxed over an enlarged blur of itself instead.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter

        frame = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
        width, height = POSTER_SIZE

        scale = max(width / frame.width, height / frame.height)
        background = frame.resize(
            (max(1, int(frame.width * scale)), max(1, int(frame.height * scale))),
            Image.LANCZOS,
        )
        left = (background.width - width) // 2
        top = (background.height - height) // 2
        background = background.crop((left, top, left + width, top + height))
        background = background.filter(ImageFilter.GaussianBlur(26))
        background = ImageEnhance.Brightness(background).enhance(0.45)

        foreground = frame.resize(
            (width, max(1, int(frame.height * width / frame.width))), Image.LANCZOS
        )
        if foreground.height > height:
            offset = (foreground.height - height) // 2
            foreground = foreground.crop((0, offset, width, offset + height))

        background.paste(foreground, (0, (height - foreground.height) // 2))
        background.save(destination, "JPEG", quality=88, optimize=True)
        return True
    except Exception:
        return False


def generate(
    video_path: str, duration: float, hdr: str | None, cancel=None
) -> dict:
    """Produce poster + backdrop from the video itself.

    Returns the columns to write back onto the media row. The `_url` columns
    are always empty here and say so out loud: this picture exists only on this
    PC, so there is nothing Discord could fetch. db.update_media would clear
    them anyway — writing a picture without its address means the old address
    no longer describes it — but a reader should not have to know that.
    """
    poster_path = _art_path(video_path, "poster")
    backdrop_path = _art_path(video_path, "backdrop")

    # 'fallback', never 'done': art cut from the file is a stopgap, and the row
    # stays eligible for an online upgrade on a later pass.
    if poster_path.is_file() and backdrop_path.is_file():
        return {
            "poster": str(poster_path),
            "poster_url": None,
            "backdrop": str(backdrop_path),
            "backdrop_url": None,
            "meta_state": "fallback",
            "meta_source": "ffmpeg",
        }

    frame = pick_best_frame(video_path, duration, hdr, cancel=cancel)
    if cancel is not None and cancel():
        return {"meta_state": "pending"}       # try again next time
    if not frame:
        return {"meta_state": "error"}

    try:
        backdrop_path.write_bytes(frame)
    except OSError:
        return {"meta_state": "error"}

    result = {
        "backdrop": str(backdrop_path),
        "backdrop_url": None,
        "meta_state": "fallback",
        "meta_source": "ffmpeg",
    }
    if compose_poster(frame, poster_path):
        result["poster"] = str(poster_path)
        result["poster_url"] = None
    return result
