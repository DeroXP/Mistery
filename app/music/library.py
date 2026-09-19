"""Scan audio files into albums and tracks, and answer the library's questions.

The scan is incremental like the video one — an untouched file costs a stat() —
with one deliberate exception: tracks still marked 'incomplete' are re-read on
every pass whatever their timestamps say. A torrent can write its last piece in
the same second as a previous scan saw it, and a finished file never changes
again, so trusting mtime there would strand a completed song as "downloading"
for good. There are only ever a handful, and each check costs about 3 ms.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .. import db
from ..config import AUDIO_EXTS
from . import art, loudness, tags

_log = logging.getLogger("music")

_ARTICLES = ("the ", "a ", "an ")
# "Fiveleaf - Winter Coat (1997 Rock) [Flac 24-96]"
_FOLDER_RE = re.compile(r"^\s*(?P<artist>.+?)\s+-\s+(?P<album>.+?)\s*(?:[\(\[].*)?$")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


@dataclass
class MusicScanResult:
    added: int = 0
    updated: int = 0
    finished: int = 0          # were downloading, now playable
    returned: int = 0          # were missing (a drive away), back again
    moved: int = 0             # renamed or moved on disk, history kept
    unchanged: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.finished or self.returned
                    or self.moved or self.removed)


def sort_key(text: str) -> str:
    low = (text or "").lower().strip()
    for article in _ARTICLES:
        if low.startswith(article):
            return low[len(article):]
    return low


def album_key(album_artist: str, album: str) -> str:
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    return f"{norm(album_artist)}|{norm(album)}"


def iter_audio_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and d.lower() not in {"$recycle.bin", "system volume information"}]
            for name in filenames:
                if Path(name).suffix.lower() in AUDIO_EXTS:
                    yield Path(dirpath) / name


def _from_folder(folder: Path) -> tuple[str, str, int | None]:
    """Artist, album and year out of a release folder name, for untagged files."""
    name = folder.name
    year_match = _YEAR_RE.search(name)
    match = _FOLDER_RE.match(name)
    if match:
        return match.group("artist").strip(), match.group("album").strip(), (
            int(year_match.group(1)) if year_match else None)
    return "", re.sub(r"\s*[\(\[].*$", "", name).strip(), (
        int(year_match.group(1)) if year_match else None)


def _link_album(record: dict, path: Path) -> int | None:
    """Find or create the album this track belongs to."""
    folder_artist, folder_album, folder_year = _from_folder(path.parent)
    artist = record.get("album_artist") or record.get("artist") or folder_artist or "Unknown Artist"
    title = record.get("album") or folder_album or "Unknown Album"
    key = album_key(artist, title)
    with db._write_lock:
        row = db.query_one("SELECT id FROM albums WHERE key = ?", (key,))
        if row:
            return int(row["id"])
        cursor = db.execute(
            "INSERT INTO albums (key, title, artist, sort_title, sort_artist, year, genre, added_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (key, title, artist, sort_key(title), sort_key(artist),
             record.get("year") or folder_year, record.get("genre") or None, time.time()),
        )
        return int(cursor.lastrowid)


_TRACK_COLUMNS = (
    "title", "artist", "album_artist", "album", "track_no", "disc_no", "year", "genre",
    "duration", "codec", "sample_rate", "bit_depth", "channels", "bitrate",
    "rg_track", "rg_album", "size",
)
# A song read twice gives the same length to the sample; this only absorbs
# rounding between readers.
_MOVED_DURATION_SLACK = 0.5


def _within(path: str, folders: list[str]) -> bool:
    """Is `path` inside one of `folders` (already normcased)?"""
    folded = os.path.normcase(path)
    return any(folded.startswith(folder.rstrip("\\/") + os.sep) for folder in folders)


def _moved_from(record: dict, path: Path, by_size: dict[int, list],
                claimed: set[int], away: list[str]):
    """The one vanished track this new, complete file is, or None when unsure.

    Renaming or moving an album folder used to add every song again as new,
    with no plays, no last-played time and no lyrics or loudness, while the old
    rows were left missing. The file itself is byte-for-byte the same, so a row
    whose file has gone and that agrees on size to the byte, on length, and on
    its title or track number is the same song. When several still fit, the
    file name has to agree as well, then the row that was still present before
    this pass wins (an older rename left a missing twin behind); anything still
    ambiguous is added as new rather than guessed at. A row under a library
    folder that is away right now is not gone, only unplugged, so it is never
    taken: plugging the drive back in must find its songs where they were.
    """
    size, duration = record.get("size"), float(record.get("duration") or 0)
    title = (record.get("title") or "").strip().casefold()
    number = (record.get("track_no"), record.get("disc_no") or 1)
    candidates = []
    for row in by_size.get(size, ()):
        if int(row["id"]) in claimed:
            continue
        if abs(float(row["duration"] or 0) - duration) > _MOVED_DURATION_SLACK:
            continue
        same_title = bool(title) and (row["title"] or "").strip().casefold() == title
        same_number = number[0] is not None and (row["track_no"], row["disc_no"] or 1) == number
        if not (same_title or same_number):
            continue
        if os.path.exists(row["path"]) or _within(row["path"], away):
            continue                # a copy was made, or its drive is only away
        candidates.append(row)
    if len(candidates) > 1:
        name = os.path.normcase(path.name)
        candidates = [row for row in candidates if os.path.normcase(Path(row["path"]).name) == name]
    if len(candidates) > 1:
        candidates = [row for row in candidates if not row["missing"]]
    return candidates[0] if len(candidates) == 1 else None


def scan(roots: Iterable[Path], force: bool = False,
         cancel: Callable[[], bool] | None = None,
         progress: Callable[[str], None] | None = None) -> MusicScanResult:
    result = MusicScanResult()
    roots = list(roots)
    if not roots:
        return result

    rows = db.query("SELECT id, path, mtime, size, state, missing, album_id, duration, "
                    "title, track_no, disc_no FROM tracks")
    known = {row["path"]: row for row in rows}
    # Windows paths ignore case, so a folder renamed only in case is the same
    # file under a different spelling, not a move. The present row wins over a
    # missing twin an older version left behind.
    by_case: dict[str, sqlite3.Row] = {}
    for row in sorted(rows, key=lambda r: r["missing"] or 0):
        by_case.setdefault(os.path.normcase(row["path"]), row)
    by_size: dict[int, list] = {}
    for row in rows:
        if row["state"] == "ready" and row["size"]:
            by_size.setdefault(int(row["size"]), []).append(row)
    away = [os.path.normcase(str(root)) for root in roots if not root.is_dir()]
    present: set[str] = set()
    claimed: set[int] = set()
    returned_albums: set[int] = set()

    for path in iter_audio_files(roots):
        if cancel and cancel():
            return result
        key = str(path)
        present.add(key)
        try:
            stat = path.stat()
        except OSError as exc:
            result.errors.append(f"{path.name}: {exc}")
            continue

        cached = known.get(key)
        if cached is None:
            same = by_case.get(os.path.normcase(key))
            if same is not None and int(same["id"]) not in claimed:
                cached = same
        if cached is not None:
            claimed.add(int(cached["id"]))
        if (cached and not force and cached["state"] == "ready"
                and cached["size"] == stat.st_size
                and abs((cached["mtime"] or 0) - stat.st_mtime) < 1.0):
            if cached["path"] == key and not cached["missing"]:
                result.unchanged += 1
                continue
            if cached["album_id"] is not None:
                # Back from a drive or share that was away, or the same file
                # with its folder renamed only in case, and untouched: its row
                # already says everything a read would. Reading it again meant
                # a full completeness check per song, every time an external
                # drive with a whole library on it was plugged back in.
                db.execute("UPDATE tracks SET path = ?, folder = ?, missing = 0 WHERE id = ?",
                           (key, str(path.parent), cached["id"]))
                if cached["path"] != key:
                    result.moved += 1
                else:
                    result.returned += 1
                if cached["missing"]:
                    returned_albums.add(int(cached["album_id"]))
                continue

        record = tags.read(path)
        state = record.get("state", "error")
        if cached is None and state == "ready":
            cached = _moved_from(record, path, by_size, claimed, away)
            if cached is not None:
                claimed.add(int(cached["id"]))
        fields = {k: record.get(k) for k in _TRACK_COLUMNS if k in record}
        fields.update(state=state, mtime=stat.st_mtime, size=stat.st_size,
                      folder=str(path.parent), missing=0)
        if cached is not None and cached["path"] != key:
            fields["path"] = key
        # Only a complete file can be measured; anything else waits for the pass
        # that finds it ready. Re-reading a file at all means its bytes changed,
        # so an old measurement no longer describes it.
        if not cached or cached["state"] != state or cached["size"] != stat.st_size:
            fields.update(loudness=None, peak=None,
                          loud_state="pending" if state == "ready" else "skip")
        if not fields.get("title"):
            fields["title"] = tags._title_from_filename(path)

        # An unreadable header tells us nothing about the album yet.
        if record.get("album") or record.get("artist") or state == "ready":
            fields["album_id"] = _link_album(record, path)

        if cached:
            # Tracks still downloading, and files that never read, come through
            # here on every pass. Writing back a row that hasn't moved still
            # counts as the database changing, and the window reloads every page
            # when it does — every 20 s for as long as anything is downloading.
            stored = db.query_one("SELECT * FROM tracks WHERE id = ?", (cached["id"],))
            compared = fields
            if (stored is not None and state != "ready" and stored["state"] == state
                    and stored["size"] == stat.st_size and stored["path"] == key):
                # A download in progress changes its modified time with every
                # piece that lands, and nothing else until it is whole. The
                # time is only trusted for rows that are ready, since the rest
                # are read again on every pass anyway, so it is left behind
                # here; the pass that finds the file ready writes the real one.
                compared = {column: value for column, value in fields.items() if column != "mtime"}
            if stored is not None and all(stored[column] == value
                                          for column, value in compared.items()):
                result.unchanged += 1
                continue
            if cached["state"] != "ready" and state == "ready":
                result.finished += 1
            elif cached["path"] != key:
                result.moved += 1
                if fields.get("album_id"):
                    # A folder name can be all an untagged album goes by, so
                    # the move may have put the song on another album.
                    returned_albums.add(int(fields["album_id"]))
            elif cached["missing"]:
                result.returned += 1
                if fields.get("album_id"):
                    returned_albums.add(int(fields["album_id"]))
            elif cached["state"] != state or cached["size"] != stat.st_size:
                result.updated += 1
            sets = ", ".join(f"{column} = ?" for column in fields)
            db.execute(f"UPDATE tracks SET {sets} WHERE id = ?",
                       (*fields.values(), cached["id"]))
        else:
            fields.update(path=key, added_at=time.time())
            columns = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            try:
                db.execute(f"INSERT INTO tracks ({columns}) VALUES ({marks})",
                           tuple(fields.values()))
                result.added += 1
            except sqlite3.IntegrityError:
                # Another pass inserted it a moment ago; update instead.
                fields.pop("added_at", None)
                path_value = fields.pop("path")
                sets = ", ".join(f"{column} = ?" for column in fields)
                db.execute(f"UPDATE tracks SET {sets} WHERE path = ?", (*fields.values(), path_value))
                result.updated += 1
        if fields.get("album_id"):
            # A new or changed file may carry a cover its album didn't have; the
            # album's retries after 'none' only look at folders.
            db.execute("UPDATE albums SET art_state = 'pending' "
                       "WHERE id = ? AND art_state = 'none'", (fields["album_id"],))
        if progress:
            progress(fields["title"])

    gone = [row["id"] for p, row in known.items()
            if p not in present and not row["missing"] and int(row["id"]) not in claimed]
    for track_id in gone:
        db.execute("UPDATE tracks SET missing = 1 WHERE id = ?", (track_id,))
    result.removed = len(gone)
    _refresh_album_details()
    # Songs measured before they went away keep their loudness, but an album
    # made again by an older version lost its own; work it out from them.
    for album_id in returned_albums:
        refresh_album_loudness(album_id)
    return result


def _refresh_album_details() -> None:
    """Year and genre by majority of tracks; drop albums no track belongs to.

    An album whose tracks are only *missing* is kept. Missing is most often a
    drive or network folder that is away for now, and one watcher pass while it
    was unplugged used to delete every album on it: bringing it back made them
    again as new albums, with new ids, a new place in Recent, covers made again
    and the album loudness gone for good, because its songs were already
    measured. Every listing joins on tracks that are present, so a kept album is
    simply out of sight until its files return.

    This runs after every scan, including the watcher's passes where nothing
    changed, so it writes only what differs.
    """
    years: dict[int, Counter] = {}
    genres: dict[int, Counter] = {}
    linked: set[int] = set()
    for row in db.query("SELECT album_id, year, genre, missing FROM tracks "
                        "WHERE album_id IS NOT NULL ORDER BY album_id, disc_no, track_no, id"):
        album_id = int(row["album_id"])
        linked.add(album_id)
        if row["missing"]:
            continue
        if row["year"]:
            years.setdefault(album_id, Counter())[row["year"]] += 1
        if row["genre"]:
            genres.setdefault(album_id, Counter())[row["genre"]] += 1

    for album in db.query("SELECT id, year, genre FROM albums"):
        album_id = int(album["id"])
        if album_id not in linked:
            db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            continue
        year = years[album_id].most_common(1)[0][0] if album_id in years else album["year"]
        genre = genres[album_id].most_common(1)[0][0] if album_id in genres else album["genre"]
        if year != album["year"] or genre != album["genre"]:
            db.execute("UPDATE albums SET year = ?, genre = ? WHERE id = ?",
                       (year, genre, album_id))


def reclaim_missing_artwork() -> int:
    """Put albums whose cover files have gone back in the queue to be made.

    An album marked 'done' is never looked at again, so a cover deleted by a
    cleanup tool, or never really written where this process can see it, would
    stay a blank placeholder for good. Four did exactly that: they were made by a
    process running inside another app's AppData sandbox (see
    config.virtualized_appdata), so the files went to a private copy while the
    library recorded them as made. Costs two stat() calls per album.
    """
    reclaimed = 0
    for album in db.query("SELECT id, cover FROM albums WHERE art_state = 'done'"):
        cover = album["cover"]
        if cover and os.path.exists(cover) and os.path.exists(cover.replace(".jpg", "-sm.jpg")):
            continue
        db.execute("UPDATE albums SET art_state = 'pending' WHERE id = ?", (album["id"],))
        reclaimed += 1
    if reclaimed:
        _log.info("%d album cover(s) had gone missing; making them again", reclaimed)
    return reclaimed


def build_artwork(cancel: Callable[[], bool] | None = None) -> int:
    """Covers and palettes for albums that don't have them yet.

    'none' (every song finished, no image anywhere) is not taken as final.
    Albums arrive by torrent, which completes files in any order, and
    qBittorrent hides a file behind '.!qB' until it is whole, so cover.jpg often
    lands after the last song; an album marked 'none' at that moment kept its
    placeholder for good. Those albums have their folders listed again on each
    pass, which is the whole cost, and nothing is written unless an image has
    appeared. Art inside the songs can only appear by a file changing, and the
    scan puts that album back to 'pending' itself.
    """
    reclaim_missing_artwork()
    built = 0
    for album in db.query(
            "SELECT id, key, art_state FROM albums WHERE art_state IN ('pending', 'none')"):
        if cancel and cancel():
            break
        tracks = db.query(
            "SELECT path, folder, state FROM tracks WHERE album_id = ? AND missing = 0 "
            "ORDER BY state = 'ready' DESC, disc_no, track_no", (album["id"],))
        if not tracks:
            continue
        retry = album["art_state"] == "none"
        data = None
        for track in tracks:
            if retry:
                break
            # A partial file's PICTURE block can itself be zeroed; use it only
            # if nothing else turns up.
            if track["state"] == "ready":
                data = art.embedded_cover(track["path"])
                if data:
                    break
        if not data:
            # Every folder the album's songs sit in, not just the first: a
            # two-disc set is often CD1/ and CD2/.
            for folder in dict.fromkeys(t["folder"] for t in tracks if t["folder"]):
                image = art.folder_cover(folder)
                if image is None:
                    continue
                try:
                    data = image.read_bytes()
                except OSError:
                    data = None
                break
        if not data:
            # Nothing yet; an album that is still arriving may get one later.
            if not retry and all(t["state"] == "ready" for t in tracks):
                db.execute("UPDATE albums SET art_state = 'none' WHERE id = ?", (album["id"],))
            continue
        saved = art.save_cover(album["key"], data)
        if not saved:
            continue
        colours = art.palette_json(saved[0])
        db.execute("UPDATE albums SET cover = ?, palette = ?, art_state = 'done' WHERE id = ?",
                   (saved[0], colours, album["id"]))
        built += 1
    return built


def measure_loudness(cancel: Callable[[], bool] | None = None,
                     progress: Callable[[str, int, int], None] | None = None,
                     limit: int = 400) -> int:
    """Measure how loud each new song is, so albums can be played at one level.

    About half a second of decoding per song, once per file, and only for files
    that are complete. Album figures are recomputed from the tracks as they
    arrive, so an album that is still downloading gets its own level as soon as
    its last song does.
    """
    pending = db.query(
        "SELECT id, path, album_id FROM tracks "
        "WHERE state = 'ready' AND missing = 0 AND loud_state = 'pending' "
        "ORDER BY album_id, disc_no, track_no LIMIT ?", (limit,))
    if not pending:
        return 0
    measured = 0
    touched: set[int] = set()
    for index, row in enumerate(pending, start=1):
        if cancel and cancel():
            break
        if progress:
            progress(Path(row["path"]).name, index, len(pending))
        try:
            # `cancel` reaches the ffmpeg run too: a long hi-res file is half a
            # minute of decoding, and quitting should not wait that out.
            lufs, peak = loudness.measure(row["path"], cancel=cancel)
        except loudness.Unmeasurable as exc:
            if cancel and cancel():
                break                   # stopped, not unmeasurable: still pending
            db.execute("UPDATE tracks SET loud_state = 'error' WHERE id = ?", (row["id"],))
            _log.warning("could not measure %s: %s", Path(row["path"]).name, exc)
        else:
            db.execute(
                "UPDATE tracks SET loudness = ?, peak = ?, loud_state = 'done' WHERE id = ?",
                (round(lufs, 2), round(peak, 2), row["id"]))
            measured += 1
        if row["album_id"]:
            touched.add(int(row["album_id"]))
    for album_id in touched:
        refresh_album_loudness(album_id)
    return measured


def refresh_album_loudness(album_id: int) -> None:
    """One loudness for the whole album, from whichever tracks are measured."""
    rows = db.query(
        "SELECT loudness, peak, duration FROM tracks "
        "WHERE album_id = ? AND missing = 0 AND loud_state = 'done'", (album_id,))
    combined = loudness.combine([(r["loudness"], r["peak"], r["duration"] or 1.0) for r in rows])
    if combined is None:
        return
    db.execute("UPDATE albums SET loudness = ?, peak = ? WHERE id = ?",
               (round(combined[0], 2), round(combined[1], 2), album_id))


def loudness_progress() -> tuple[int, int]:
    """(measured, total) across the playable library."""
    row = db.query_one(
        "SELECT SUM(loud_state = 'done') AS done, COUNT(*) AS total FROM tracks "
        "WHERE state = 'ready' AND missing = 0")
    return (int(row["done"] or 0), int(row["total"] or 0)) if row else (0, 0)


# --- queries ------------------------------------------------------------------

_ALBUM_SELECT = """
    SELECT a.*,
           SUM(t.state = 'ready')                        AS ready_count,
           COUNT(t.id)                                   AS track_count,
           SUM(CASE WHEN t.state = 'ready' THEN t.duration ELSE 0 END) AS duration,
           MAX(t.last_played)                            AS last_played,
           MAX(t.sample_rate)                            AS sample_rate,
           MAX(t.bit_depth)                              AS bit_depth,
           MAX(t.codec)                                  AS codec,
           MAX(t.bitrate)                                AS bitrate
    FROM albums a
    JOIN tracks t ON t.album_id = a.id AND t.missing = 0
