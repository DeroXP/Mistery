"""Health check for Mistery: tools, database, library and parser.

    python selfcheck.py            report only
    python selfcheck.py --fix      also queue anything broken for reprocessing

Exit code is 0 when everything passes, 1 when something needs attention.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# A Windows console is cp1252 by default, and this file talks in em dashes
# and "≥". Without this the report dies halfway through on the first one.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from app import db, parser, scanner
from app.config import (
    art_dir, db_path, find_ffmpeg, find_ffprobe, find_mpv, settings, thumbs_dir,
)
from app.metadata import thumbs as thumbs_module
from app.util import fmt_duration, fmt_size

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    mark = {PASS: "  ok ", WARN: " warn", FAIL: " FAIL"}[status]
    print(f"[{mark}] {name}" + (f"  —  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n--- {title} ---")


# --- external tools ---------------------------------------------------------

def check_environment() -> None:
    """Is this process seeing the real data folder, or a sandboxed view of it?"""
    section("environment")
    from app.config import data_dir, virtualized_appdata

    private = virtualized_appdata()
    if private is None:
        check("data folder", PASS, str(data_dir()))
    else:
        check("data folder is sandboxed", WARN,
              f"this process was started inside a packaged app, so new files go to {private}; "
              "results below reflect that private view, not what Mistery itself sees. "
              "Run selfcheck from a normal terminal for the real picture.")


def check_tools() -> None:
    section("external tools")
    for label, found, needed in (
        ("mpv (playback)", find_mpv(), True),
        ("ffprobe (media info)", find_ffprobe(), True),
        ("ffmpeg (artwork, thumbnails)", find_ffmpeg(), True),
    ):
        if found:
            check(label, PASS, found)
        else:
            check(label, FAIL if needed else WARN, "not found on PATH")

    for module, label in (("PySide6", "PySide6 (interface)"),
                          ("PIL", "Pillow (image handling)"),
                          ("requests", "requests (TMDB)")):
        try:
            __import__(module)
            check(label, PASS)
        except ImportError:
            check(label, FAIL, "not installed")
    try:
        __import__("mutagen")
        check("mutagen (music tags)", PASS)
    except ImportError:
        # Music still works through ffprobe, just ~35x slower to scan.
        check("mutagen (music tags)", WARN, "not installed — pip install mutagen")


# --- database ---------------------------------------------------------------

def check_database() -> None:
    section("database")
    path = db_path()
    if not path.is_file():
        check("library database", WARN, "not created yet — it appears on first run")
        return
    check("library database", PASS, f"{path}  ({fmt_size(path.stat().st_size)})")

    try:
        result = db.query_one("PRAGMA integrity_check")
        value = list(result)[0] if result else "unknown"
        check("integrity", PASS if value == "ok" else FAIL, str(value))
    except sqlite3.Error as exc:
        check("integrity", FAIL, str(exc))

    try:
        orphans = db.query_one(
            "SELECT COUNT(*) n FROM media WHERE show_id IS NOT NULL AND show_id "
            "NOT IN (SELECT id FROM shows)"
        )
        count = orphans["n"] if orphans else 0
        check("episodes point at a real show", PASS if not count else FAIL,
              "" if not count else f"{count} orphaned")
    except sqlite3.Error as exc:
        check("episode/show links", FAIL, str(exc))

    empty = db.query_one("SELECT COUNT(*) n FROM shows WHERE id NOT IN "
                         "(SELECT DISTINCT show_id FROM media WHERE show_id IS NOT NULL)")
    count = empty["n"] if empty else 0
    check("no empty shows", PASS if not count else WARN,
          "" if not count else f"{count} show(s) with no episodes")

    for table, wanted in (
        ("shows", {"intro_start", "intro_end", "credits_len", "subs_on",
                   "sub_lang", "audio_lang", "tvmaze_id"}),
        ("media", {"intro_start", "intro_end", "credits_at", "tv_state"}),
    ):
        columns = {row["name"] for row in db.query(f"PRAGMA table_info({table})")}
        missing = wanted - columns
        check(f"{table} migrations applied", PASS if not missing else FAIL,
              "" if not missing else "missing: " + ", ".join(sorted(missing)))


# --- library ----------------------------------------------------------------

def check_library(fix: bool) -> None:
    section("library")
    folders = settings.get("library_folders", [])
    missing_folders = [f for f in folders if not Path(f).is_dir()]
    check("library folders", PASS if folders and not missing_folders else WARN,
          ", ".join(folders) if not missing_folders
          else f"unreachable: {', '.join(missing_folders)}")

    stats = db.library_stats()
    check("contents", PASS,
          f"{stats['movies']} movies · {stats['shows']} shows · {stats['files']} files "
          f"· {fmt_size(stats['bytes'])} · {fmt_duration(stats['seconds'])}")

    for column, label in (("probe_state", "media info"),
                          ("meta_state", "artwork"),
                          ("thumbs_state", "seek thumbnails")):
        rows = db.query(f"SELECT {column} s, COUNT(*) n FROM media GROUP BY {column}")
        states = {r["s"]: r["n"] for r in rows}
        bad = states.get("error", 0)
        pending = states.get("pending", 0) + states.get("incomplete", 0)
        if bad:
            check(label, WARN, f"{bad} failed, {states.get('done', 0)} ok "
                               "— run with --fix or press Retry failed in Settings")
        elif pending:
            check(label, PASS, f"{states.get('done', 0)} done, {pending} still queued")
        else:
            check(label, PASS, f"all {states.get('done', 0)} done")

    gone = [r["path"] for r in db.query("SELECT path FROM media WHERE missing = 0")
            if not Path(r["path"]).is_file()]
    check("every catalogued file exists", PASS if not gone else WARN,
          "" if not gone else f"{len(gone)} missing, e.g. {Path(gone[0]).name}")

    dangling_art = db.query_one(
        "SELECT COUNT(*) n FROM media WHERE poster IS NOT NULL"
    )
    broken = [r["poster"] for r in db.query("SELECT poster FROM media WHERE poster IS NOT NULL")
              if not Path(r["poster"]).is_file()]
    check("artwork files present", PASS if not broken else WARN,
          f"{(dangling_art['n'] if dangling_art else 0) - len(broken)} on disk"
          + (f", {len(broken)} missing" if broken else ""))

    analyzed = db.query_one(
        "SELECT SUM(tv_state='done') done, COUNT(*) total, "
        "SUM(intro_end IS NOT NULL) intros, SUM(credits_at IS NOT NULL) credits "
        "FROM media WHERE kind='episode' AND missing=0"
    )
    if analyzed and analyzed["total"]:
        check("intro/credits analysis", PASS,
              f"{analyzed['done']}/{analyzed['total']} analyzed — "
              f"{analyzed['intros']} intros, {analyzed['credits']} credit points found")

    stale_sprites = []
    for row in db.query("SELECT path, thumbs FROM media WHERE thumbs IS NOT NULL"):
        index = thumbs_module.load_index(row["thumbs"])
        if index is None:
            stale_sprites.append(row["path"])
            continue
        try:
            if index.get("source_size") not in (None, Path(row["path"]).stat().st_size):
                stale_sprites.append(row["path"])
        except OSError:
            stale_sprites.append(row["path"])
    check("sprites match their source file", PASS if not stale_sprites else WARN,
          "" if not stale_sprites else f"{len(stale_sprites)} stale — will rebuild on rescan")

    if fix:
        queued = db.retry_failed()
        for path in stale_sprites:
            db.execute("UPDATE media SET thumbs_state='pending' WHERE path = ?", (path,))
        print(f"\n  --fix: queued {queued} failed row(s) and "
              f"{len(stale_sprites)} stale sprite(s) for reprocessing")


# --- parser -----------------------------------------------------------------

_PARSER_CASES = [
    (r"C:\M\Spider-Man.2.2004.2160p.BluRayRip.EAC3.5.1.HDR.x265-Groupless[TGx]\Spider-Man.2.2004.2160p.mkv",
     "Spider-Man 2", None, None),
    (r"C:\M\Breaking Bad S01\S01E01 - Pilot.mkv", "Breaking Bad", 1, 1),
    (r"C:\M\Breaking Bad S05\S05E14 - Ozymandias.mkv", "Breaking Bad", 5, 14),
    (r"D:\TV\Breaking Bad\Season 02\S02E03 - Bit by a Dead Bee.mkv", "Breaking Bad", 2, 3),
    (r"D:\TV\The Wire\Season 1\E05 - The Pager.mkv", "The Wire", 1, 5),
    (r"D:\TV\Severance.S02E07.Chikhai.Bardo.2160p.WEB-DL.mkv", "Severance", 2, 7),
    (r"D:\TV\Firefly - 1x04 - Shindig.avi", "Firefly", 1, 4),
    (r"D:\Movies\2012.2009.1080p.BluRay.x264.mkv", "2012", None, None),
    (r"D:\Movies\Blade Runner 2049 (2017) [2160p] [HDR].mkv", "Blade Runner 2049", None, None),
    (r"D:\Movies\Se7en (1995)\Se7en.1995.1080p.mkv", "Se7en", None, None),
    # Anime revisions, four-digit absolute numbers, a mid-name E-number in a
    # season folder, and the film names those rules must leave alone.
    (r"D:\Anime\[SubsPlease] Dandadan - 12v2 (1080p) [0F1E2D3C].mkv", "Dandadan", 1, 12),
    (r"D:\Anime\One Piece\[SubsPlease] One Piece - 1071 (1080p) [5A1B2C3D].mkv", "One Piece", 1, 1071),
    (r"D:\TV\Show Name\Season 02\Show.Name.E05.1080p.WEB-DL.mkv", "Show Name", 2, 5),
    (r"D:\Movies\The Movie - 10 Years Later (2019).mkv", "The Movie - 10 Years Later", None, None),
    (r"D:\Movies\Toy Story 4 - 2019.mkv", "Toy Story 4", None, None),
    # Four digits need a release-style name, and a resolution without its "p"
    # is no episode, unless the name gives its resolution as well.
    (r"D:\Movies\Borat - 1492.mkv", "Borat - 1492", None, None),
    (r"D:\Movies\Some Film - 2160 HDR.mkv", "Some Film - 2160", None, None),
    (r"D:\Anime\[SubsPlease] One Piece - 1080 (1080p) [5A1B2C3D].mkv", "One Piece", 1, 1080),
    (r"D:\Anime\[Group] Show - 12 END (2019) [1080p].mkv", "Show", 1, 12),
]


def check_parser() -> None:
    section("filename parsing")
    failures = []
    for path, title, season, episode in _PARSER_CASES:
        got = parser.parse(path)
        if got.title != title or got.season != season or got.episode != episode:
            failures.append(
                f"{Path(path).name} -> {got.title!r} S{got.season}E{got.episode} "
                f"(wanted {title!r} S{season}E{episode})"
            )
    check(f"{len(_PARSER_CASES)} naming patterns", PASS if not failures else FAIL,
          "" if not failures else failures[0])
    for extra in failures[1:]:
        print(f"          also: {extra}")

    keys = {parser.show_key(parser.parse(
        rf"C:\M\Breaking Bad S{s:02d}\S{s:02d}E{e:02d} - Title.mkv").title, None)
        for s in range(1, 6) for e in range(1, 8)}
    check("episodes group into one show", PASS if len(keys) == 1 else FAIL,
          f"{len(keys)} distinct key(s)")


# --- music ------------------------------------------------------------------

def check_music() -> None:
    section("music")
    from app.music import art, library as music_library

    tracks = db.query_one(
        "SELECT SUM(state = 'ready') AS ready, SUM(state = 'incomplete') AS partial, "
        "SUM(state = 'error') AS broken, COUNT(*) AS total FROM tracks WHERE missing = 0")
    if not tracks or not tracks["total"]:
        check("music library", PASS, "no music yet")
        return
    albums = music_library.albums()
    check("music library", PASS,
          f"{len(albums)} album(s) · {tracks['ready'] or 0} song(s) ready")
    partial = int(tracks["partial"] or 0)
    # Downloading is a normal state, not a fault — say so without failing.
    check("downloads in progress", PASS,
          f"{partial} track(s) still arriving — they unlock when finished" if partial else "none")
    if tracks["broken"]:
        check("unreadable audio files", WARN, f"{tracks['broken']} file(s)")

    missing_art = [a["title"] for a in albums if a["cover"] and not Path(a["cover"]).is_file()]
    check("album covers present", PASS if not missing_art else WARN,
          "all on disk" if not missing_art else f"missing for {', '.join(missing_art[:3])}")
    unreadable = []
    for album in albums:
        colours = music_library.parse_palette(album["palette"])
        if art.contrast("#FFFFFF", colours["dark"]) < 7:
            unreadable.append(album["title"])
    check("album colours keep text readable", PASS if not unreadable else WARN,
          "white text ≥ 7:1 on every album" if not unreadable else ", ".join(unreadable[:3]))

    import os as _os
    gone = [a["title"] for a in db.query("SELECT title, cover FROM albums WHERE art_state = 'done'")
            if not a["cover"] or not _os.path.exists(a["cover"])
            or not _os.path.exists(a["cover"].replace(".jpg", "-sm.jpg"))]
    check("album cover files exist", PASS if not gone else WARN,
          "every cover recorded as made is on disk" if not gone else
          f"{len(gone)} recorded as made but missing ({', '.join(gone[:3])}) — "
          "Mistery makes them again on its next library pass")

    done, total = music_library.loudness_progress()
    broken = db.query_one("SELECT COUNT(*) AS n FROM tracks WHERE loud_state = 'error'")["n"]
    if not total:
        pass
    elif done >= total:
        check("loudness measured", PASS, f"all {total} song(s) — every album plays at one level")
    else:
        check("loudness measured", WARN if done else FAIL,
              f"{done}/{total} — the rest are measured in the background"
              + (f", {broken} could not be read" if broken else ""))


# --- storage ----------------------------------------------------------------

def check_storage() -> None:
    section("stored data")
    for label, folder in (("artwork cache", art_dir()), ("thumbnail cache", thumbs_dir())):
        files = list(folder.glob("*")) if folder.is_dir() else []
        total = sum(f.stat().st_size for f in files if f.is_file())
        check(label, PASS, f"{len(files)} file(s), {fmt_size(total)}  ({folder})")

    leftovers = [d for d in thumbs_dir().glob("tmp-*") if d.is_dir()]
    check("no leftover temp folders", PASS if not leftovers else WARN,
          "" if not leftovers else f"{len(leftovers)} found — safe to delete")


def main() -> int:
    fix = "--fix" in sys.argv
    print("Mistery self-check" + ("  (--fix enabled)" if fix else ""))
    check_environment()
    db.init()

    check_tools()
    check_database()
    check_library(fix)
    check_music()
    check_parser()
    check_storage()

    fails = sum(1 for s, _, _ in _results if s == FAIL)
    warns = sum(1 for s, _, _ in _results if s == WARN)
    print(f"\n{len(_results)} checks — {len(_results) - fails - warns} passed, "
          f"{warns} warning(s), {fails} failure(s)")
    if fails:
        print("Something needs attention above.")
    elif warns:
        print("Usable, but see the warnings.")
    else:
        print("Everything looks healthy.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
