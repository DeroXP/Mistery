"""ffprobe wrapper — the ground truth for runtime, resolution, HDR and audio."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import find_ffprobe

# Keep console windows from flashing up when we shell out on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_HDR_TRANSFERS = {
    "smpte2084": "HDR10",
    "arib-std-b67": "HLG",
    "smpte428": "HDR",
}

_BIT_DEPTH_RE = re.compile(r"p(\d{1,2})(?:le|be)?$")

_CODEC_LABELS = {
    "hevc": "HEVC",
    "h264": "H.264",
    "av1": "AV1",
    "vp9": "VP9",
    "mpeg4": "MPEG-4",
    "mpeg2video": "MPEG-2",
    "vc1": "VC-1",
    "eac3": "EAC3",
    "ac3": "AC3",
    "dts": "DTS",
    "truehd": "TrueHD",
    "aac": "AAC",
    "flac": "FLAC",
    "opus": "Opus",
    "mp3": "MP3",
    "pcm_s16le": "PCM",
}

_CHANNEL_LABELS = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


@dataclass
class Track:
    index: int
    codec: str
    language: str | None = None
    title: str | None = None
    channels: int | None = None
    default: bool = False
    forced: bool = False

    def label(self) -> str:
        bits = []
        if self.title:
            bits.append(self.title)
        if self.language:
            bits.append(self.language.upper())
        if self.channels:
            bits.append(_CHANNEL_LABELS.get(self.channels, f"{self.channels}ch"))
        bits.append(_CODEC_LABELS.get(self.codec, self.codec.upper()))
        return " · ".join(dict.fromkeys(bits))


# A file that is still being written looks corrupt to ffprobe. These are the
# messages it produces for a container whose header has not landed yet.
_INCOMPLETE_MARKERS = (
    "ebml header parsing failed",
    "invalid as first byte of an ebml number",
    "invalid data found when processing input",
    "moov atom not found",
    "could not find codec parameters",
    "end of file",
)


def _looks_incomplete(stderr: str, path: str | Path) -> bool:
    """True when the file is probably still downloading rather than broken."""
    lowered = (stderr or "").lower()
    if any(marker in lowered for marker in _INCOMPLETE_MARKERS):
        return True
    try:
        # Torrent clients preallocate, so a sparse file with a zeroed header is
        # almost always a download in flight.
        with open(path, "rb") as handle:
            return handle.read(4) == b"\x00\x00\x00\x00"
    except OSError:
        return False


# A header that reads is not a finished file. A torrent writes the file at full
# size and fills in pieces as they come, and the first piece is usually among
# the first to arrive, so ffprobe happily reports a two-hour film when a third
# of it is there: measured, a preallocated MKV with only its header piece, and
# a faststart MP4 at 10%, both probed as done. The film was listed as playable
# and its seek-bar sprite was built from the frames that had arrived.
#
# The same check the music scan makes, then: compressed audio and video never
# hold 16 KB of zero bytes, and a piece not downloaded yet is nothing else. 256
# windows spread over the media data find a download in flight; a film down to
# its last few pieces can slip through, but finishing changes its modified
# time, and everything built from it is made again then.
_WINDOW = 16 * 1024
_SAMPLES = 256
# Uncompressed streams really are zeros where they are silent or black.
_UNCOMPRESSED = ("pcm_", "rawvideo", "s302m", "v210", "v308", "v408", "v410", "r210", "yuv4")
_MKV_CLUSTER = 0x1F43B675
_MKV_SEGMENT = 0x18538067
_MKV_EBML = 0x1A45DFA3
# QuickTime files older than the 'ftyp' atom start straight in with one of these.
_MP4_FIRST_ATOMS = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot")


def _vint(handle, keep_marker: bool) -> tuple[int | None, int]:
    """One EBML variable-length integer: (value, bytes used), (None, 0) if invalid."""
    first = handle.read(1)
    if not first or first[0] == 0:
        return None, 0
    length = 9 - first[0].bit_length()
    rest = handle.read(length - 1)
    if len(rest) < length - 1:
        return None, 0
    value = first[0] if keep_marker else first[0] & ((1 << (8 - length)) - 1)
    for byte in rest:
        value = (value << 8) | byte
    return value, length


def _ebml_element(handle, offset: int) -> tuple[int, int, int] | None:
    """(id, where its body starts, body length) of the element at `offset`.

    None when the bytes there are not an element header, which inside a file
    that has been read correctly up to this point means they have not arrived.
    A length of -1 means the writer left the size open (a live recording).
    """
    handle.seek(offset)
    element, id_bytes = _vint(handle, keep_marker=True)
    if element is None:
        return None
    length, size_bytes = _vint(handle, keep_marker=False)
    if length is None:
        return None
    if length == (1 << (7 * size_bytes)) - 1:
        length = -1
    return element, offset + id_bytes + size_bytes, length


def _printable_atom(kind: bytes) -> bool:
    return len(kind) == 4 and all(0x20 <= b <= 0x7E or b == 0xA9 for b in kind)


def _media_spans(path: Path, size: int) -> tuple[list[tuple[int, int]], bool] | None:
    """Where the audio and video data lie, for the containers that say so.

    Returns (spans, is_matroska), or None when the container is another kind or
    its header doesn't add up, and the caller guesses.

    Only the media data may be sampled, because containers keep legitimate
    zeros on both sides of it. Matroska: from the first Cluster to the end the
    Segment declares. Before it sit attachments (megabytes of fonts in an anime
    release) and Void elements reserved for rewriting the header; after the
    Segment's end, a Void that a header editor such as mkvpropedit left behind.
    MP4: each 'mdat' atom and nothing else, since 'free' atoms are zeros wherever
    they are, before the data (space reserved for the index) or after it (the
    old place of an index that a tagger grew and moved). Sampling to the end of
    the file, as this did at first, called such complete films incomplete: they
    were hidden as still downloading and probed again on every pass, for good.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
            if int.from_bytes(head[:4], "big") == _MKV_EBML:
                offset, segment_end = 0, size
                for _ in range(256):
                    element = _ebml_element(handle, offset)
                    if element is None:
                        return None
                    kind, body, length = element
                    if kind == _MKV_CLUSTER:
                        return ([(offset, segment_end)], True) if offset < segment_end else None
                    if kind == _MKV_SEGMENT:
                        if length >= 0:
                            segment_end = min(body + length, size)
                        offset = body                               # its children
                        continue
                    if length < 0:
                        return None
                    offset = body + length
                    if offset >= size:
                        return None
                return None

            if head[4:8] in _MP4_FIRST_ATOMS:
                spans: list[tuple[int, int]] = []
                offset = 0
                for _ in range(1 << 16):
                    if offset >= size:
                        break
                    handle.seek(offset)
                    atom = handle.read(16)
                    length, kind, header = int.from_bytes(atom[:4], "big"), atom[4:8], 8
                    if length == 1 and len(atom) == 16:
                        length, header = int.from_bytes(atom[8:16], "big"), 16
                    elif length == 0 and _printable_atom(kind):
                        length = size - offset                     # runs to the end
                    if len(atom) < 8 or not _printable_atom(kind) or length < header:
                        # Not an atom. Before any media data that is a piece not
                        # yet downloaded, so what follows is sampled as media;
                        # after it, trailing bytes that are no part of the film.
                        if not spans:
                            spans.append((offset, size))
                        break
                    if kind == b"mdat":
                        spans.append((offset + header, min(offset + length, size)))
                    offset += length
                else:
                    spans.append((offset, size))                   # too many atoms to walk
                return (spans, False) if spans else None
    except OSError:
        return None
    return None


