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

-- Playlists, for songs and for films alike. Making, renaming, reordering and
-- emptying one is the same work either way; only the query that turns an entry
-- back into a row differs, so `kind` says which table `item_id` points at and
-- the column carries no foreign key of its own. An id that no longer resolves
-- is skipped when the list is read, the way a restored queue drops songs that
-- have gone (music/library.tracks_by_id).
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,                          -- music | video
    name       TEXT NOT NULL,
    note       TEXT,
    created_at REAL,
    updated_at REAL                                    -- the page lists newest change first
);

-- One row per entry rather than per item: the same song or film may appear
-- twice (the music queue already allows it), and an entry id is what makes
-- "remove this one" unambiguous when it does.
CREATE TABLE IF NOT EXISTS playlist_items (
    id          INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    item_id     INTEGER NOT NULL,                      -- tracks.id or media.id, per kind
    position    INTEGER NOT NULL,                      -- 0-based, rewritten by a reorder
    added_at    REAL
);

CREATE INDEX IF NOT EXISTS idx_playlist_items ON playlist_items(playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_playlists_kind ON playlists(kind, name);

-- Where a movie night got to, kept apart from `progress` on purpose: watching
-- episode 3 with friends must not move your own place in the series, which may
-- be episode 7. One row per party and thing watched, on every side of it — the
-- host keeps one and so does each guest, who has no media row of their own.
-- party_id is random per movie night, and reused when the host continues one.
CREATE TABLE IF NOT EXISTS party_progress (
    id          INTEGER PRIMARY KEY,
    party_id    TEXT NOT NULL,
    media_key   TEXT NOT NULL,                     -- "<host person id>:<host media id>"
    media_id    INTEGER,                           -- the host's own media row; NULL for a guest
    title       TEXT,                              -- as the host described it
    members     TEXT,                              -- JSON [{"id", "name"}] of everyone who came
    role        TEXT NOT NULL,                     -- host | guest
    position    REAL NOT NULL DEFAULT 0,
    duration    REAL,
    updated_at  REAL,
    UNIQUE(party_id, media_key)
);
CREATE INDEX IF NOT EXISTS idx_party_recent ON party_progress(updated_at);

-- Library sharing. A friend is another Mistery this one has been paired with;
-- the row is written on both sides, and either can browse and play what the
-- other has. `pin` is the fingerprint of their certificate, and a connection
-- claiming to be them is refused unless the certificate on it matches (see
-- app/share/identity.py). Removing a friend deletes the row, and with it the
-- copy of their library and your place in it.
CREATE TABLE IF NOT EXISTS friends (
    id           INTEGER PRIMARY KEY,
    person_id    TEXT NOT NULL UNIQUE,         -- their install's id, the one movie night uses
    name         TEXT NOT NULL,                -- what they call themselves
    pin          TEXT NOT NULL,                -- hex, their certificate's first 16 SHA-256 bytes
    certificate  TEXT NOT NULL,                -- their certificate, PEM: the listener's trust store
    lan_ip       TEXT,                         -- where they answered on a home network
    wan_ip       TEXT,                         -- where they answered from outside
    port         INTEGER,
    added_at     REAL,
    last_seen    REAL,                         -- the last time a connection with them worked
    sharing      INTEGER NOT NULL DEFAULT 1,   -- 0: still a friend, served nothing for now
    catalog_at   REAL,                         -- when their library was last copied
    catalog_mark TEXT                          -- their mark for it, so only changes come next time
);

-- What a friend has, as they last said. Kept so their library can be browsed
-- while their PC is off, and only ever written from what they sent: nothing
-- here is the truth about anyone's disk, it is a copy of their catalogue.
CREATE TABLE IF NOT EXISTS friend_media (
    id         INTEGER PRIMARY KEY,
    friend_id  INTEGER NOT NULL,
    kind       TEXT NOT NULL,                  -- movie | episode | album | track
    remote_id  INTEGER NOT NULL,               -- the id it has on their PC
    parent_id  INTEGER,                        -- their show or album, for an episode or a track
    title      TEXT,
    sort_title TEXT,
    year       INTEGER,
    season     INTEGER,
    episode    INTEGER,
    duration   REAL,
    artist     TEXT,
    genres     TEXT,
    overview   TEXT,
    art        TEXT,                           -- our copy of their artwork, once fetched
    art_mark   TEXT,                           -- their mark for that artwork
    backdrop_mark TEXT,                        -- their mark for its wide picture (a film's backdrop)
    updated_at REAL,
    UNIQUE(friend_id, kind, remote_id)
);
CREATE INDEX IF NOT EXISTS idx_friend_media ON friend_media(friend_id, kind, sort_title);

-- Where you got to in something of a friend's, kept on your PC. Their own
-- progress is theirs and never moves because you watched it, which is the rule
-- party_progress follows for a movie night.
CREATE TABLE IF NOT EXISTS friend_progress (
    id         INTEGER PRIMARY KEY,
    friend_id  INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    remote_id  INTEGER NOT NULL,
    position   REAL NOT NULL DEFAULT 0,
    duration   REAL,
    watched    INTEGER NOT NULL DEFAULT 0,
    updated_at REAL,
    UNIQUE(friend_id, kind, remote_id)
);
CREATE INDEX IF NOT EXISTS idx_friend_progress ON friend_progress(updated_at);

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
        # Categories chosen by a person, comma-joined like `genres`. Kept apart
        # from it because the metadata pass rewrites `genres` whenever better
        # data arrives, and a hand-made choice must survive that.
        "user_genres TEXT",
        # Where `poster` and `backdrop` were downloaded from. See the artwork
        # addresses section below for why we keep them.
        "poster_url TEXT",
        "backdrop_url TEXT",
    ],
    "media": [
        # Per-episode values learned by audio fingerprinting; they win over the
        # show-level markers because cold opens shift the intro per episode.
        "intro_start REAL",
        "intro_end REAL",
        "credits_at REAL",
        "tv_state TEXT NOT NULL DEFAULT 'pending'",   # pending | done
        "user_genres TEXT",       # see shows.user_genres
        "poster_url TEXT",        # see shows.poster_url
        "backdrop_url TEXT",
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
    "friend_media": [
        "backdrop_mark TEXT",                        # a friend's film's wide picture
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    added: set[str] = set()
    for table, columns in _MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            name = column.split()[0]
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
                added.add(f"{table}.{name}")

    # Only on the launch that adds the columns. The statements in it rewrite
    # rows unconditionally, so run every time they would re-queue a library
    # that the metadata pass had since settled.
    if "media.poster_url" in added:
        _recover_art_urls(conn)
    # A catalogue copied before the column was there has no backdrop marks, and
    # its friends' PCs would answer "unchanged" to the marks already held: so
    # none are held, and the next look at each friend's library brings it all.
    if "friend_media.backdrop_mark" in added:
        conn.execute("UPDATE friends SET catalog_mark = NULL")

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


def execute_many(sql: str, rows: Sequence[Sequence[Any]]) -> None:
    """One statement over many rows, under one lock. Nothing at all for none.

    A friend's catalogue arrives a few hundred rows at a time; one executemany
    is one write where a loop over execute() would take and release the lock
    for each row.
    """
    if not rows:
        return
    with _write_lock:
        connect().executemany(sql, rows)


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
    record = _with_art_urls(record)
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
    fields = _with_art_urls(fields)
    assignments = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE media SET {assignments} WHERE id = ?", [*fields.values(), media_id])


def update_show(show_id: int, **fields) -> None:
    if not fields:
        return
    fields = _with_art_urls(fields)
    assignments = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE shows SET {assignments} WHERE id = ?", [*fields.values(), show_id])


# --- artwork addresses ------------------------------------------------------
#
# `poster` and `backdrop` are files on this PC. `poster_url` and `backdrop_url`
# are the public web addresses those files were downloaded from, where there
# was one: TMDB, TVmaze and Wikipedia all serve their pictures openly, and the
# app used to keep the picture and throw the address away.
#
# The address is worth keeping because Discord's rich presence accepts a plain
# image URL and fetches the picture itself, so a new film can show its poster
# to friends the day it is added, instead of waiting for someone to export PNGs
# and upload them to the developer portal by hand.
#
# NULL is an ordinary answer, not a failure: artwork composed here out of the
# film's own frames (meta_source 'ffmpeg') has no address and never will.

ART_URL_COLUMNS = {"poster": "poster_url", "backdrop": "backdrop_url"}

# What `kind` means to art_url. Films and episodes are both rows in media; a
# show is the series, which is where an episode's poster actually lives.
_ART_TABLES = {"movie": "media", "episode": "media", "media": "media", "show": "shows"}


def _with_art_urls(fields: dict) -> dict:
    """The same fields, with an address cleared wherever its picture is replaced.

    Artwork changes source in both directions: a film matched on TMDB today can
    be re-cut from its own frames tomorrow, when a refetch finds nothing and the
    metadata pass falls back. Whoever writes the new file knows its address, or
    knows there is none, and writes both columns together — so a write that sets
    `poster` and says nothing about `poster_url` is a picture that came from
    somewhere else, and the old address no longer describes it.

    Doing it here rather than at each of the writers is what stops a stale
    https:// address reaching Discord, where it would show friends the wrong
    poster with no sign on this PC that anything was wrong.
    """
    stale = {url_column: None for column, url_column in ART_URL_COLUMNS.items()
             if column in fields and url_column not in fields}
    return {**fields, **stale} if stale else fields


def art_url(kind: str, item_id: int | None, which: str = "poster") -> str | None:
    """The public web address of this title's artwork, or None if it has none.

    `kind` is "movie" or "episode" (or plain "media") for a row in the library,
    and "show" for a series. This is the whole of what the Discord side needs to
    know: it asks whether there is a public picture, and never has to learn what
    TMDB, TVmaze, Wikipedia or ffmpeg are.
    """
    table = _ART_TABLES.get((kind or "").strip().lower())
    column = ART_URL_COLUMNS.get((which or "").strip().lower())
    if table is None or column is None or not item_id:
        return None
    row = query_one(f"SELECT {column} AS url FROM {table} WHERE id = ?", (int(item_id),))
    address = ((row["url"] if row else None) or "").strip()
    # A web address or nothing: a local path in this column is a bug somewhere,
    # and answering with it would send a path off this PC. This is the coarse
    # check — anything actually bound for Discord goes through
    # discord_presence.public_image_url, which is stricter on purpose.
    return address if address.lower().startswith(("http://", "https://")) else None


def _recover_art_urls(conn: sqlite3.Connection) -> None:
    """Fill the new columns in for art already on disk, without asking anyone.

    Every downloader here names the file after the reply that described it —
    `tvmaze-4242-s01e04.jpg`, `tmdb-movie-424242-p.jpg` — and those replies are
    still in http_cache, which nothing prunes: the month-long age limit only
    stops them being read as fresh. So the address can be matched back to the
    file with certainty and no network at all. Measured on a copy of this
    library (254 rows, 75 cached replies, no TMDB key): 228 of the 229 episode
    stills and all 4 show posters came back, with db.init() taking 0.19 s in
    total. The one still that did not was cut from the episode itself, because
    TVmaze had none for it — so there is no address to find.

    A Wikipedia poster cannot be matched: `wiki-low-tide-2019.jpg` is named
    after the film, not the article it came from, and nothing in a cached
    article says which film settled on it. Those rows (11 films here) go back
    to 'pending' instead, and the next metadata pass writes the address without
    fetching the picture again — every downloader here hands back the file
    already on disk.
    """
    from .metadata import art_urls        # deferred: app.metadata imports db

    # Only the replies that can name a picture. The rest of http_cache is
    # search results, Wikipedia articles, Wikidata and iTunes — and an iTunes
    # reply alone ran to 100 KB before it was trimmed, so a library with a few
    # thousand of those would be parsed for nothing.
    cached = conn.execute(
        "SELECT key, body FROM http_cache WHERE key LIKE 'tmdb:/movie/%' "
        "OR key LIKE 'tmdb:/tv/%' OR key LIKE 'online:https://api.tvmaze.com/%'"
    ).fetchall()
    known = art_urls.addresses_by_name((row["key"], row["body"]) for row in cached)
    for table in ("media", "shows"):
        for column, url_column in ART_URL_COLUMNS.items():
            rows = conn.execute(
                f"SELECT id, {column} AS art FROM {table} "
                f"WHERE {column} IS NOT NULL AND {url_column} IS NULL"
            ).fetchall()
            for row in rows:
                address = known.get(Path(row["art"]).stem)
                if address:
                    conn.execute(f"UPDATE {table} SET {url_column} = ? WHERE id = ?",
                                 (address, row["id"]))

    # Whatever is left had its art from a source that publishes addresses, but
    # none we could match. Back to 'pending' so the next metadata pass fills it
    # in; rows already 'pending' or 'fallback' are queued anyway, and rows whose
    # art this app composed itself are left alone — they have nothing to fetch.
    conn.execute(
        "UPDATE media SET meta_state = 'pending' WHERE meta_state = 'done' "
        "AND meta_source IN ('tmdb', 'tvmaze', 'wikipedia') "
        "AND poster_url IS NULL AND backdrop_url IS NULL "
        "AND (poster IS NOT NULL OR backdrop IS NOT NULL)"
    )
    conn.execute(
        "UPDATE shows SET meta_state = 'pending' WHERE meta_state = 'done' "
        "AND poster IS NOT NULL AND poster_url IS NULL"
    )


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


# --- playlists --------------------------------------------------------------
#
# The same two tables hold music and video playlists; `kind` decides whether an
# entry's item_id means tracks.id or media.id. Reading a playlist skips ids that
# no longer resolve rather than deleting them: a song whose file is missing
# today may be back tomorrow, exactly as with a restored queue.

def playlists(kind: str) -> list[sqlite3.Row]:
    """Playlists of one kind, most recently changed first, with their sizes."""
    return query(
        "SELECT p.*, COUNT(i.id) AS item_count FROM playlists p "
        "LEFT JOIN playlist_items i ON i.playlist_id = p.id "
        "WHERE p.kind = ? GROUP BY p.id ORDER BY p.updated_at DESC, p.name COLLATE NOCASE",
        (kind,),
    )


def playlist(playlist_id: int) -> sqlite3.Row | None:
    return query_one(
        "SELECT p.*, COUNT(i.id) AS item_count FROM playlists p "
        "LEFT JOIN playlist_items i ON i.playlist_id = p.id WHERE p.id = ? GROUP BY p.id",
        (playlist_id,),
    )


def create_playlist(kind: str, name: str, note: str | None = None) -> int:
    now = time.time()
    with _write_lock:
        cursor = connect().execute(
            "INSERT INTO playlists (kind, name, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (kind, name.strip() or "Untitled", note, now, now),
        )
        return int(cursor.lastrowid)


def rename_playlist(playlist_id: int, name: str, note: str | None = None) -> None:
    execute("UPDATE playlists SET name = ?, note = ?, updated_at = ? WHERE id = ?",
            (name.strip() or "Untitled", note, time.time(), playlist_id))


def delete_playlist(playlist_id: int) -> None:
    # playlist_items goes with it: the foreign key cascades, and every
    # connection turns foreign keys on (see connect).
    execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))


def playlist_entries(playlist_id: int) -> list[sqlite3.Row]:
    """(id, item_id, position) in playing order."""
    return query(
        "SELECT id, item_id, position FROM playlist_items WHERE playlist_id = ? ORDER BY position",
        (playlist_id,),
    )


def add_to_playlist(playlist_id: int, item_ids: Sequence[int]) -> int:
    """Append items; returns how many were added, 0 if that playlist has gone.

    Every connection has foreign keys on, so inserting into a playlist deleted
    since the menu was built (a second instance, a maintenance script) raised
    IntegrityError from inside a slot: PySide6 prints the traceback and carries
    on, so the click did nothing and said nothing. The other helpers are UPDATE
    and DELETE and already no-op on a missing row; this one now answers the
    same way, and the caller says so.
    """
    ids = [int(i) for i in item_ids]
    if not ids:
        return 0
    now = time.time()
    with _write_lock:
        conn = connect()
        if conn.execute("SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)).fetchone() is None:
            return 0
        row = conn.execute("SELECT MAX(position) AS last FROM playlist_items WHERE playlist_id = ?",
                           (playlist_id,)).fetchone()
        start = (row["last"] + 1) if row and row["last"] is not None else 0
        conn.executemany(
            "INSERT INTO playlist_items (playlist_id, item_id, position, added_at) VALUES (?, ?, ?, ?)",
            [(playlist_id, item_id, start + offset, now) for offset, item_id in enumerate(ids)],
        )
        conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now, playlist_id))
    return len(ids)


