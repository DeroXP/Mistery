"""A movie night on a phone: a page, a seat in the room, the film as HLS.

The host's PC serves it, on the movie night's own port, to phones on its own
network: server.py hands over a connection that speaks plain HTTP (a browser)
instead of TLS (Mistery), when it comes from a private address and asks for
/p/<token>/. The token is 128 random bits, in the link the host panel shows as
a QR code; anything else gets the same bare 404 as a wrong movie night token.

A phone is a guest like any other: in the people list, and its play, pause and
seek are the room's. Each phone that joins gets a sync.Client of its own on this
PC, connected to the Hub through a socket pair, so the room treats it exactly
as it treats a PC: the same hello, the same waiting for somebody who is
buffering, the same "Sam paused". The page talks to that Client over HTTP:

    GET  /p/<t>/                  the page (phone_page.py)
    POST /p/<t>/join              {id, name}: take a seat, or the one already taken
    GET  /p/<t>/sync?id=…         the room as that seat sees it; waits up to 2 s for
                                  news (wait=2). Carries the page's buffering and
                                  where its video is (pos, at, seq). Every answer
                                  has t1 and t2, the host's clock when the request
                                  arrived and when the answer left, so the page
                                  keeps its own ClockSync from them.
    POST /p/<t>/do                {id, action, position}: play, pause, seek
    POST /p/<t>/bye               {id}: leave
    GET  /p/<t>/v/<s>/index.m3u8  the film: HLS, which Safari plays by itself
         /p/<t>/v/<s>/media.m3u8  every 4 s segment of it, listed from the start
         /p/<t>/v/<s>/<n>.ts      one segment, made when asked for
         /p/<t>/v/<s>/subs/<k>.m3u8, subs/<k>.vtt   a text subtitle track

A phone not heard from for 12 s (locked, the tab closed) has left; the page
joins again by itself when it comes back.

The film. An iPhone cannot play most of what is in a library (HEVC 10-bit,
DTS, Matroska), so every phone gets H.264 1080p with AAC stereo in MPEG-TS
segments, made by transcode.py's pipeline (GPU decode where it can, HDR
tone-mapped) at the phone's own pace. Segment n is the film from 4n s to the
first frame at or after 4n + 4, whichever ffmpeg run made it: ffmpeg keeps
the film's own timestamps (-copyts), puts a keyframe on the first frame at
each 4 s mark (a list of times: the expression form counts from the run's
first frame, and two runs disagreed by a frame), and offsets everything by the
same 10 s so nothing is ever negative. So a run started for a seek to 1:20:00
makes segments that fit exactly between the ones a run from the start made
(features7/phone: the first frame of a segment has the same timestamp from
every run). One ffmpeg at a time runs ahead of the phones, is paused (not
killed: a new run is a new seek) two minutes ahead of the newest segment asked
for, and carries on when they get within 80 s of it. What is more than three
minutes behind them is deleted; the folder goes when the movie night ends.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs

from ..config import find_ffmpeg, settings, subprocess_flags
from . import sync, transcode
from .people import Person, clean_name, is_person_id

_log = logging.getLogger("party.phone")

SEGMENT = 4.0
QUALITY = "1080p"
# Film time 0 is at this PTS in every segment: -output_ts_offset 10 s, plus the
# 1.4 s the MPEG-TS muxer adds (twice its default 0.7 s max_delay). WebVTT
# subtitles are pinned to it with X-TIMESTAMP-MAP.
TS_OFFSET = 10.0
MPEGTS_DELAY = 1.4
MAX_PHONES = 4
IDLE = 12.0                  # a page polls every 2 s at most; six missed polls and it has gone
FORGET_ENDED = 60.0          # an ended seat is kept this long, for the page to read why
FILM_IDLE = 60.0             # no phone watching this long: the film's ffmpeg and folder go
POLL_WAIT = 2.0
JOIN_WAIT = 6.0
AHEAD = 30                   # segments made past the newest asked for, then ffmpeg is paused
RESUME = 20                  # ...and carries on once the phones are this close
BEHIND = 45                  # segments kept behind the newest asked for
LOOK_AHEAD = 3               # a segment this close past the running ffmpeg is waited for
MAX_KEYS = 1500              # keyframe times on one command line (Windows allows 32767 characters)
SEGMENT_WAIT = 30.0
MAX_BODY = 4096
MAX_SUBTITLES = 16
NAME = "Phone"

_LAN = [ipaddress.ip_network(n) for n in
        ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")]
_VIDEO_RE = re.compile(r"v/(?P<sid>[0-9a-f]{12})/(?:(?P<master>index\.m3u8)|(?P<media>media\.m3u8)"
                       r"|(?P<seg>\d{1,6})\.ts|subs/(?P<sub>\d{1,2})\.(?P<subext>m3u8|vtt))")
# A later Windows: ffmpeg's command line lists every keyframe time, capped above.
_ENCODER_EXTRA = {
    "h264_nvenc": [],                               # -forced-idr is in transcode.ENCODERS already
    "libx264": ["-forced-idr", "1", "-sc_threshold", "0"],
    "h264_qsv": ["-forced_idr", "1"],
    "h264_amf": ["-forced_idr", "1"],
}


def on_lan(address: str) -> bool:
    """Whether an address is this network's own (or this PC's): the only places
    a phone's plain HTTP is answered from. A friend across the internet comes
    through the router with their own public address, so never counts."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in _LAN)


def _language_matches(have: str | None, want: str) -> bool:
    have = (have or "").strip().lower()
    want = want.strip().lower()
    return bool(have and want) and (have == want or have[:2] == want[:2])


