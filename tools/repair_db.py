"""Rebuild the library database, salvaging everything still readable.

    python tools/repair_db.py

SQLite corruption is usually confined to a few pages. This copies every row it
can still read into a fresh database with the current schema, leaves the damaged
original (with its -wal and -shm files) beside it as a .corrupt backup, and
reports exactly what was saved and what was lost. Only the HTTP cache, which
refills on its own, is left behind on purpose. A database with no damage is
left alone.

Mistery offers the same repair itself when it cannot open the library.
Your video and music files are never touched.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import app_is_running, db_path

# Refills by itself on the next lookup, so it is not worth carrying over.
DISPOSABLE = ("http_cache",)

# Stepping past bad pages costs one query per range tried. A damaged root page
# makes every range fail, down to single rows; this caps that at about 20 s a
# table (a query that fails costs about 90 us here), which only a table with
# around 100,000 rows can reach.
_PROBE_BUDGET = 200_000


class RepairError(Exception):
    pass


@dataclass
class Salvage:
    table: str
    rows: dict[int, dict] = field(default_factory=dict)
    # rowids some index says exist, with whatever columns that index holds
    indexed: dict[int, dict] = field(default_factory=dict)
    exists: bool = True
    counted: bool = True          # False when not even the number of rows is known
    stubs: int = 0
    orphaned: int = 0
    skipped: int = 0

    @property
    def expected(self) -> int:
        return len(self.rows.keys() | self.indexed.keys())

    @property
    def restored(self) -> int:
        return len(self.rows) + self.stubs - self.orphaned - self.skipped

    @property
    def lost(self) -> int:
        return self.expected - len(self.rows) - self.stubs + self.orphaned + self.skipped


@dataclass
class RepairResult:
    repaired: bool                 # a fresh database replaced the damaged one
    complete: bool                 # nothing the user would miss was lost
    backup: Path | None = None
    message: str = ""


def _tables() -> list[str]:
    """Every table of the current schema, in creation order, bar the caches.

    Taken from the schema rather than a fixed list, so a table added later is
    salvaged too. A fixed list of shows, media and progress once meant a repair
    silently emptied albums, tracks and lyrics: every play count gone, even when
    those pages were undamaged.
    """
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(db.SCHEMA)
        names = [r[0] for r in scratch.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
    finally:
        scratch.close()
    return [n for n in names if n not in DISPOSABLE and not n.startswith("sqlite_")]


def _read_table(source: sqlite3.Connection, table: str) -> Salvage:
    """Every row still readable, read in layers so one bad page costs only its rows.

    1. One pass straight down the table's own b-tree. NOT INDEXED, because a
       plain scan may walk a covering index instead, and a damaged index (which
       is only a copy) must not cost the table.
    2. If that fails part way, the rowids every readable index knows about. An
       index is a separate b-tree, so it usually survives damage to the table.
    3. Rowid ranges across the whole span, halving any range that fails until
       single rows are left, which steps past the bad pages to the rows beyond.
    Rows read before an error are always kept.
    """
    salvage = Salvage(table)
    try:
        columns = [r[1] for r in source.execute(f"PRAGMA table_info({table})")]
    except sqlite3.DatabaseError:
        salvage.counted = False          # the schema page itself is unreadable
        return salvage
    if not columns:
        salvage.exists = False           # an older library, before this table
        return salvage
    select = ", ".join(columns)
    last_read: list[int | None] = [None]

    def keep(sql: str, params: tuple = ()) -> None:
        # Row by row, so what was read before an error is kept and last_read
        # says where the error struck.
        last_read[0] = None
        for rowid, *values in source.execute(sql, params):
            salvage.rows[rowid] = dict(zip(columns, values))
            last_read[0] = rowid

    try:
        keep(f"SELECT rowid, {select} FROM {table} NOT INDEXED")
        return salvage
    except sqlite3.DatabaseError:
        pass

    whole_index = False
    try:
        indexes = [r[1] for r in source.execute(f"PRAGMA index_list({table})")]
    except sqlite3.DatabaseError:
        indexes = []
    for index in indexes:
        try:
            names = [r[2] for r in source.execute(f"PRAGMA index_info({index})") if r[2]]
            picked = ", ".join(["rowid", *names])
            for rowid, *values in source.execute(
                    f"SELECT {picked} FROM {table} INDEXED BY {index}"):
                salvage.indexed.setdefault(rowid, {}).update(zip(names, values))
            whole_index = True
        except sqlite3.DatabaseError:
            continue
    # Without one index read to the end, rows past the damage may exist that
    # nothing here knows about, so the count is a lower bound.
    salvage.counted = whole_index

    known = salvage.rows.keys() | salvage.indexed.keys()
    for edge in ("MIN", "MAX"):
        try:
            value = source.execute(f"SELECT {edge}(rowid) FROM {table}").fetchone()[0]
        except sqlite3.DatabaseError:
            continue
        if value is not None:
            known = known | {int(value)}
    if not known:
        return salvage

    budget = _PROBE_BUDGET
    # Every id here is handed out from 1 upward, so a damaged first page (which
    # also hides MIN(rowid)) still leaves the start of the span known.
    pending = [(min(1, *known), max(known))]
    while pending and budget > 0:
        low, high = pending.pop()
        budget -= 1
        try:
            keep(f"SELECT rowid, {select} FROM {table} WHERE rowid BETWEEN ? AND ? "
                 "ORDER BY rowid", (low, high))
            continue
        except sqlite3.DatabaseError:
            pass
        last = last_read[0]
        if last is not None:
            # Read up to a bad row: carry on after the last good one.
            if last < high:
                pending.append((last + 1, high))
        elif low < high:
            middle = (low + high) // 2
            pending.append((middle + 1, high))
            pending.append((low, middle))
    if pending:
        salvage.counted = False
    return salvage


# Tables whose unreadable rows can be stood in for from the path index, and
# which of the indexed columns to carry over. Watch history hangs on the media
# id and lyrics on the track id, and neither has a path of its own to be matched
# up by later, so without a placeholder every one of them would have to go.
_STUBS = {
    "media": ("kind", "missing", "show_id", "season", "episode"),
    "tracks": ("album_id", "disc_no", "track_no", "missing"),
}


def _stub(table: str, rowid: int, indexed: dict) -> dict | None:
    """A placeholder row: the id, the path, a title from the file name.

    mtime 0 (and, for songs, a state that is not 'ready') makes the next scan
    treat the file as changed and read it again by path, keeping the id, so the
    history stays on the right title. Everything else the row held, including a
    song's play count, is gone with the page.
    """
    path = indexed.get("path")
    if table not in _STUBS or not path:
        return None
    stem = Path(path).stem
    stub = {"id": rowid, "path": path, "folder": str(Path(path).parent),
            "title": stem, "sort_title": stem.lower(), "mtime": 0, "size": 0}
    for column in _STUBS[table]:
        if indexed.get(column) is not None:
            stub[column] = indexed[column]
    return stub


def _set_aside(original: Path) -> Path:
    """Move the damaged file, and its -wal and -shm, to a name nothing else has.

    The -wal has to go with it: left behind, SQLite would replay the old file's
    log into the fresh database. os.rename, not shutil.move, because on Windows
    shutil.move quietly overwrites an existing backup.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in itertools.count(1):
        backup = original.with_name(
            f"{original.stem}.corrupt-{stamp}{'' if n == 1 else f'-{n}'}.db")
        if not any(Path(f"{backup}{ext}").exists() for ext in ("", "-wal", "-shm")):
            break
    moved: list[tuple[Path, Path]] = []
    try:
        for ext in ("", "-wal", "-shm"):
            source, target = Path(f"{original}{ext}"), Path(f"{backup}{ext}")
            if source.exists():
                os.rename(source, target)
                moved.append((source, target))
    except OSError as exc:
        for source, target in reversed(moved):
            try:
                os.rename(target, source)
            except OSError:
                pass
        raise RepairError(
            f"could not move {original.name} aside ({exc}). Something still has "
            "it open: close Mistery and anything else using it, then try again."
        ) from exc
    return backup