class _MatroskaWalk:
    """Says what a byte of a Matroska file belongs to, walking forward only.

    Clusters are the audio and video. A zero run inside one, or where the chain
    of elements breaks, is a piece still missing; one inside a Void, the Cues,
    Tags or an attachment added after the clusters is part of a complete file.
    Walking every cluster costs a read each (a feature film has one or two
    thousand), so it only happens once a sampled window has come up zero.
    """

    def __init__(self, handle, start: int, end: int) -> None:
        self._handle = handle
        self._next = start
        self._end = end
        self._current: tuple[int, int] | None = None     # (end, id) of the element holding the last answer
        self._budget = 1 << 17

    def is_media(self, position: int) -> bool:
        while self._current is None or position >= self._current[0]:
            if self._next >= self._end or self._budget <= 0:
                return True
            self._budget -= 1
            element = _ebml_element(self._handle, self._next)
            if element is None or element[2] < 0:
                return True                  # broken chain, or a size nobody wrote down
            kind, body, length = element
            self._next = body + length
            self._current = (self._next, kind)
        return self._current[1] == _MKV_CLUSTER


def _sample_offsets(spans: list[tuple[int, int]]) -> list[int]:
    """_SAMPLES window positions spread evenly over the spans laid end to end."""
    rooms = [(start, end - _WINDOW - start) for start, end in spans if end - start >= _WINDOW]
    total = sum(room for _, room in rooms)
    if not rooms or total <= 0:
        return [start for start, _ in rooms]
    step = max(_WINDOW, total // (_SAMPLES - 1))
    offsets: list[int] = []
    index = base = 0
    for point in [*range(0, total, step), total]:
        while point > base + rooms[index][1] and index + 1 < len(rooms):
            base += rooms[index][1]
            index += 1
        offsets.append(rooms[index][0] + min(point - base, rooms[index][1]))
    return offsets


def _has_zero_hole(path: Path) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    layout = _media_spans(path, size)
    if layout is None:
        # Past where a header, attachments and reserved space would end.
        spans, matroska = [(max(int(size * 0.10), 1536 * 1024), size)], False
    else:
        spans, matroska = layout
    offsets = _sample_offsets(spans)
    if not offsets:
        return False
    zero = bytes(_WINDOW)
    try:
        with open(path, "rb") as handle:
            walk = _MatroskaWalk(handle, spans[0][0], spans[-1][1]) if matroska else None
            for offset in offsets:
                handle.seek(offset)
                if handle.read(_WINDOW) == zero and (walk is None or walk.is_media(offset)):
                    return True
    except OSError:
        return False
    return False


@dataclass
class ProbeResult:
    ok: bool = False
    error: str | None = None
    incomplete: bool = False
    duration: float = 0.0
    size: int = 0
    width: int = 0
    height: int = 0
    video_codec: str | None = None
    fps: float = 0.0
    bit_depth: int = 0
    hdr: str | None = None
    audio_codec: str | None = None
    audio_channels: int = 0
    chapters: int = 0
    audio_tracks: list[Track] = field(default_factory=list)
    sub_tracks: list[Track] = field(default_factory=list)

    @property
    def resolution_label(self) -> str:
        if self.height >= 2000 or self.width >= 3600:
            return "4K"
        if self.height >= 1400:
            return "1440p"
        if self.height >= 900:
            return "1080p"
        if self.height >= 700:
            return "720p"
        if self.height >= 540:
            return "576p"
        if self.height > 0:
            return f"{self.height}p"
        return ""

    def badges(self) -> list[str]:
        """Short spec chips for the detail page."""
        out = []
        if self.resolution_label:
            out.append(self.resolution_label)
        if self.hdr:
            out.append(self.hdr)
        if self.video_codec:
            out.append(_CODEC_LABELS.get(self.video_codec, self.video_codec.upper()))
        if self.audio_codec:
            label = _CODEC_LABELS.get(self.audio_codec, self.audio_codec.upper())
            if self.audio_channels:
                label += " " + _CHANNEL_LABELS.get(self.audio_channels, f"{self.audio_channels}ch")
            out.append(label)
        if self.bit_depth >= 10:
            out.append(f"{self.bit_depth}-bit")
        return out


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_fps(rate: str | None) -> float:
    if not rate or "/" not in rate:
        return _to_float(rate)
    num, _, den = rate.partition("/")
    denominator = _to_float(den)
    return round(_to_float(num) / denominator, 3) if denominator else 0.0


def _detect_hdr(stream: dict) -> str | None:
    for side in stream.get("side_data_list") or []:
        kind = (side.get("side_data_type") or "").lower()
        if "dovi" in kind or "dolby vision" in kind:
            return "Dolby Vision"
    transfer = (stream.get("color_transfer") or "").lower()
    if transfer in _HDR_TRANSFERS:
        return _HDR_TRANSFERS[transfer]
    primaries = (stream.get("color_primaries") or "").lower()
    if primaries == "bt2020" and transfer not in ("", "bt709"):
        return "HDR"
    return None


def _bit_depth(stream: dict) -> int:
    raw = stream.get("bits_per_raw_sample")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    match = _BIT_DEPTH_RE.search(stream.get("pix_fmt") or "")
    return int(match.group(1)) if match else 8


def _track(stream: dict) -> Track:
    tags = stream.get("tags") or {}
    disposition = stream.get("disposition") or {}
    return Track(
        index=int(stream.get("index", 0)),
        codec=stream.get("codec_name") or "?",
        language=tags.get("language") or tags.get("LANGUAGE"),
        title=tags.get("title") or tags.get("TITLE"),
        channels=stream.get("channels"),
        default=bool(disposition.get("default")),
        forced=bool(disposition.get("forced")),
    )


def probe(path: str | Path, timeout: float = 90.0) -> ProbeResult:
    """Run ffprobe and fold the JSON into a flat ProbeResult."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return ProbeResult(ok=False, error="ffprobe not found on PATH")

    # "-v error" rather than "-v quiet": we need the reason on stderr to tell an
    # unfinished download apart from a genuinely broken file.
    command = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(ok=False, error="ffprobe timed out")
    except OSError as exc:
        return ProbeResult(ok=False, error=str(exc))

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        return ProbeResult(
            ok=False,
            error=detail or f"ffprobe exit {completed.returncode}",
            incomplete=_looks_incomplete(detail, path),
        )

    try:
        data = json.loads(completed.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        return ProbeResult(ok=False, error=f"unreadable ffprobe output: {exc}")

    streams = data.get("streams") or []
    if (not any((s.get("codec_name") or "").startswith(_UNCOMPRESSED) for s in streams)
            and _has_zero_hole(Path(path))):
        return ProbeResult(ok=False, error="pieces still missing (runs of zero bytes)",
                           incomplete=True)

    result = ProbeResult(ok=True)
    fmt = data.get("format") or {}
    result.duration = _to_float(fmt.get("duration"))
    result.size = int(_to_float(fmt.get("size")))
    result.chapters = len(data.get("chapters") or [])

    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video:
        result.width = int(video.get("width") or 0)
        result.height = int(video.get("height") or 0)
        result.video_codec = video.get("codec_name")
        result.fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        result.bit_depth = _bit_depth(video)
        result.hdr = _detect_hdr(video)
        if not result.duration:
            result.duration = _to_float((video.get("tags") or {}).get("DURATION-eng"))

    result.audio_tracks = [_track(s) for s in streams if s.get("codec_type") == "audio"]
    result.sub_tracks = [_track(s) for s in streams if s.get("codec_type") == "subtitle"]

    if result.audio_tracks:
        primary = next((t for t in result.audio_tracks if t.default), result.audio_tracks[0])
        result.audio_codec = primary.codec
        result.audio_channels = primary.channels or 0

    return result


def to_media_fields(result: ProbeResult) -> dict:
    """Map a ProbeResult onto the `media` table columns."""
    if not result.ok:
        # 'incomplete' is retried on every pass; 'error' is given up on.
        return {"probe_state": "incomplete" if result.incomplete else "error"}
    return {
        "duration": result.duration,
        "width": result.width,
        "height": result.height,
        "video_codec": result.video_codec,
        "hdr": result.hdr,
        "bit_depth": result.bit_depth,
        "fps": result.fps,
        "audio_codec": result.audio_codec,
        "audio_channels": result.audio_channels,
        "sub_count": len(result.sub_tracks),
        "chapters": result.chapters,
        "probe_state": "done",
    }
