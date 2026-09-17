"""Find intros and credits automatically by matching audio across episodes.

There is no public database of intro timestamps, so this does what Plex and
Jellyfin do: the intro is, by definition, the stretch of *identical audio* that
appears in every episode of a season. We fingerprint the audio and look for it.

Pipeline per episode:
    ffmpeg → 11 kHz mono PCM for the head (default 8 min) and tail (4 min)
    → short-time FFT → 16 log-band energies per ~186 ms frame
    → 32-bit hash per frame (band-to-band and frame-to-frame energy deltas,
      chromaprint-style), so a season is just a few thousand ints.

Matching: for a pair of episodes, slide one fingerprint over the other and
find sustained runs where the per-frame Hamming distance stays low. A run that
shows up against most sibling episodes at a consistent place is the intro
(head) or the credits music (tail). Cold opens are handled naturally: the
match is *per episode*, wherever the intro actually starts in that file.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

import numpy as np

from ..config import find_ffmpeg, subprocess_flags

SAMPLE_RATE = 11025
FRAME = 4096                      # ~372 ms of audio per FFT window
HOP = 2048                        # ~186 ms per fingerprint frame
BANDS = 16
HEAD_SECONDS = 480.0              # search the first 8 minutes for the intro
TAIL_SECONDS = 240.0              # and the last 4 for the credits

SECONDS_PER_FRAME = HOP / SAMPLE_RATE

_MAX_HAMMING = 8                  # ≤8 of 32 bits differing still counts as a match
                                  # (random frames average 16, so this is selective)
_MIN_RUN_FRAMES = int(8.0 / SECONDS_PER_FRAME)      # intros shorter than 8s aren't
_MAX_GAP_FRAMES = 4               # bridge tiny mismatches inside a run

# How much of the sibling episodes must agree before we trust a window.
_MIN_VOTES = 2


def _pcm(path: str, start: float, duration: float, timeout: float = 120.0,
         cancel=None) -> np.ndarray | None:
    """Decode a slice of the file to mono float32 PCM.

    `cancel` is asked while ffmpeg runs, and a yes kills it. A decode was only
    bounded by the two-minute timeout before, so quitting mid-season could leave
    a hidden Mistery running that long. Time spent inside `cancel` (a pause)
    does not count towards `timeout`: ffmpeg finishes meanwhile, and an episode
    must not lose its fingerprint because a film was watched in between.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    command = [
        ffmpeg, "-nostdin", "-v", "error",
        "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}",
        "-i", path, "-map", "0:a:0",
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "-",
    ]
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=subprocess_flags(low_priority=True),
        )
    except OSError:
        return None

    # Eight minutes of PCM is about 21 MB, far past a pipe's buffer, so it is
    # read while waiting; communicate() with a timeout does that and can be
    # called again without losing output.
    started = time.monotonic()
    held = 0.0
    while True:
        try:
            output, _ = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        stop = False
        if cancel is not None:
            asked = time.monotonic()
            stop = cancel()
            held += time.monotonic() - asked
        if stop or time.monotonic() - started - held > timeout:
            process.kill()
            try:
                process.communicate(timeout=5)
            except subprocess.SubprocessError:
                pass
            return None
    if process.returncode != 0 or len(output) < FRAME * 4:
        return None
    return np.frombuffer(output, dtype=np.float32)


