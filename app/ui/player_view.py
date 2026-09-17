"""The playback page: mpv's window, the overlay chrome and progress bookkeeping."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .. import db
from ..config import settings
from ..models import MediaItem
from ..discord_presence import DiscordPresence, asset_key as discord_asset_key
from ..player.audio_filters import build_chain
from ..player.mpv_process import MpvProcess, MpvUnavailable, quality_preset
from ..util import fmt_clock
from .player_overlay import PlayerOverlay

_SAVE_INTERVAL_MS = 5000
_GEOMETRY_SYNC_MS = 350

# Settings that reach mpv only on its command line. The video mpv idles between
# films for the rest of the session, so Settings' "takes effect the next time
# playback starts" needs play() to restart it when one of these has changed.
# Quality is not here: it can be applied to a running mpv, and is.
_LAUNCH_SETTINGS = ("hwdec", "hdr_tone_mapping", "subs_on_by_default",
                    "preferred_audio_lang", "preferred_sub_lang")

# A negative subtitle id in the progress table means subtitles were off — hidden,
# or switched Off in the menu. Only an id that was on screen is stored as itself.
_SUBS_OFF = -1


def _subtitle_state_since() -> float:
    """When saved progress began to record whether subtitles were on screen.

    Rows saved before then hold a positive subtitle id whether subtitles were
    showing or not: mpv selects a track while they are hidden, and that id was
    saved. The table has no column that tells those rows apart from ones saved
    since, but their updated_at does, measured against this moment. It is taken
    the first time a player is built, before anything can be saved the new way.
    Other writes move updated_at too, so an old row marked watched or unwatched
    since, or opened and closed before mpv reported a position, reads as new:
    it resumes with subtitles on, as every old row did before this check, and
    hiding them once is what gets recorded.
    """
    try:
        since = float(settings.get("subtitle_state_since") or 0)
    except (TypeError, ValueError):      # edited by hand; better late than a crash
        since = 0.0
    if since <= 0:
        since = time.time()
        settings.set("subtitle_state_since", since)
    return since


class VideoSurface(QWidget):
    """The native window mpv draws into. Qt must not paint over it."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: #000000;")

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))


