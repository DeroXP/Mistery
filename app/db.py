"""SQLite library cache.

One connection per thread (background scan/probe/metadata workers all write),
WAL mode so readers never block behind a writer.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import db_path

_local = threading.local()


class _WriteLock:
    """The lock every write in this process takes, which also tells this
    process's writes apart from anyone else's.

    PRAGMA data_version changes when any *other* connection commits, and every
    worker thread has a connection of its own. So to the window, a lyrics lookup
    or a music-watcher pass on a pool thread looked exactly like a maintenance
    script editing the library: each one rebuilt the page on screen, wiping
    half-typed Settings fields and the selected song, every 20 s while an album
    was downloading.

    All writes here already queue behind this lock, so it can keep score, with
    two readings each time it is taken and let go:

    - A private connection that never writes. A change it sees on taking the
      lock happened while nothing here was writing, so it came from outside.
      A change across the hold is ours, and is not counted.
    - The writing thread's own connection, which never sees its own commits.
      Nothing else here can commit during the hold, so a change it sees across
      the hold is an outside commit. That is not rare: when another process is
      writing, our write waits for it, and its commit lands inside our hold.
      Measured with a second process writing while a thread here wrote every
      5 ms, the private connection alone took all 5 outside commits for ours.

    The readings are ordered so that a commit landing between two of them is
    counted twice rather than missed; twice only means one reload. The outside
    count is what data_version() reports.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self._watch: sqlite3.Connection | None = None
        self._seen: int | None = None
        self._writer: tuple[sqlite3.Connection, int] | None = None
        self.external = 0

    def acquire(self, blocking: bool = True) -> bool:
        if not self._lock.acquire(blocking):
            return False
        self._depth += 1
        if self._depth == 1:
            self._writer = self._own_reading(connect_if_needed=True)
            self._observe(outside=True)
        return True

    def release(self) -> None:
        try:
            if self._depth == 1:
                self._observe(outside=False)
                before, self._writer = self._writer, None
                after = self._own_reading(connect_if_needed=False)
                # Only the same connection's readings compare: one closed and
                # opened again inside the hold starts counting afresh.
                if before and after and before[0] is after[0] and before[1] != after[1]:
                    self.external += 1
        finally:
            self._depth -= 1
            self._lock.release()

    def __enter__(self) -> "_WriteLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def poll(self) -> int:
        """The outside-change count, brought up to date unless a write is under
        way here; that write looks on its way out, and the next poll will look."""
        if self._lock.acquire(blocking=False):
            try:
                if self._depth == 0:
                    self._observe(outside=True)
            finally:
                self._lock.release()
        return self.external

    @staticmethod
    def _own_reading(connect_if_needed: bool) -> tuple[sqlite3.Connection, int] | None:
        # Bookkeeping must never stop a write, so any failure just skips it.
        try:
            conn = connect() if connect_if_needed else getattr(_local, "conn", None)
            if conn is None:
                return None
            return conn, int(conn.execute("PRAGMA data_version").fetchone()[0])
        except Exception:
            return None

    def _observe(self, outside: bool) -> None:
        try:
            if self._watch is None:
                self._watch = sqlite3.connect(db_path(), timeout=30.0, isolation_level=None,
                                              check_same_thread=False)
            version = int(self._watch.execute("PRAGMA data_version").fetchone()[0])
        except Exception:
            self.close()                # a damaged or missing file; try again next time
            return
        if outside and self._seen is not None and version != self._seen:
            self.external += 1
        self._seen = version

    def close(self) -> None:
        if self._watch is not None:
            try:
                self._watch.close()
            except sqlite3.Error:
                pass
        self._watch = None
        self._seen = None


_write_lock = _WriteLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id           INTEGER PRIMARY KEY,
    key          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    sort_title   TEXT,
    year         INTEGER,
    tmdb_id      INTEGER,
    overview     TEXT,
    poster       TEXT,
    backdrop     TEXT,
    genres       TEXT,
    rating       REAL,
    meta_state   TEXT NOT NULL DEFAULT 'pending',
    added_at     REAL
);