def fingerprint(path: str, start: float, duration: float, cancel=None) -> np.ndarray | None:
    """One uint32 hash per ~186 ms of audio, or None if the audio is unreadable."""
    samples = _pcm(path, start, duration, cancel=cancel)
    if samples is None or samples.size < FRAME + HOP:
        return None

    count = (samples.size - FRAME) // HOP + 1
    window = np.hanning(FRAME).astype(np.float32)
    frames = np.lib.stride_tricks.as_strided(
        samples,
        shape=(count, FRAME),
        strides=(samples.strides[0] * HOP, samples.strides[0]),
    )
    spectrum = np.abs(np.fft.rfft(frames * window, axis=1))

    # 16 log-spaced bands between 100 Hz and 4 kHz — where themes and dialogue live.
    freqs = np.fft.rfftfreq(FRAME, d=1.0 / SAMPLE_RATE)
    edges = np.geomspace(100.0, 4000.0, BANDS + 1)
    bands = np.empty((count, BANDS), dtype=np.float64)
    for band in range(BANDS):
        mask = (freqs >= edges[band]) & (freqs < edges[band + 1])
        bands[:, band] = spectrum[:, mask].sum(axis=1)
    bands = np.log1p(bands)

    # A full 32 bits per frame, or Hamming thresholds mean nothing:
    #   15 bits — sign of the band-to-band energy gradient (second derivative)
    #   16 bits — sign of each band's change since the previous frame
    #    1 bit  — overall energy rising or falling
    spatial = (bands[1:, :-1] - bands[1:, 1:]
               - (bands[:-1, :-1] - bands[:-1, 1:]) > 0).astype(np.uint32)   # 15
    temporal = (bands[1:] - bands[:-1] > 0).astype(np.uint32)                # 16
    energy_rising = (bands[1:].sum(axis=1) > bands[:-1].sum(axis=1)).astype(np.uint32)

    hashes = energy_rising
    for bit in range(spatial.shape[1]):
        hashes = (hashes << np.uint32(1)) | spatial[:, bit]
    for bit in range(temporal.shape[1]):
        hashes = (hashes << np.uint32(1)) | temporal[:, bit]
    return hashes.astype(np.uint32)


_POPCOUNT = np.array([bin(i).count("1") for i in range(65536)], dtype=np.uint8)