def remove_playlist_entries(playlist_id: int, entry_ids: Sequence[int]) -> None:
    ids = [int(i) for i in entry_ids]
    if not ids:
        return
    now = time.time()
    with _write_lock:
        conn = connect()
        conn.executemany("DELETE FROM playlist_items WHERE id = ? AND playlist_id = ?",
                         [(entry_id, playlist_id) for entry_id in ids])
        # Close the gaps, so positions stay 0..n-1 and a later reorder has
        # nothing odd to work around.
        rows = conn.execute("SELECT id FROM playlist_items WHERE playlist_id = ? ORDER BY position",
                            (playlist_id,)).fetchall()
        conn.executemany("UPDATE playlist_items SET position = ? WHERE id = ?",
                         [(index, row["id"]) for index, row in enumerate(rows)])
        conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now, playlist_id))


def set_playlist_order(playlist_id: int, entry_ids: Sequence[int]) -> None:
    """The whole run of entry ids, in the order they should play."""
    now = time.time()
    with _write_lock:
        conn = connect()
        conn.executemany("UPDATE playlist_items SET position = ? WHERE id = ? AND playlist_id = ?",
                         [(index, int(entry_id), playlist_id) for index, entry_id in enumerate(entry_ids)])
        conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now, playlist_id))


def playlists_holding(kind: str, item_id: int) -> set[int]:
    """Which playlists of this kind already hold the item — for the ticks in
    the "Add to playlist" menu."""
    rows = query(
        "SELECT DISTINCT p.id FROM playlists p JOIN playlist_items i ON i.playlist_id = p.id "
        "WHERE p.kind = ? AND i.item_id = ?", (kind, item_id))
    return {int(row["id"]) for row in rows}


