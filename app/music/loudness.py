"""How loud a song actually is, so every album plays at the same level.

Downloaded albums disagree wildly about loudness. In this library the Deftones
FLACs carry ReplayGain tags asking for −11 to −13 dB while the Die Lit MP3s
carry none, so the same volume knob played one album twelve decibels below the
other. ReplayGain also aims at a 1990s reference level that is far below what
streaming services use today, which is why everything sounded quiet.

So Mistery measures the songs itself, once, with the same yardstick everyone
now uses: EBU R128 integrated loudness (LUFS) and true peak, via ffmpeg. A
measurement is a decode with no output — about half a second for a four-minute
song — and it is stored, so it is paid for once per file.

Playback then applies one number per file: the decibels needed to land on the
chosen target. mpv takes it as a per-file option on its playlist, the same way
it applies ReplayGain, so the gain is already in place when a track starts and
an album still runs gapless.

Nothing is ever pushed into clipping: the measured true peak caps the gain, so
a quiet song is only lifted as far as its own peaks allow.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import time
from pathlib import Path

from ..config import find_ffmpeg, subprocess_flags

_log = logging.getLogger("music")

# What "normal" means, in LUFS. The names are the ones streaming services use;
# the numbers are theirs too (Spotify normalises to −14, its "loud" setting to
# −11, and ReplayGain's own reference lands near −18).
TARGETS: dict[str, float | None] = {
    "off": None,
    "quiet": -18.0,
    "normal": -14.0,
    "loud": -11.0,
}

TARGET_LABELS = {
    "off": "Off — play files as they are",
    "quiet": "Quiet",
    "normal": "Normal",
    "loud": "Loud",
}

# ReplayGain tags are written against a reference near −18 LUFS, so a tag can
# stand in for a real measurement until one is made.
_RG_REFERENCE = -18.0

# The gain mpv is allowed to apply. The ceiling matches --volume-gain-max, and
# nothing sane needs more than a few dB up; the floor is for wildly hot masters.
MAX_GAIN = 12.0
MIN_GAIN = -24.0
# Leave a decibel of room under full scale: inter-sample peaks and the odd
# resampler overshoot live there.
PEAK_CEILING = -1.0

_INTEGRATED_RE = re.compile(r"I:\s*(-?[\d.]+|-inf)\s*LUFS")
_PEAK_RE = re.compile(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS")


class Unmeasurable(RuntimeError):
    """ffmpeg could not measure this file (missing, broken, or still arriving)."""


def measure(path: str | Path, timeout: float = 180.0, cancel=None) -> tuple[float, float]:
    """(integrated LUFS, true peak dBFS) for one file. Raises Unmeasurable.

    ffmpeg runs below normal priority, like all background ffmpeg work: it
    decodes flat out on one core, and a new album of 24/96 FLACs measured at
    normal priority competed with a game or a 4K film.

    `cancel` is asked while ffmpeg runs. When it says stop, ffmpeg is killed and
    Unmeasurable raised, so a caller should ask it again before blaming the
    file. A 37-minute hi-res track takes about 30 s, and quitting used to leave
    the process running, hidden, for that long. Time spent inside `cancel` (a
    pause) does not count towards `timeout`.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise Unmeasurable("ffmpeg not found")
    try:
        process = subprocess.Popen(
            [ffmpeg, "-nostdin", "-hide_banner", "-i", str(path), "-map", "a:0",
             "-af", "ebur128=peak=true", "-f", "null", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=subprocess_flags(low_priority=True),
        )
    except OSError as exc:
        raise Unmeasurable(str(exc)) from exc

    # The per-frame log fills stderr far beyond a pipe's buffer, so it has to
    # be read while waiting; communicate() with a timeout does that and can be
    # called again without losing output.
    started = time.monotonic()
    held = 0.0
    while True:
        try:
            _, stderr = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None:
            asked = time.monotonic()
            if cancel():
                _kill(process)
                raise Unmeasurable("stopped")
            held += time.monotonic() - asked
        if time.monotonic() - started - held > timeout:
            _kill(process)
            raise Unmeasurable(f"ffmpeg took longer than {timeout:.0f} s")
    text = stderr.decode("utf-8", "replace")
    summary = text[text.rfind("Integrated loudness"):]
    found = _INTEGRATED_RE.search(summary)
    peaks = _PEAK_RE.findall(summary)
    if not found or not peaks:
        raise Unmeasurable((text.strip().splitlines() or ["no output"])[-1][:160])
    if found.group(1) == "-inf" or peaks[-1] == "-inf":
        raise Unmeasurable("silent")
    return float(found.group(1)), float(peaks[-1])


def _kill(process: subprocess.Popen) -> None:
    process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.SubprocessError:
        pass


def combine(measurements: list[tuple[float, float]]) -> tuple[float, float] | None:
    """One loudness for a whole album, from its tracks.

    Loudness is a mean of energy over time, so tracks combine as their energies
    weighted by length — the same answer playing the album as one long file
    would give, give or take the gating at song boundaries. The album's peak is
    simply its loudest moment.
    """
    usable = [(lufs, peak, length) for lufs, peak, length in
              ((m[0], m[1], m[2] if len(m) > 2 else 1.0) for m in measurements)
              if lufs is not None and length and length > 0]
    if not usable:
        return None
    energy = sum(length * 10 ** (lufs / 10) for lufs, _, length in usable)
    total = sum(length for _, _, length in usable)
    return 10 * math.log10(energy / total), max(peak for _, peak, _ in usable)


def target_db(setting: str | None) -> float | None:
    return TARGETS.get(str(setting or "loud").lower(), TARGETS["loud"])


def gain_for(loudness: float | None, peak: float | None, target: float | None,
             rg_db: float | None = None) -> float:
    """Decibels to apply to a file so it lands on `target`, without clipping.

    Falls back to a ReplayGain tag when the file has not been measured yet, so
    a fresh library is levelled roughly from the first play and exactly from
    the second.
    """
    if target is None:
        return 0.0
    if loudness is not None:
        gain = target - float(loudness)
    elif rg_db is not None:
        gain = float(rg_db) + (target - _RG_REFERENCE)
    else:
        return 0.0
    gain = max(MIN_GAIN, min(MAX_GAIN, gain))
    if peak is not None:
        # Never lift a file past the point where its own peaks would clip.
        gain = min(gain, PEAK_CEILING - float(peak))
    return round(gain, 2)
