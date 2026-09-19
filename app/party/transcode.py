"""ffmpeg, for a friend who cannot take the original.

A movie night sends the film as it is. This PC's upload measured 909 Mbit/s,
and the heaviest film in its library needs 11.7 (4K HEVC with Dolby
Vision): 1.3 % of the upload per friend. Re-encoding to save
upload would spend the host's GPU on making everyone's picture worse. What
can fall short is on a friend's side: their download, or a PC that cannot
decode 4K HEVC 10-bit or Dolby Vision smoothly. So each guest chooses for
themselves, per request: the original, or H.264 at 1080p (8 Mbit/s) or 720p
(4 Mbit/s), made on the fly from wherever the party is.

One pipeline serves every PC: the GPU decodes whatever it can (-hwaccel auto
falls back to software when it can't), the CPU scales and tone-maps, and the
first H.264 encoder that works here encodes. Measured on this PC (Core Ultra 9
285K, RTX 5070 Ti, NVENC), each guest's stream costs, while they watch:

    4K HDR10 film -> 1080p SDR       1.4-1.5 cores; flat out 5.5x real time
    1080p 10-bit episode -> 1080p    0.2 of a core; flat out 17.7x real time

and a guest's player is playing 1.2 s after it asks for the episode, and 1.0 to
3.6 s for the heaviest 4K film: ffmpeg decodes from the film's previous
keyframe, and that film's are up to 8.5 s apart. With no GPU encoder, libx264
costs 2.2 and 1.1 cores for the same two.
Doing the scaling on the GPU as well (scale_cuda) saved 0.3 of a core on the
HDR film and needs an NVIDIA card; not worth a second pipeline.

HDR is tone-mapped to SDR on the host with the same chain the artwork uses.
Kept as HDR, an 8-bit H.264 stream either shows guests the washed-out grey of
PQ read as ordinary video, or bands in every sky.

Every audio track is kept (up to four), as AAC stereo, and every text subtitle
track, so what you hear and read stays your own choice as it is with the
original. Image subtitles (Blu-ray PGS) are not free: one show's 23 tracks
cost every guest 0.6 to 0.8 Mbit/s, for languages nobody at the party reads.
So only those in the host's subtitle language go (Settings → Playback; the
first two when none is), at most four. Fonts attached for styled subtitles are
left out: they come before the first frame and would be sent again after
every seek.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections import deque
from pathlib import Path

from ..config import find_ffmpeg, settings, subprocess_flags
from ..metadata.artwork import tonemap_prefix
from ..probe import probe

_log = logging.getLogger("party.transcode")

# name -> (longest side, shortest side, video bits per second)
QUALITIES = {
    "1080p": (1920, 1080, 8_000_000),
    "720p": (1280, 720, 4_000_000),
}
AUDIO_BITRATE = 160_000          # per track, AAC stereo
# The encoders run variable bitrate, and on a grainy film they average above
# their target: a grainy episode, five minutes at 1080p, 8.8 Mbit/s of
# video for an 8 Mbit/s target; the 4K films stayed within 3%.
VBR_OVERSHOOT = 1.10
_MAX_AUDIO = 4                   # an 8-language remux would add 1.3 Mbit/s
_MAX_TEXT_SUBTITLES = 32         # a few KB a minute each
_MAX_IMAGE_SUBTITLES = 4         # 15-26 kbit/s each, measured

# Subtitle codecs Matroska takes as they are. mov_text (MP4's own) has to be
# turned into SRT; teletext and closed captions are left behind.
_TEXT_SUBTITLES = {"subrip", "ass", "ssa", "webvtt", "text"}
_IMAGE_SUBTITLES = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
_CONVERT_SUBTITLES = {"mov_text": "srt"}

# Tried in this order; the first that encodes a test clip is used. The settings
# here are part of the test, so an ffmpeg too old for one of them moves on to
# the next encoder instead of failing every stream later.
ENCODERS = {
    "h264_nvenc": ["-preset", "p4", "-rc", "vbr", "-profile:v", "high", "-forced-idr", "1"],
    "h264_qsv": ["-preset", "veryfast", "-profile:v", "high"],
    "h264_amf": ["-quality", "speed", "-rc", "vbr_peak", "-profile:v", "high"],
    "libx264": ["-preset", "veryfast", "-profile:v", "high"],
}

_picked: str | None = None
_pick_done = False
_pick_lock = threading.Lock()

_sources: dict[tuple, object] = {}
_sources_lock = threading.Lock()


def needed_bitrate(size: int | None, duration: float | None) -> float:
    """What the original file needs on average, in bits per second (0 if unknown).

    An average: an action scene runs well above it for a while, and the
    guest's player rides that out on what it has buffered.
    """
    if not size or not duration or duration <= 0:
        return 0.0
    return size * 8 / duration


def resolve_quality(quality: str, size: int | None = None, duration: float | None = None) -> str:
    """'original', '1080p' or '720p' for the host's setting, which may say 'auto'.

    'auto' is the original, whatever the file needs. The rule this replaced
    (the original up to 10 Mbit/s, 1080p above) re-encoded the 11.7 Mbit/s
    4K film for a host whose 909 Mbit/s upload had room for it 77
    times over. The answer is only the default, for a guest who has not
    chosen: any guest can ask for another per request. An unknown length or
    setting is the original too, which costs the host nothing. `size` and
    `duration` no longer change the answer; they are still taken so older
    callers keep working.
    """
    if quality in QUALITIES or quality == "original":
        return quality
    return "original"


def per_friend_mbps(size: int | None, duration: float | None) -> float | None:
    """Mbit/s of the host's upload one friend takes watching the original (what
    'auto' sends), to a tenth: the file's own average, for the host panel's
    "about 12 Mbit/s per friend" (round it there). 11.7 for the heaviest film
    here, 21.4 for the heaviest episode. None when the size or length is
    unknown: no line is better than a wrong one. The host's own upload is not
    guessed at; only the host knows it.
    """
    needed = needed_bitrate(size, duration)
    return round(needed / 1e6, 1) if needed > 0 else None


def stream_bitrate(quality: str, size: int | None, duration: float | None,
                   audio_tracks: int = 1) -> float:
    """Upload one guest takes at this quality, in bits per second, for "needs
    about N Mbit/s per friend": the file's own average for the original; for a
    transcode, the video as it really averages plus the audio tracks."""
    if quality in QUALITIES:
        tracks = max(1, min(audio_tracks, _MAX_AUDIO))
        return QUALITIES[quality][2] * VBR_OVERSHOOT + AUDIO_BITRATE * tracks
    return needed_bitrate(size, duration)


def _encoder_works(ffmpeg: str, name: str) -> bool:
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:r=24:d=0.25",
               "-vf", "format=yuv420p", "-c:v", name, *ENCODERS[name],
               "-b:v", "1M", "-maxrate", "1250k", "-bufsize", "2M", "-f", "null", "-"]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=15,
                                   creationflags=subprocess_flags())
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        _log.info("encoder %s does not work here: %s", name, detail[-1][:160] if detail else "")
    return completed.returncode == 0


def pick_encoder() -> str | None:
    """The first H.264 encoder that really encodes on this PC, found once.

    Listing encoders is not enough: this ffmpeg lists QSV and AMF on a PC with
    neither an Intel GPU switched on nor an AMD one. A 0.25 s test clip takes
    0.24 s through NVENC and 0.06 s to fail. None when ffmpeg is missing.
    """
    global _picked, _pick_done
    with _pick_lock:
        if not _pick_done:
            ffmpeg = find_ffmpeg()
            if ffmpeg:
                _picked = next((name for name in ENCODERS if _encoder_works(ffmpeg, name)), None)
            _pick_done = True
            _log.info("movie night encoder: %s", _picked or "none")
        return _picked


def describe(path: str | Path):
    """app.probe's view of the file (HDR, audio and subtitle tracks), asked once
    per version of the file rather than at every seek: each seek on a
    transcoded stream starts a new ffmpeg, and the probe reads 4 MB."""
    try:
        stat = os.stat(path)
    except OSError:
        return probe(path, timeout=20.0)          # fails, and says why
    key = (str(path), stat.st_size, stat.st_mtime)
    with _sources_lock:
        found = _sources.get(key)
    if found is None:
        found = probe(path, timeout=20.0)
        with _sources_lock:
            if len(_sources) >= 8:
                _sources.clear()
            _sources[key] = found
    return found


def _video_filter(quality: str, hdr: str | None) -> str:
    width, height, _ = QUALITIES[quality]
    # Fit inside the box, never upscale, keep the shape; even sizes for 4:2:0.
    # Scaling comes first so tone mapping runs on a quarter of a 4K frame.
    chain = (f"scale=w='min(iw,{width})':h='min(ih,{height})'"
             ":force_original_aspect_ratio=decrease:force_divisible_by=2:flags=bicubic")
    chain += tonemap_prefix(hdr)
    # 8-bit whatever came in: a 10-bit source would otherwise make High 10,
    # which the 'high' profile refuses and many decoders cannot play.
    return chain + ",format=yuv420p"


def _subtitles_to_send(tracks, languages) -> list[tuple[int, str]]:
    """(subtitle number in the file, codec to write) for what goes to guests:
    every text track, and image tracks in the host's languages (the first two
    when none is), at most four."""
    wanted = {code.strip().lower()[:2] for code in languages if code.strip()}
    images = [n for n, t in enumerate(tracks) if t.codec in _IMAGE_SUBTITLES]
    chosen_images = [n for n in images if (tracks[n].language or "").lower()[:2] in wanted] or images[:2]
    chosen_images = set(chosen_images[:_MAX_IMAGE_SUBTITLES])
    kept: list[tuple[int, str]] = []
    for number, track in enumerate(tracks):
        if track.codec in _TEXT_SUBTITLES and len(kept) < _MAX_TEXT_SUBTITLES:
            kept.append((number, "copy"))
        elif track.codec in _CONVERT_SUBTITLES and len(kept) < _MAX_TEXT_SUBTITLES:
            kept.append((number, _CONVERT_SUBTITLES[track.codec]))
        elif number in chosen_images:
            kept.append((number, "copy"))
    return kept


def command(path: str | Path, start: float, quality: str, encoder: str,
            ffmpeg: str = "ffmpeg", source=None, languages=()) -> list[str]:
    """The ffmpeg command for `path` from `start` seconds at `quality`, as a list:
    it goes to CreateProcess as arguments, never through a shell, so a file
    called Tom & Jerry's "100%" night.mkv is one argument like any other.

    `source` is app.probe's ProbeResult for the file; without one (probe failed)
    the stream carries the first audio track and no subtitles. `languages` are
    the host's subtitle languages ("eng", or mpv's "eng,jpn").

    Timestamps come out offset by `start`, so a guest's player (run with
    rebase-start-time=no) shows the film's own clock: a stream opened at 600
    reads 600, not 0.
    """
    _, _, video_bitrate = QUALITIES[quality]
    hdr = source.hdr if source is not None and source.ok else None
    audio = source.audio_tracks[:_MAX_AUDIO] if source is not None and source.ok else None
    subtitles = source.sub_tracks if source is not None and source.ok else []

    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-hwaccel", "auto"]
    if start > 0:
        # Before -i: seek the input, then decode up to the exact frame.
        args += ["-ss", f"{start:.3f}"]
    # file: so a name can never be read as a protocol (concat:, pipe:, subfile,).
    args += ["-i", f"file:{path}", "-map", "0:V:0"]      # V: not a cover picture
    if audio is None:
        args += ["-map", "0:a:0?"]
    for number in range(len(audio or [])):
        args += ["-map", f"0:a:{number}"]
    kept = _subtitles_to_send(subtitles, languages)
    for number, _ in kept:
        args += ["-map", f"0:s:{number}"]

    args += ["-vf", _video_filter(quality, hdr),
             "-c:v", encoder, *ENCODERS[encoder],
             "-b:v", str(video_bitrate), "-maxrate", str(video_bitrate * 5 // 4),
             "-bufsize", str(video_bitrate * 2),
             # A keyframe every 2 s whatever the frame rate: where a guest's
             # player can land when it seeks inside what it has buffered.
             "-force_key_frames", "expr:gte(t,n_forced*2)",
             "-c:a", "aac", "-ac", "2", "-b:a", str(AUDIO_BITRATE)]
    for output_number, (_, codec) in enumerate(kept):
        args += [f"-c:s:{output_number}", codec]
    # Chapters would come out shifted and cut; the host's are the ones that count.
    args += ["-map_chapters", "-1", "-output_ts_offset", f"{start:.3f}", "-f", "matroska", "-"]
    return args


class Stream:
    """One running ffmpeg: Matroska on stdout, the last lines of stderr kept.

    Below normal priority, like every background ffmpeg in Mistery: the host is
    watching the same film on the same PC, and their picture comes first.
    """

    def __init__(self, path: str | Path, start: float, quality: str) -> None:
        ffmpeg = find_ffmpeg()
        encoder = pick_encoder()
        if not ffmpeg or not encoder:
            raise OSError("ffmpeg is not installed, or no H.264 encoder works")
        source = describe(path)
        if not source.ok:
            _log.info("probe failed (%s); streaming the first audio track only", source.error)
        languages = str(settings.get("preferred_sub_lang", "eng") or "").split(",")
        self.command = command(path, start, quality, encoder, ffmpeg, source, languages)
        self._errors: deque[str] = deque(maxlen=20)
        self._process = subprocess.Popen(
            self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=subprocess_flags(low_priority=True))
        # ffmpeg must never wait on a full stderr pipe (64 KB on Windows) while
        # the stream is being read: a damaged file can log a line per frame.
        threading.Thread(target=self._drain, name="party-ffmpeg-log", daemon=True).start()

    def _drain(self) -> None:
        try:
            for raw in iter(self._process.stderr.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    self._errors.append(text)
        except (OSError, ValueError):
            pass

    @property
    def pid(self) -> int:
        return self._process.pid

    def read(self, size: int) -> bytes:
        """Up to `size` bytes as soon as there are any; b"" once ffmpeg is done."""
        return self._process.stdout.read1(size)

    def errors(self) -> str:
        return " | ".join(self._errors)

    def close(self) -> None:
        """Stop ffmpeg now. If Mistery itself dies instead, ffmpeg's next write
        hits a closed pipe and it exits on its own."""
        if self._process.poll() is None:
            self._process.kill()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (self._process.stdout, self._process.stderr):
            try:
                pipe.close()
            except OSError:
                pass
