"""Live smoke test: launches the interface and really plays video.

    python smoketest.py

Complements selfcheck.py, which only inspects data. This one drives the app:
every page, a movie, an episode, the autoplay chain, VR detection and shutdown.
It plays muted, and runs against a copy of your library in a temporary folder,
so nothing it scans, builds or plays is written to the real one.

    python smoketest.py --live     run against the real data folder instead

Exit code is 0 when every check passes.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The test scans, makes artwork, measures loudness and plays things. Pointed at
# the real data folder that all lands in the user's library — and run from inside
# a packaged app's sandbox, the artwork files go to a private copy while the
# library records them as made. That is how four album covers once went missing.
# So by default it works on a copy, set up before anything imports app.config.
if "--live" not in sys.argv and not os.environ.get("MISTERY_DATA_DIR"):
    _real = Path(os.environ.get("APPDATA") or Path.home()) / "Mistery"
    _sandbox = Path(tempfile.mkdtemp(prefix="mistery-smoketest-"))
    if (_real / "library.db").is_file():
        _source = sqlite3.connect(f"file:{_real / 'library.db'}?mode=ro", uri=True)
        _copy = sqlite3.connect(str(_sandbox / "library.db"))
        _source.backup(_copy)
        _source.close()
        _copy.close()
    if (_real / "settings.json").is_file():
        shutil.copy2(_real / "settings.json", _sandbox / "settings.json")
    os.environ["MISTERY_DATA_DIR"] = str(_sandbox)
    atexit.register(shutil.rmtree, _sandbox, True)

# A Windows console is cp1252 by default, and this file talks in em dashes
# and "≥". Without this the report dies halfway through on the first one.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

ERRORS: list[str] = []


def _hook(kind, exc, tb) -> None:
    text = "".join(traceback.format_exception(kind, exc, tb))
    ERRORS.append(text)
    print("\n!!! UNCAUGHT EXCEPTION\n" + text, flush=True)


sys.excepthook = _hook

from PySide6.QtCore import (                                   # noqa: E402
    QEvent, QPoint, QPointF, Qt, QtMsgType, qInstallMessageHandler,
)
from PySide6.QtGui import QMouseEvent                          # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

WARNINGS: list[str] = []
qInstallMessageHandler(
    lambda mode, ctx, msg: WARNINGS.append(msg)
    if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg) else None
)

from app import db, vr                                         # noqa: E402
from app.config import settings                                # noqa: E402
from app.models import MediaItem, ShowItem                     # noqa: E402
from app.ui.main_window import MainWindow                      # noqa: E402
from app.ui.widgets.icons import IconButton                    # noqa: E402
from app.util import fmt_clock                                 # noqa: E402
from app.music import library as music_library                 # noqa: E402
from app.music import player as music_player                   # noqa: E402

# Every music player this test starts is silent.
_audio_args = music_player.AudioMpv._base_arguments
music_player.AudioMpv._base_arguments = (
    lambda self, wid: _audio_args(self, wid) + ["--mute=yes"])

_results: list[bool] = []
# Media touched by the test. Cleared at the very end rather than as we go:
# starting playback saves the *previous* item's position, so an early delete
# gets written straight back.
_touched: set[int] = set()


def ok(label: str, condition, note: str = "") -> None:
    _results.append(bool(condition))
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({note})" if note else ""))


def _app_is_live() -> str | None:
    """A reason string when Mistery appears to be in use, else None.

    The test plays video and manipulates watch progress; running it against a
    live session risks the user's real data. Two independent signals:
    the app's lock file, and any progress row written in the last minute.
    """
    from app.config import app_is_running

    pid = app_is_running()
    if pid is not None:
        return f"the app is running (pid {pid})"
    recent = db.query_one(
        "SELECT MAX(updated_at) AS t FROM progress"
    )
    if recent and recent["t"] and (time.time() - float(recent["t"])) < 60:
        return "watch progress was written in the last minute — someone is watching"
    return None


def _window_under(x: int, y: int, ratio: float) -> int:
    """The native window Windows would send a click at this screen point to.

    mpv draws into its own child window underneath our controls. Windows
    hit-tests a translucent window per pixel, so if the overlay ever stops
    painting over the picture the mouse silently falls through to mpv — which
    ignores it, and the player stops responding to the mouse entirely. This asks
    the OS the same question the mouse does.
    """
    import ctypes
    from ctypes import wintypes

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    return int(user32.WindowFromPoint(POINT(int(x * ratio), int(y * ratio))) or 0)


def _pin_on_top(hwnd: int, on_top: bool) -> None:
    """Whatever is running this test would otherwise win the hit test."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(-1 if on_top else -2),
        0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010,       # NOMOVE | NOSIZE | NOACTIVATE
    )


