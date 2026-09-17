"""Walk the library folders and reconcile what's on disk with the database."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import db, parser
from .config import VIDEO_EXTS

# Directories that never hold a feature we care about.
_SKIP_DIRS = {
    "sample", "samples", "extras", "featurettes", "trailers", "bonus",
    "behind the scenes", "deleted scenes", "$recycle.bin", "system volume information",
    "proof", "screens", "subs", "subtitles",
}

# Anything smaller than this is almost certainly a clip, not a feature or episode.
_MIN_SIZE = 20 * 1024 * 1024


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    moved: int = 0              # renamed or moved on disk, history kept
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.moved or self.removed)

    def summary(self) -> str:
        if not self.changed:
            return f"Library up to date — {self.unchanged} file(s)"
        bits = []
        if self.added:
            bits.append(f"{self.added} added")
        if self.moved:
            bits.append(f"{self.moved} moved")
        if self.updated:
            bits.append(f"{self.updated} updated")
        if self.removed:
            bits.append(f"{self.removed} missing")
        return "Library scan: " + ", ".join(bits)


def iter_video_files(
    folders: Iterable[Path], on_error: Callable[[str], None] | None = None
) -> Iterable[Path]:
    """Yield every video file under the given roots, skipping extras folders."""
    seen: set[str] = set()

    def _walk_error(exc: OSError) -> None:
        # Unreadable folders (permissions, disconnected drives) are reported
        # rather than silently swallowed, which is os.walk's default.
        if on_error is not None:
            on_error(f"{getattr(exc, 'filename', '?')}: {exc.strerror or exc}")

    for root in folders:
        if not root.is_dir():
            if on_error is not None:
                on_error(f"{root}: folder not found")
            continue
        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=_walk_error
        ):
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS
                           and not d.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() not in VIDEO_EXTS:
                    continue
                path = Path(dirpath) / name
                key = str(path).lower()
                if key not in seen:
                    seen.add(key)
                    yield path


def _naming_fields(path: Path, parsed: parser.ParsedName) -> dict:
    """Just the columns derived from the filename, with show grouping applied."""
    record: dict = {
        "title": parsed.title or path.stem,
        "sort_title": parsed.sort_title or path.stem.lower(),
        "year": parsed.year,
        "edition": parsed.edition,
        "tags": json.dumps(parsed.tags) if parsed.tags else None,
    }

    if parsed.is_episode:
        show_id = db.upsert_show(
            key=parser.show_key(parsed.title, parsed.year),
            title=parsed.title,
            sort_title=parsed.sort_title,
            year=parsed.year,
        )
        record.update(
            kind="episode",
            show_id=show_id,
            season=parsed.season,
            episode=parsed.episode,
            title=parsed.episode_title or f"Episode {parsed.episode}",
            sort_title=(parsed.episode_title or "").lower(),
            year=None,
        )
    else:
        record.update(kind="movie", show_id=None, season=None, episode=None)

    return record


def _record_for(path: Path, stat: os.stat_result) -> dict:
    record = _naming_fields(path, parser.parse(path))
    record.update(
        path=str(path),
        folder=str(path.parent),
        size=stat.st_size,
        mtime=stat.st_mtime,
    )
    return record


def _renamed_fields(row: sqlite3.Row, path: Path) -> dict:
    """The naming columns for an existing row, read again from `path`."""
    fields = _naming_fields(path, parser.parse(path))

    # An official title from an online source beats anything we can read
    # off a filename, so re-parsing must not undo it.
    online = row["meta_source"] in ("tmdb", "tvmaze", "wikipedia")
    changed_kind = fields.get("kind") != row["kind"]
    if online and not changed_kind:
        fields.pop("title", None)
        fields.pop("sort_title", None)
        fields.pop("year", None)
    elif online:
        # A file that has just turned from a film into an episode was looked
        # up as the wrong sort of thing, so keeping that title would be
        # worse than the filename. Drop it and queue a proper lookup.
        fields["meta_state"] = "pending"
        fields["meta_source"] = None
    return fields


def reparse_names() -> int:
    """Re-derive titles and show grouping for everything already in the library.

    Used when the parser itself changes: the files are untouched, so re-probing
    and re-thumbnailing them would be pure waste. Only the naming columns move,
    which keeps media ids — and therefore watch progress — intact.
    """
    changed = 0
    for row in db.query("SELECT * FROM media"):
        path = Path(row["path"])
        try:
            fields = _renamed_fields(row, path)
        except Exception:
            continue

        delta = {k: v for k, v in fields.items() if row[k] != v}
        if delta:
            db.update_media(int(row["id"]), **delta)
            changed += 1

    db.execute(
        "DELETE FROM shows WHERE id NOT IN "
        "(SELECT DISTINCT show_id FROM media WHERE show_id IS NOT NULL)"
    )
    return changed


def _same_file_stat(row: sqlite3.Row, stat: os.stat_result) -> bool:
    return (row["size"] or 0) == stat.st_size and abs((row["mtime"] or 0.0) - stat.st_mtime) < 1.0


def _relink(row: sqlite3.Row, path: Path) -> bool:
    """Point an existing row at the file's new location, keeping its id.

    Watch progress, play counts, learned intro markers, thumbnails and artwork
    all hang off the media id, so moving the row rather than adding a new one
    is what keeps them. The file is byte-for-byte the same, so nothing derived
    from its contents is queued again; only the naming columns are re-read,
    since a new folder can mean a new title or show.
    """
    try:
        fields = _renamed_fields(row, path)
    except Exception:
        return False
    fields.update(path=str(path), folder=str(path.parent), missing=0)
    db.update_media(int(row["id"]), **fields)
    return True


def _moved_from(path: Path, stat: os.stat_result,
                orphans: list[sqlite3.Row]) -> sqlite3.Row | None:
    """The one vanished row this new file is, or None when unsure.

    A rename or a move within a drive keeps a file's size and modified time, and
    so does an Explorer copy. At 20 MB and up, two different videos agreeing on
    both to the second is not a coincidence worth worrying about; when several
    rows still fit, the file name has to agree as well, and anything still
    ambiguous is treated as new rather than guessed at.

    One kind of "several rows" is really one file. A folder renamed before
    this matching existed left its old rows behind, marked missing, beside the
    rows the files were given afterwards, which hold every play since. The two
    agree on size, time and name, so with no tie-break the next rename of that
    folder lost the newer history too. When the rows left are all that one
    file, the present one is followed. scan() only offers a row here once its
    own file is gone, so a row whose file is still where it says is never taken.
    """
    candidates = [row for row in orphans if _same_file_stat(row, stat)]
    if len(candidates) > 1:
        name = os.path.normcase(path.name)
        named = [row for row in candidates
                 if os.path.normcase(Path(row["path"]).name) == name]
        old_names = {os.path.normcase(Path(row["path"]).name) for row in candidates}
        # With no row under the new name, rows that all share one old name are
        # still one file whose name was tidied along with its folder.
        if named or len(old_names) > 1:
            candidates = named
    if len(candidates) > 1:
        present = [row for row in candidates if not row["missing"]]
        if len(present) == 1:
            candidates = present
    return candidates[0] if len(candidates) == 1 else None


def scan(
    folders: Iterable[Path] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    force: bool = False,
) -> ScanResult:
    """Reconcile the database with what's currently on disk.

    Files whose size and mtime are unchanged are left alone, so a rescan of an
    untouched library costs one stat() per file and no re-parsing.

    A file that was renamed or moved (a show folder tidied up, a film put into
    its own folder) keeps its row: new files are matched against rows whose
    files have vanished before anything is added, so history survives. Paths
    are compared the way Windows does, so changing only the case of a folder
    name is not a move at all.
    """
    from .config import settings

    roots = list(folders) if folders is not None else settings.library_folders()
    result = ScanResult()
    if not roots:
        return result

    rows = db.query("SELECT * FROM media")
    known = {row["path"]: row for row in rows}
    # Before this matching existed, a case-only rename left two rows for one
    # file; the one still present is the one holding recent history.
    by_case: dict[str, sqlite3.Row] = {}
    for row in sorted(rows, key=lambda r: r["missing"] or 0):
        by_case.setdefault(os.path.normcase(row["path"]), row)
    present: list[str] = []
    claimed: set[int] = set()
    unmatched: list[tuple[Path, os.stat_result]] = []

    for path in iter_video_files(roots, on_error=result.errors.append):
        try:
            stat = path.stat()
        except OSError as exc:
            result.errors.append(f"{path.name}: {exc}")
            continue

        if stat.st_size < _MIN_SIZE or parser.looks_like_extra(path, stat.st_size):
            result.skipped += 1
            continue

        key = str(path)
        present.append(key)

        cached = known.get(key)
        if cached is None:
            same = by_case.get(os.path.normcase(key))
            if same is not None and int(same["id"]) not in claimed and _relink(same, path):
                cached = same
                result.moved += 1
        if cached is None:
            # Decided after the walk, once every vanished row is known.
            unmatched.append((path, stat))
            continue
        claimed.add(int(cached["id"]))

        if not force and _same_file_stat(cached, stat):
            if cached["path"] == key:
                result.unchanged += 1
            continue

        try:
            record = _record_for(path, stat)
        except Exception as exc:  # a bad filename must not abort the whole scan
            result.errors.append(f"{path.name}: {exc}")
            continue

        # The file changed on disk, so everything derived from it is stale.
        # Artwork only when we made it ourselves — an online poster does not
        # depend on the file's contents, and its title must survive too.
        record.update(probe_state="pending", thumbs_state="pending",
                      tv_state="pending")
        if not _same_file_stat(cached, stat):
            # Intro and credits times belong to the old file. Another release
            # of the episode puts them elsewhere, and detection only writes
            # what it is sure of, so stale ones would skip real story.
            record.update(intro_start=None, intro_end=None, credits_at=None)
        if cached["meta_source"] in ("tmdb", "tvmaze", "wikipedia"):
            record.pop("title", None)
            record.pop("sort_title", None)
            record.pop("year", None)
        else:
            record["meta_state"] = "pending"
        result.updated += 1

        db.upsert_media(record)
        if progress_callback:
            progress_callback(record.get("title") or path.stem)

    if unmatched:
        seen = {os.path.normcase(key) for key in present}
        orphans = [row for row in rows
                   if int(row["id"]) not in claimed
                   and os.path.normcase(row["path"]) not in seen
                   and not os.path.exists(row["path"])]
        proposals: dict[int, list[tuple[Path, os.stat_result, sqlite3.Row]]] = {}
        leftovers: list[tuple[Path, os.stat_result]] = []
        for path, stat in unmatched:
            row = _moved_from(path, stat, orphans) if orphans else None
            if row is None:
                leftovers.append((path, stat))
            else:
                proposals.setdefault(int(row["id"]), []).append((path, stat, row))
        for claims in proposals.values():
            # Two new files that both look like the same vanished one: a copy
            # was made somewhere, so neither can be trusted to be the original.
            if len(claims) == 1 and _relink(claims[0][2], claims[0][0]):
                result.moved += 1
            else:
                leftovers.extend((path, stat) for path, stat, _ in claims)

        for path, stat in leftovers:
            try:
                record = _record_for(path, stat)
            except Exception as exc:  # a bad filename must not abort the whole scan
                result.errors.append(f"{path.name}: {exc}")
                continue
            record.update(
                probe_state="pending", meta_state="pending",
                thumbs_state="pending", added_at=time.time(),
            )
            result.added += 1
            db.upsert_media(record)
            if progress_callback:
                progress_callback(record["title"])

    result.removed = db.mark_missing(present)
    return result