def _restore(salvaged: list[Salvage], say: Callable[[str], None]) -> None:
    """Replay the salvaged rows, then deal with what points at lost rows."""
    conn = db.connect()
    # Checked afterwards instead: with the checks on, a progress row inserted
    # before its film (or whose film was lost) would just fail.
    conn.execute("PRAGMA foreign_keys=OFF")
    by_name = {s.table: s for s in salvaged}
    try:
        conn.execute("BEGIN")
        for salvage in salvaged:
            if not salvage.exists:
                continue
            valid = {r[1] for r in conn.execute(f"PRAGMA table_info({salvage.table})")}
            records = list(salvage.rows.values())
            for rowid, indexed in salvage.indexed.items():
                if rowid not in salvage.rows:
                    stub = _stub(salvage.table, rowid, indexed)
                    if stub is not None:
                        records.append(stub)
                        salvage.stubs += 1
            for record in records:
                fields = {k: v for k, v in record.items() if k in valid}
                if not fields:
                    continue
                try:
                    conn.execute(
                        f"INSERT INTO {salvage.table} ({', '.join(fields)}) "
                        f"VALUES ({', '.join('?' for _ in fields)})",
                        list(fields.values()),
                    )
                except sqlite3.DatabaseError as exc:
                    salvage.skipped += 1
                    say(f"  skipped a {salvage.table} row: {exc}")

        for table, rowid, parent, fk_id in conn.execute("PRAGMA foreign_key_check").fetchall():
            link = next(r for r in conn.execute(f"PRAGMA foreign_key_list({table})")
                        if r[0] == fk_id)
            column = link[3]
            info = list(conn.execute(f"PRAGMA table_info({table})"))
            if column in {r[1] for r in info if r[5]}:
                # progress or lyrics for a row that is gone: nothing to hang on.
                conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
                if table in by_name:
                    by_name[table].orphaned += 1
            else:
                # A film whose show, or a song whose album, was lost: unlink it
                # and let the next scan (mtime 0) find or make the parent again.
                rescan = ", mtime = 0" if "mtime" in {r[1] for r in info} else ""
                conn.execute(f"UPDATE {table} SET {column} = NULL{rescan} "
                             "WHERE rowid = ?", (rowid,))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def repair(say: Callable[[str], None] = print) -> RepairResult:
    """Salvage the library into a fresh database. Mistery must not have it open.

    Used by main() below and by Mistery itself, which offers this when the
    library cannot be opened at startup.
    """
    original = db_path()
    if not original.is_file():
        say("No database to repair.")
        return RepairResult(False, False, message="No database to repair.")

    db.release_file()
    source = sqlite3.connect(original)
    try:
        try:
            verdict = str(source.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            verdict = str(exc)
        verdict = next((line for line in verdict.splitlines()
                        if line.strip() and not line.startswith("***")), verdict)
        if verdict == "ok":
            say(f"{original} has no damage; nothing was changed.")
            return RepairResult(False, True, message="The library database is not damaged.")
        say(f"reading {original}")
        say(f"  damage: {verdict}")
        salvaged = [_read_table(source, table) for table in _tables()]
    finally:
        source.close()

    backup = _set_aside(original)
    say(f"\noriginal moved to {backup.name}")

    db.init()
    _restore(salvaged, say)

    check = db.query_one("PRAGMA integrity_check")
    status = list(check)[0] if check else "unknown"
    stats = db.library_stats()

    say("")
    losses = []
    for s in salvaged:
        if not s.exists:
            continue
        if not s.counted and not s.rows:
            line = f"  {s.table:10} nothing readable, number of rows unknown"
            losses.append(f"{s.table} (all)")
        else:
            total = f"{s.expected}" if s.counted else f"at least {s.expected}"
            line = f"  {s.table:10} {s.restored:6} of {total} rows restored"
            if s.stubs:
                line += f", {s.stubs} rebuilt from the path index"
            if s.orphaned:
                line += f", {s.orphaned} dropped with the rows they belonged to"
            if s.lost or not s.counted:
                losses.append(f"{s.table} ({s.lost}{'' if s.counted else '+'})")
            if s.table == "tracks" and s.stubs:
                losses.append(f"play counts of {s.stubs} song(s)")
        say(line)
    say(f"\nintegrity: {status}")
    say(f"library  : {stats['movies']} movies, {stats['shows']} shows, "
        f"{stats['files']} files")
    say(f"dropped  : {', '.join(DISPOSABLE)} (refills on the next lookup)")
    if any(s.stubs for s in salvaged):
        say("rebuilt  : rows rebuilt from the path index are read again from their "
            "files on the next scan, keeping the watch history and lyrics on them")
    if losses:
        say(f"\nLOST     : {', '.join(losses)}")
        say(f"The damaged original still holds those rows. Keep {backup.name}: "
            "do not delete it.")
    return RepairResult(status == "ok", status == "ok" and not losses, backup,
                        message="\n".join(losses))


def main() -> int:
    pid = app_is_running()
    if pid is not None:
        print(f"Mistery is running (pid {pid}). Close it first — repairing a "
              "database in use would make things worse.")
        return 1
    try:
        result = repair()
    except RepairError as exc:
        print(f"repair stopped: {exc}")
        return 1
    if not result.repaired:
        return 0 if result.complete else 1
    return 0 if result.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