def _hamming(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x = a ^ b
    return _POPCOUNT[x & np.uint32(0xFFFF)] + _POPCOUNT[x >> np.uint32(16)]


@dataclass
class Match:
    a_start: int          # frame index in episode A
    b_start: int          # frame index in episode B
    length: int           # frames


def _longest_true_run(mask: np.ndarray) -> tuple[int, int] | None:
    """(start, length) of the longest run of True, bridging small gaps."""
    if not mask.any():
        return None
    edges = np.flatnonzero(np.diff(np.concatenate(
        (np.zeros(1, np.int8), mask.view(np.int8), np.zeros(1, np.int8))
    )))
    starts, ends = edges[::2], edges[1::2]

    merged: list[tuple[int, int]] = []
    current_start, current_end = int(starts[0]), int(ends[0])
    for start, end in zip(starts[1:], ends[1:]):
        if int(start) - current_end <= _MAX_GAP_FRAMES:
            current_end = int(end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = int(start), int(end)
    merged.append((current_start, current_end))

    start, end = max(merged, key=lambda run: run[1] - run[0])
    return start, end - start


def _runs_in_mask(mask: np.ndarray) -> list[tuple[int, int]]:
    """All (start, length) runs of True, bridging small gaps."""
    if not mask.any():
        return []
    edges = np.flatnonzero(np.diff(np.concatenate(
        (np.zeros(1, np.int8), mask.view(np.int8), np.zeros(1, np.int8))
    )))
    starts, ends = edges[::2], edges[1::2]

    merged: list[tuple[int, int]] = []
    current_start, current_end = int(starts[0]), int(ends[0])
    for start, end in zip(starts[1:], ends[1:]):
        if int(start) - current_end <= _MAX_GAP_FRAMES:
            current_end = int(end)
        else:
            merged.append((current_start, current_end - current_start))
            current_start, current_end = int(start), int(end)
    merged.append((current_start, current_end - current_start))
    return merged


def common_runs(fp_a: np.ndarray, fp_b: np.ndarray) -> list[tuple[float, float]]:
    """Every sustained matching stretch, as (start, end) seconds in A's timeline.

    All qualifying runs are collected — the intro is not always the *longest*
    shared audio between two episodes (recycled music cues can beat it), but it
    is the stretch shared with *most* siblings, which the voting stage finds.
    """
    intervals: list[tuple[float, float]] = []
    len_a, len_b = fp_a.size, fp_b.size

    for delta in range(-(len_b - _MIN_RUN_FRAMES), len_a - _MIN_RUN_FRAMES):
        a_off, b_off = max(delta, 0), max(-delta, 0)
        span = min(len_a - a_off, len_b - b_off)
        if span < _MIN_RUN_FRAMES:
            continue
        good = _hamming(fp_a[a_off:a_off + span], fp_b[b_off:b_off + span]) <= _MAX_HAMMING
        for start, length in _runs_in_mask(good):
            if length >= _MIN_RUN_FRAMES:
                begin = (a_off + start) * SECONDS_PER_FRAME
                intervals.append((begin, begin + length * SECONDS_PER_FRAME))

    return _union(intervals)


def _union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping intervals so one sibling casts one vote per region."""
    if not intervals:
        return []
    intervals.sort()
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1.0:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _consensus_window(intervals: list[tuple[float, float]]) -> tuple[float, float] | None:
    """The time span agreed on by at least _MIN_VOTES sibling matches."""
    if len(intervals) < _MIN_VOTES:
        return None
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events += [(start, 1), (end, -1)]
    events.sort()
    depth, span_start, best = 0, None, None
    for at, step in events:
        depth += step
        if depth >= _MIN_VOTES and span_start is None:
            span_start = at
        elif depth < _MIN_VOTES and span_start is not None:
            if best is None or (at - span_start) > (best[1] - best[0]):
                best = (span_start, at)
            span_start = None
    if best is None or best[1] - best[0] < 8.0:
        return None
    return best


def analyze_season(
    episodes: list[dict],
    cancel=None,
    progress=None,
) -> dict[int, dict]:
    """Detect intro and credits for every episode of one season.

    `episodes` are media rows (dicts with id, path, duration). Returns
    {media_id: {intro_start, intro_end, credits_at}} with absolute seconds;
    keys are present only when detection was confident.
    """
    if len(episodes) < 2:
        return {}

    heads: dict[int, np.ndarray] = {}
    tails: dict[int, np.ndarray] = {}
    for row in episodes:
        if cancel is not None and cancel():
            return {}
        if progress is not None:
            progress(f"listening to {row.get('title') or 'episode'}")
        head = fingerprint(row["path"], 0.0, min(HEAD_SECONDS, row["duration"]), cancel=cancel)
        if head is not None:
            heads[int(row["id"])] = head
        tail_start = max(0.0, row["duration"] - TAIL_SECONDS)
        tail = fingerprint(row["path"], tail_start, row["duration"] - tail_start, cancel=cancel)
        if tail is not None:
            tails[int(row["id"])] = tail

    results: dict[int, dict] = {}
    for row in episodes:
        if cancel is not None and cancel():
            return results
        media_id = int(row["id"])
        siblings = [e for e in episodes if int(e["id"]) != media_id][:4]

        fields: dict = {}
        head = heads.get(media_id)
        if head is not None:
            intervals = []
            for other in siblings:
                other_fp = heads.get(int(other["id"]))
                if other_fp is None:
                    continue
                intervals.extend(common_runs(head, other_fp))
            window = _consensus_window(intervals)
            if window is not None and window[1] - window[0] <= 180.0:
                fields["intro_start"] = round(max(0.0, window[0] - 0.5), 2)
                fields["intro_end"] = round(window[1], 2)

        tail = tails.get(media_id)
        if tail is not None:
            tail_base = max(0.0, row["duration"] - TAIL_SECONDS)
            intervals = []
            for other in siblings:
                other_fp = tails.get(int(other["id"]))
                if other_fp is None:
                    continue
                intervals.extend(
                    (tail_base + start, tail_base + end)
                    for start, end in common_runs(tail, other_fp)
                )
            window = _consensus_window(intervals)
            if window is not None:
                # Credits must reach (near) the end of the file, and a match
                # spanning the entire tail window is noise, not credits.
                spans_everything = (window[0] <= tail_base + 5.0
                                    and window[1] >= row["duration"] - 5.0)
                if window[1] >= row["duration"] - 60.0 and not spans_everything:
                    fields["credits_at"] = round(window[0], 2)

        if fields:
            results[media_id] = fields

    return results