def _pick_audio(source) -> int | None:
    """The audio track a phone hears (one: HLS alternatives would double the
    work): the first in the host's audio languages (Settings → Playback, as mpv
    takes them), else the file's default, else the first."""
    if source is None or not source.ok or not source.audio_tracks:
        return None
    tracks = source.audio_tracks
    for code in str(settings.get("preferred_audio_lang", "") or "").split(","):
        for number, track in enumerate(tracks):
            if _language_matches(track.language, code):
                return number
    return next((number for number, track in enumerate(tracks) if track.default), 0)


# What a subtitle menu on a phone calls a language, by its ISO 639-1 or -2 code.
_LANGUAGES = {
    "en": "English", "eng": "English", "fr": "French", "fre": "French", "fra": "French",
    "es": "Spanish", "spa": "Spanish", "de": "German", "ger": "German", "deu": "German",
    "it": "Italian", "ita": "Italian", "pt": "Portuguese", "por": "Portuguese",
    "nl": "Dutch", "dut": "Dutch", "nld": "Dutch", "sv": "Swedish", "swe": "Swedish",
    "no": "Norwegian", "nor": "Norwegian", "nb": "Norwegian", "nob": "Norwegian",
    "da": "Danish", "dan": "Danish", "fi": "Finnish", "fin": "Finnish", "pl": "Polish", "pol": "Polish",
    "cs": "Czech", "cze": "Czech", "ces": "Czech", "hu": "Hungarian", "hun": "Hungarian",
    "ro": "Romanian", "rum": "Romanian", "ron": "Romanian", "el": "Greek", "gre": "Greek", "ell": "Greek",
    "tr": "Turkish", "tur": "Turkish", "ru": "Russian", "rus": "Russian", "uk": "Ukrainian", "ukr": "Ukrainian",
    "ar": "Arabic", "ara": "Arabic", "he": "Hebrew", "heb": "Hebrew", "hi": "Hindi", "hin": "Hindi",
    "th": "Thai", "tha": "Thai", "vi": "Vietnamese", "vie": "Vietnamese", "id": "Indonesian", "ind": "Indonesian",
    "ms": "Malay", "may": "Malay", "msa": "Malay", "ja": "Japanese", "jpn": "Japanese",
    "ko": "Korean", "kor": "Korean", "zh": "Chinese", "chi": "Chinese", "zho": "Chinese",
}


def _subtitle_name(title: str | None, language: str, forced: bool) -> str:
    """"English", "French (forced)", or the track's own title when it has one
    that says more than its language code does ("English SDH")."""
    named = _LANGUAGES.get(language.lower(), "")
    words = (title or "").strip()
    if not words or words.lower() in _LANGUAGES or words.lower() in (language.lower(), f"{language} forced".lower()):
        words = named or (language.upper() if language else "")
    if forced and "forced" not in words.lower():
        words = f"{words} (forced)" if words else "Forced"
    return words


def _text_subtitles(path: str, source) -> list[dict]:
    """The text subtitles a phone can have: text tracks in the file, and text
    files beside it named after it (server.py's rule). Picture subtitles (Blu-ray
    PGS) would have to be read by OCR, so a phone does without them."""
    from .server import _sidecar_subtitles, _subtitle_label

    found: list[dict] = []
    if source is not None and source.ok:
        for number, track in enumerate(source.sub_tracks):
            if track.codec in transcode._TEXT_SUBTITLES or track.codec in transcode._CONVERT_SUBTITLES:
                language = (track.language or "").lower()
                name = _subtitle_name(track.title, language, bool(track.forced)) or f"Subtitles {len(found) + 1}"
                found.append({"stream": number, "lang": language, "name": name,
                              "forced": bool(track.forced), "default": bool(track.default)})
    for sidecar in _sidecar_subtitles(path):
        title, language = _subtitle_label(path, sidecar)
        forced = "forced" in title.lower()
        found.append({"file": sidecar, "lang": language,
                      "name": _subtitle_name(title, language, forced) or f"Subtitles {len(found) + 1}",
                      "forced": forced, "default": False})
    return found[:MAX_SUBTITLES]


def _attribute(text: str) -> str:
    """A quoted-string attribute value in a playlist: no quotes, no line breaks."""
    return re.sub(r'["\r\n]', "", text)[:60] or "Subtitles"


_AAC_FRAME = 1024 / 48000


def _audio_span(path: Path) -> tuple[float, float] | None:
    """(start, end) of a segment's sound in film time: its first packet's
    timestamp, and its last packet's end. None when it can't be read."""
    from ..config import find_ffprobe

    ffprobe = find_ffprobe()
    if not ffprobe or not path.exists():
        return None
    try:
        done = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                               "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                              creationflags=subprocess_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    packets = []
    for line in done.stdout.decode("ascii", "replace").splitlines():
        fields = [field for field in line.strip().split(",") if field not in ("", "N/A")]
        try:
            if len(fields) == 2:
                packets.append((float(fields[0]), float(fields[1])))
        except ValueError:
            continue
    if not packets:
        return None
    shift = TS_OFFSET + MPEGTS_DELAY
    return min(p for p, _ in packets) - shift, max(p + d for p, d in packets) - shift


def _remove_later(folder: Path) -> None:
    for _ in range(30):
        time.sleep(1.0)
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return


def remove_leftovers(work_dir: str | None = None, older_than: float = 3600.0) -> int:
    """Film folders a Mistery that was killed left in the temp folder: only our
    own (mistery-phone-*), only what we write in them, and only an hour old."""
    base = Path(work_dir or tempfile.gettempdir())
    removed = 0
    now = time.time()
    for folder in base.glob("mistery-phone-*"):
        try:
            if not folder.is_dir() or now - folder.stat().st_mtime < older_than:
                continue
            if any(p.suffix.lower() not in (".ts", ".m3u8", ".tmp") for p in folder.iterdir()):
                continue
        except OSError:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed += not folder.exists()
    return removed


