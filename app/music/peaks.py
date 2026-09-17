"""A song's shape, for the screensaver's waveform.

One ffmpeg decode per file, reduced to a few hundred bars and kept in a file of
its own. The decode is the same shape as metadata/introdetect.py's — mono float
PCM down a pipe, below normal priority, polled so a quit does not leave ffmpeg
running — but it asks for 8 kHz rather than 11025: this wants how loud the song
is over time, not what is in it, and a lower rate is fewer bytes to move.

What is stored is the RMS of each bar, not its peak. Peak per bar on a modern
master is a flat wall at full scale — every bar reads 1.0 and the picture says
nothing. RMS shows the song: the quiet intro, the chorus, the drop. It is
normalised against the 98th percentile bar so one stray click cannot flatten the
rest, and quantised to a byte, which is finer than any screen can show.

Cache: one 494 byte file per song under data_dir()/peaks, named after the
file's path and carrying its size and mtime, so a re-rip is measured again. Not
a database table — this is a picture of a file, it is worthless if the file
changes, and a blob on `tracks` would ride along in every queued song's dict
(library.tracks() projects SELECT t.*).

Measured on this library's 24/96 FLACs: 0.29 s for a song of three to five
minutes (six songs, median), 1.5 s for a 37-minute one. That is the same class
of cost as loudness.measure, whose docstring records "about half a second for a
four-minute song", and it is paid once per file. Reading it back is 0.68 ms from
disk and 0.009 ms once it is in memory.

Measuring happens on one background thread, one song at a time, like the
loudness pass. Nothing here ever blocks the GUI thread: peaks_for() reads the
cache, request() drops a job on the queue, and a song with no measurement yet
draws a flat line until one arrives.

"Nothing blocks" includes the freshness check, which is the part that bit. The
header carries the source file's size and mtime so a re-rip is measured again,
and comparing them is a stat() — cheap on a local disk, 14.03 s here for one
UNC path on a NAS that had gone away, on whatever thread asked. peaks_for() is
called from WaveformView on the GUI thread once a second while a song is still
being measured, so that was a 14 s frozen window. The cache is therefore read
and handed back without asking the filesystem anything about the song, and the
worker does the stat afterwards: if the file has really changed it is measured
again and the new picture is in place by the next track change. The cost of
being wrong is one song showing its old shape for one play; the cost of being
right on the GUI thread was the whole window.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import queue
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from ..config import data_dir, find_ffmpeg, subprocess_flags

_log = logging.getLogger("music")

SAMPLE_RATE = 8000      # enough for an envelope; the picture has no frequency in it
STORED_BARS = 480       # what goes in the file: a 4K screen can ask for more than 240
MAX_SECONDS = 3600.0    # a decode this long is a broken file or a DJ set, not a song

_MAGIC = b"MPK1"
# magic, bars, the source file's size and mtime — the picture is only valid for
# the bytes it was measured from.
_HEADER = struct.Struct("<4sHII")

_lock = threading.Lock()
_memory: dict[str, np.ndarray] = {}      # path -> STORED_BARS of uint8
_failed: set[str] = set()                # no ffmpeg, or ffmpeg could not read it
_queued: set[str] = set()
_stamps: dict[str, tuple[int, int]] = {}  # path -> the (size, mtime) its cache was measured from
_rechecked: set[str] = set()             # cache handed back once; the worker has been asked
_jobs: "queue.Queue[str | None]" = queue.Queue()
_worker: threading.Thread | None = None
_stop = threading.Event()
_current: subprocess.Popen | None = None


# --- what the screensaver calls ---------------------------------------------------


def peaks_for(track: dict, bars: int = 240) -> list[float] | None:
    """`bars` levels between 0 and 1 across the whole song, or None if the file
    has not been measured yet. Never blocks the GUI thread."""
    path = _path_of(track)
    if not path:
        return None
    stored = _memory.get(path)
    if stored is None:
        if path in _failed:
            return None
        stored = _read_cache(path)
        if stored is None:
            return None
        with _lock:
            _memory[path] = stored
        _recheck(path)
    return _reduce(stored, bars)


def request(track: dict) -> None:
    """Measure this song in the background if it has not been measured."""
    path = _path_of(track)
    if not path or path in _memory or path in _failed:
        return
    with _lock:
        if path in _queued:
            return
        _queued.add(path)
    _ensure_worker()
    _jobs.put(path)


def _recheck(path: str) -> None:
    """Ask the worker, once per path per run, whether the file still matches the
    picture peaks_for() just handed back out of the cache. That comparison is a
    stat, and a stat is the one thing peaks_for() must not do."""
    with _lock:
        if path in _rechecked or path in _queued:
            return
        _rechecked.add(path)
        _queued.add(path)
    _ensure_worker()
    _jobs.put(path)


def measured(track: dict) -> bool:
    """True once peaks_for() would answer with something."""
    return peaks_for(track, 8) is not None


# --- the measurement --------------------------------------------------------------


def measure(path: str, cancel=None) -> np.ndarray | None:
    """STORED_BARS levels as uint8 for one file, or None if it cannot be read.

    Runs ffmpeg below normal priority and asks `cancel` four times a second
    while it does, the way loudness.measure does: without it, quitting mid-song
    left a hidden ffmpeg decoding for as long as the file took.
    """
    samples = _decode(path, cancel)
    if samples is None or samples.size < STORED_BARS * 4:
        return None
    # Trim to a whole number of bars and reshape, rather than reduceat over
    # ragged edges: a 4-minute song is 1.9 M samples, so the remainder thrown
    # away is under a millisecond of audio.
    per_bar = samples.size // STORED_BARS
    block = samples[: per_bar * STORED_BARS].reshape(STORED_BARS, per_bar)
    rms = np.sqrt(np.mean(np.square(block, dtype=np.float32), axis=1, dtype=np.float64))
    # The 98th percentile, not the maximum: one clipped sample or a cymbal
    # crash would otherwise push the whole song down into a thin line.
    reference = float(np.percentile(rms, 98))
    if not reference > 0:
        return None
    levels = np.clip(rms / reference, 0.0, 1.0)
    return (levels * 255.0 + 0.5).astype(np.uint8)


def _decode(path: str, cancel=None) -> np.ndarray | None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    command = [
        ffmpeg, "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0",
        "-t", f"{MAX_SECONDS:.0f}", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "-",
    ]
    global _current
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=subprocess_flags(low_priority=True),
        )
    except (OSError, ValueError):       # ValueError: a NUL in the path
        return None
    _current = process
    try:
        # A four-minute song at 8 kHz is 7.7 MB of float — far past a pipe's
        # buffer, so it has to be read while waiting. communicate() with a
        # timeout does that and can be called again without losing output.
        while True:
            try:
                output, _ = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if _stop.is_set() or (cancel is not None and cancel()):
                process.kill()
                try:
                    process.communicate(timeout=5)
                except subprocess.SubprocessError:
                    pass
                return None
    finally:
        _current = None
    if process.returncode != 0 or not output:
        return None
    return np.frombuffer(output, dtype=np.float32)


def _work() -> None:
    while not _stop.is_set():
        path = _jobs.get()
        if path is None:
            return
        try:
            _run_job(path)
        except Exception:                       # a broken file must not end the thread
            _log.exception("peaks: %s", path)
            with _lock:
                _queued.discard(path)


def _run_job(path: str) -> None:
    """The cache first, ffmpeg only if it is missing or out of date.

    This is where the stat lives. It also means a song already in the cache
    costs nothing on a fresh run: `request` only knows what is in memory, and
    memory is empty at startup, so without this every song played would be
    decoded again to rewrite the file it already had.
    """
    cached = _read_cache(path)
    if cached is not None and _matches_source(path) is not False:
        # Fresh, or a file we could not ask about (a NAS asleep, a drive
        # unplugged): either way the stored picture is the best answer there is.
        with _lock:
            _queued.discard(path)
            _memory[path] = cached
        return
    try:
        levels = measure(path, cancel=_stop.is_set)
    except Exception:
        _log.exception("peaks: %s", path)
        levels = None
    with _lock:
        _queued.discard(path)
        if levels is None:
            if not _stop.is_set() and cached is None:
                _failed.add(path)
        else:
            _memory[path] = levels
    if levels is not None:
        _write_cache(path, levels)


def _matches_source(path: str) -> bool | None:
    """Does the file still match what its cached picture was measured from?
    True, False, or None when the file could not be asked. Worker thread only —
    see the note at the top about the 14 s stat."""
    stat = _stat(path)
    if stat is None:
        return None
    with _lock:
        stamp = _stamps.get(path)
    return None if stamp is None else stat == stamp


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _stop.clear()
            _worker = threading.Thread(target=_work, name="peaks", daemon=True)
            _worker.start()


def shutdown() -> None:
    """Stop measuring and kill any ffmpeg we started. Daemon threads do not get
    to finish at exit, and the child would outlive the interpreter."""
    _stop.set()
    _jobs.put(None)
    process = _current
    if process is not None and process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


atexit.register(shutdown)


# --- the cache --------------------------------------------------------------------


_folder: Path | None = None
_folder_for: str | None = None


def cache_dir() -> Path:
    """Memoised against MISTERY_DATA_DIR: this is read on the GUI thread once a
    second while a song is still being measured, and data_dir() mkdirs on every
    call."""
    global _folder, _folder_for
    key = os.environ.get("MISTERY_DATA_DIR", "")
    if _folder is None or _folder_for != key:
        folder = data_dir() / "peaks"
        folder.mkdir(parents=True, exist_ok=True)
        _folder, _folder_for = folder, key
    return _folder


def _path_of(track) -> str | None:
    """The queue carries dicts and library.tracks() hands out sqlite3.Rows;
    both reach here, and a Row is not a dict."""
    if track is None:
        return None
    try:
        path = track["path"]
    except (TypeError, KeyError, IndexError):
        return None
    return str(path) if path else None


def _key(path: str) -> str:
    # Case-folded: Windows hands the same file back as C:\ and c:\ depending on
    # who asked, and two cache files for one song is just waste.
    return hashlib.sha1(path.lower().encode("utf-8", "replace")).hexdigest()[:16]


def _stat(path: str) -> tuple[int, int] | None:
    # ValueError as well as OSError: a path with an embedded NUL in it — one
    # malformed row in the library — raises ValueError out of stat(), and this
    # used to put a traceback through poll_levels once a second.
    try:
        info = Path(path).stat()
    except (OSError, ValueError):
        return None
    return int(info.st_size) & 0xFFFFFFFF, int(info.st_mtime) & 0xFFFFFFFF


def _read_cache(path: str) -> np.ndarray | None:
    """The stored picture, without asking the filesystem about the song itself.

    The header's (size, mtime) is remembered rather than compared, because
    comparing it means a stat — see the note at the top. _matches_source() does
    the comparison on the worker.
    """
    try:
        raw = (cache_dir() / f"{_key(path)}.mpk").read_bytes()
    except (OSError, ValueError):
        return None
    if len(raw) < _HEADER.size:
        return None
    magic, bars, size, mtime = _HEADER.unpack_from(raw)
    if magic != _MAGIC or len(raw) != _HEADER.size + bars:
        return None
    with _lock:
        _stamps[path] = (size, mtime)
    return np.frombuffer(raw, dtype=np.uint8, offset=_HEADER.size, count=bars)


def _write_cache(path: str, levels: np.ndarray) -> None:
    stat = _stat(path)
    if stat is None:
        return
    try:
        target = cache_dir() / f"{_key(path)}.mpk"
        header = _HEADER.pack(_MAGIC, int(levels.size), stat[0], stat[1])
        # Written beside and renamed: a half-written file read by the next run
        # would be a picture of nothing, and the header check cannot see it.
        temporary = target.with_suffix(".part")
        temporary.write_bytes(header + levels.tobytes())
        temporary.replace(target)
    except (OSError, ValueError) as exc:
        _log.debug("peaks: could not cache %s (%s)", path, exc)
        return
    with _lock:
        _stamps[path] = stat


def _reduce(stored: np.ndarray, bars: int) -> list[float]:
    """Fewer bars than are stored, by taking the loudest in each group."""
    bars = max(8, min(int(bars), int(stored.size)))
    if bars == stored.size:
        return (stored.astype(np.float32) / 255.0).tolist()
    edges = np.linspace(0, stored.size, bars + 1).astype(np.int64)
    grouped = np.maximum.reduceat(stored, edges[:-1])
    return (grouped.astype(np.float32) / 255.0).tolist()


def forget(track: dict) -> None:
    """Drop what is remembered about one song (tests, and a file that changed)."""
    path = _path_of(track)
    if not path:
        return
    with _lock:
        _memory.pop(path, None)
        _failed.discard(path)
        _stamps.pop(path, None)
        _rechecked.discard(path)
    try:
        (cache_dir() / f"{_key(path)}.mpk").unlink()
    except (OSError, ValueError):
        pass


def wait_for(track: dict, timeout: float = 30.0) -> bool:
    """Block until this song has been measured. Tests only — never call this
    from the GUI thread."""
    path = _path_of(track)
    if not path:
        return False
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path in _memory or path in _failed:
            return path in _memory
        time.sleep(0.05)
    return False