CREATE TABLE IF NOT EXISTS media (
    id             INTEGER PRIMARY KEY,
    path           TEXT NOT NULL UNIQUE,
    folder         TEXT,
    kind           TEXT NOT NULL DEFAULT 'movie',      -- movie | episode
    title          TEXT NOT NULL,
    sort_title     TEXT,
    year           INTEGER,
    show_id        INTEGER REFERENCES shows(id) ON DELETE CASCADE,
    season         INTEGER,
    episode        INTEGER,
    edition        TEXT,
    size           INTEGER,
    mtime          REAL,

    duration       REAL,
    width          INTEGER,
    height         INTEGER,
    video_codec    TEXT,
    hdr            TEXT,
    bit_depth      INTEGER,
    fps            REAL,
    audio_codec    TEXT,
    audio_channels INTEGER,
    sub_count      INTEGER DEFAULT 0,
    chapters       INTEGER DEFAULT 0,
    probe_state    TEXT NOT NULL DEFAULT 'pending',    -- pending | done | error

    tmdb_id        INTEGER,
    overview       TEXT,
    poster         TEXT,
    backdrop       TEXT,
    genres         TEXT,
    rating         REAL,
    tagline        TEXT,
    meta_state     TEXT NOT NULL DEFAULT 'pending',
    meta_source    TEXT,

    thumbs         TEXT,
    thumbs_state   TEXT NOT NULL DEFAULT 'pending',

    tags           TEXT,
    added_at       REAL,
    missing        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS progress (
    media_id   INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    position   REAL NOT NULL DEFAULT 0,
    duration   REAL NOT NULL DEFAULT 0,
    watched    INTEGER NOT NULL DEFAULT 0,
    play_count INTEGER NOT NULL DEFAULT 0,
    audio_id   INTEGER,
    sub_id     INTEGER,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    body       TEXT,
    fetched_at REAL
);

-- Music. Albums are keyed on album artist + album title, so an album whose
-- tracks sit in two folders (or were downloaded twice) is still one album.
CREATE TABLE IF NOT EXISTS albums (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    artist      TEXT NOT NULL,
    sort_title  TEXT,
    sort_artist TEXT,
    year        INTEGER,
    genre       TEXT,
    cover       TEXT,
    palette     TEXT,                                   -- JSON colours from the cover
    art_state   TEXT NOT NULL DEFAULT 'pending',        -- pending | done | none
    added_at    REAL
);

CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    folder       TEXT,
    album_id     INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    artist       TEXT,
    album_artist TEXT,
    album        TEXT,
    track_no     INTEGER,
    disc_no      INTEGER,
    year         INTEGER,
    genre        TEXT,
    duration     REAL,
    codec        TEXT,
    sample_rate  INTEGER,
    bit_depth    INTEGER,
    channels     INTEGER,
    bitrate      INTEGER,
    rg_track     REAL,                                  -- ReplayGain dB
    rg_album     REAL,
    size         INTEGER,
    mtime        REAL,
    state        TEXT NOT NULL DEFAULT 'pending',       -- ready | incomplete | error
    missing      INTEGER NOT NULL DEFAULT 0,
    play_count   INTEGER NOT NULL DEFAULT 0,
    last_played  REAL,
    added_at     REAL
);

CREATE TABLE IF NOT EXISTS lyrics (
    track_id   INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    synced     TEXT,
    plain      TEXT,
    source     TEXT,                                    -- lrc | embedded | lrclib | none
    fetched_at REAL
);

CREATE INDEX IF NOT EXISTS idx_tracks_album  ON tracks(album_id, disc_no, track_no);
CREATE INDEX IF NOT EXISTS idx_tracks_state  ON tracks(state, missing);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(sort_artist, year);

