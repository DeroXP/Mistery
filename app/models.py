"""View models — a thin, display-oriented wrapper over database rows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .probe import _CHANNEL_LABELS, _CODEC_LABELS
from .util import episode_code, fmt_duration, fmt_size, progress_fraction


def _get(row: Any, key: str, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


@dataclass
class MediaItem:
    id: int = 0
    path: str = ""
    kind: str = "movie"
    title: str = ""
    year: int | None = None
    show_id: int | None = None
    season: int | None = None
    episode: int | None = None
    edition: str | None = None

    duration: float = 0.0
    size: int = 0
    width: int = 0
    height: int = 0
    hdr: str | None = None
    bit_depth: int = 0
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_channels: int = 0
    sub_count: int = 0
    chapters: int = 0
    fps: float = 0.0

    overview: str | None = None
    tagline: str | None = None
    genres: str | None = None
    rating: float | None = None
    poster: str | None = None
    backdrop: str | None = None
    meta_source: str | None = None
    thumbs: str | None = None

    position: float = 0.0
    watched: bool = False
    tags: dict = field(default_factory=dict)
    added_at: float = 0.0        # when the scan first found the file

    # learned per-episode markers (audio fingerprinting)
    intro_start: float | None = None
    intro_end: float | None = None
    credits_at: float | None = None

    @classmethod
    def from_row(cls, row) -> "MediaItem":
        if row is None:
            return cls()
        raw_tags = _get(row, "tags")
        try:
            tags = json.loads(raw_tags) if raw_tags else {}
        except ValueError:
            tags = {}
        return cls(
            id=int(_get(row, "id", 0)),
            path=_get(row, "path", ""),
            kind=_get(row, "kind", "movie"),
            title=_get(row, "title", ""),
            year=_get(row, "year"),
            show_id=_get(row, "show_id"),
            season=_get(row, "season"),
            episode=_get(row, "episode"),
            edition=_get(row, "edition"),
            duration=float(_get(row, "duration", 0.0)),
            size=int(_get(row, "size", 0)),
            width=int(_get(row, "width", 0)),
            height=int(_get(row, "height", 0)),
            hdr=_get(row, "hdr"),
            bit_depth=int(_get(row, "bit_depth", 0)),
            video_codec=_get(row, "video_codec"),
            audio_codec=_get(row, "audio_codec"),
            audio_channels=int(_get(row, "audio_channels", 0)),
            sub_count=int(_get(row, "sub_count", 0)),
            chapters=int(_get(row, "chapters", 0)),
            fps=float(_get(row, "fps", 0.0)),
            overview=_get(row, "overview"),
            tagline=_get(row, "tagline"),
            genres=_get(row, "genres"),
            rating=_get(row, "rating"),
            poster=_get(row, "poster"),
            backdrop=_get(row, "backdrop"),
            meta_source=_get(row, "meta_source"),
            thumbs=_get(row, "thumbs"),
            position=float(_get(row, "position", 0.0)),
            watched=bool(_get(row, "watched", 0)),
            tags=tags,
            added_at=float(_get(row, "added_at", 0.0)),
            intro_start=_get(row, "intro_start"),
            intro_end=_get(row, "intro_end"),
            credits_at=_get(row, "credits_at"),
        )

    # --- display helpers ----------------------------------------------------

    @property
    def is_episode(self) -> bool:
        return self.kind == "episode"

    @property
    def code(self) -> str:
        return episode_code(self.season, self.episode)

    @property
    def display_title(self) -> str:
        if self.is_episode and self.code:
            return f"{self.code} · {self.title}" if self.title else self.code
        return self.title

    @property
    def subtitle(self) -> str:
        bits = []
        if not self.is_episode and self.year:
            bits.append(str(self.year))
        if self.duration:
            bits.append(fmt_duration(self.duration))
        if self.edition:
            bits.append(self.edition)
        return " · ".join(bits)

    @property
    def progress(self) -> float:
        return progress_fraction(self.position, self.duration)

    @property
    def resume_position(self) -> float:
        """Where playback should start: a little before where you stopped."""
        if self.watched or self.position < 30:
            return 0.0
        if self.duration and self.position > self.duration * 0.97:
            return 0.0
        return max(0.0, self.position - 6.0)

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
        if self.height > 0:
            return f"{self.height}p"
        return self.tags.get("resolution", "")

    @property
    def badges(self) -> list[str]:
        """Spec chips for the detail page and hero banner."""
        out: list[str] = []
        if self.resolution_label:
            out.append(self.resolution_label)
        if self.hdr:
            out.append(self.hdr)
        elif self.tags.get("hdr"):
            out.append(self.tags["hdr"])

        codec = self.video_codec or ""
        if codec:
            out.append(_CODEC_LABELS.get(codec, codec.upper()))
        elif self.tags.get("codec"):
            out.append(self.tags["codec"])

        if self.audio_codec:
            label = _CODEC_LABELS.get(self.audio_codec, self.audio_codec.upper())
            if self.audio_channels:
                label += " " + _CHANNEL_LABELS.get(
                    self.audio_channels, f"{self.audio_channels}ch"
                )
            out.append(label)
        elif self.tags.get("audio"):
            audio = self.tags["audio"]
            if self.tags.get("channels"):
                audio += " " + self.tags["channels"]
            out.append(audio)

        if self.bit_depth >= 10:
            out.append(f"{self.bit_depth}-bit")
        if self.size:
            out.append(fmt_size(self.size))
        return out

    @property
    def art(self) -> str | None:
        return self.poster or self.backdrop

    @property
    def wide_art(self) -> str | None:
        return self.backdrop or self.poster


@dataclass
class ShowItem:
    id: int = 0
    title: str = ""
    year: int | None = None
    overview: str | None = None
    poster: str | None = None
    backdrop: str | None = None
    genres: str | None = None
    rating: float | None = None
    episode_count: int = 0
    season_count: int = 0
    watched_count: int = 0
    # The newest episode's arrival (db.all_shows), else when the show was first
    # seen, so a new episode brings an old show back to the top of "Recently added".
    added_at: float = 0.0

    @classmethod
    def from_row(cls, row) -> "ShowItem":
        if row is None:
            return cls()
        return cls(
            id=int(_get(row, "id", 0)),
            title=_get(row, "title", ""),
            year=_get(row, "year"),
            overview=_get(row, "overview"),
            poster=_get(row, "poster"),
            backdrop=_get(row, "backdrop"),
            genres=_get(row, "genres"),
            rating=_get(row, "rating"),
            episode_count=int(_get(row, "episode_count", 0)),
            season_count=int(_get(row, "season_count", 0)),
            watched_count=int(_get(row, "watched_count", 0)),
            added_at=float(_get(row, "latest_added") or _get(row, "added_at", 0.0)),
        )

    @property
    def subtitle(self) -> str:
        bits = []
        if self.season_count:
            bits.append(f"{self.season_count} season{'s' if self.season_count != 1 else ''}")
        if self.episode_count:
            bits.append(f"{self.episode_count} episode{'s' if self.episode_count != 1 else ''}")
        return " · ".join(bits)

    @property
    def progress(self) -> float:
        if not self.episode_count:
            return 0.0
        return max(0.0, min(1.0, self.watched_count / self.episode_count))

    @property
    def art(self) -> str | None:
        return self.poster or self.backdrop