def _suspend(process: subprocess.Popen, pause: bool) -> bool:
    """Pause or resume a whole process. Windows has no signal for it; NtSuspendProcess
    is what Task Manager's and Process Explorer's suspend use."""
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.c_void_p(int(process._handle))       # noqa: SLF001 - Popen's own handle
            call = ctypes.windll.ntdll.NtSuspendProcess if pause else ctypes.windll.ntdll.NtResumeProcess
            return call(handle) == 0
        import signal

        os.kill(process.pid, signal.SIGSTOP if pause else signal.SIGCONT)
        return True
    except (OSError, AttributeError, ValueError) as exc:
        _log.info("could not %s ffmpeg: %s", "pause" if pause else "resume", exc)
        return False


# --- the film ----------------------------------------------------------------------------

class _Job:
    """One ffmpeg writing segments start, start + 1, … (up to stop) into the folder."""

    def __init__(self, command: list[str], start: int, stop: int, folder: Path) -> None:
        self.start = start
        self.stop = stop
        self.folder = folder
        self.paused = False
        self._front = start
        self._errors: deque[str] = deque(maxlen=12)
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE,
                                        creationflags=subprocess_flags(low_priority=True))
        threading.Thread(target=self._drain, name="party-phone-ffmpeg-log", daemon=True).start()

    def _drain(self) -> None:
        try:
            for raw in iter(self.process.stderr.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    self._errors.append(text)
        except (OSError, ValueError):
            pass

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def front(self) -> int:
        """The first segment of this run not made yet (stop once it has made them all)."""
        while self._front < self.stop and (self.folder / f"{self._front}.ts").exists():
            self._front += 1
        return self._front

    def errors(self) -> str:
        return " | ".join(self._errors)

    def pause(self, on: bool) -> None:
        if on != self.paused and self.alive and _suspend(self.process, on):
            self.paused = on

    def stop_now(self) -> None:
        if self.process.poll() is None:
            self.process.kill()          # a paused process is killed just the same
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            self.process.stderr.close()
        except OSError:
            pass


class _Film:
    """The room's film for phones: HLS made on demand, shared by every phone."""

    def __init__(self, key: str, path: str, duration: float, work_dir: str | None = None) -> None:
        self.key = key
        self.path = path
        self.duration = max(0.1, float(duration))
        self.id = secrets.token_hex(6)
        self.count = max(1, math.ceil(self.duration / SEGMENT - 1e-6))
        self.folder = Path(tempfile.mkdtemp(prefix="mistery-phone-", dir=work_dir))
        self.source = transcode.describe(path)
        self.audio = _pick_audio(self.source)
        self.subtitles = _text_subtitles(path, self.source)
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._newest = 0
        self._pruned_at = 0.0
        self._vtt: dict[int, bytes] = {}
        self._vtt_lock = threading.Lock()
        self.closed = False
        self.problem = ""                    # the last reason ffmpeg made nothing

    # --- playlists -------------------------------------------------------------

    def master(self) -> bytes:
        # No #EXT-X-START here, though HLS allows it: in a master playlist
        # ffmpeg's HLS reader took the master for a media playlist with no
        # segments (no length at all). The media playlist carries it.
        bandwidth = int(transcode.QUALITIES[QUALITY][2] * 1.25 + transcode.AUDIO_BITRATE)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS"]
        group = ""
        if self.subtitles:
            group = ',SUBTITLES="subs"'
            wanted = self._default_subtitle()
            for number, sub in enumerate(self.subtitles):
                language = f',LANGUAGE="{_attribute(sub["lang"])}"' if sub["lang"] else ""
                lines.append(f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{_attribute(sub["name"])}"'
                             f'{language},DEFAULT={"YES" if number == wanted else "NO"},AUTOSELECT=YES,'
                             f'FORCED={"YES" if sub["forced"] else "NO"},URI="subs/{number}.m3u8"')
        # H.264 High at level 4.0 up to 1080p30 (what NVENC writes for a 24 fps
        # film), 4.2 above: the level a player checks it can decode.
        fps = self.source.fps if self.source is not None and self.source.ok else 0.0
        level = "2a" if fps > 30.5 else "28"
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},CODECS="avc1.6400{level},mp4a.40.2"{group}')
        lines.append("media.m3u8")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _default_subtitle(self) -> int | None:
        """Subtitles on from the start when the film is not in the host's own
        language: a Japanese film with English subtitles for a host who reads
        English. The phone can change it; this is only where it starts."""
        wanted = [code for code in str(settings.get("preferred_sub_lang", "") or "").split(",") if code.strip()]
        spoken = self.source.audio_tracks[self.audio].language if (
            self.audio is not None and self.source.ok) else None
        if not wanted or not spoken or _language_matches(spoken, wanted[0]):
            return None
        return next((n for n, sub in enumerate(self.subtitles)
                     if _language_matches(sub["lang"], wanted[0]) and not sub["forced"]), None)

    def media(self, start_at: float | None = None) -> bytes:
        """Every segment from the start. `start_at` (#EXT-X-START) is where a
        player opening it should begin: the room's place, so a phone joining at
        1:20:00 asks for the segment there first rather than for the opening."""
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(SEGMENT)}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
        if start_at is not None and 0 < start_at < self.duration:
            lines.append(f"#EXT-X-START:TIME-OFFSET={start_at:.3f},PRECISE=YES")
        for number in range(self.count):
            length = min(SEGMENT, self.duration - number * SEGMENT)
            lines.append(f"#EXTINF:{max(0.001, length):.3f},")
            lines.append(f"{number}.ts")
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode("ascii")

    def subtitle_playlist(self, number: int) -> bytes | None:
        if not 0 <= number < len(self.subtitles):
            return None
        whole = math.ceil(self.duration)
        return ("\n".join(["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{whole}",
                           "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
                           f"#EXTINF:{self.duration:.3f},", f"{number}.vtt", "#EXT-X-ENDLIST"])
                + "\n").encode("ascii")

    def subtitle(self, number: int) -> bytes | None:
        """One track as WebVTT, made once (a subtitle track inside a film means
        reading the whole file), pinned to the segments' timestamps."""
        if not 0 <= number < len(self.subtitles):
            return None
        with self._vtt_lock:
            if number not in self._vtt:
                self._vtt[number] = self._convert(self.subtitles[number])
            return self._vtt[number]

    def _convert(self, sub: dict) -> bytes:
        ffmpeg = find_ffmpeg()
        text = b""
        if ffmpeg:
            source = sub.get("file") or self.path
            chosen = "0:s:0" if sub.get("file") else f"0:s:{sub['stream']}"
            try:
                done = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                                       "-i", f"file:{source}", "-map", chosen, "-c:s", "webvtt",
                                       "-f", "webvtt", "-"], stdin=subprocess.DEVNULL,
                                      capture_output=True, timeout=180,
                                      creationflags=subprocess_flags(low_priority=True))
                text = done.stdout if done.returncode == 0 else b""
                if done.returncode != 0:
                    _log.info("subtitles for the phone failed: %s",
                              done.stderr.decode("utf-8", "replace").strip()[-200:])
            except (OSError, subprocess.SubprocessError) as exc:
                _log.info("subtitles for the phone failed: %s", exc)
        body = text.decode("utf-8", "replace").replace("\r\n", "\n").lstrip("﻿")
        if not body.startswith("WEBVTT"):
            body = "WEBVTT\n\n"
        first, _, rest = body.partition("\n")
        stamp = round((TS_OFFSET + MPEGTS_DELAY) * 90000)
        return f"{first}\nX-TIMESTAMP-MAP=MPEGTS:{stamp},LOCAL:00:00:00.000\n{rest}".encode("utf-8")

    # --- segments ----------------------------------------------------------------

    def segment(self, number: int, timeout: float = SEGMENT_WAIT) -> Path | None:
        """Segment `number`'s file, made if need be (waiting up to `timeout`), or None.

        The running ffmpeg is waited for when it will get there soon: it is on
        its way (up to LOOK_AHEAD segments short of it) and does not stop
        before it. Anything else starts a run there, twice at most."""
        if not 0 <= number < self.count or self.closed:
            return None
        path = self.folder / f"{number}.ts"
        deadline = time.monotonic() + timeout
        starts = 0
        with self._lock:
            self._newest = number
        while not self.closed:
            if path.exists():
                self.pace()
                return path
            with self._lock:
                job = self._job
                coming = (job is not None and job.alive and job.start <= number < job.stop
                          and number <= job.front() + LOOK_AHEAD)
                if not coming:
                    if job is not None and not job.alive and job.process.returncode not in (0, None):
                        self.problem = job.errors() or f"ffmpeg stopped ({job.process.returncode})"
                        _log.warning("phone stream: ffmpeg stopped short of segment %d: %s", number,
                                     self.problem)
                    if starts >= 2:
                        return None
                    starts += 1
                    self._start(number)
                    if self._job is None:
                        return None
                self._pace()
            if time.monotonic() > deadline:
                return None
            time.sleep(0.03)
        return None

    def _start(self, number: int) -> None:
        """A new ffmpeg from segment `number`, up to the next segment already
        made (two runs never write the same file) or MAX_KEYS on. With the lock."""
        if self._job is not None:
            self._job.stop_now()
            self._job = None
        stop = min(self.count, number + MAX_KEYS)
        for later in range(number + 1, min(stop, number + AHEAD + BEHIND + 2)):
            if (self.folder / f"{later}.ts").exists():
                stop = later
                break
        sound = self._sound_edges(number, stop)
        command = self.command(number, stop, *sound)
        if command is None:
            self.problem = "ffmpeg is not installed, or no H.264 encoder works here"
            return
        self._job = _Job(command, number, stop, self.folder)
        _log.info("phone stream: ffmpeg from segment %d (%.0f s) to %d", number, number * SEGMENT, stop)

    def _sound_edges(self, number: int, stop: int) -> tuple[float | None, float | None]:
        """Where a run's sound should begin and end, in film time, to follow on
        from segments another run made on either side of it: None where there
        is no such segment, and the run's own cut is used.

        Each run cuts its sound between segments in its own order of packets
        (by decode time, which B-frames put a few frames before the picture's),
        so a new run's sound starting at its first picture left 75 ms of silence
        after the segment before it (features7/phone/test_phone_hls). Here it
        begins where that segment's sound ends, plus one AAC frame, which the
        new encoder's first packet (its priming) takes: the segments' clocks
        meet exactly, and at most 21 ms of sound is lost at the join."""
        begin = end = None
        if number > 0:
            edges = _audio_span(self.folder / f"{number - 1}.ts")
            if edges is not None:
                begin = edges[1] + _AAC_FRAME
        if stop < self.count:
            edges = _audio_span(self.folder / f"{stop}.ts")
            if edges is not None:
                end = edges[0]
        return begin, end

    def command(self, number: int, stop: int, sound_from: float | None = None,
                sound_to: float | None = None) -> list[str] | None:
        ffmpeg, encoder = find_ffmpeg(), transcode.pick_encoder()
        if not ffmpeg or not encoder:
            return None
        start, end = number * SEGMENT, stop * SEGMENT
        source = self.source
        hdr = source.hdr if source is not None and source.ok else None
        _, _, rate = transcode.QUALITIES[QUALITY]
        args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-hwaccel", "auto",
                # The film's own timestamps from 0, whatever the run: see the module docstring.
                "-copyts", "-start_at_zero"]
        # Read from where the first of picture and sound begins, to where the
        # last ends; each is trimmed to its own edge below.
        read_from = min(start, sound_from) if sound_from is not None else start
        if read_from > 0:
            args += ["-ss", f"{max(0.0, read_from - (0.3 if sound_from is not None else 0.0)):.3f}"]
        if stop < self.count:
            args += ["-to", f"{max(end, sound_to or 0.0) + (0.3 if sound_to is not None else 0.0):.3f}"]
        args += ["-i", f"file:{self.path}", "-map", "0:V:0",
                 "-map", f"0:a:{self.audio}?" if self.audio is not None else "0:a:0?"]
        keys = ",".join(f"{k * SEGMENT:g}" for k in range(number, stop))
        picture = transcode._video_filter(QUALITY, hdr)
        if sound_from is not None or sound_to is not None:
            # The input was opened wider than the picture: back to its own edges.
            edges = [f"start={start:.6f}"] + ([f"end={end:.6f}"] if stop < self.count else [])
            picture = f"trim={':'.join(edges)}," + picture
            sound = [f"start={sound_from:.6f}"] if sound_from is not None else [f"start={start:.6f}"]
            if stop < self.count:
                sound.append(f"end={sound_to if sound_to is not None else end:.6f}")
            args += ["-af", f"atrim={':'.join(sound)}"]
        args += ["-vf", picture, "-fps_mode", "passthrough",
                 "-c:v", encoder, *transcode.ENCODERS[encoder], *_ENCODER_EXTRA.get(encoder, []),
                 "-g", "100000", "-b:v", str(rate), "-maxrate", str(rate * 5 // 4),
                 "-bufsize", str(rate * 2), "-force_key_frames", keys,
                 "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", str(transcode.AUDIO_BITRATE),
                 "-map_chapters", "-1", "-map_metadata", "-1",
                 "-output_ts_offset", f"{TS_OFFSET:g}",
                 # hls_time under the 4 s: segments are cut at our keyframes only
                 # (-g keeps the encoder from adding any), never later.
                 "-f", "hls", "-hls_time", "1", "-hls_list_size", "3", "-hls_segment_type", "mpegts",
                 "-start_number", str(number), "-hls_flags", "temp_file",
                 "-hls_segment_filename", str(self.folder / "%d.ts"), str(self.folder / f"run{number}.m3u8")]
        return args

    def pace(self) -> None:
        with self._lock:
            self._pace()

    def _pace(self) -> None:
        """Pause ffmpeg well ahead of the phones and let it on again as they near
        it; delete what is well behind them. With the lock."""
        job = self._job
        if job is not None and job.alive:
            ahead = job.front() - self._newest
            if not job.paused and ahead > AHEAD:
                job.pause(True)
            elif job.paused and ahead <= RESUME:
                job.pause(False)
        now = time.monotonic()
        if now - self._pruned_at < 5.0:
            return
        self._pruned_at = now
        # Ahead is kept for longer: a run that got past AHEAD before the next
        # look paused it made those for the phones to reach soon.
        low, high = self._newest - BEHIND, self._newest + AHEAD * 3
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        for name in names:
            stem, dot, ext = name.partition(".")
            if ext == "ts" and stem.isdigit() and not low <= int(stem) <= high:
                try:
                    os.remove(self.folder / name)
                except OSError:
                    pass                    # being sent right now; next time

    def close(self) -> None:
        self.closed = True
        with self._lock:
            job, self._job = self._job, None
        if job is not None:
            job.stop_now()
        shutil.rmtree(self.folder, ignore_errors=True)
        if self.folder.exists():
            # Windows keeps a file that is being sent: a phone's last segment.
            threading.Thread(target=_remove_later, args=(self.folder,), name="party-phone-tidy",
                             daemon=True).start()


# --- the phones -----------------------------------------------------------------------------

class _Phone:
    """One phone's seat: its own Client in the room, and what its page has not heard yet."""

    def __init__(self, pid: str, name: str, address: str) -> None:
        self.id = pid
        self.name = name
        self.address = address
        self.heard = time.monotonic()
        self.version = 0
        self.news: deque[tuple[int, str]] = deque(maxlen=24)
        self._news_n = 0
        self.over = False
        self.over_at = 0.0
        self.reason = ""
        self.joined = threading.Event()
        self.changed = threading.Condition()
        self.client = sync.Client(Person(pid, name), on_event=self._on_event)

    def _on_event(self, event) -> None:            # the Client's thread
        with self.changed:
            if event.kind == "connected":
                self.joined.set()
            elif event.kind == "ended":
                self.over, self.over_at, self.reason = True, time.monotonic(), event.text
            if event.text and event.kind in ("state", "people", "notice"):
                self._news_n += 1
                self.news.append((self._news_n, event.text))
            self.version += 1
            self.changed.notify_all()

    def wait(self, version: int, timeout: float) -> None:
        with self.changed:
            self.changed.wait_for(lambda: self.version != version or self.over, timeout)

    def wait_joined(self, timeout: float) -> None:
        with self.changed:
            self.changed.wait_for(lambda: self.joined.is_set() or self.over, timeout)


class PhoneRoom:
    """Phones watching this PC's movie night. server.phone_handler, while it runs.

        phones = PhoneRoom(hub, find_file)        # find_file(media key) -> (path, duration)
        server.phone_handler = phones
        link = phones.link(lan_ip, port)          # for the QR code
        ...
        phones.close()                            # after hub.end(): the film's folder goes
    """

    def __init__(self, hub, find_file, *, work_dir: str | None = None) -> None:
        self.token = secrets.token_hex(16)
        self._prefix = f"/p/{self.token}/"
        self._hub = hub
        self._find_file = find_file
        self._work_dir = work_dir
        self._lock = threading.Lock()
        self._phones: dict[str, _Phone] = {}
        self._film: _Film | None = None
        self._closed = threading.Event()
        threading.Thread(target=self._tend, name="party-phones", daemon=True).start()
        threading.Thread(target=remove_leftovers, args=(work_dir,), name="party-phone-leftovers",
                         daemon=True).start()

    # --- for the session --------------------------------------------------------

    def link(self, lan_ip: str | None, port: int | None) -> str:
        if not lan_ip or not port:
            return ""
        return f"http://{lan_ip}:{int(port)}{self._prefix}"

    @property
    def watching(self) -> int:
        with self._lock:
            return sum(1 for phone in self._phones.values() if not phone.over)

    def close(self) -> None:
        """Every phone leaves (after the room's own end they have already been
        told), and ffmpeg and its folder go."""
        self._closed.set()
        with self._lock:
            phones, self._phones = list(self._phones.values()), {}
            film, self._film = self._film, None
        for phone in phones:
            phone.client.leave()
        if film is not None:
            film.close()

    # --- for the server ------------------------------------------------------------

    def owns(self, target: bytes) -> bool:
        """Whether a request's target is this movie night's phone page (or under it)."""
        prefix = self._prefix.encode("ascii")
        return len(target) >= len(prefix) and hmac.compare_digest(target[:len(prefix)], prefix)

    def serve(self, conn, method: bytes, target: bytes, headers: dict[str, list[str]], peer=None) -> None:
        """One request (server.py has read its head). Answers it and returns;
        the server closes the connection."""
        try:
            path, _, query = target.decode("ascii")[len(self._prefix):].partition("?")
        except UnicodeDecodeError:
            _reply(conn, 404)
            return
        verb = method.decode("ascii", "replace")
        address = peer[0] if isinstance(peer, tuple) and peer else "?"
        if self._closed.is_set():
            _reply(conn, 404)
            return
        try:
            fields = parse_qs(query, max_num_fields=16) if query else {}
        except ValueError:                  # more fields than any page sends
            _reply(conn, 400)
            return
        if path == "" and verb in ("GET", "HEAD"):
            self._page(conn, verb == "HEAD")
        elif path == "join" and verb == "POST":
            self._join(conn, _read_json(conn, headers), address)
        elif path == "sync" and verb == "GET":
            self._sync(conn, fields)
        elif path == "do" and verb == "POST":
            self._do(conn, _read_json(conn, headers))
        elif path == "bye" and verb == "POST":
            self._bye(conn, _read_json(conn, headers))
        elif verb in ("GET", "HEAD") and (match := _VIDEO_RE.fullmatch(path)):
            self._video(conn, match, fields, headers, verb == "HEAD")
        else:
            _reply(conn, 404)

    # --- the page and the seat -------------------------------------------------------

    def _page(self, conn, head_only: bool) -> None:
        from .phone_page import render

        state = self._hub.state
        media = state.media or {}
        film = self._film_for(media)
        boot = {"title": str(media.get("title") or "Movie night"),
                "people": [p["name"] for p in list(self._hub.people)],
                "video": f"v/{film.id}/index.m3u8" if film is not None else None,
                "playing": state.playing}
        nonce = secrets.token_urlsafe(12)
        body = render(boot, nonce).encode("utf-8")
        _reply(conn, 200, body, "text/html; charset=utf-8", head_only=head_only, extra=[
            f"Content-Security-Policy: default-src 'none'; script-src 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}'; connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"])

    def _join(self, conn, body: dict | None, address: str) -> None:
        t1 = self._hub.host_time()
        pid = body.get("id") if isinstance(body, dict) else None
        if not is_person_id(pid) or len(pid) < 16:
            _reply(conn, 400)
            return
        name = clean_name(body.get("name")) or NAME
        with self._lock:
            phone = self._phones.get(pid)
            if phone is not None and phone.over:
                del self._phones[pid]
                phone = None
            fresh = phone is None
            if fresh:
                if sum(1 for p in self._phones.values() if not p.over) >= MAX_PHONES:
                    _send_json(conn, {"ended": f"{MAX_PHONES} phones are watching already. One of them "
                                               "has to leave first.", "t1": t1, "t2": self._hub.host_time()})
                    return
                phone = _Phone(pid, name, address)
                self._phones[pid] = phone
        phone.heard = time.monotonic()
        if fresh:
            self._seat(phone)
            _log.info("a phone joined the movie night as %s (%s)", name, address)
        phone.wait_joined(JOIN_WAIT)
        if not phone.joined.is_set() and not phone.over:
            phone.client.leave()
            with self._lock:
                if self._phones.get(pid) is phone:
                    del self._phones[pid]
            _send_json(conn, {"ended": "The movie night didn't answer. Try again in a moment.",
                              "t1": t1, "t2": self._hub.host_time()})
            return
        self._answer(conn, phone, 0, t1)

    def _seat(self, phone: _Phone) -> None:
        """The phone's Client, connected to the Hub through a socket pair: the Hub
        reads the line server.py would have routed on, then takes the rest."""
        room_side, phone_side = socket.socketpair()
        hub = self._hub

        def hand_over() -> None:
            try:
                room_side.settimeout(5.0)
                line = b""
                while not line.endswith(b"\n"):
                    byte = room_side.recv(1)
                    if not byte or len(line) > 300:
                        raise OSError("no greeting")
                    line += byte
                if not line.startswith(sync.PREAMBLE.encode("ascii") + b" "):
                    raise OSError("not a greeting")
                room_side.settimeout(None)
                hub.accept(room_side, ("phone", phone.address))
            except OSError as exc:
                _log.info("a phone's seat could not be taken: %s", exc)
                try:
                    room_side.close()
                except OSError:
                    pass
        threading.Thread(target=hand_over, name="party-phone-seat", daemon=True).start()
        phone.client.connect_socket(phone_side, "0" * 32)

    def _sync(self, conn, query: dict) -> None:
        t1 = self._hub.host_time()
        pid = _first(query, "id")
        with self._lock:
            phone = self._phones.get(pid) if pid else None
        if phone is None:
            _send_json(conn, {"gone": True, "t1": t1, "t2": self._hub.host_time()})
            return
        phone.heard = time.monotonic()
        client = phone.client
        told = _first(query, "ev")
        if told:
            # What the page did (its starts, fixes, stalls): for the log, where an
            # evening on a real phone can be read afterwards.
            _log.info("phone %s: %s", phone.name, "".join(c for c in told[:400] if c.isprintable()))
        if not phone.over:
            buffering = _first(query, "buf")
            if buffering in ("0", "1"):
                client.set_buffering(buffering == "1")
            position, at, seq = (_number(query, "pos", 0.0, sync.MAX_POSITION),
                                 _number(query, "at", -sync.MAX_CLOCK, sync.MAX_CLOCK),
                                 _number(query, "seq", 0, 2 ** 53))
            if position is not None and at is not None and seq is not None:
                # `at` is on the host's clock; the Client wants its own, and turns it back.
                client.report_position(position, client.clock.local_time(at), seq=int(seq))
        seen_version = _number(query, "v", 0, 2 ** 53)
        wait = _number(query, "wait", 0.0, POLL_WAIT) or 0.0
        if wait > 0 and seen_version is not None and not phone.over:
            phone.wait(int(seen_version), wait)
        self._answer(conn, phone, int(_number(query, "n", 0, 2 ** 53) or 0), t1)

    def _answer(self, conn, phone: _Phone, news_seen: int, t1: float) -> None:
        client = phone.client
        state = client.state
        media = state.media or {}
        film = self._film_for(media) if phone.joined.is_set() else None
        payload = {
            "v": phone.version,
            "you": phone.id,
            "joined": phone.joined.is_set(),
            "ended": phone.reason if phone.over else None,
            "final": self._hub.ending is not None,        # no joining again: the movie night is over
            "state": state.to_wire() if phone.joined.is_set() else None,
            "people": [{"id": p["id"], "name": p["name"], "host": p["host"],
                        "buffering": p["buffering"], "slow": p["slow"]} for p in list(client.people)],
            "news": [[n, text] for n, text in list(phone.news) if n > news_seen],
            "video": f"v/{film.id}/index.m3u8" if film is not None else None,
            # The same subtitles as files, for a browser whose HLS player
            # leaves the playlist's subtitle tracks out (Chrome's).
            "subs": [{"name": sub["name"], "lang": sub["lang"], "url": f"v/{film.id}/subs/{n}.vtt"}
                     for n, sub in enumerate(film.subtitles)] if film is not None else [],
            "title": str(media.get("title") or ""),
            "t1": t1,
        }
        payload["t2"] = self._hub.host_time()
        _send_json(conn, payload)

    def _do(self, conn, body: dict | None) -> None:
        t1 = self._hub.host_time()
        phone = self._phone_for(body)
        action = body.get("action") if isinstance(body, dict) else None
        if phone is None or phone.over or action not in sync.INTENTS:
            _reply(conn, 400 if phone is not None else 404)
            return
        position = None
        if action == "seek":
            try:
                position = sync.number(body.get("position"), 0.0, sync.MAX_POSITION)
            except sync.BadMessage:
                _reply(conn, 400)
                return
        phone.heard = time.monotonic()
        phone.client.intent(action, position)
        _send_json(conn, {"ok": True, "t1": t1, "t2": self._hub.host_time()})

    def _bye(self, conn, body: dict | None) -> None:
        phone = self._phone_for(body)
        if phone is not None:
            with self._lock:
                if self._phones.get(phone.id) is phone:
                    del self._phones[phone.id]
            phone.client.leave()
        _send_json(conn, {"ok": True})

    def _phone_for(self, body: dict | None) -> _Phone | None:
        pid = body.get("id") if isinstance(body, dict) else None
        if not isinstance(pid, str):
            return None
        with self._lock:
            return self._phones.get(pid)

    # --- the film ---------------------------------------------------------------------

    def _film_for(self, media: dict) -> _Film | None:
        """The room's film for phones, made the first time it is asked for (the
        probe is cached since the server shared it). Another film (next episode)
        closes the last one's ffmpeg and folder."""
        key = media.get("key")
        if not isinstance(key, str) or not key:
            return None
        with self._lock:
            film = self._film
            if film is not None and film.key == key:
                return film
        if not (find_ffmpeg() and transcode.pick_encoder()):
            return None                     # the page says a phone needs ffmpeg here
        found = self._find_file(key)
        if not found:
            return None
        path, duration = found
        duration = duration or media.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
            return None
        try:
            made = _Film(key, str(path), float(duration), self._work_dir)
        except OSError as exc:
            _log.warning("a phone can't have %s: %s", os.path.basename(str(path)), exc)
            return None
        with self._lock:
            if self._closed.is_set():
                old, keep = made, None
            elif self._film is not None and self._film.key == key:
                old, keep = made, self._film              # another request got there first
            else:
                old, keep = self._film, made
                self._film = made
        if old is not None:
            old.close()
        return keep

    def _video(self, conn, match, query: dict, headers: dict, head_only: bool) -> None:
        with self._lock:
            film = self._film
        if film is None or match.group("sid") != film.id:
            _reply(conn, 404)
            return
        if match.group("master"):
            _reply(conn, 200, film.master(), "application/vnd.apple.mpegurl", head_only=head_only)
        elif match.group("media"):
            _reply(conn, 200, film.media(self._start_at(film)), "application/vnd.apple.mpegurl",
                   head_only=head_only)
        elif match.group("seg") is not None:
            number = int(match.group("seg"))
            if number >= film.count:
                _reply(conn, 404)
                return
            path = film.segment(number)
            if path is None:
                _reply(conn, 503, extra=["Retry-After: 2"])
                return
            _send_file(conn, path, "video/mp2t", headers.get("range"), head_only)
        else:
            number = int(match.group("sub"))
            body = (film.subtitle_playlist(number) if match.group("subext") == "m3u8"
                    else film.subtitle(number))
            if body is None:
                _reply(conn, 404)
                return
            kind = "application/vnd.apple.mpegurl" if match.group("subext") == "m3u8" else "text/vtt; charset=utf-8"
            _reply(conn, 200, body, kind, head_only=head_only)

    def _start_at(self, film: _Film) -> float | None:
        """Where a player opening the film now should begin: where the room will
        be once it has the first segment (a moment for ffmpeg), or where it holds."""
        state = self._hub.state
        if (state.media or {}).get("key") != film.key:
            return None
        position = state.position_at(self._hub.host_time() + (3.0 if state.playing else 0.0))
        return position if position > 0.5 else None

    def _tend(self) -> None:
        """Twice a second: phones not heard from have gone, ended seats are
        forgotten, and the running ffmpeg is paced (NVENC makes up to ten
        segments a second of an easy film, so a look every half second lets it
        past AHEAD by a few at most)."""
        empty_since = time.monotonic()
        while not self._closed.wait(0.5):
            now = time.monotonic()
            gone = []
            idle = None
            with self._lock:
                for pid, phone in list(self._phones.items()):
                    if phone.over and now - phone.over_at > FORGET_ENDED:
                        del self._phones[pid]
                    elif not phone.over and now - phone.heard > IDLE:
                        del self._phones[pid]
                        gone.append(phone)
                if any(not phone.over for phone in self._phones.values()):
                    empty_since = now
                elif self._film is not None and now - empty_since > FILM_IDLE:
                    # Nobody watching on a phone for a while: its ffmpeg (paused,
                    # but holding one of the GPU's encoder sessions) and folder
                    # go. The next phone starts them again where it is.
                    idle, self._film = self._film, None
                film = self._film
            for phone in gone:
                _log.info("%s's phone went quiet; leaving the movie night for it", phone.name)
                phone.client.leave()
            if idle is not None:
                idle.close()
            if film is not None:
                film.pace()


# --- HTTP ----------------------------------------------------------------------------------

_STATUS = {200: "200 OK", 206: "206 Partial Content", 400: "400 Bad Request", 404: "404 Not Found",
           416: "416 Range Not Satisfiable", 503: "503 Service Unavailable"}


def _reply(conn, status: int, body: bytes = b"", kind: str = "text/plain; charset=utf-8", *,
           head_only: bool = False, extra: list[str] | tuple = ()) -> None:
    lines = [f"HTTP/1.1 {_STATUS[status]}", f"Content-Type: {kind}", f"Content-Length: {len(body)}",
             "Cache-Control: no-store", "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer",
             "Connection: close", *extra, "", ""]
    conn.sendall("\r\n".join(lines).encode("ascii") + (b"" if head_only else body))


def _send_json(conn, payload: dict) -> None:
    _reply(conn, 200, json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
           .encode("utf-8"), "application/json; charset=utf-8")


def _send_file(conn, path: Path, kind: str, ranges: list[str] | None, head_only: bool) -> None:
    from .server import _parse_range

    try:
        handle = open(path, "rb")
    except OSError:
        _reply(conn, 503, extra=["Retry-After: 1"])
        return
    with handle:
        size = os.fstat(handle.fileno()).st_size
        start, end, status, extra = 0, size - 1, 200, ["Accept-Ranges: bytes"]
        if ranges and len(ranges) == 1 and ranges[0].lower().startswith("bytes="):
            wanted = _parse_range(ranges[0], size)
            if wanted is None:
                _reply(conn, 416, extra=[f"Content-Range: bytes */{size}"])
                return
            start, end = wanted
            status = 206
            extra.append(f"Content-Range: bytes {start}-{end}/{size}")
        length = max(0, end - start + 1)
        lines = [f"HTTP/1.1 {_STATUS[status]}", f"Content-Type: {kind}", f"Content-Length: {length}",
                 "Cache-Control: no-store", "Connection: close", *extra, "", ""]
        conn.sendall("\r\n".join(lines).encode("ascii"))
        if head_only:
            return
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(256 * 1024, remaining))
            if not chunk:
                break
            conn.sendall(chunk)
            remaining -= len(chunk)


def _read_json(conn, headers: dict[str, list[str]]) -> dict | None:
    """A small JSON body, read within 5 s; None when there is none or it is not an object."""
    try:
        length = int((headers.get("content-length") or ["0"])[0])
    except ValueError:
        return None
    if not 0 < length <= MAX_BODY:
        return None
    data = b""
    deadline = time.monotonic() + 5.0
    while len(data) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        conn.settimeout(remaining)
        try:
            chunk = conn.recv(length - len(data))
        except (socket.timeout, TimeoutError, OSError):
            return None
        if not chunk:
            return None
        data += chunk
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=lambda _: None)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _first(query: dict, name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _number(query: dict, name: str, low: float, high: float) -> float | None:
    raw = _first(query, name)
    if raw is None or len(raw) > 24:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and low <= value <= high else None
