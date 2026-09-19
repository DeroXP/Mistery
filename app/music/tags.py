"""Read audio tags and stream details, and tell a finished file from a download.

mutagen reads a FLAC header in about 2 ms; ffprobe takes about 73 ms because
most of that is starting a process. Over a music library that is the difference
between a scan you don't notice and one you wait for, so mutagen is used
whenever it is installed and ffprobe is the fallback.

The completeness check exists because albums arrive by torrent, and a torrent
file on disk lies in three different ways (all three observed on real downloads):

    preallocated, header still zeroed      tags unreadable
    header arrived, audio has not          0.1 MB file whose header says 3:45
    right size, pieces missing inside      holes of zero bytes

A track that fails any check is stored as 'incomplete' — hidden from the library
and never played — and looked at again when the file next changes.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

_log = logging.getLogger("music")

_LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF", "APE", "WavPack", "DSD"}
# Uncompressed PCM: digital silence really is zero bytes, so hole sampling would
# flag quiet passages. Those rely on the size check alone.
_PCM = {"WAV", "AIFF", "DSD"}

_WINDOW = 16 * 1024
# How many windows to look at, spread evenly over the audio. A torrent client
# sizes its pieces so a whole torrent is one to two thousand of them, so a song
# on an album spans at most a couple of hundred, and a piece not yet downloaded
# is that much of the song in zeros. 256 windows are closer together than any
# such piece, so one always lands inside it. The twelve used before were 8 MB
# apart on a 100 MB hi-res track: a third of downloads with 5-10% of their 4 MiB
# pieces missing passed as ready, and played with the gaps skipped (a 4:06 song
# ran 3:45). This is paid only when a file is new or has changed; over this
# library's 111 files it came to about 1 ms more a file, with the files cached.
_SAMPLES = 256


def _first(tags, *names) -> str:
    """The first non-empty value among several tag spellings."""
    if not tags:
        return ""
    for name in names:
        try:
            value = tags.get(name)
        except Exception:
            value = None
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = str(value).strip()
        if text:
            return text
    return ""


def _leading_int(text: str) -> int | None:
    """'05', '5/12', '1997-08-26' -> 5, 5, 1997."""
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else None


def _gain(text: str) -> float | None:
    match = re.match(r"\s*([+-]?\d+(?:\.\d+)?)", text or "")
    return float(match.group(1)) if match else None


def _codec_name(audio) -> str:
    kind = type(audio).__name__
    if kind == "MP4":
        codec = str(getattr(audio.info, "codec", "") or "").lower()
        return "ALAC" if "alac" in codec else "AAC"
    return {
        "FLAC": "FLAC", "MP3": "MP3", "EasyMP3": "MP3", "OggVorbis": "Vorbis",
        "OggOpus": "Opus", "WAVE": "WAV", "AIFF": "AIFF", "ASF": "WMA",
        "MonkeysAudio": "APE", "WavPack": "WavPack", "DSF": "DSD",
    }.get(kind, kind)


def read(path: str | Path) -> dict:
    """Everything the library stores about one audio file.

    Always returns a dict with a 'state' of ready / incomplete / error, so a bad
    file becomes a row to retry rather than an exception that stops a scan.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"state": "error", "error": str(exc)}

    try:
        import mutagen
    except ImportError:
        return _read_with_ffprobe(path, size)

    try:
        audio = mutagen.File(path, easy=True)
    except Exception as exc:
        # A header that won't parse is what a preallocated download looks like.
        # It can't be told apart from real corruption, so treat it as unfinished:
        # it stays out of sight and is retried whenever the file changes.
        return {"state": "incomplete", "size": size, "error": type(exc).__name__}
    if audio is None or not getattr(audio, "info", None):
        return {"state": "error", "size": size, "error": "not an audio file"}

    info = audio.info
    tags = audio.tags
    duration = float(getattr(info, "length", 0) or 0)
    codec = _codec_name(audio)

    record = {
        "title": _first(tags, "title") or _title_from_filename(path),
        "artist": _first(tags, "artist", "performer"),
        "album_artist": _first(tags, "albumartist", "album artist", "album_artist", "band"),
        "album": _first(tags, "album"),
        "track_no": _leading_int(_first(tags, "tracknumber", "track")),
        "disc_no": _leading_int(_first(tags, "discnumber", "disc")) or 1,
        "year": _leading_int(_first(tags, "date", "year", "originaldate")),
        "genre": _first(tags, "genre"),
        "duration": duration,
        "codec": codec,
        "sample_rate": int(getattr(info, "sample_rate", 0) or 0) or None,
        "bit_depth": int(getattr(info, "bits_per_sample", 0) or 0) or None,
        "channels": int(getattr(info, "channels", 0) or 0) or None,
        "bitrate": int(getattr(info, "bitrate", 0) or 0) or None,
        "rg_track": _gain(_first(tags, "replaygain_track_gain")),
        "rg_album": _gain(_first(tags, "replaygain_album_gain")),
        "size": size,
    }
    record["state"] = _completeness(path, size, duration, codec, info)
    return record