def playlist_media(playlist_id: int) -> list[sqlite3.Row]:
    """The films and episodes of a video playlist, in playing order.

    Rows come back exactly as the library pages expect them (position and
    watched joined in), so a playlist page can use the same cards. Entries
    whose media row has gone, or which cannot be played yet, are left out.
    """
    entries = playlist_entries(playlist_id)
    if not entries:
        return []
    ids = [int(row["item_id"]) for row in entries]
    found = {}
    for start in range(0, len(ids), 400):        # SQLite's variable limit is 999
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for row in query(f"{MEDIA_WITH_PROGRESS} WHERE m.id IN ({marks}) AND {READY}", chunk):
            found[int(row["id"])] = row
    return [found[item_id] for item_id in ids if item_id in found]


# --- movie night --------------------------------------------------------------
#
# A movie night's place in what it is watching. Never read or written by the
# ordinary progress code above, and never the other way round: that separation
# is the whole point of the table.

def save_party_progress(party_id: str, media_key: str, *, role: str, position: float,
                        duration: float | None = None, media_id: int | None = None,
                        title: str | None = None, members: str | None = None) -> None:
    """Record where a party got to. One row per (party, thing watched)."""
    execute(
        "INSERT INTO party_progress (party_id, media_key, media_id, title, members, role, "
        "position, duration, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(party_id, media_key) DO UPDATE SET "
        "position = excluded.position, duration = COALESCE(excluded.duration, duration), "
        "title = COALESCE(excluded.title, title), members = COALESCE(excluded.members, members), "
        "media_id = COALESCE(excluded.media_id, media_id), updated_at = excluded.updated_at",
        (party_id, media_key, media_id, title, members, role, float(position),
         duration, time.time()),
    )