"""


def albums(order: str = "artist") -> list:
    ordering = {
        "artist": "a.sort_artist, a.year, a.sort_title",
        "title": "a.sort_title",
        "recent": "a.added_at DESC",
        "played": "last_played IS NULL, last_played DESC",
    }.get(order, "a.sort_artist, a.year, a.sort_title")
    return db.query(_ALBUM_SELECT + f" GROUP BY a.id ORDER BY {ordering}")


def album(album_id: int):
    return db.query_one(_ALBUM_SELECT + " WHERE a.id = ? GROUP BY a.id", (album_id,))


def album_tracks(album_id: int) -> list:
    return db.query(
        "SELECT t.*, a.cover AS cover, a.palette AS palette, a.title AS album_title, "
        "a.loudness AS album_loudness, a.peak AS album_peak "
        "FROM tracks t LEFT JOIN albums a ON a.id = t.album_id "
        "WHERE t.album_id = ? AND t.missing = 0 "
        "ORDER BY COALESCE(t.disc_no, 1), COALESCE(t.track_no, 9999), t.title",
        (album_id,),
    )


def artists() -> list:
    return db.query(
        """
        SELECT a.artist AS name, a.sort_artist AS sort_name,
               COUNT(DISTINCT a.id) AS album_count,
               SUM(t.state = 'ready') AS track_count,
               (SELECT a2.cover FROM albums a2 WHERE a2.artist = a.artist AND a2.cover IS NOT NULL
                  AND EXISTS (SELECT 1 FROM tracks t2 WHERE t2.album_id = a2.id AND t2.missing = 0)
                ORDER BY a2.year LIMIT 1) AS cover
        FROM albums a JOIN tracks t ON t.album_id = a.id AND t.missing = 0
        GROUP BY a.artist ORDER BY a.sort_artist
        """
    )


def artist_albums(name: str) -> list:
    return db.query(_ALBUM_SELECT + " WHERE a.artist = ? GROUP BY a.id ORDER BY a.year, a.sort_title",
                    (name,))


def tracks(where: str = "", params: tuple = (), order: str = "") -> list:
    return db.query(
        "SELECT t.*, a.cover AS cover, a.palette AS palette, a.title AS album_title, "
        "a.artist AS album_artist_name, a.loudness AS album_loudness, a.peak AS album_peak "
        "FROM tracks t LEFT JOIN albums a ON a.id = t.album_id "
        "WHERE t.missing = 0 AND t.state = 'ready' " + (f"AND {where} " if where else "")
        + "ORDER BY " + (order or "a.sort_artist, a.year, a.sort_title, t.disc_no, t.track_no"),
        params,
    )


def track(track_id: int):
    rows = tracks("t.id = ?", (track_id,))
    return rows[0] if rows else None


def tracks_by_id(track_ids: Iterable[int]) -> dict[int, sqlite3.Row]:
    """Playable tracks by id, for putting a saved queue back together.

    Ids of songs that have since gone missing, or are not ready, are simply
    absent from the result. Asked in chunks, since SQLite caps how many values
    one statement may bind.
    """
    wanted = list(dict.fromkeys(int(track_id) for track_id in track_ids))
    found: dict[int, sqlite3.Row] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        marks = ", ".join("?" for _ in chunk)
        for row in tracks(f"t.id IN ({marks})", tuple(chunk), "t.id"):
            found[int(row["id"])] = row
    return found


def set_liked(track_id: int, liked: bool) -> float | None:
    """Like or unlike a song. Liking again keeps the original time, so a second
    click from another view does not move it to the top of Liked songs.

    Returns the song's liked_at as stored afterwards (None once unliked, or for
    an unknown id). A caller holding an older copy of the row can't tell a
    fresh like from a repeat one, so it takes the time from here instead of
    guessing, and its copy sorts the same as the Liked songs list.
    """
    if liked:
        db.execute("UPDATE tracks SET liked = 1, liked_at = COALESCE(liked_at, ?) "
                   "WHERE id = ? AND (liked = 0 OR liked_at IS NULL)",
                   (time.time(), int(track_id)))
    else:
        db.execute("UPDATE tracks SET liked = 0, liked_at = NULL "
                   "WHERE id = ? AND (liked != 0 OR liked_at IS NOT NULL)", (int(track_id),))
        return None
    row = db.query_one("SELECT liked_at FROM tracks WHERE id = ?", (int(track_id),))
    return row["liked_at"] if row is not None else None


def liked_tracks() -> list:
    """Liked songs that can be played, most recently liked first."""
    return tracks("t.liked = 1", (), "t.liked_at DESC, t.id DESC")


def playlist_tracks(playlist_id: int) -> list[dict]:
    """The songs of a music playlist, in the order they were put in.

    Keyed on track ids, so a rescan, a finished download or a moved folder
    leaves the list alone — only the paths under it move. An id that no longer
    resolves (file gone, or a song still downloading) is skipped rather than
    dropped from the playlist, exactly as a restored queue skips one: the file
    may be back tomorrow.

    Each row carries the `entry_id` that put it there. The same song may be in
    a list twice, and then "remove this one" and "move this one up" have to
    mean one of them; a track id cannot say which.

    111 songs in 1.3 ms, measured — the whole library as one playlist.
    """
    entries = db.playlist_entries(playlist_id)
    if not entries:
        return []
    found = tracks_by_id(int(row["item_id"]) for row in entries)
    rows = []
    for entry in entries:
        track = found.get(int(entry["item_id"]))
        if track is not None:
            rows.append({**dict(track), "entry_id": int(entry["id"])})
    return rows


def playlist_covers(playlist_ids: Iterable[int]) -> dict[int, str]:
    """One sleeve per playlist, for the tiles: the first song's album cover.

    The cheap choice over a mosaic of four, and it costs one query for the
    whole page rather than one per tile: 0.10 ms for 12 playlists, measured.
    A playlist whose first songs have no cover yet simply isn't in the result.
    """
    wanted = [int(i) for i in playlist_ids]
    covers: dict[int, str] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        marks = ", ".join("?" for _ in chunk)
        rows = db.query(
            "SELECT i.playlist_id AS pid, a.cover AS cover FROM playlist_items i "
            "JOIN tracks t ON t.id = i.item_id AND t.missing = 0 AND t.state = 'ready' "
            "JOIN albums a ON a.id = t.album_id "
            f"WHERE i.playlist_id IN ({marks}) AND a.cover IS NOT NULL "
            "ORDER BY i.playlist_id, i.position",
            tuple(chunk),
        )
        for row in rows:
            covers.setdefault(int(row["pid"]), row["cover"])
    return covers


_DETAIL_COLUMNS = (
    "id", "album_id", "title", "artist", "album_artist", "album_title", "path", "folder",
    "size", "codec", "sample_rate", "bit_depth", "bitrate", "channels", "duration",
    "loudness", "peak", "album_loudness", "album_peak", "rg_track", "rg_album",
    "play_count", "last_played", "added_at", "liked", "liked_at", "track_no", "disc_no",
    "year", "genre", "state", "missing",
)


def track_details(track_id: int) -> dict | None:
    """Everything the library knows about one file, for the Details tab.

    Not limited to playable songs: a song still downloading or gone missing
    can be on screen too, and saying so is part of the details.
    """
    row = db.query_one(
        "SELECT t.*, a.title AS album_title, a.loudness AS album_loudness, "
        "a.peak AS album_peak "
        "FROM tracks t LEFT JOIN albums a ON a.id = t.album_id WHERE t.id = ?",
        (int(track_id),))
    if row is None:
        return None
    keys = set(row.keys())
    details = {column: row[column] for column in _DETAIL_COLUMNS if column in keys}
    details["album_title"] = details.get("album_title") or row["album"]
    details["liked"] = int(details.get("liked") or 0)
    return details


def recently_played(limit: int = 12) -> list:
    return tracks("t.last_played IS NOT NULL", (), "t.last_played DESC")[:limit]


def search(text: str) -> list:
    like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return tracks(
        "(t.title LIKE ? ESCAPE '\\' OR t.artist LIKE ? ESCAPE '\\' OR a.title LIKE ? ESCAPE '\\')",
        (like, like, like),
    )


def mark_played(track_id: int) -> None:
    db.execute("UPDATE tracks SET play_count = play_count + 1, last_played = ? WHERE id = ?",
               (time.time(), track_id))


def has_music() -> bool:
    row = db.query_one("SELECT 1 FROM tracks WHERE missing = 0 AND state = 'ready' LIMIT 1")
    return row is not None


def incomplete_count() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM tracks WHERE missing = 0 AND state = 'incomplete'")
    return int(row["n"]) if row else 0


def parse_palette(raw: str | None) -> dict:
    try:
        colours = json.loads(raw) if raw else None
    except ValueError:
        colours = None
    return colours or {"dark": "#181818", "mid": "#2A2A2A", "accent": "#E50914"}