def _title_from_filename(path: Path) -> str:
    """'05. Fiveleaf - Low Tide' -> 'Low Tide' when a file has no title tag."""
    stem = re.sub(r"^\s*\d{1,3}\s*[.\-_)]\s*", "", path.stem)
    if " - " in stem:
        stem = stem.split(" - ", 1)[1]
    return stem.strip() or path.stem


# --- is the download finished? ---------------------------------------------

def _completeness(path: Path, size: int, duration: float, codec: str, info) -> str:
    if duration <= 0:
        return "incomplete"

    # Far too small for what the header claims: only the start has arrived. The
    # floors are generous — well below any real encoding — so a quiet track is
    # never mistaken for a partial one.
    floor_kbps = 32 if codec in _LOSSLESS else 8
    if size < duration * floor_kbps * 1000 / 8:
        return "incomplete"

    if codec == "FLAC":
        reaches_end = _flac_reaches_end(path, size, info)
        if reaches_end is False:
            return "incomplete"

    if codec not in _PCM and _has_zero_hole(path, size, _audio_span(path, size)):
        return "incomplete"
    return "ready"


def _has_zero_hole(path: Path, size: int, span: tuple[int, int] | None = None) -> bool:
    """Sample the file for runs of zero bytes where audio should be.

    Compressed audio frames never contain 16 KB of zeros, but a torrent's
    not-yet-downloaded pieces are exactly that. FLAC padding blocks, ID3 padding
    and MP4 'free' atoms are legitimately zero, so sampling covers only `span`,
    the audio itself, when the container says where that is. Otherwise it starts
    a guessed distance in, past where a header would end.
    """
    if size < 64 * 1024:
        return False
    if span is not None:
        start, end = span[0], min(span[1], size) - _WINDOW
    else:
        start = max(int(size * 0.10), 1536 * 1024) if size > 8 * 1024 * 1024 else int(size * 0.35)
        end = size - _WINDOW
    if end <= start:
        return False
    step = max(_WINDOW, (end - start) // (_SAMPLES - 1))
    offsets = list(range(start, end, step)) + [end]
    zero = bytes(_WINDOW)
    try:
        with open(path, "rb") as handle:
            for offset in offsets:
                handle.seek(offset)
                block = handle.read(_WINDOW)
                if len(block) == _WINDOW and block == zero:
                    return True
    except OSError:
        return True            # can't read it yet: treat as still arriving
    return False


def _audio_span(path: Path, size: int) -> tuple[int, int] | None:
    """(start, end) of the audio data, for the containers that say so plainly.

    FLAC: after the last metadata block. MP3: after the ID3v2 tag. MP4: the
    'mdat' atom, wherever it sits. Anything else, or a header that doesn't add
    up, gives None and the caller guesses. A handful of small reads.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
            offset = 0
            # Retagging sometimes leaves one ID3v2 tag stacked on another.
            for _ in range(4):
                if head[:3] != b"ID3" or len(head) < 10:
                    break
                offset += 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14
                                | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
                if head[5] & 0x10:
                    offset += 10                                   # footer
                handle.seek(offset)
                head = handle.read(12)

            if head[:4] == b"fLaC":
                offset += 4
                for _ in range(1024):
                    handle.seek(offset)
                    block = handle.read(4)
                    if len(block) < 4:
                        return None
                    offset += 4 + int.from_bytes(block[1:4], "big")
                    if block[0] & 0x80:                            # last metadata block
                        return (offset, size) if offset < size else None
                return None

            if head[4:8] == b"ftyp":
                for _ in range(1024):
                    handle.seek(offset)
                    atom = handle.read(16)
                    if len(atom) < 8:
                        return None
                    length, kind = int.from_bytes(atom[:4], "big"), atom[4:8]
                    header = 8
                    if length == 1 and len(atom) == 16:
                        length, header = int.from_bytes(atom[8:16], "big"), 16
                    elif length == 0:
                        length = size - offset                     # runs to the end
                    if length < header:
                        return None
                    if kind == b"mdat":
                        return offset + header, min(offset + length, size)
                    offset += length
                return None

            if head[:2] in (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2",
                            b"\xff\xe3", b"\xff\xe2"):
                return offset, size                                # MP3 frames, after any ID3
    except OSError:
        return None
    return None


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _flac_reaches_end(path: Path, size: int, info) -> bool | None:
    """Does the audio actually run to the end the header promises?

    The size check can't catch a download whose last piece arrived early, and a
    silent track can legitimately be tiny. FLAC frames carry their own position,
    so read the final frame and compare it with STREAMINFO's sample count. None
    means undecidable (no trustworthy frame found), which is not a failure.
    """
    total = int(getattr(info, "total_samples", 0) or 0)
    block = int(getattr(info, "max_blocksize", 0) or 0)
    fixed = block and block == int(getattr(info, "min_blocksize", 0) or 0)
    if not total or not block:
        return None

    window = min(size, 256 * 1024)
    try:
        with open(path, "rb") as handle:
            handle.seek(size - window)
            tail = handle.read(window)
    except OSError:
        return False

    best = -1
    index = tail.find(b"\xff")
    while index != -1 and index < len(tail) - 16:
        if tail[index + 1] in (0xF8, 0xF9):
            position = _frame_position(tail, index, block, fixed)
            if position is not None and position <= total:
                best = max(best, position)
        index = tail.find(b"\xff", index + 1)

    if best < 0:
        return None
    # The last frame starts within one block of the end in a complete file.
    return best + block >= total


def _frame_position(data: bytes, start: int, block: int, fixed: bool) -> int | None:
    """Sample position of the FLAC frame header at `start`, if it is a real one."""
    variable = data[start + 1] == 0xF9
    if variable == bool(fixed):
        return None                         # blocking strategy disagrees with STREAMINFO
    byte2, byte3 = data[start + 2], data[start + 3]
    if (byte2 >> 4) == 0 or (byte3 & 0x01) or ((byte3 >> 1) & 0x07) in (3, 7):
        return None                         # reserved codes: not a header
    if (byte2 & 0x0F) == 0x0F:
        return None

    # UTF-8-style coded frame or sample number.
    cursor = start + 4
    lead = data[cursor]
    if lead < 0x80:
        value, extra = lead, 0
    elif 0xC0 <= lead < 0xE0:
        value, extra = lead & 0x1F, 1
    elif 0xE0 <= lead < 0xF0:
        value, extra = lead & 0x0F, 2
    elif 0xF0 <= lead < 0xF8:
        value, extra = lead & 0x07, 3
    elif 0xF8 <= lead < 0xFC:
        value, extra = lead & 0x03, 4
    elif 0xFC <= lead < 0xFE:
        value, extra = lead & 0x01, 5
    elif lead == 0xFE:
        value, extra = 0, 6
    else:
        return None
    cursor += 1
    for _ in range(extra):
        follow = data[cursor]
        if follow & 0xC0 != 0x80:
            return None
        value = (value << 6) | (follow & 0x3F)
        cursor += 1

    size_code = byte2 >> 4
    if size_code == 6:
        cursor += 1
    elif size_code == 7:
        cursor += 2
    rate_code = byte2 & 0x0F
    if rate_code == 12:
        cursor += 1
    elif rate_code in (13, 14):
        cursor += 2

    if cursor >= len(data) or _crc8(data[start:cursor]) != data[cursor]:
        return None
    return value * block if fixed else value


# --- fallback without mutagen ------------------------------------------------

def _read_with_ffprobe(path: Path, size: int) -> dict:
    from ..config import find_ffprobe, subprocess_flags

    ffprobe = find_ffprobe()
    if not ffprobe:
        return {"state": "error", "size": size, "error": "no tag reader available"}
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, timeout=30, creationflags=subprocess_flags(low_priority=True),
        )
        data = json.loads(completed.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"state": "error", "size": size, "error": str(exc)}

    fmt = data.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(fmt.get("duration") or 0)
    if stream is None or duration <= 0:
        return {"state": "incomplete", "size": size}

    codec = {"flac": "FLAC", "mp3": "MP3", "aac": "AAC", "alac": "ALAC", "vorbis": "Vorbis",
             "opus": "Opus"}.get(stream.get("codec_name", ""), stream.get("codec_name", "").upper())
    record = {
        "title": tags.get("title") or _title_from_filename(path),
        "artist": tags.get("artist", ""),
        "album_artist": tags.get("album_artist", ""),
        "album": tags.get("album", ""),
        "track_no": _leading_int(tags.get("track", "")),
        "disc_no": _leading_int(tags.get("disc", "")) or 1,
        "year": _leading_int(tags.get("date", "")),
        "genre": tags.get("genre", ""),
        "duration": duration,
        "codec": codec,
        "sample_rate": int(stream.get("sample_rate") or 0) or None,
        "bit_depth": int(stream.get("bits_per_raw_sample") or 0) or None,
        "channels": int(stream.get("channels") or 0) or None,
        "bitrate": int(fmt.get("bit_rate") or 0) or None,
        "rg_track": _gain(tags.get("replaygain_track_gain", "")),
        "rg_album": _gain(tags.get("replaygain_album_gain", "")),
        "size": size,
    }
    record["state"] = "incomplete" if _has_zero_hole(path, size, _audio_span(path, size)) else "ready"
    return record


def quality_label(codec: str | None, sample_rate: int | None, bit_depth: int | None,
                  bitrate: int | None) -> tuple[str, str]:
    """('Hi-Res Lossless', '24-bit/96 kHz') — honest labels from the actual stream."""
    codec = codec or ""
    rate = sample_rate or 0
    depth = bit_depth or 0
    khz = f"{rate / 1000:g} kHz" if rate else ""
    if codec in _LOSSLESS:
        detail = f"{depth}-bit/{khz}" if depth and khz else (khz or codec)
        hi_res = depth > 16 or rate > 48000
        return ("Hi-Res Lossless" if hi_res else "Lossless"), detail
    kbps = round((bitrate or 0) / 1000)
    return codec or "Audio", (f"{kbps} kbps" if kbps else khz)