class PlayerView(QWidget):
    closed = Signal()
    playback_started = Signal()      # real video is on screen
    transition_requested = Signal(object, object)   # pixmap, source rect
    fullscreen_requested = Signal(bool)
    progress_changed = Signal()
    error = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._item: MediaItem | None = None
        self._duration = 0.0
        self._position = 0.0
        self._paused = False
        # Paused by the sleep timer, with a line on the picture saying so,
        # until you press play again.
        self._sleep_paused = False
        self._fullscreen = False
        self._ending = False
        self._pending_start = 0.0
        self._last_saved = 0.0
        # True from a successful play() until the player is closed. Everything
        # that can arrive late — the Up Next countdown, mpv's queued events, the
        # presence timer — checks it, so nothing starts or publishes a title
        # after you have left the player.
        self._active = False
        # Between play() and the new file's file-loaded, mpv is still delivering
        # what it said about the previous file: its time-pos, its eof-reached.
        self._awaiting_file = False
        self._launched_with: tuple | None = None
        self._quality_applied = ""
        self._subtitle_state_since = _subtitle_state_since()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.surface = VideoSurface(self)
        layout.addWidget(self.surface)

        self._closing = False

        # TV state, reset per file: the show's intro/credits model and what has
        # already fired this playback.
        self._show_id: int | None = None
        self._intro: tuple[float, float] | None = None
        self._credits_at: float | None = None
        self._intro_skipped = False
        self._next_card_shown = False
        self._next_card_declined = False

        self.mpv = MpvProcess(self)
        self.mpv.property_changed.connect(self._on_property)
        self.mpv.file_loaded.connect(self._on_file_loaded)
        self.mpv.end_file.connect(self._on_end_file)
        self.mpv.log_message.connect(self._on_log)
        self.mpv.exited.connect(self._on_mpv_exited)
        # Fires once decoding is actually producing frames, which is the moment
        # it is safe to take a cover off the picture.
        self.mpv.playback_restart.connect(self.playback_started.emit)

        self.overlay = PlayerOverlay(owner=self)
        self.overlay.play_pause.connect(self.toggle_pause)
        self.overlay.seek_relative.connect(lambda s: self.mpv.seek(s))
        self.overlay.seek_absolute.connect(self.mpv.seek_absolute)
        self.overlay.volume_changed.connect(self._on_volume_changed)
        self.overlay.mute_toggled.connect(lambda: self.mpv.command("cycle", "mute"))
        self.overlay.audio_track_selected.connect(self.mpv.set_audio_track)
        self.overlay.sub_track_selected.connect(self.mpv.set_subtitle_track)
        self.overlay.speed_selected.connect(self._on_speed)
        self.overlay.quality_selected.connect(self._on_quality)
        self.overlay.chapter_selected.connect(self._on_chapter)
        self.overlay.boost_toggled.connect(self.set_dialogue_boost)
        self.overlay.fullscreen_toggled.connect(self.toggle_fullscreen)
        self.overlay.close_requested.connect(self.stop_and_close)
        self.overlay.next_requested.connect(lambda: self._advance_to_next(False))
        self.overlay.next_from_card.connect(lambda: self._advance_to_next(True))
        self.overlay.previous_requested.connect(self._go_to_previous)
        self.overlay.autoplay_toggled.connect(self._on_autoplay_toggled)
        self.overlay.skip_intro.connect(self.skip_intro)
        self.overlay.next_cancelled.connect(self._on_next_declined)
        self.overlay.tv_action.connect(self._on_tv_action)

        self.presence = DiscordPresence(str(settings.get("discord_client_id", "")))
        if settings.get("discord_presence"):
            self.presence.start()

        self._save_timer = QTimer(self)
        self._save_timer.setInterval(_SAVE_INTERVAL_MS)
        self._save_timer.timeout.connect(lambda: self.save_progress())

        self._geometry_timer = QTimer(self)
        self._geometry_timer.setInterval(_GEOMETRY_SYNC_MS)
        self._geometry_timer.timeout.connect(self._sync_overlay)

        # Presence waits a moment after the file opens, until the duration is
        # known. A member rather than a singleShot, so closing can cancel it.
        self._presence_timer = QTimer(self)
        self._presence_timer.setSingleShot(True)
        self._presence_timer.setInterval(1200)
        self._presence_timer.timeout.connect(self._publish_presence)
        # A seek (the bar, the arrows, Skip intro) moves the end Discord counts
        # down to, and was never sent: mpv restarts playback after every seek,
        # so publish again once the new position has come in.
        self.mpv.playback_restart.connect(self._presence_timer.start)

        # "Opening…" only for a file that is slow to open (a disk spinning up, a
        # network share). A local file is loaded within a few hundred ms, under
        # the cover the library animates in; shown at once, the label flashed
        # over that cover on every play.
        self._opening_timer = QTimer(self)
        self._opening_timer.setSingleShot(True)
        self._opening_timer.setInterval(1000)
        self._opening_timer.timeout.connect(self._show_opening)

    # --- playback -----------------------------------------------------------

    def play(self, item: MediaItem, start_at: float | None = None) -> bool:
        """Load a file. Returns False if it cannot be played."""
        self.save_progress()

        # The library is a cache: a file can be moved or deleted after a scan.
        if not Path(item.path).is_file():
            message = f"{Path(item.path).name} is no longer on disk."
            self.error.emit(message)
            db.update_media(item.id, missing=1)
            if self._active:
                # Moving on from inside the player (Up Next, N): the top bar that
                # shows errors is hidden here, and what's playing carries on.
                self.overlay.show_message(message, 6000)
                self.overlay.wake()
            return False

        # Before any state changes, so a player that cannot start leaves the
        # current title exactly as it was.
        if not self._ensure_mpv():
            return False

        self._item = item
        self._ending = False
        self._duration = item.duration or 0.0
        self._position = 0.0
        self._pending_start = item.resume_position if start_at is None else max(0.0, start_at)

        self.overlay.set_title(
            item.title or item.path,
            item.code if item.is_episode else (str(item.year) if item.year else ""),
        )
        self.overlay.set_duration(self._duration)
        self.overlay.set_position(self._pending_start)
        self.overlay.set_paused(False)
        self.overlay.set_chapters([])
        self.overlay.set_tracks([], [])
        self.overlay.set_thumbnails(item.thumbs)
        self.overlay.set_boost(bool(settings.get("dialogue_boost")))
        self.overlay.set_episode_nav(
            item.is_episode,
            bool(item.is_episode and db.previous_episode(item.id)),
            bool(item.is_episode and db.next_episode(item.id)),
        )
        self.overlay.set_autoplay(bool(settings.get("autoplay_next", True)))
        self.overlay.set_quality(
            quality_preset(settings.get("video_quality", "balanced")), ""
        )

        # Reset TV state for the new file and load the show's model.
        self._intro_skipped = False
        self._next_card_shown = False
        self._next_card_declined = False
        self.overlay.next_card.hide_quietly()
        self.overlay.show_skip_pill(False)
        self._show_id = item.show_id if item.is_episode else None
        self._load_tv_model()

        self.overlay.clear_message()
        self._opening_timer.start()

        # Read before bump_play_count, which creates the progress row.
        options = self._track_options(item)
        self._awaiting_file = True
        self.mpv.load(item.path, start_at=self._pending_start, options=options)
        db.bump_play_count(item.id)
        self._active = True
        self._save_timer.start()
        self._geometry_timer.start()
        self._sync_overlay()
        self.overlay.show()
        self.overlay.wake()
        return True

    def _launch_settings(self) -> tuple:
        return tuple(settings.get(key) for key in _LAUNCH_SETTINGS)

    def _ensure_mpv(self) -> bool:
        if self.mpv.is_running:
            if self._active or self._launch_settings() == self._launched_with:
                self._apply_quality_setting()
                return True
            # Changed in Settings since mpv started, and nothing is on screen:
            # an idle mpv quits in a moment, and a fresh one reads them all.
            self.mpv.terminate()
        self.surface.show()
        window_id = int(self.surface.winId())
        try:
            self.mpv.start(window_id)
        except MpvUnavailable as exc:
            self.error.emit(str(exc))
            return False
        self._launched_with = self._launch_settings()
        self._quality_applied = quality_preset(settings.get("video_quality", "balanced"))
        self.mpv.set_volume(int(settings.get("volume", 80)))
        self._apply_boost(bool(settings.get("dialogue_boost")))
        return True

    def _apply_quality_setting(self) -> None:
        """Quality chosen in Settings since the last film reaches the running mpv."""
        wanted = quality_preset(settings.get("video_quality", "balanced"))
        if wanted != self._quality_applied:
            self._quality_applied = self.mpv.set_quality(wanted)

    def _track_options(self, item: MediaItem) -> dict[str, str]:
        """Which audio and subtitles this file opens with, as per-file options.

        A title you have played before gets back exactly what was on when you
        left it; an episode you haven't gets what you last chose for its show.
        Handing these to loadfile, rather than setting tracks once file-loaded
        arrives, means mpv picks them while opening the file — nothing depends
        on which of file-loaded or track-list is delivered first, and mpv
        matches 'jpn' to 'ja' itself (both spellings are in the library).
        """
        options: dict[str, str] = {}
        saved = db.get_progress(item.id)
        if saved is not None and (saved["audio_id"] or saved["sub_id"]):
            if saved["audio_id"]:
                options["aid"] = str(int(saved["audio_id"]))
            sub_id = int(saved["sub_id"] or 0)
            if sub_id > 0:
                options["sid"] = str(sub_id)
                if (saved["updated_at"] or 0) >= self._subtitle_state_since:
                    options["sub-visibility"] = "yes"
                else:
                    # Saved by an earlier version, which stored this id with
                    # subtitles hidden too. It still says which track, but not
                    # whether it was showing. Moving on inside the player (N, Up
                    # Next) the show's own choice was saved a moment ago from
                    # what was on screen, so that decides; opened from the
                    # library, "Turn subtitles on by default" does. Passed here
                    # rather than left to mpv, where S pressed (or a track
                    # picked) in an earlier film could still be in effect. The
                    # next save records which it was.
                    subs_on = None
                    if self._active and item.is_episode and item.show_id is not None:
                        subs_on = db.show_prefs(item.show_id).get("subs_on")
                    if subs_on is None:
                        subs_on = settings.get("subs_on_by_default")
                    options["sub-visibility"] = "yes" if subs_on else "no"
            elif sub_id < 0:
                options["sub-visibility"] = "no"
            return options
        if not item.is_episode or item.show_id is None:
            return options
        prefs = db.show_prefs(item.show_id)
        subs_on = prefs.get("subs_on")
        if subs_on is not None:
            options["sub-visibility"] = "yes" if subs_on else "no"
        if subs_on and prefs.get("sub_lang"):
            options["slang"] = str(prefs["sub_lang"])
        if prefs.get("audio_lang"):
            options["alang"] = str(prefs["audio_lang"])
        return options

    def _show_opening(self) -> None:
        if self._active and self._awaiting_file and self._item is not None:
            self.overlay.show_message(f"Opening {self._item.title}…")

    def _on_file_loaded(self) -> None:
        self._awaiting_file = False
        if not self._active:
            return
        self._opening_timer.stop()
        self.overlay.clear_message()
        self._apply_boost(bool(settings.get("dialogue_boost")))
        self._presence_timer.start()        # once duration is known

    # --- TV: intro / credits / subtitle memory -------------------------------

    def _load_tv_model(self) -> None:
        """Resolve the intro window and credits point for the current episode."""
        self._intro = None
        self._credits_at = None
        if self._show_id is None:
            self.overlay.set_tv_state(False, False, False, False)
            return

        prefs = db.show_prefs(self._show_id)
        item = self._item

        # Intro: the fingerprinted per-episode window wins (cold opens move it
        # around); the show-level manual marker is the fallback.
        if item is not None and item.intro_end and item.intro_end > 0:
            self._intro = (float(item.intro_start or 0.0), float(item.intro_end))
        else:
            start, end = prefs.get("intro_start"), prefs.get("intro_end")
            if end is not None and end > 0:
                self._intro = (float(start or 0.0), float(end))

        # Credits: several sources may know a point; take the earliest credible
        # one (a late marker means sitting through most of the credits).
        duration = self._duration or (item.duration if item else 0) or 0
        candidates: list[float] = []
        if item is not None and item.credits_at:
            candidates.append(float(item.credits_at))
        credits_len = prefs.get("credits_len")
        if credits_len and duration:
            candidates.append(max(0.0, duration - float(credits_len)))
        if duration:
            candidates = [c for c in candidates if duration - 300 <= c <= duration - 8]
        self._credits_at = min(candidates) if candidates else None

        self.overlay.set_tv_state(
            True, self._intro is not None, credits_len is not None,
            bool(settings.get("auto_skip_intro", True)),
        )

    def _credits_from_chapters(self) -> float | None:
        """Fallback: a final chapter starting in the last four minutes is credits."""
        if not self._duration:
            return None
        chapters = self.mpv.chapters()
        if len(chapters) < 2:
            return None
        last = float(chapters[-1].get("time") or 0)
        if self._duration - 240 <= last < self._duration - 15:
            return last
        return None

    def _remember_show_tracks(self) -> None:
        """Persist subtitle/audio choices so the next episode matches."""
        if self._show_id is None or not self.mpv.is_running:
            return
        visible = self.mpv.cached("sub-visibility")
        subs = self.mpv.tracks("sub")
        sub_lang = next((t.get("lang") for t in subs if t.get("selected")), None)
        audio_lang = next((t.get("lang") for t in self.mpv.tracks("audio")
                           if t.get("selected")), None)
        fields: dict = {}
        if visible is not None and subs:
            # Off in the subtitle menu leaves sub-visibility on with no track
            # selected; that is still "no subtitles" for the next episode. An
            # episode with no subtitles at all says nothing either way.
            selected = any(t.get("selected") for t in subs)
            fields["subs_on"] = int(bool(visible) and selected)
        if sub_lang:
            fields["sub_lang"] = str(sub_lang)
        if audio_lang:
            fields["audio_lang"] = str(audio_lang)
        if fields:
            db.set_show_prefs(self._show_id, **fields)

    def skip_intro(self) -> None:
        if self._intro is None:
            return
        self._intro_skipped = True
        self.overlay.show_skip_pill(False)
        self.mpv.seek_absolute(self._intro[1])

    def _on_tv_action(self, action: str) -> None:
        if self._show_id is None:
            return
        position = self._position
        if action == "skip_now":
            self.skip_intro()
        elif action == "auto_toggle":
            settings.set("auto_skip_intro", not bool(settings.get("auto_skip_intro", True)))
        elif action == "intro_start":
            db.set_show_prefs(self._show_id, intro_start=max(0.0, position))
        elif action == "intro_end":
            db.set_show_prefs(self._show_id, intro_end=max(1.0, position))
            self._intro_skipped = True     # you are already past it right now
        elif action == "intro_clear":
            db.set_show_prefs(self._show_id, intro_start=None, intro_end=None)
        elif action == "credits_here":
            duration = self._duration or 0
            if duration > 0 and position < duration:
                db.set_show_prefs(self._show_id, credits_len=duration - position)
        elif action == "credits_clear":
            db.set_show_prefs(self._show_id, credits_len=None)
        self._load_tv_model()
        self.overlay.wake()

    def _tv_tick(self, position: float) -> None:
        """Runs on every position update; drives the pill and the Up Next card."""
        if self._show_id is None:
            return

        if self._intro is not None:
            start, end = self._intro
            in_window = start <= position < end - 1.0
            # Auto-skip only when playback flows into the window on its own
            # (position just past its start, first time). A deliberate seek back
            # into the intro gets the pill, never a forced jump.
            if (in_window and not self._intro_skipped
                    and position - start < 4.0
                    and settings.get("auto_skip_intro", True)):
                self.skip_intro()
                return
            self.overlay.show_skip_pill(in_window)
        else:
            self.overlay.show_skip_pill(False)

        if (not self._next_card_shown and not self._next_card_declined
                and self._duration > 0):
            candidates = [c for c in (self._credits_at, self._credits_from_chapters())
                          if c is not None]
            credits_at = min(candidates) if candidates else None
            if credits_at is not None and position >= credits_at:
                self._offer_next_episode()

    def _offer_next_episode(self) -> None:
        row = db.next_episode(self._item.id) if self._item else None
        if row is None:
            return
        self._next_card_shown = True
        nxt = MediaItem.from_row(db.get_media(int(row["id"])))
        countdown = 8 if settings.get("autoplay_next", True) else None
        self.overlay.present_next_card(
            nxt.display_title, countdown, nxt.wide_art or nxt.art or ""
        )
        if self._paused and not self.mpv.cached("eof-reached"):
            # Seeking into the credits while paused: the countdown waits too.
            self.overlay.next_card.hold(True)

    def _on_next_declined(self) -> None:
        self._next_card_declined = True

    def _on_autoplay_toggled(self, enabled: bool) -> None:
        settings.set("autoplay_next", bool(enabled))
        self.overlay.wake()

    def _advance_to_next(self, mark_watched: bool = False) -> None:
        """Move to the next episode.

        `mark_watched` only when the episode actually ran to its end — reaching
        for the next-episode button ten minutes in doesn't mean you watched it.
        """
        # The Up Next countdown is a timer: it can run out after you've left.
        if not self._active or self._item is None:
            return
        finished = self._item
        row = db.next_episode(finished.id)
        if row is None and not mark_watched:
            return      # N on a finale does what its greyed-out button does
        # Save first and mark after. play() saves the outgoing episode too, and
        # when the card came up before the watched threshold (credits at 90%)
        # that save wrote a fresh mark straight back to unfinished.
        self.save_progress()
        position, self._position = self._position, 0.0     # saved: no second save
        if row is None:
            db.set_watched(finished.id, True)     # only reachable from the card
            self.progress_changed.emit()
            self.stop_and_close()
            return
        # Hand the Up Next still over before the card disappears, so the next
        # episode grows out of it instead of cutting through black.
        pixmap, rect = self.overlay.next_card_snapshot()
        # No start point: a next episode you are part-way through resumes.
        if not self.play(MediaItem.from_row(db.get_media(int(row["id"])))):
            # Its file has gone, and play() said so over the picture. This one
            # plays on, unmarked; its own end offers whatever comes after (the
            # missing file is marked now, so that skips it).
            self._position = position
            if mark_watched and self._ending:
                # Up Next on the held last frame: that end has already been, so
                # offer the episode after the missing one straight away.
                self._ending = False
                self._on_end_file("eof")
            return
        if mark_watched:
            db.set_watched(finished.id, True)
            self.progress_changed.emit()
        if not pixmap.isNull():
            self.transition_requested.emit(pixmap, rect)

    def _go_to_previous(self) -> None:
        if not self._active or self._item is None:
            return
        row = db.previous_episode(self._item.id)
        if row is None:
            return
        self.play(MediaItem.from_row(db.get_media(int(row["id"]))), start_at=0.0)

    def _on_end_file(self, reason: str) -> None:
        if reason not in ("eof", "error"):
            return
        item = self._item
        if not self._active or item is None or self._ending:
            return
        self._ending = True

        if reason == "error":
            self._opening_timer.stop()      # file-loaded will never come
            self.overlay.show_message("This file could not be played.")
            self.overlay.wake()
            return

        db.set_watched(item.id, True)
        self.progress_changed.emit()

        row = db.next_episode(item.id) if item.is_episode else None
        if row is not None and not self._next_card_declined:
            if self.overlay.next_card.isVisible():
                # --keep-open paused on the last frame, which is not you
                # pausing: a countdown held for that pause carries on.
                self.overlay.next_card.hold(False)
                return          # the card is already up, waiting or counting down
            # No credits marker fired, so nothing has been offered yet. Offer it
            # over the held last frame — counting down only if autoplay is on,
            # otherwise waiting for a click.
            self._next_card_shown = True
            nxt = MediaItem.from_row(db.get_media(int(row["id"])))
            countdown = 5 if settings.get("autoplay_next", True) else None
            self.overlay.present_next_card(
                nxt.display_title, countdown, nxt.wide_art or nxt.art or ""
            )
            self.overlay.wake()
            return
        self.stop_and_close()

    def toggle_pause(self) -> None:
        self.mpv.toggle_pause()
        self.overlay.wake()

    def _end_session(self) -> None:
        """This playback is over: save it, then forget it.

        The title stayed in memory after closing, and the next play() or quit
        saved it again from there — undoing a Mark watched (or unwatched) made in
        the library meanwhile. And the Up Next card kept counting down behind a
        hidden overlay, then started the next episode with nobody in the player.
        """
        self.save_progress()
        self._active = False
        self._awaiting_file = False
        self._save_timer.stop()
        self._geometry_timer.stop()
        self._presence_timer.stop()
        self._opening_timer.stop()
        self.overlay.next_card.hide_quietly()
        self.overlay.show_skip_pill(False)
        self.overlay.clear_message()
        self._sleep_paused = False
        self._next_card_shown = False
        self._next_card_declined = False
        self._item = None
        self._show_id = None
        self._position = 0.0
        self._duration = 0.0

    def stop_and_close(self) -> None:
        self._end_session()
        self.mpv.stop()
        self.presence.clear()
        self.overlay.hide()
        if self._fullscreen:
            self.toggle_fullscreen()
        self.closed.emit()

    def shutdown(self) -> None:
        self._closing = True
        self.presence.clear()
        self.presence.stop()
        self._end_session()
        self.overlay.hide()
        self.mpv.terminate()

    # --- discord presence ---------------------------------------------------

    def apply_presence_settings(self) -> None:
        """Re-read the Discord settings after they're changed in Settings."""
        client_id = str(settings.get("discord_client_id", ""))
        wanted = bool(settings.get("discord_presence")) and bool(client_id.strip())
        if client_id != self.presence.client_id:
            self.presence.stop()
            self.presence = DiscordPresence(client_id)
        if wanted:
            self.presence.start()
            self._publish_presence()
        else:
            self.presence.clear()
            self.presence.stop()

    def _publish_presence(self) -> None:
        """Push what's on screen to Discord. Never allowed to disturb playback."""
        item = self._item
        # Only while a film is actually in the player. A changed Discord setting
        # or a publish still on its timer used to bring back the last film
        # watched, hours after it was closed.
        if not self._active or item is None or not self.presence.enabled:
            return
        try:
            if settings.get("discord_hide_titles"):
                self.presence.set_watching(
                    "Watching something", "", None, self._paused, "Mistery"
                )
                return
            if item.is_episode:
                show = db.get_show(item.show_id) if item.show_id else None
                title = (show["title"] if show else item.title) or item.title
                subtitle = f"{item.code} · {item.title}".strip(" ·")
            else:
                title = item.title
                subtitle = str(item.year) if item.year else ""
            remaining = max(0.0, (self._duration or 0) - self._position)
            # The poster is per show / per film, so the key comes from that
            # title rather than the episode's. Falls back to the app icon when
            # nothing matching has been uploaded.
            self.presence.set_watching(
                title, subtitle, remaining, self._paused, title,
                image_key=discord_asset_key(title),
            )
        except Exception:
            pass

    def _on_mpv_exited(self, code: int) -> None:
        """mpv died on its own — don't leave the player sitting on a dead frame."""
        # The signal is queued from mpv's pipe thread, so it can arrive after
        # play() has already replaced that mpv with a fresh one (see _ensure_mpv).
        if self._closing or not self._active or self.mpv.is_running:
            return
        self.save_progress()
        self.error.emit(
            f"The video player stopped unexpectedly (exit {code}). Returning to the library."
        )
        self.stop_and_close()

    # --- state --------------------------------------------------------------

    def _on_property(self, name: str, value) -> None:
        if name in ("time-pos", "eof-reached") and (self._awaiting_file or not self._active):
            # Events cross from mpv's pipe thread in a queue, so ones mpv sent
            # about the previous file still arrive after play() has moved on —
            # measured: its time-pos lands after the loadfile, every time. Taken
            # as the new episode's, a position in the old one's credits put up
            # the new one's Up Next card, and the countdown skipped an episode.
            # mpv's stream is ordered, so the new file's file-loaded comes after.
            return
        if name == "time-pos" and value is not None:
            self._position = float(value)
            self.overlay.set_position(self._position)
            self._tv_tick(self._position)
        elif name == "duration" and value:
            self._duration = float(value)
            self.overlay.set_duration(self._duration)
            self._load_tv_model()          # credits point depends on duration
        elif name == "pause":
            self._paused = bool(value)
            self.overlay.set_paused(self._paused)
            if not self._paused and self._sleep_paused:
                self._sleep_paused = False
                self.overlay.clear_message()
            self._publish_presence()
            if self._paused:
                self.overlay.wake()
            # Pausing holds an Up Next countdown. mpv also pauses by itself on
            # the last frame (--keep-open), which is not you asking to wait;
            # _on_end_file lets the countdown go on if that pause got here first.
            if not (self._paused and self.mpv.cached("eof-reached")):
                self.overlay.next_card.hold(self._paused)
        elif name in ("volume", "mute"):
            self.overlay.set_volume(
                float(self.mpv.cached("volume", 0) or 0), bool(self.mpv.cached("mute"))
            )
        elif name == "track-list":
            self.overlay.set_tracks(self.mpv.tracks("audio"), self.mpv.tracks("sub"))
        elif name == "chapter-list":
            self.overlay.set_chapters(self.mpv.chapters())
        elif name == "speed" and value:
            self.overlay.set_speed(float(value))
        elif name == "sub-visibility":
            self.overlay.set_subs_active(bool(value))
        elif name.startswith("video-params/"):
            # The source resolution only becomes known once decoding starts.
            self.overlay.set_quality(
                quality_preset(settings.get("video_quality", "balanced")),
                self.mpv.video_size(),
            )
        elif name == "eof-reached" and value:
            # --keep-open=yes means mpv holds the last frame instead of ending
            # the file, so no end-file event ever arrives for a normal finish.
            # This property is the only signal that playback actually ran out.
            self._on_end_file("eof")

    def _on_log(self, text: str) -> None:
        if "error" in text.lower():
            self.error.emit(text)

    def _on_volume_changed(self, value: int) -> None:
        self.mpv.set_volume(value)
        settings.set("volume", int(value))

    def _on_speed(self, speed: float) -> None:
        self.mpv.set_speed(speed)
        self.overlay.set_speed(speed)

    def _on_quality(self, name: str) -> None:
        """Apply the preset to the running player and remember it."""
        applied = self.mpv.set_quality(name)
        self._quality_applied = applied
        settings.set("video_quality", applied)
        self.overlay.set_quality(applied, self.mpv.video_size())
        self.overlay.wake()

    def _on_chapter(self, index: int) -> None:
        self.mpv.set_property("chapter", index)
        self.overlay.wake()

    def set_dialogue_boost(self, enabled: bool) -> None:
        settings.set("dialogue_boost", bool(enabled))
        self._apply_boost(bool(enabled))
        self.overlay.wake()

    def _apply_boost(self, enabled: bool) -> None:
        self.mpv.set_audio_filters(build_chain(dialogue_boost=enabled))

    # --- progress -----------------------------------------------------------

    def save_progress(self) -> None:
        item = self._item
        if item is None or self._position <= 0:
            return
        self._remember_show_tracks()
        duration = self._duration or item.duration or 0.0
        threshold = float(settings.get("watched_threshold", 0.92))
        watched = bool(duration and self._position >= duration * threshold)

        tracks = self.mpv.tracks("audio")
        audio_id = next((t.get("id") for t in tracks if t.get("selected")), None)
        subs = self.mpv.tracks("sub")
        sub_id = next((t.get("id") for t in subs if t.get("selected")), None)
        if subs and (sub_id is None or not self.mpv.cached("sub-visibility")):
            # mpv selects a subtitle track even while subtitles are hidden, so
            # saving that id alone turned them on at every resume. And None
            # (Off in the menu) is what set_progress reads as "keep the old id".
            sub_id = _SUBS_OFF

        db.set_progress(
            item.id,
            position=0.0 if watched else self._position,
            duration=duration,
            watched=watched,
            audio_id=audio_id,
            sub_id=sub_id,
        )
        self._last_saved = time.monotonic()
        self.progress_changed.emit()

    # --- geometry -----------------------------------------------------------

    def _sync_overlay(self) -> None:
        """Keep the overlay window exactly on top of the video surface."""
        if not self.isVisible() or not self.surface.isVisible():
            return
        try:
            top_left = self.surface.mapToGlobal(QPoint(0, 0))
            target = QRect(top_left, self.surface.size())
            if self.overlay.geometry() != target:
                self.overlay.setGeometry(target)
            if not self.overlay.isVisible():
                self.overlay.show()
            self.overlay.raise_()
        except RuntimeError:
            self._geometry_timer.stop()      # overlay is gone; stop chasing it

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_overlay()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_overlay()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        # The overlay is a separate top-level window, so on teardown Qt can
        # destroy it before this widget stops receiving events.
        try:
            self.overlay.hide()
        except RuntimeError:
            pass

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        self.overlay.set_fullscreen(self._fullscreen)
        self.fullscreen_requested.emit(self._fullscreen)
        QTimer.singleShot(120, self._sync_overlay)

    @property
    def is_fullscreen(self) -> bool:
        return self._fullscreen

    @property
    def current_item(self) -> MediaItem | None:
        return self._item

    def pause_for_sleep(self) -> bool:
        """The sleep timer ran out during a film: pause it, and say why.

        Otherwise it looked like the film had stopped by itself, or like a
        fault. The line stays on the picture until play is pressed again.
        Returns whether anything was paused.
        """
        if not self._active or self._item is None or self._paused:
            return False
        self._sleep_paused = True
        self.mpv.pause()
        self.overlay.show_message("Paused by the sleep timer")
        self.overlay.wake()
        return True

    # --- keyboard (routed from the main window) -----------------------------

    def handle_key(self, event) -> bool:
        key = event.key()
        step = int(settings.get("seek_step", 10))
        modifiers = event.modifiers()

        if key == Qt.Key.Key_Space or key == Qt.Key.Key_K:
            self.toggle_pause()
        elif key == Qt.Key.Key_Left:
            self.mpv.seek(-1 if modifiers & Qt.KeyboardModifier.ShiftModifier else -step)
        elif key == Qt.Key.Key_Right:
            self.mpv.seek(1 if modifiers & Qt.KeyboardModifier.ShiftModifier else step)
        elif key == Qt.Key.Key_J:
            self.mpv.seek(-step * 3)
        elif key == Qt.Key.Key_L:
            self.mpv.seek(step * 3)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            # 0 is a volume, not a missing one: `or 80` made Down at silence 75.
            volume = self.mpv.cached("volume")
            volume = settings.get("volume", 80) if volume is None else volume
            delta = 5 if key == Qt.Key.Key_Up else -5
            # Through the slider's handler, so the keys are remembered as well.
            self._on_volume_changed(max(0, min(150, int(round(float(volume))) + delta)))
        elif key == Qt.Key.Key_M:
            self.mpv.command("cycle", "mute")
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_S:
            self.mpv.command("cycle", "sub-visibility")
        elif key == Qt.Key.Key_B:
            self.set_dialogue_boost(not bool(settings.get("dialogue_boost")))
            self.overlay.set_boost(bool(settings.get("dialogue_boost")))
        elif key == Qt.Key.Key_PageUp:
            self.mpv.previous_chapter()
        elif key == Qt.Key.Key_PageDown:
            self.mpv.next_chapter()
        elif key == Qt.Key.Key_I:
            self.skip_intro()
        elif key == Qt.Key.Key_N:
            if self._item is not None and self._item.is_episode:
                self._advance_to_next(False)
            else:
                return False
        elif key == Qt.Key.Key_P:
            if self._item is not None and self._item.is_episode:
                self._go_to_previous()
            else:
                return False
        elif key == Qt.Key.Key_Period:
            self.mpv.command("frame-step")
        elif key == Qt.Key.Key_Comma:
            self.mpv.command("frame-back-step")
        elif key == Qt.Key.Key_Escape:
            if self._fullscreen:
                self.toggle_fullscreen()
            else:
                self.stop_and_close()
        else:
            return False

        self.overlay.wake()
        return True