def party_progress(party_id: str, media_key: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM party_progress WHERE party_id = ? AND media_key = ?",
                     (party_id, media_key))


def recent_parties(limit: int = 20) -> list[sqlite3.Row]:
    """Movie nights, most recent first: one row per party and thing watched."""
    return query("SELECT * FROM party_progress ORDER BY updated_at DESC LIMIT ?", (limit,))


def forget_party(party_id: str) -> None:
    execute("DELETE FROM party_progress WHERE party_id = ?", (party_id,))


# --- library sharing ----------------------------------------------------------
#
# Friends, the copy of what each of them has, and where you got to in it. Your
# own progress table is never touched by any of this, and theirs is never
# touched by you: a friend's row moves only on the PC it belongs to.

def friends(include_paused: bool = True) -> list[sqlite3.Row]:
    """Everyone this Mistery is paired with, by name."""
    where = "" if include_paused else "WHERE sharing = 1 "
    return query(f"SELECT * FROM friends {where}ORDER BY name COLLATE NOCASE, id")


def friend(friend_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM friends WHERE id = ?", (friend_id,))


def friend_by_person(person_id: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM friends WHERE person_id = ?", (person_id,))


def friend_by_pin(pin: str) -> sqlite3.Row | None:
    """Which friend a connection belongs to, from the certificate it showed."""
    return query_one("SELECT * FROM friends WHERE pin = ?", (pin.lower(),))


def friend_certificates(include_paused: bool = True) -> list[str]:
    """Every friend's certificate, for the listener's trust store.

    A paused friend is included on purpose: pausing stops them being served,
    and that refusal is a sentence they can read, not a handshake that fails
    with nothing to say.
    """
    return [row["certificate"] for row in friends(include_paused) if row["certificate"]]


def add_friend(person_id: str, name: str, pin: str, certificate: str, *,
               lan_ip: str | None = None, wan_ip: str | None = None,
               port: int | None = None) -> int:
    """Write down a friend, or bring an existing one up to date. Returns their id.

    Pairing with somebody already paired replaces their certificate and name:
    that is what a friend who reinstalled Mistery looks like, and they had to
    show a fresh code to get here.
    """
    now = time.time()
    execute(
        "INSERT INTO friends (person_id, name, pin, certificate, lan_ip, wan_ip, port, "
        "added_at, last_seen, sharing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(person_id) DO UPDATE SET name = excluded.name, pin = excluded.pin, "
        "certificate = excluded.certificate, lan_ip = COALESCE(excluded.lan_ip, lan_ip), "
        "wan_ip = COALESCE(excluded.wan_ip, wan_ip), port = COALESCE(excluded.port, port), "
        "last_seen = excluded.last_seen",
        (person_id, name, pin.lower(), certificate, lan_ip, wan_ip, port, now, now),
    )
    row = friend_by_person(person_id)
    return int(row["id"]) if row else 0


_FRIEND_FIELDS = {"name", "lan_ip", "wan_ip", "port", "last_seen", "sharing",
                  "catalog_at", "catalog_mark", "pin", "certificate"}


def update_friend(friend_id: int, **fields) -> None:
    """Change what a friend's row says. Only the columns above can be set."""
    unknown = set(fields) - _FRIEND_FIELDS
    if unknown:
        raise ValueError(f"not a friend column: {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    execute(f"UPDATE friends SET {assignments} WHERE id = ?",
            (*fields.values(), friend_id))


def remove_friend(friend_id: int) -> None:
    """Forget a friend, the copy of their library and your place in it."""
    execute("DELETE FROM friend_media WHERE friend_id = ?", (friend_id,))
    execute("DELETE FROM friend_progress WHERE friend_id = ?", (friend_id,))
    execute("DELETE FROM friends WHERE id = ?", (friend_id,))


def save_friend_media(friend_id: int, items: list[dict]) -> int:
    """Store part of a friend's catalogue. Returns how many rows were written.

    Each item is what their catalogue sent: kind and remote_id identify it, and
    whatever else is there is kept. A second call with the same ids updates them
    rather than making a second copy, so an update that only carries what
    changed can be applied on its own.
    """
    columns = ("kind", "remote_id", "parent_id", "title", "sort_title", "year", "season",
               "episode", "duration", "artist", "genres", "overview", "art", "art_mark",
               "backdrop_mark")
    now = time.time()
    rows = []
    for item in items:
        if not item.get("kind") or item.get("remote_id") is None:
            raise ValueError("every catalogue item needs a kind and a remote_id")
        rows.append((friend_id, *(item.get(name) for name in columns), now))
    if not rows:
        return 0
    names = ", ".join(("friend_id", *columns, "updated_at"))
    marks = ", ".join("?" * (len(columns) + 2))
    updates = ", ".join(f"{name} = excluded.{name}" for name in columns[2:])
    execute_many(
        f"INSERT INTO friend_media ({names}) VALUES ({marks}) "
        f"ON CONFLICT(friend_id, kind, remote_id) DO UPDATE SET {updates}, "
        "parent_id = COALESCE(excluded.parent_id, parent_id), updated_at = excluded.updated_at",
        rows)
    return len(rows)


def friend_media(friend_id: int, kind: str | None = None, *, parent_id: int | None = None,
                 limit: int | None = None) -> list[sqlite3.Row]:
    """A friend's things, as they last described them."""
    where = ["friend_id = ?"]
    values: list = [friend_id]
    if kind:
        where.append("kind = ?")
        values.append(kind)
    if parent_id is not None:
        where.append("parent_id = ?")
        values.append(parent_id)
    tail = f" LIMIT {int(limit)}" if limit else ""
    return query(f"SELECT * FROM friend_media WHERE {' AND '.join(where)} "
                 f"ORDER BY sort_title COLLATE NOCASE, season, episode{tail}", tuple(values))


def friend_media_one(friend_id: int, kind: str, remote_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM friend_media WHERE friend_id = ? AND kind = ? "
                     "AND remote_id = ?", (friend_id, kind, remote_id))


def forget_friend_media(friend_id: int, kind: str, remote_ids: list[int]) -> None:
    """Drop things a friend no longer has, named in their catalogue update."""
    execute_many("DELETE FROM friend_media WHERE friend_id = ? AND kind = ? AND remote_id = ?",
                 [(friend_id, kind, int(remote_id)) for remote_id in remote_ids])


def save_friend_progress(friend_id: int, kind: str, remote_id: int, position: float, *,
                         duration: float | None = None, watched: bool | None = None) -> None:
    """Where you got to in something of theirs. Never sent to them."""
    # `watched` left out means "leave it as it was", and a new row starts at 0:
    # a position saved every ten seconds must not keep unmarking something the
    # viewer has already finished, and the column cannot hold NULL.
    flag = None if watched is None else int(watched)
    execute(
        "INSERT INTO friend_progress (friend_id, kind, remote_id, position, duration, "
        "watched, updated_at) VALUES (?, ?, ?, ?, ?, COALESCE(?, 0), ?) "
        "ON CONFLICT(friend_id, kind, remote_id) DO UPDATE SET position = excluded.position, "
        "duration = COALESCE(excluded.duration, duration), "
        "watched = COALESCE(?, watched), updated_at = excluded.updated_at",
        (friend_id, kind, remote_id, float(position), duration, flag, time.time(), flag),
    )


def friend_progress(friend_id: int, kind: str, remote_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM friend_progress WHERE friend_id = ? AND kind = ? "
                     "AND remote_id = ?", (friend_id, kind, remote_id))


def friend_continue_watching(limit: int = 20, min_seconds: float = 30.0) -> list[sqlite3.Row]:
    """Friends' films and episodes you are part way through, most recent first,
    by continue_watching's rules for your own. Only what they still have: a
    title gone from their catalogue cannot be resumed, so it is not offered."""
    return query(
        "SELECT p.friend_id, p.kind, p.remote_id, p.position, p.updated_at, m.title, "
        "f.name AS friend_name "
        "FROM friend_progress p JOIN friends f ON f.id = p.friend_id "
        "JOIN friend_media m ON m.friend_id = p.friend_id AND m.kind = p.kind "
        "AND m.remote_id = p.remote_id "
        "WHERE p.kind IN ('movie', 'episode') AND p.watched = 0 AND p.position > ? "
        "AND (COALESCE(p.duration, m.duration, 0) <= 0 "
        "OR p.position < COALESCE(p.duration, m.duration) * 0.97) "
        "ORDER BY p.updated_at DESC LIMIT ?", (min_seconds, limit))


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