CREATE INDEX IF NOT EXISTS idx_media_kind   ON media(kind, missing);
CREATE INDEX IF NOT EXISTS idx_media_show   ON media(show_id, season, episode);
CREATE INDEX IF NOT EXISTS idx_media_probe  ON media(probe_state);
CREATE INDEX IF NOT EXISTS idx_media_meta   ON media(meta_state);
CREATE INDEX IF NOT EXISTS idx_media_thumbs ON media(thumbs_state);
CREATE INDEX IF NOT EXISTS idx_progress_upd ON progress(updated_at);
"""


def connect() -> sqlite3.Connection:
    """Thread-local connection, initialised on first use in each thread."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(db_path(), timeout=30.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            # A damaged file fails right here ("file is not a database"). Left
            # open, the connection kept the file locked until garbage collection
            # got to it, so a repair could not move the damaged file aside.
            conn.close()
            raise
        _local.conn = conn
    return conn


# Columns added after the first release; applied to existing databases on
# startup. SQLite's ADD COLUMN is cheap and non-destructive.
_MIGRATIONS: dict[str, list[str]] = {
    "shows": [
        "intro_start REAL",       # seconds; NULL = no intro marked
        "intro_end REAL",
        "credits_len REAL",       # seconds from the end; NULL = use chapters
        "subs_on INTEGER",        # NULL = never chosen for this show
        "sub_lang TEXT",
        "audio_lang TEXT",
        "tvmaze_id INTEGER",      # keyless metadata source for shows
    ],
    "media": [
        # Per-episode values learned by audio fingerprinting; they win over the
        # show-level markers because cold opens shift the intro per episode.
        "intro_start REAL",
        "intro_end REAL",
        "credits_at REAL",
        "tv_state TEXT NOT NULL DEFAULT 'pending'",   # pending | done
    ],
    "tracks": [
        # EBU R128 loudness and true peak, measured once per file, so every
        # album can be played at the same level. See music/loudness.py.
        "loudness REAL",                             # LUFS
        "peak REAL",                                 # dBFS, true peak
        "loud_state TEXT NOT NULL DEFAULT 'pending'",  # pending | done | error
        # Liked songs. Only library.set_liked writes these; a scan updates the
        # columns it reads from the file and nothing else, so a rescan, a
        # finished download or a folder the scan recognises as moved keeps
        # every like, the same way it keeps play counts.
        "liked INTEGER NOT NULL DEFAULT 0",
        "liked_at REAL",                             # when; Liked songs lists newest first
    ],
    "albums": [
        "loudness REAL",                             # the album as one piece
        "peak REAL",
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            name = column.split()[0]
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")

    # Loudness needs a complete file: a track still downloading would measure
    # whatever has arrived so far, and an unreadable one cannot be measured at
    # all. Both are put back to 'pending' by the scan when they become ready.
    conn.execute("UPDATE tracks SET loud_state = 'skip' WHERE state != 'ready' "
                 "AND loud_state = 'pending'")

    # Intro detection only applies to episodes; movies would sit 'pending'
    # forever and make the queue look permanently unfinished. Only the rows not
    # already done, so an ordinary start does not rewrite every film.
    conn.execute("UPDATE media SET tv_state = 'done' WHERE kind != 'episode' "
                 "AND tv_state != 'done'")


def init() -> None:
    conn = connect()
    with _write_lock:
        conn.executescript(SCHEMA)
        _migrate(conn)


def close_thread_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def release_file() -> None:
    """Close this thread's connection and the write lock's watcher, so the file
    can be moved: Windows refuses to rename a file that is still open. The next
    query or write opens both again."""
    close_thread_connection()
    with _write_lock._lock:
        _write_lock.close()


def data_version() -> int:
    """Changes whenever another process writes to the database.

    Lets a running window notice edits made by a maintenance script or a second
    instance, instead of showing a stale library until it is restarted. Writes
    made by this process, on any thread, do not count: those announce
    themselves through the service's signals. See _WriteLock.
    """
    return _write_lock.poll()


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    with _write_lock:
        return connect().execute(sql, params)


# --- shows ------------------------------------------------------------------

def upsert_show(key: str, title: str, sort_title: str, year: int | None) -> int:
    with _write_lock:
        conn = connect()
        row = conn.execute("SELECT id FROM shows WHERE key = ?", (key,)).fetchone()
        if row:
            if year:
                conn.execute(
                    "UPDATE shows SET year = COALESCE(year, ?) WHERE id = ?", (year, row["id"])
                )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO shows (key, title, sort_title, year, added_at) VALUES (?,?,?,?,?)",
            (key, title, sort_title, year, time.time()),
        )
        return int(cur.lastrowid)


def all_shows() -> list[sqlite3.Row]:
    return query(
        """
        SELECT s.*,
               COUNT(m.id)                                   AS episode_count,
               COUNT(DISTINCT m.season)                      AS season_count,
               SUM(COALESCE(p.watched, 0))                   AS watched_count,
               -- a show is as new as its newest episode: that is what
               -- "Recently added" is for, finding what just finished downloading
               MAX(m.added_at)                               AS latest_added
        FROM shows s
        JOIN media m ON m.show_id = s.id AND m.missing = 0 AND m.probe_state != 'incomplete'
        LEFT JOIN progress p ON p.media_id = m.id
        GROUP BY s.id
        ORDER BY s.sort_title
        """
    )


def get_show(show_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM shows WHERE id = ?", (show_id,))


def episodes_for_show(show_id: int) -> list[sqlite3.Row]:
    return query(
        """
        SELECT m.*, p.position, p.watched
        FROM media m
        LEFT JOIN progress p ON p.media_id = m.id
        WHERE m.show_id = ? AND """ + READY + """
        ORDER BY COALESCE(m.season, 0), COALESCE(m.episode, 0), m.title
        """,
        (show_id,),
    )


def show_prefs(show_id: int) -> dict:
    """Per-show playback preferences (intro window, credits, subtitle memory)."""
    row = query_one(
        "SELECT intro_start, intro_end, credits_len, subs_on, sub_lang, audio_lang "
        "FROM shows WHERE id = ?", (show_id,)
    )
    return dict(row) if row else {}


def set_show_prefs(show_id: int, **fields) -> None:
    allowed = {"intro_start", "intro_end", "credits_len", "subs_on", "sub_lang", "audio_lang"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields:
        update_show(show_id, **fields)


def next_up(limit: int = 12) -> list[sqlite3.Row]:
    """For every show you've started: the first episode not yet watched.

    Excludes episodes that are mid-play (those live in Continue Watching), so
    the two home rows never show the same thing twice.
    """
    return query(
        """
        SELECT m.*, COALESCE(p.position, 0) AS position, COALESCE(p.watched, 0) AS watched
        FROM media m
        LEFT JOIN progress p ON p.media_id = m.id
        WHERE m.kind = 'episode' AND """ + READY + """
          AND COALESCE(p.watched, 0) = 0
          AND COALESCE(p.position, 0) < 30
          AND m.show_id IN (
              SELECT DISTINCT m2.show_id FROM media m2
              JOIN progress p2 ON p2.media_id = m2.id
              WHERE m2.show_id IS NOT NULL
                AND (p2.watched = 1 OR p2.position > 30)
          )
          AND NOT EXISTS (
              SELECT 1 FROM media m3
              LEFT JOIN progress p3 ON p3.media_id = m3.id
              WHERE m3.show_id = m.show_id AND m3.missing = 0
                AND m3.probe_state != 'incomplete'
                AND COALESCE(p3.watched, 0) = 0
                AND (COALESCE(m3.season, 0), COALESCE(m3.episode, 0)) <
                    (COALESCE(m.season, 0), COALESCE(m.episode, 0))
          )
        ORDER BY m.show_id
        LIMIT ?
        """,
        (limit,),
    )


def next_episode(media_id: int) -> sqlite3.Row | None:
    """The following episode in the same show, or None at the end of a run."""
    current = get_media(media_id)
    if current is None or current["show_id"] is None:
        return None
    return query_one(
        """
        SELECT * FROM media
        WHERE show_id = ? AND missing = 0 AND probe_state != 'incomplete'
          AND (COALESCE(season, 0), COALESCE(episode, 0)) > (?, ?)
        ORDER BY COALESCE(season, 0), COALESCE(episode, 0)
        LIMIT 1
        """,
        (current["show_id"], current["season"] or 0, current["episode"] or 0),
    )


def previous_episode(media_id: int) -> sqlite3.Row | None:
    """The episode before this one, or None at the start of a show."""
    current = get_media(media_id)
    if current is None or current["show_id"] is None:
        return None
    return query_one(
        """
        SELECT * FROM media
        WHERE show_id = ? AND missing = 0 AND probe_state != 'incomplete'
          AND (COALESCE(season, 0), COALESCE(episode, 0)) < (?, ?)
        ORDER BY COALESCE(season, 0) DESC, COALESCE(episode, 0) DESC
        LIMIT 1
        """,
        (current["show_id"], current["season"] or 0, current["episode"] or 0),
    )


# --- media ------------------------------------------------------------------

MEDIA_WITH_PROGRESS = """
    SELECT m.*, COALESCE(p.position, 0) AS position, COALESCE(p.watched, 0) AS watched
    FROM media m
    LEFT JOIN progress p ON p.media_id = m.id
"""

# Files still downloading are unplayable, so they stay out of every listing
# until ffprobe can read them.
READY = "m.missing = 0 AND m.probe_state != 'incomplete'"


def known_paths() -> dict[str, tuple[float, int]]:
    rows = query("SELECT path, mtime, size FROM media")
    return {r["path"]: (r["mtime"] or 0.0, r["size"] or 0) for r in rows}


def upsert_media(record: dict) -> int:
    """Insert a newly discovered file, or refresh an existing row in place."""
    with _write_lock:
        conn = connect()
        row = conn.execute("SELECT id FROM media WHERE path = ?", (record["path"],)).fetchone()
        if row:
            media_id = int(row["id"])
            fields = [k for k in record if k != "path"]
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE media SET {assignments}, missing = 0 WHERE id = ?",
                [record[k] for k in fields] + [media_id],
            )
            return media_id
        record = {**record, "added_at": record.get("added_at", time.time())}
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        cur = conn.execute(
            f"INSERT INTO media ({columns}) VALUES ({placeholders})", list(record.values())
        )
        return int(cur.lastrowid)


def update_media(media_id: int, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE media SET {assignments} WHERE id = ?", [*fields.values(), media_id])


def update_show(show_id: int, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE shows SET {assignments} WHERE id = ?", [*fields.values(), show_id])


def get_media(media_id: int) -> sqlite3.Row | None:
    return query_one(MEDIA_WITH_PROGRESS + " WHERE m.id = ?", (media_id,))


def movies() -> list[sqlite3.Row]:
    return query(
        MEDIA_WITH_PROGRESS
        + f" WHERE m.kind = 'movie' AND {READY} ORDER BY m.sort_title"
    )


def recently_added(limit: int = 20) -> list[sqlite3.Row]:
    return query(
        MEDIA_WITH_PROGRESS
        + f" WHERE {READY} AND m.kind = 'movie' ORDER BY m.added_at DESC LIMIT ?",
        (limit,),
    )


def continue_watching(limit: int = 20, min_seconds: float = 30.0) -> list[sqlite3.Row]:
    """Partly-watched items, most recently touched first."""
    return query(
        """
        SELECT m.*, p.position, p.watched, p.updated_at
        FROM media m
        JOIN progress p ON p.media_id = m.id
        WHERE """ + READY + """
          AND p.watched = 0
          AND p.position > ?
          AND (p.duration <= 0 OR p.position < p.duration * 0.97)
        ORDER BY p.updated_at DESC
        LIMIT ?
        """,
        (min_seconds, limit),
    )


def search(term: str, limit: int = 100) -> list[sqlite3.Row]:
    # Escape LIKE wildcards, otherwise searching for "100%" matches everything.
    cleaned = (
        term.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    like = f"%{cleaned}%"
    return query(
        MEDIA_WITH_PROGRESS
        + """
        WHERE """ + READY + r"""
          AND (m.title LIKE ? ESCAPE '\' OR m.overview LIKE ? ESCAPE '\'
               OR m.genres LIKE ? ESCAPE '\'
               OR m.show_id IN (SELECT id FROM shows WHERE title LIKE ? ESCAPE '\'))
        ORDER BY m.sort_title LIMIT ?
        """,
        (like, like, like, like, limit),
    )


def retry_failed() -> int:
    """Queue everything that previously errored for another attempt."""
    cursor = execute(
        "UPDATE media SET "
        "  probe_state = CASE WHEN probe_state = 'error' THEN 'pending' ELSE probe_state END,"
        "  meta_state  = CASE WHEN meta_state  = 'error' THEN 'pending' ELSE meta_state  END,"
        "  thumbs_state= CASE WHEN thumbs_state= 'error' THEN 'pending' ELSE thumbs_state END "
        "WHERE 'error' IN (probe_state, meta_state, thumbs_state)"
    )
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def failed_count() -> int:
    row = query_one(
        "SELECT COUNT(*) AS n FROM media "
        "WHERE 'error' IN (probe_state, meta_state, thumbs_state)"
    )
    return int(row["n"]) if row else 0


def hero_candidate() -> sqlite3.Row | None:
    """Prefer something in progress, else the newest title that has a backdrop."""
    started = query_one(
        """
        SELECT m.*, p.position, p.watched FROM media m
        JOIN progress p ON p.media_id = m.id
        WHERE """ + READY + """ AND p.watched = 0 AND p.position > 30
        ORDER BY p.updated_at DESC LIMIT 1
        """
    )
    if started:
        return started
    with_art = query_one(
        MEDIA_WITH_PROGRESS
        + f" WHERE {READY} AND m.backdrop IS NOT NULL ORDER BY m.added_at DESC LIMIT 1"
    )
    return with_art or query_one(
        MEDIA_WITH_PROGRESS + f" WHERE {READY} ORDER BY m.added_at DESC LIMIT 1"
    )


def pending(state_column: str, limit: int = 50) -> list[sqlite3.Row]:
    if state_column not in {"probe_state", "meta_state", "thumbs_state"}:
        raise ValueError(f"unknown state column: {state_column}")
    if state_column == "probe_state":
        # 'incomplete' means a download still in flight — keep retrying it.
        where = "probe_state IN ('pending', 'incomplete')"
    elif state_column == "meta_state":
        # 'fallback' = art cut from the file because an online source was
        # unreachable. Retried so a transient outage isn't permanent.
        where = "meta_state IN ('pending', 'fallback') AND probe_state = 'done'"
    else:
        # Artwork and thumbnails need a readable file, so don't waste ffmpeg
        # runs on anything we could not probe.
        where = f"{state_column} = 'pending' AND probe_state = 'done'"
    return query(
        f"SELECT * FROM media WHERE {where} AND missing = 0 LIMIT ?", (limit,)
    )


def downloading_count() -> int:
    row = query_one("SELECT COUNT(*) AS n FROM media WHERE probe_state = 'incomplete'")
    return int(row["n"]) if row else 0


def pending_show_metadata(limit: int = 50) -> list[sqlite3.Row]:
    return query("SELECT * FROM shows WHERE meta_state = 'pending' LIMIT ?", (limit,))


def seasons_needing_tv_analysis() -> list[tuple[int, int]]:
    """(show_id, season) pairs with ≥2 ready episodes, any of them unanalyzed."""
    rows = query(
        """
        SELECT show_id, season FROM media
        WHERE kind = 'episode' AND missing = 0 AND probe_state = 'done'
          AND show_id IS NOT NULL AND season IS NOT NULL
        GROUP BY show_id, season
        HAVING COUNT(*) >= 2 AND SUM(tv_state = 'pending') > 0
        """
    )
    return [(int(r["show_id"]), int(r["season"])) for r in rows]


def season_episodes(show_id: int, season: int) -> list[sqlite3.Row]:
    return query(
        "SELECT * FROM media WHERE show_id = ? AND season = ? AND missing = 0 "
        "AND probe_state = 'done' ORDER BY episode",
        (show_id, season),
    )


def mark_missing(existing_paths: Iterable[str]) -> int:
    """Flag rows whose files have disappeared; unflag ones that came back."""
    present = set(existing_paths)
    changed = 0
    with _write_lock:
        conn = connect()
        for row in conn.execute("SELECT id, path, missing FROM media").fetchall():
            gone = 1 if row["path"] not in present else 0
            if gone != (row["missing"] or 0):
                conn.execute("UPDATE media SET missing = ? WHERE id = ?", (gone, row["id"]))
                changed += 1
        conn.execute(
            "DELETE FROM shows WHERE id NOT IN (SELECT DISTINCT show_id FROM media WHERE show_id IS NOT NULL)"
        )
    return changed


def prune_path(path: str) -> None:
    execute("DELETE FROM media WHERE path = ?", (path,))


# --- playback progress ------------------------------------------------------

def set_progress(
    media_id: int,
    position: float,
    duration: float,
    watched: bool | None = None,
    audio_id: int | None = None,
    sub_id: int | None = None,
) -> None:
    with _write_lock:
        conn = connect()
        existing = conn.execute(
            "SELECT watched, play_count, audio_id, sub_id FROM progress WHERE media_id = ?",
            (media_id,),
        ).fetchone()
        resolved = int(watched) if watched is not None else (existing["watched"] if existing else 0)
        conn.execute(
            """
            INSERT INTO progress (media_id, position, duration, watched, play_count,
                                  audio_id, sub_id, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(media_id) DO UPDATE SET
                position = excluded.position,
                duration = excluded.duration,
                watched = excluded.watched,
                audio_id = COALESCE(excluded.audio_id, progress.audio_id),
                sub_id = COALESCE(excluded.sub_id, progress.sub_id),
                updated_at = excluded.updated_at
            """,
            (
                media_id,
                max(0.0, position),
                max(0.0, duration),
                resolved,
                (existing["play_count"] if existing else 0),
                audio_id,
                sub_id,
                time.time(),
            ),
        )


def get_progress(media_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM progress WHERE media_id = ?", (media_id,))


def bump_play_count(media_id: int) -> None:
    with _write_lock:
        connect().execute(
            """
            INSERT INTO progress (media_id, play_count, updated_at) VALUES (?, 1, ?)
            ON CONFLICT(media_id) DO UPDATE SET
                play_count = progress.play_count + 1, updated_at = excluded.updated_at
            """,
            (media_id, time.time()),
        )


def set_watched(media_id: int, watched: bool) -> None:
    row = get_media(media_id)
    duration = (row["duration"] if row else 0) or 0
    with _write_lock:
        connect().execute(
            """
            INSERT INTO progress (media_id, position, duration, watched, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(media_id) DO UPDATE SET
                watched = excluded.watched,
                position = CASE WHEN excluded.watched = 1 THEN 0 ELSE progress.position END,
                updated_at = excluded.updated_at
            """,
            (media_id, 0.0, duration, int(watched), time.time()),
        )


# --- http cache -------------------------------------------------------------

def cache_get(key: str, max_age: float = 60 * 60 * 24 * 30) -> str | None:
    row = query_one("SELECT body, fetched_at FROM http_cache WHERE key = ?", (key,))
    if row and (time.time() - (row["fetched_at"] or 0)) < max_age:
        return row["body"]
    return None


def cache_put(key: str, body: str) -> None:
    execute(
        "INSERT OR REPLACE INTO http_cache (key, body, fetched_at) VALUES (?,?,?)",
        (key, body, time.time()),
    )


def library_stats() -> dict:
    row = query_one(
        """
        SELECT COUNT(*) AS files,
               COALESCE(SUM(size), 0) AS bytes,
               COALESCE(SUM(duration), 0) AS seconds
        FROM media WHERE missing = 0 AND probe_state != 'incomplete'
        """
    )
    shows_row = query_one("SELECT COUNT(*) AS n FROM shows")
    movies_row = query_one(
        "SELECT COUNT(*) AS n FROM media "
        "WHERE kind='movie' AND missing=0 AND probe_state != 'incomplete'"
    )
    return {
        "files": row["files"] if row else 0,
        "bytes": row["bytes"] if row else 0,
        "seconds": row["seconds"] if row else 0,
        "shows": shows_row["n"] if shows_row else 0,
        "movies": movies_row["n"] if movies_row else 0,
        "downloading": downloading_count(),
    }
