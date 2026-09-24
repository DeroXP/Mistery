"""What this library holds, as a friend's Mistery sees it.

A friend keeps a copy of this so your films and albums are there to browse when
your PC is off, and so opening their library is instant rather than a wait on
the network. It is only ever a copy of what you sent: nothing here reads their
disk, and nothing there reads yours.

Three rules shape it.

**No paths, ever.** A friend gets titles, years, numbers and durations, and an
id to ask for. Where a file sits on your disk is not their business, it is what
the server refuses to derive anything from (app/party/server.py), and a folder
name can say plenty about a person. `check_no_paths` in the tests fails the
build if a path ever appears in what goes out.

**A mark per kind, so nothing is sent twice.** Films, shows, episodes, albums
and songs each carry a mark: a short hash of exactly what would be sent for
them. A friend asks with the marks it already has; every kind whose mark still
matches is answered with "unchanged" and no rows at all. When one does differ,
that whole kind is sent again — simpler than tracking single rows, and a
library changes a few times a day, not a few times a second.

**Artwork is a mark too, not a picture.** Each item says whether it has
artwork and a mark for it. The picture itself is fetched once, when the friend
first shows it, and again only when the mark changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

from .. import db
from ..config import settings

_log = logging.getLogger("share")

KINDS = ("show", "movie", "episode", "album", "track")
VIDEO_KINDS = ("show", "movie", "episode")
MUSIC_KINDS = ("album", "track")
CACHE_SECONDS = 20.0            # a second friend asking straight after the first gets the same


def marks_of(payload: dict) -> dict:
    return dict(payload.get("marks") or {})


def snapshot(*, music: bool | None = None) -> dict:
    """Everything a friend may see: {"marks": {kind: mark}, "items": {kind: [...]}}.

    Built from the library as it is now, and kept for a few seconds so that two
    friends connecting together do not build it twice. Measured on the library
    this was written against: 196 films and episodes, 14 albums, 79 songs, 31 ms.
    """
    music = bool(settings.get("sharing_music", True)) if music is None else bool(music)
    with _lock:
        cached = _cache.get(bool(music))
        if cached and time.monotonic() - cached["built_at"] < CACHE_SECONDS:
            return cached["payload"]
    items = {
        "show": _shows(),
        "movie": _films(),
        "episode": _episodes(),
        "album": _albums() if music else [],
        "track": _tracks() if music else [],
    }
    payload = {
        "items": items,
        "marks": {kind: _mark(rows) for kind, rows in items.items()},
        "music": music,
        "at": time.time(),
    }
    with _lock:
        _cache[bool(music)] = {"payload": payload, "built_at": time.monotonic()}
    return payload


def for_friend(their_marks: object, *, music: bool | None = None) -> dict:
    """The answer to "here is what I have, what has changed?".

    Kinds whose mark still matches come back named in `unchanged` with no rows.
    Everything else comes back whole. The marks in the answer are what the
    friend should send next time.
    """
    held = their_marks if isinstance(their_marks, dict) else {}
    whole = snapshot(music=music)
    items, unchanged = {}, []
    for kind in KINDS:
        if held.get(kind) == whole["marks"][kind]:
            unchanged.append(kind)
        else:
            items[kind] = whole["items"][kind]
    return {"items": items, "marks": whole["marks"], "unchanged": unchanged,
            "music": whole["music"], "at": whole["at"]}


def apply(friend_id: int, payload: object) -> dict:
    """Store what a friend sent about their library. Returns what it did.

    A kind that came whole replaces that kind: anything of theirs we hold and
    they did not mention is gone from their disk, and goes from ours. A kind
    they called unchanged is left alone.
    """
    if not isinstance(payload, dict):
        raise ValueError("a catalogue is an object")
    items = payload.get("items")
    if not isinstance(items, dict):
        raise ValueError("a catalogue has items")
    stored = dropped = 0
    touched = []
    for kind, rows in items.items():
        if kind not in KINDS or not isinstance(rows, list):
            continue                        # a kind this version does not know: ignored, not fatal
        clean = [item for item in (_clean(kind, row) for row in rows) if item]
        stored += db.save_friend_media(friend_id, clean)
        keep = {item["remote_id"] for item in clean}
        gone = [row["remote_id"] for row in db.friend_media(friend_id, kind)
                if row["remote_id"] not in keep]
        db.forget_friend_media(friend_id, kind, gone)
        dropped += len(gone)
        touched.append(kind)
    marks = payload.get("marks")
    db.update_friend(friend_id, catalog_at=time.time(),
                     catalog_mark=json.dumps(marks) if isinstance(marks, dict) else None)
    _log.info("share: friend %d's library: %d stored, %d gone (%s)",
              friend_id, stored, dropped, ", ".join(touched) or "nothing")
    return {"stored": stored, "dropped": dropped, "kinds": touched}


def held_marks(friend_id: int) -> dict:
    """The marks we hold for a friend, to ask them what has changed since."""
    row = db.friend(friend_id)
    if not row or not row["catalog_mark"]:
        return {}
    try:
        marks = json.loads(row["catalog_mark"])
    except ValueError:
        return {}
    return marks if isinstance(marks, dict) else {}


# --- what each kind sends -----------------------------------------------------

_lock = threading.Lock()
_cache: dict[bool, dict] = {}


def forget() -> None:
    """Drop the kept snapshot: the library changed, or a test wants a fresh one."""
    with _lock:
        _cache.clear()


def _mark(rows: list[dict]) -> str:
    """A short hash of exactly what would be sent, so it changes when that does."""
    text = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


def _art_mark(path: object) -> str | None:
    """A mark for a picture, from its size and date. None when there is none.

    The path never leaves this function: only the mark does. A file that is
    replaced gets a new mark, and the friend fetches it again.
    """
    if not path or not isinstance(path, str):
        return None
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return hashlib.blake2b(f"{stat.st_size}:{stat.st_mtime_ns}".encode(),
                           digest_size=6).hexdigest()


def _shows() -> list[dict]:
    rows = db.query(
        "SELECT s.id, s.title, s.sort_title, s.year, s.genres, s.overview, s.poster, s.backdrop, "
        "COUNT(m.id) AS episodes FROM shows s JOIN media m ON m.show_id = s.id "
        "AND m.kind = 'episode' AND m.missing = 0 GROUP BY s.id ORDER BY s.id")
    return [{"id": row["id"], "title": row["title"], "sort_title": row["sort_title"],
             "year": row["year"], "genres": row["genres"], "overview": row["overview"],
             "episodes": row["episodes"], "art": _art_mark(row["poster"]),
             "backdrop": _art_mark(row["backdrop"])} for row in rows]


def _films() -> list[dict]:
    rows = db.query(
        "SELECT id, title, sort_title, year, duration, genres, overview, tagline, rating, "
        "width, height, hdr, video_codec, audio_codec, audio_channels, size, poster, backdrop "
        "FROM media WHERE kind = 'movie' AND missing = 0 ORDER BY id")
    return [_video(row) for row in rows]


def _episodes() -> list[dict]:
    rows = db.query(
        "SELECT id, show_id, title, sort_title, year, season, episode, duration, overview, "
        "rating, width, height, hdr, video_codec, audio_codec, audio_channels, size, "
        "poster, backdrop FROM media WHERE kind = 'episode' AND missing = 0 ORDER BY id")
    return [_video(row) for row in rows]


def _video(row) -> dict:
    keys = row.keys()
    item = {
        "id": row["id"], "title": row["title"], "sort_title": row["sort_title"],
        "year": row["year"], "duration": row["duration"],
        "overview": row["overview"], "rating": row["rating"],
        "width": row["width"], "height": row["height"], "hdr": row["hdr"],
        "video_codec": row["video_codec"], "audio_codec": row["audio_codec"],
        "audio_channels": row["audio_channels"], "size": row["size"],
        "art": _art_mark(row["poster"]), "backdrop": _art_mark(row["backdrop"]),
    }
    for name in ("show_id", "season", "episode", "genres", "tagline"):
        if name in keys:
            item[name] = row[name]
    return item


def _albums() -> list[dict]:
    rows = db.query(
        "SELECT a.id, a.title, a.artist, a.sort_title, a.sort_artist, a.year, a.genre, a.cover, "
        "COUNT(t.id) AS songs, SUM(t.duration) AS duration FROM albums a "
        "JOIN tracks t ON t.album_id = a.id AND t.state = 'ready' AND t.missing = 0 "
        "GROUP BY a.id ORDER BY a.id")
    return [{"id": row["id"], "title": row["title"], "artist": row["artist"],
             "sort_title": row["sort_title"], "sort_artist": row["sort_artist"],
             "year": row["year"], "genre": row["genre"], "songs": row["songs"],
             "duration": row["duration"], "art": _art_mark(row["cover"])} for row in rows]


def _tracks() -> list[dict]:
    rows = db.query(
        "SELECT id, album_id, title, artist, album_artist, album, track_no, disc_no, year, "
        "genre, duration, codec, bitrate FROM tracks WHERE state = 'ready' AND missing = 0 "
        "ORDER BY id")
    return [{"id": row["id"], "album_id": row["album_id"], "title": row["title"],
             "artist": row["artist"], "album_artist": row["album_artist"],
             "album": row["album"], "track_no": row["track_no"], "disc_no": row["disc_no"],
             "year": row["year"], "genre": row["genre"], "duration": row["duration"],
             "codec": row["codec"], "bitrate": row["bitrate"]} for row in rows]


# --- what a friend's catalogue turns into here --------------------------------

def _clean(kind: str, row: object) -> dict | None:
    """One item of theirs, as a row for friend_media. None when it makes no sense.

    Everything is checked: this is a file from another PC, and the only thing
    keeping nonsense out of the library is this function. A bad item is
    dropped, never a reason to lose the rest of their catalogue.
    """
    if not isinstance(row, dict):
        return None
    remote_id = row.get("id")
    if not isinstance(remote_id, int) or isinstance(remote_id, bool) or remote_id <= 0:
        return None
    item = {
        "kind": kind,
        "remote_id": remote_id,
        "title": _text(row.get("title"), 300) or "Untitled",
        "sort_title": _text(row.get("sort_title"), 300) or None,
        "year": _whole(row.get("year"), 1800, 2200),
        "duration": _number(row.get("duration"), 0, 60 * 60 * 24),
        "overview": _text(row.get("overview"), 4000) or None,
        "genres": _text(row.get("genres") or row.get("genre"), 300) or None,
        "artist": _text(row.get("artist") or row.get("album_artist"), 300) or None,
        "art_mark": _text(row.get("art"), 64) or None,
        # A film's or show's wide picture (their backdrop): Continue Watching's
        # card shows it, fetched as art.py WIDE when the film is played or opened.
        "backdrop_mark": _text(row.get("backdrop"), 64) or None,
        "art": None,                        # filled in when the picture is fetched
    }
    if kind == "episode":
        item["parent_id"] = _whole(row.get("show_id"), 1, 2 ** 31)
        item["season"] = _whole(row.get("season"), 0, 1000)
        item["episode"] = _whole(row.get("episode"), 0, 100000)
    elif kind == "track":
        item["parent_id"] = _whole(row.get("album_id"), 1, 2 ** 31)
        # A song's disc and track numbers live in the season and episode
        # columns: the same "which one of these, in which group" they hold for
        # an episode, and one table rather than two nearly identical ones.
        item["season"] = _whole(row.get("disc_no"), 0, 1000)
        item["episode"] = _whole(row.get("track_no"), 0, 100000)
    return item


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Control characters (including the newline that would split a line on the
    # wire) out, and a hard limit: a friend's title cannot be a megabyte.
    cleaned = "".join(char for char in value[:limit] if char.isprintable() or char == " ")
    return cleaned.strip()


def _whole(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _number(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or not low <= value <= high:      # NaN, or out of range
        return None
    return float(value)