def _click(widget, x: int, y: int) -> None:
    local, screen = QPointF(x, y), widget.mapToGlobal(QPoint(x, y))
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(widget, QMouseEvent(
            kind, local, screen, Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))


def _snapshot_progress() -> list[dict]:
    return [dict(row) for row in db.query("SELECT * FROM progress")]


def _restore_progress(snapshot: list[dict], touched: set[int]) -> None:
    """Put the progress rows for everything the test touched back exactly."""
    by_id = {int(row["media_id"]): row for row in snapshot}
    for media_id in touched:
        db.execute("DELETE FROM progress WHERE media_id = ?", (media_id,))
        original = by_id.get(int(media_id))
        if original:
            columns = ", ".join(original)
            marks = ", ".join("?" for _ in original)
            db.execute(
                f"INSERT INTO progress ({columns}) VALUES ({marks})",
                list(original.values()),
            )


def _music_checks(window, pump) -> None:
    """Library, album page, the bar, Now Playing, and music yielding to film."""
    from PySide6.QtGui import QKeyEvent

    print("\n-- music --")
    music_library.scan(settings.library_folders())
    music_library.build_artwork()
    albums = [a for a in music_library.albums() if a["ready_count"]]
    if not albums:
        print("  (no music in the library — nothing to check)")
        return

    keys = ("music_volume", "music_shuffle", "music_repeat", "close_to_tray",
            "tray_hint_shown", "music_normalize", "music_boost", "music_bass",
            "music_clarity", "music_spatial")
    saved = {k: settings.get(k) for k in keys}
    plays = [tuple(r) for r in db.query("SELECT id, play_count, last_played FROM tracks")]
    settings.set("music_shuffle", False)
    settings.set("music_repeat", "off")
    music = window.music
    try:
        window._on_nav(3)
        pump(1.2)
        cards = window.music_page._album_flow.count()
        ok("Music page lists albums", window.stack.currentWidget() is window.music_page and cards > 0,
           f"{cards} album(s)")

        album = max(albums, key=lambda a: a["ready_count"])
        window.open_album(int(album["id"]))
        pump(0.8)
        ok("album page lists its songs",
           len(window.album_page._tracks.tracks) == int(album["track_count"]),
           f"{album['title']}: {len(window.album_page._tracks.tracks)} songs")

        window.album_page._play_album()
        deadline = time.time() + 10
        while time.time() < deadline and not (music.is_playing and music.position > 0.5):
            pump(0.1)
        ok("an album plays", music.is_playing and music.position > 0.5,
           f"{music.current['title'] if music.current else None} at {music.position:.1f}s")
        pump(0.6)
        ok("the Now Playing bar appears", window.now_bar.isVisible())
        # The exact regression: mpv's pause and idle events race, and the button
        # once showed ▶ over a song that was playing.
        ok("the bar's button shows pause while playing",
           window.now_bar._buttons["play"]._icon == "pause")

        window.now_bar._buttons["play"].click()
        pump(1.0)
        ok("the bar pauses it", not music.is_playing
           and window.now_bar._buttons["play"]._icon == "play")
        QApplication.sendEvent(window, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Space,
                                                 Qt.KeyboardModifier.NoModifier, " "))
        pump(1.2)
        ok("Space resumes it from anywhere outside the film player", music.is_playing)

        window.open_now_playing("lyrics")
        pump(0.6)
        ok("Now Playing takes the window",
           not window.topbar.isVisible() and not window.now_bar.isVisible())
        deadline = time.time() + 20
        view = window.now_playing.lyrics
        while time.time() < deadline and view._status == "Finding lyrics…":
            pump(0.2)
        # Network-independent: the lookup must *finish*, whatever it found.
        ok("the lyrics lookup finishes", view._status != "Finding lyrics…",
           view.lyrics.source if view.lyrics else view._status)
        window.go_back()
        pump(0.6)
        ok("leaving Now Playing brings the bar back", window.now_bar.isVisible())

        print("\n-- sound --")
        from app.music import audio_fx
        settings.set("music_normalize", "loud")
        settings.set("music_bass", "off")
        settings.set("music_spatial", "off")
        settings.set("music_boost", "off")
        settings.set("music_clarity", False)
        music.apply_sound()
        pump(0.8)

        def graph_now():
            chain = music._mpv.get_property("af") if music._mpv else None
            return chain[0]["params"]["graph"] if chain else ""

        ok("an album is levelled by a gain mpv applies to the file",
           graph_now().startswith("volume="), graph_now() or "(nothing)")
        at = music.position
        settings.set("music_spatial", "normal")
        music.apply_sound()
        pump(1.2)
        ok("3D audio switches on mid-song, without missing a beat",
           "extrastereo" in graph_now() and "adelay" in graph_now()
           and music.is_playing and music.position > at,
           f"{at:.1f}s -> {music.position:.1f}s, {graph_now()}")
        settings.set("music_bass", "deep")
        music.apply_sound()
        pump(0.8)
        ok("bass joins the same chain, with the limiter last",
           "bass=g=6" in graph_now() and graph_now().endswith("level=disabled"))
        settings.set("music_spatial", "off")
        settings.set("music_bass", "off")
        music.apply_sound()
        pump(0.8)
        ok("and switching everything off leaves only the levelling",
           "," not in graph_now() and music.is_playing)

        sound_button = next(b for b in window.now_bar.findChildren(IconButton)
                            if b.icon_name() == "sound")
        sound_button.click()
        pump(0.5)
        popup = QApplication.activePopupWidget()
        ok("the Sound button opens the panel over the bar",
           popup is not None and popup.isVisible() and popup.width() > 200,
           type(popup).__name__ if popup else "none")
        if popup is not None:
            popup._spatial.changed.emit("subtle")
            pump(0.8)
            ok("choosing 3D in the panel is heard straight away",
               "extrastereo" in graph_now() and audio_fx.current()["spatial"] == "subtle",
               graph_now())
            popup._spatial.changed.emit("off")
            pump(0.5)
            popup.close()

        movies = db.movies()
        if movies:
            film = MediaItem.from_row(movies[0])
            window.play(film, start_at=90)
            pump(4.0)
            window.player.mpv.set_property("mute", True)
            ok("starting a film pauses the music", not music.is_playing)
            ok("the bar gets out of the film's way", not window.now_bar.isVisible())
            window.player.stop_and_close()
            pump(1.0)
            _touched.add(film.id)

        print("\n-- a whole discography at once --")
        # The regression: Shuffle on an artist queued ~90 songs in one burst, the
        # IPC pipe to mpv deadlocked, and mpv played on while the bar froze on
        # the first song, showed it paused, and ignored every button. Asked of
        # mpv directly here, because the app's own cached state is what lied.
        artists = {}
        for row in music_library.albums():
            artists[row["artist"]] = artists.get(row["artist"], 0) + int(row["ready_count"] or 0)
        biggest = max(artists, key=artists.get)
        window.open_artist(biggest)
        pump(0.8)
        window.artist_page._shuffle.click()
        pump(3.0)

        def mpv_says(name):
            reply = music._mpv.command_sync("get_property", name, timeout=3.0)
            return reply.get("data") if reply.get("error") == "success" else f"<{reply.get('error')}>"

        count = mpv_says("playlist-count")
        ok(f"{biggest}: {artists[biggest]} songs queued in one go and mpv still answers",
           isinstance(count, int) and count >= artists[biggest] - 1, f"playlist-count={count}")
        ok("...the bar shows it playing, and mpv agrees",
           music.is_playing and mpv_says("pause") is False)
        before = music.current["id"] if music.current else None
        window.now_bar._buttons["next"].click()
        pump(1.8)
        ok("...and follows it to the next song",
           music.current and music.current["id"] != before
           and music.current["path"] == mpv_says("path")
           and window.now_bar._title.full_text() == music.current["title"],
           window.now_bar._title.full_text())
        music.stop()
        pump(0.5)
        music.set_shuffle(False)

        print("\n-- background playback --")
        settings.set("close_to_tray", True)
        settings.set("tray_hint_shown", True)
        window.album_page._play_album()
        deadline = time.time() + 10
        while time.time() < deadline and not music.is_playing:
            pump(0.2)
        window.close()
        pump(1.0)
        ok("closing while music plays keeps it playing, with no window",
           window.in_tray and not window.isVisible() and music.is_playing)
        ok("the tray icon is the way back", window.tray.visible)
        ok("the library and the position stream stop (nothing stealing frames)",
           window.service.paused and not music._mpv.is_observing("time-pos"))
        window.show_from_tray()
        pump(1.2)
        ok("opening it again restores the window and everything behind it",
           window.isVisible() and not window.service.paused
           and music._mpv.is_observing("time-pos"))
        ok("...showing the song that is actually playing",
           window.now_bar._title.full_text() == music.current["title"])

        music.stop()
        pump(0.5)
    finally:
        for key, value in saved.items():
            settings.set(key, value)
        for track_id, count, last in plays:
            db.execute("UPDATE tracks SET play_count = ?, last_played = ? WHERE id = ?",
                       (count, last, track_id))


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    db.init()

    live = _app_is_live()
    if live:
        print(f"Refusing to run: {live}.")
        print("Close Mistery (or finish watching) and run the test again.")
        return 1

    if not db.movies() and not db.all_shows():
        print("Nothing in the library yet — run the app once so it can scan.")
        return 1

    progress_snapshot = _snapshot_progress()
    restore_scan = settings.get("scan_on_startup", True)
    settings.set("scan_on_startup", False)

    window = MainWindow()
    window.resize(1440, 900)
    window.show()

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.008)

    pump(2.0)

    print("-- navigation --")
    for index, name in enumerate(["Home", "Movies", "Shows", "Music", "Search", "Settings"]):
        window._on_nav(index)
        pump(0.45)
        ok(f"{name} renders", window.stack.currentWidget().isVisible())

    shows = db.all_shows()
    if shows:
        print("\n-- shows --")
        show = ShowItem.from_row(shows[0])
        window.open_show(show)
        pump(1.5)
        ok("show page opens", window.stack.currentWidget() is window.show_page)
        episodes = db.episodes_for_show(show.id)
        ok(f"{len(episodes)} episodes listed", len(episodes) == show.episode_count)

        chain, current, seen = [], episodes[0], set()
        while current is not None and len(chain) < 500:
            key = (current["season"], current["episode"])
            if key in seen:
                break
            seen.add(key)
            chain.append(key)
            nxt = db.next_episode(int(current["id"]))
            current = db.get_media(int(nxt["id"])) if nxt else None
        ok("autoplay chain covers the show", len(chain) == len(episodes),
           f"{len(chain)}/{len(episodes)}")
        ok("chain is strictly ordered", chain == sorted(chain))

        print("\n-- episode playback --")
        first = MediaItem.from_row(db.get_media(int(episodes[0]["id"])))
        window.play(first, start_at=300)
        pump(1.0)
        window.player.mpv.set_property("mute", True)
        pump(5.0)
        before = window.player._position
        pump(2.5)
        ok("episode plays", window.player._position > before,
           f"{fmt_clock(before)} -> {fmt_clock(window.player._position)}")
        ok("background work paused", window.service.paused)
        window.player.save_progress()
        window.player.stop_and_close()
        pump(1.2)
        ok("background work resumed", not window.service.paused)
        _touched.add(first.id)
        progress = db.get_progress(first.id)
        ok("progress saved", progress is not None and progress["position"] > 0)

    if shows:
        print("\n-- tv features --")
        show_id = int(shows[0]["id"])
        episodes = db.episodes_for_show(show_id)
        saved_prefs = db.show_prefs(show_id)
        try:
            if len(episodes) >= 2:
                second = MediaItem.from_row(db.get_media(int(episodes[1]["id"])))
                _touched.add(second.id)

                # Use this episode's real intro if one was detected; otherwise
                # mark one so the check works on any library.
                if second.intro_end and second.intro_end > 5:
                    intro_start, intro_end = float(second.intro_start or 0), float(second.intro_end)
                    source = "detected"
                else:
                    intro_start, intro_end = 60.0, 150.0
                    db.set_show_prefs(show_id, intro_start=intro_start, intro_end=intro_end)
                    source = "marked"
                db.set_show_prefs(show_id, subs_on=1)
                settings.set("auto_skip_intro", True)

                window.play(second, start_at=max(0.0, intro_start - 2.0))
                pump(1.0)
                window.player.mpv.set_property("mute", True)
                deadline = time.time() + 20
                while time.time() < deadline and window.player._position <= intro_end - 5:
                    app.processEvents()
                    time.sleep(0.05)
                ok(f"intro auto-skips ({source} at {fmt_clock(intro_start)})",
                   window.player._position > intro_end - 5,
                   fmt_clock(window.player._position))
                ok("subtitle memory applied from the show",
                   window.player.mpv.cached("sub-visibility") is not None)

                duration = window.player._duration
                if duration:
                    window.player.mpv.seek_absolute(duration - 35)
                    pump(4.0)
                    ok("Up Next card appears at the credits",
                       window.player.overlay.next_card.isVisible())
                    window.player.overlay.next_card.dismiss()
                    pump(0.4)
                window.player.stop_and_close()
                pump(1.0)

            # Controlled state for the Next Up check, scoped to this show's
            # episodes only, every row restored afterwards from the snapshot.
            episode_ids = [int(e["id"]) for e in episodes]
            _touched.update(episode_ids)
            for episode_id in episode_ids:
                db.execute("DELETE FROM progress WHERE media_id = ?", (episode_id,))
            db.set_watched(episode_ids[0], True)
            _touched.add(episode_ids[0])
            rows = db.next_up()
            ok("Next Up resolves to the episode after the last watched",
               len(rows) == 1 and int(rows[0]["id"]) == episode_ids[1])

            # Which intro/credits source wins, without touching playback.
            view = window.player
            probe_item = MediaItem.from_row(db.get_media(episode_ids[0]))
            probe_item.duration = 2880.0
            view._item, view._show_id, view._duration = probe_item, show_id, 2880.0

            probe_item.intro_start, probe_item.intro_end = 120.0, 140.0
            probe_item.credits_at = 2700.0
            db.set_show_prefs(show_id, intro_start=10.0, intro_end=30.0, credits_len=60.0)
            view._load_tv_model()
            ok("detected episode intro beats the show marker",
               view._intro == (120.0, 140.0), str(view._intro))
            ok("earliest credible credits point wins",
               view._credits_at == 2700.0, str(view._credits_at))

            probe_item.intro_start = probe_item.intro_end = probe_item.credits_at = None
            view._load_tv_model()
            ok("falls back to the show marker", view._intro == (10.0, 30.0))

            probe_item.credits_at = 100.0      # not credits in a 48-minute episode
            view._load_tv_model()
            ok("implausible credits point rejected", view._credits_at == 2820.0,
               str(view._credits_at))
            view._item, view._show_id = None, None
        finally:
            db.set_show_prefs(show_id, **{
                k: saved_prefs.get(k)
                for k in ("intro_start", "intro_end", "credits_len",
                          "subs_on", "sub_lang", "audio_lang")
            })

        if len(episodes) >= 2:
            print("\n-- end of episode --")
            episode_ids = [int(e["id"]) for e in episodes]
            restore_autoplay = settings.get("autoplay_next", True)
            settings.set("autoplay_next", True)
            first = MediaItem.from_row(db.get_media(episode_ids[0]))
            _touched.update(episode_ids[:2])

            view = window.player
            view.play(first, start_at=max(0.0, (first.duration or 60) - 5.0))
            pump(2.5)
            view.mpv.set_property("mute", True)
            view._credits_at = None          # force the pure end-of-file path
            view._next_card_shown = False
            view.overlay.next_card.hide_quietly()

            deadline = time.time() + 45
            while time.time() < deadline:
                app.processEvents()
                time.sleep(0.01)
                current = view.current_item
                if current is not None and current.id == episode_ids[1]:
                    break

            current = view.current_item
            ok("running off the end starts the next episode",
               current is not None and current.id == episode_ids[1],
               current.display_title if current else "never advanced")
            pump(2.0)
            # mpv's --keep-open pauses at EOF and `pause` is global, so without
            # clearing it the next episode loads and just sits there.
            ok("the next episode plays instead of arriving paused",
               view.mpv.cached("pause") is False,
               f"pause={view.mpv.cached('pause')}")
            moved = view._position
            pump(2.0)
            ok("...and the clock is moving", view._position > moved + 0.4,
               f"{fmt_clock(moved)} -> {fmt_clock(view._position)}")

            print("\n-- episode controls --")
            overlay = view.overlay
            second = MediaItem.from_row(db.get_media(episode_ids[1]))
            view.play(second, start_at=30.0)
            pump(2.5)
            view.mpv.set_property("mute", True)
            # isVisible() is false whenever the control bar has auto-hidden;
            # isHidden() asks only whether the widget itself was hidden.
            ok("episodes get previous/next episode buttons",
               not overlay._prev_episode.isHidden()
               and not overlay._next_episode.isHidden())
            ok("autoplay toggle shown and reflecting the setting",
               not overlay._autoplay.isHidden() and overlay._autoplay.isChecked())
            overlay._autoplay.click(); pump(0.2)
            ok("the toggle writes the setting",
               settings.get("autoplay_next") is False)
            window.settings_page.reload()
            ok("the Settings page picks the change up",
               window.settings_page._autoplay.isChecked() is False)
            overlay._autoplay.click(); pump(0.2)

            overlay._prev_episode.click()
            pump(3.0)
            current = view.current_item
            ok("the previous-episode button goes back one",
               current is not None and current.id == episode_ids[0],
               current.display_title if current else "none")
            view.mpv.set_property("mute", True)
            overlay._next_episode.click()
            pump(3.0)
            current = view.current_item
            ok("the next-episode button goes forward one",
               current is not None and current.id == episode_ids[1],
               current.display_title if current else "none")
            ok("no previous episode before the first",
               db.previous_episode(episode_ids[0]) is None)

            view.stop_and_close(); pump(0.8)
            settings.set("autoplay_next", restore_autoplay)

    movies = db.movies()
    if movies:
        print("\n-- movie playback --")
        movie = MediaItem.from_row(movies[0])
        window.play(movie, start_at=600)
        pump(1.0)
        window.player.mpv.set_property("mute", True)
        pump(5.0)
        ok("movie plays", window.player._position > 601, fmt_clock(window.player._position))
        ok("hardware decoding",
           bool(window.player.mpv.command_sync("get_property", "hwdec-current").get("data")),
           str(window.player.mpv.command_sync("get_property", "hwdec-current").get("data")))
        ok("no dropped frames",
           (window.player.mpv.command_sync("get_property", "frame-drop-count").get("data") or 0) == 0)
        window.player.toggle_pause(); pump(0.7)
        ok("pause works", window.player._paused)
        window.player.toggle_pause(); pump(0.7)
        window.player.set_dialogue_boost(True); pump(0.6)
        boosted = window.player.mpv.command_sync("get_property", "af").get("data") or []
        window.player.set_dialogue_boost(False); pump(0.5)
        cleared = window.player.mpv.command_sync("get_property", "af").get("data") or []
        ok("dialogue boost toggles", len(boosted) == 1 and len(cleared) == 0)

        print("\n-- mouse --")
        overlay = window.player.overlay
        overlay_hwnd = int(overlay.winId())
        ratio = overlay.devicePixelRatioF()
        geo = overlay.geometry()
        centre = (geo.x() + geo.width() // 2, geo.y() + geo.height() // 2)
        _pin_on_top(int(window.winId()), True)
        _pin_on_top(overlay_hwnd, True)
        pump(0.5)

        overlay.wake(); pump(0.3)
        ok("the picture answers the mouse, not mpv",
           _window_under(*centre, ratio) == overlay_hwnd)
        overlay.hide_chrome(); pump(0.3)
        ok("...still, once the controls have hidden",
           _window_under(*centre, ratio) == overlay_hwnd)

        # The controls refuse to hide under a resting pointer, by design — so
        # these checks only mean something when your mouse is elsewhere.
        resting_on_chrome = (overlay._top.underMouse() or overlay._bottom.underMouse())
        if resting_on_chrome:
            print("  (your pointer is on the controls — skipping the auto-hide checks)")
        else:
            ok("pointer hides with the controls",
               overlay.cursor().shape() == Qt.CursorShape.BlankCursor)

            rest = QPoint(500, 500)
            overlay._wake_anchor = QPoint(rest)
            overlay.wake_on_movement(QPoint(rest.x() + 2, rest.y() + 1))
            ok("a mouse twitch does not wake the controls",
               not overlay._chrome_visible)
            overlay.wake_on_movement(QPoint(rest.x() + 80, rest.y() + 40))
            ok("a deliberate move brings them back", overlay._chrome_visible)
            ok("...and the pointer with them",
               overlay.cursor().shape() != Qt.CursorShape.BlankCursor)

        was_paused = window.player._paused
        _click(overlay, geo.width() // 2, geo.height() // 2)
        pump(0.8)
        ok("clicking the picture pauses", window.player._paused != was_paused)
        _click(overlay, geo.width() // 2, geo.height() // 2)
        pump(0.8)
        ok("clicking again resumes", window.player._paused == was_paused)

        _pin_on_top(overlay_hwnd, False)
        _pin_on_top(int(window.winId()), False)

        window.player.stop_and_close(); pump(1.0)
        _touched.add(movie.id)

        print("\n-- error handling --")
        ghost = MediaItem.from_row(db.get_media(movie.id))
        ghost.path = r"C:\definitely\not\here.mkv"
        ok("refuses to play a missing file", window.player.play(ghost) is False)
        db.execute("UPDATE media SET missing = 0 WHERE id = ?", (movie.id,))

    _music_checks(window, pump)

    print("\n-- search --")
    window._on_nav(4)
    ok("the search tab opens search, not a neighbour",
       window.stack.currentWidget() is window.search)
    window.search._search.setText("100%")
    pump(0.9)
    ok("wildcard search is escaped", window.search._grid._flow.count() == 0)

    print("\n-- vr --")
    targets = vr.detect(force=True)
    if targets:
        preferred = vr.preferred_target(targets)
        ok("targets detected", True, ", ".join(t.name for t in targets))
        ok("preferred target resolves", preferred is not None and preferred.available,
           preferred.name if preferred else "none")
    else:
        print("  (no VR software installed — nothing to check)")

    print("\n-- shutdown --")
    started = time.time()
    window.close()
    app.processEvents()
    elapsed = time.time() - started
    ok(f"closes cleanly in {elapsed:.2f}s", elapsed < 5)

    settings.set("scan_on_startup", restore_scan)
    _restore_progress(progress_snapshot, _touched)

    # The table must now match the snapshot for everything we touched.
    before = {int(r["media_id"]): (round(float(r["position"]), 1), int(r["watched"]))
              for r in progress_snapshot if int(r["media_id"]) in _touched}
    after = {int(r["media_id"]): (round(float(r["position"]), 1), int(r["watched"]))
             for r in db.query("SELECT * FROM progress")
             if int(r["media_id"]) in _touched}
    ok("watch history restored exactly as it was", before == after,
       f"{len(before)} row(s) restored")

    passed = sum(_results)
    print(f"\n{passed}/{len(_results)} passed  |  exceptions={len(ERRORS)}  "
          f"qt warnings={len(WARNINGS)}")
    for warning in WARNINGS[:5]:
        print("  qt:", warning)
    return 0 if passed == len(_results) and not ERRORS else 1


if __name__ == "__main__":
    raise SystemExit(main())
