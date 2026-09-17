"""The music player: an audio-only mpv, a queue, and the rules around them.

It is a second mpv process, separate from the one that plays video, so music
keeps going while you browse and a film can pause it without tearing it down.

Gapless playback is the reason the queue is mirrored into mpv's own playlist
rather than loading one file at a time: mpv can only join two tracks without a
gap if it already has the next one open. So mpv's playlist always holds the
current track and everything after it, and this class keeps the full queue —
history included — as the source of truth.

The current track is accepted only when mpv's `path` and `playlist-pos` agree
with each other, re-checked on either event: they arrive separately and in no
fixed order, and a rebuild can leave events for the previous song in the pipe.
Changes are announced by song identity, never by queue position — the same
position in a new queue is a different song.

Around that core sit four smaller jobs, each explained where it lives: the
session file that brings the queue back paused at the next start, and hands
it to a paused mpv so the media keys can resume it (save_session /
restore_session / _preload_restored), the sleep timer and its fade
(set_sleep_timer), the output device (set_audio_device), and liked songs
(set_liked).
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from ..config import find_mpv, settings, subprocess_flags
from ..player.mpv_process import MpvProcess, MpvUnavailable
from . import audio_fx, library, loudness, session
from .shuffle import smart_shuffle

_log = logging.getLogger("music")

_RESTART_THRESHOLD = 3.0        # "previous" past this point restarts the track
_NOTHING_PLAYED = ("Nothing in the queue could be played. "
                   "The files may have moved, or there is no audio output.")
# What "Playing from" says for songs queued one at a time onto an empty player.
_QUEUE_CONTEXT = {"kind": "queue", "title": "Your queue", "id": None}

# The sleep timer's fade. mpv's volume control is cubic (gain = (volume/100)^3),
# so a straight line on it is already a perceptual fade: halfway through, the
# music is 18 dB down, not 6, and the last seconds drift out rather than drop.
# Stepped every 50 ms; measured at volume 70, 129 steps over the 8 s, none more
# than 0.7 of a point, and never louder on the way down.
_SLEEP_FADE = 8.0
_SLEEP_FADE_STEP_MS = 50
# Before the fade, the timer only wakes this often to re-read the clock. A seek
# or a new song re-plans it at once; this is for the estimate drifting.
_SLEEP_CHECK_MAX = 15.0
# How far the position clock may disagree with mpv before it counts as a jump
# (a seek from the media overlay) rather than the clock being corrected.
_CLOCK_SLACK = 1.5
# After the timer pauses, mpv's volume goes back to the user's level only once
# the pause has reached the audio device, so no tail end is heard at full level.
_SLEEP_RESTORE_MS = 300

# Session saves: soon after anything changes, and every 15 s while playing so a
# crash or a power cut loses at most that much of the position.
_SESSION_DEBOUNCE_MS = 2000
_SESSION_HEARTBEAT_MS = 15000
# A restored session goes to a paused mpv this long after it is put back: out
# of the way of the window's first paint, and still soon enough for a media key
# pressed just after startup.
_PRELOAD_DELAY_MS = 1500

# `mpv --audio-device=help` prints one device per line:  'wasapi/{guid}' (Speakers (Realtek))
_DEVICE_LINE = re.compile(r"^\s*'(?P<name>[^']+)'\s+\((?P<description>.*)\)\s*$")
_DEVICE_TIMEOUT = 1.0

# The same mpv warning is logged at most this often (seconds).
_MPV_LOG_REPEAT = 60.0
# mpv audio-output warnings that say nothing is wrong. Every time the output
# opens, WASAPI can't raise its thread to the "Pro Audio" scheduling class
# (not registered on most PCs) and the music plays exactly the same.
_MPV_LOG_HARMLESS = ("av thread to pro audio",)
# A saved position this close to the end of its song is taken as the song
# finished: opened there, paused, mpv ran off the end within a moment of
# starting and the bar moved on to the next song with nobody touching it.
_RESTORE_END_MARGIN = 2.0

class AudioMpv(MpvProcess):
    """mpv with no window, tuned for albums instead of films."""

    def observed_properties(self) -> list[str]:
        # eof-reached only changes at the end of a file; the sleep timer's
        # "end of track" hears the song finish from it (see _check_sleep_end).
        return ["path", "playlist-pos", "time-pos", "duration", "pause", "volume",
                "mute", "idle-active", "eof-reached", "audio-params/samplerate",
                "audio-params/format"]

    def _base_arguments(self, window_id: int | None) -> list[str]:
        return [
            f"--input-ipc-server={self._ipc_path}",
            "--idle=yes",
            "--no-config",
            "--load-scripts=no",
            "--really-quiet",
            "--msg-level=all=warn",
            "--no-input-default-bindings",
            "--input-vo-keyboard=no",
            "--ytdl=no",
            "--save-position-on-quit=no",
            # No window, ever: embedded cover art is a video stream to mpv.
            "--force-window=no",
            "--vid=no",
            "--audio-display=no",
            # Join tracks without a gap; 'weak' still reopens the output when two
            # files differ in format rather than resampling one to match.
            "--gapless-audio=weak",
            "--prefetch-playlist=yes",
            # Levelling is done by Mistery, not by mpv: it measures the files
            # itself (music/loudness.py) and hands mpv a gain per file, so the
            # albums with no ReplayGain tags are levelled too.
            "--replaygain=no",
            "--keep-open=no",
            f"--volume={int(settings.get('music_volume', 70))}",
            "--volume-max=100",
            f"--audio-device={_device_setting()}",
            "--audio-client-name=Mistery",
            "--title=Mistery",
            # Registers with Windows' System Media Transport Controls: the media
            # keys on a keyboard work while a game has focus, and the Windows
            # media overlay shows the song. The whole point when Mistery is only
            # a tray icon.
            "--media-controls=yes",
        ]


def _as_dict(row) -> dict:
    return dict(row) if not isinstance(row, dict) else row


def _device_setting() -> str:
    return str(settings.get("music_audio_device", "auto") or "auto")


def _devices_from_help() -> list[tuple[str, str]] | None:
    """The output devices, asked of a throwaway mpv that prints them and exits.

    Used when the player's own mpv isn't running, so listing devices never
    starts one: an idle mpv registers with Windows' media controls and would
    show an empty "Mistery" in the media overlay. Measured at 126 ms.
    """
    mpv_path = find_mpv()
    if not mpv_path:
        return None
    try:
        completed = subprocess.run([mpv_path, "--no-config", "--audio-device=help"],
                                   capture_output=True, timeout=_DEVICE_TIMEOUT,
                                   creationflags=subprocess_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    devices = []
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        match = _DEVICE_LINE.match(line)
        if match:
            devices.append((match.group("name"), match.group("description")))
    return devices or None


class MusicPlayer(QObject):
    track_changed = Signal(object)        # dict or None
    track_updated = Signal(object)        # a queue entry's fields changed in place (liked)
    state_changed = Signal()              # playing / paused / shuffle / repeat
    position_changed = Signal(float, float)
    queue_changed = Signal()
    sound_changed = Signal()       # volume matching or an effect changed
    volume_changed = Signal(int)
    sleep_timer_changed = Signal()        # started, cancelled, fired or retargeted
    sleep_timer_fired = Signal()          # it ran out and the music was paused
    error = Signal(str)
    _started_in_background = Signal(object)   # a preload's mpv, back from its thread

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mpv: AudioMpv | None = None
        self._queue: list[dict] = []
        self._original: list[dict] = []   # the order before shuffling
        self._index = -1
        self._announced: tuple | None = None  # (id, path) of the last track announced
        self._base = 0                    # queue index at mpv playlist position 0
        self._playing = False
        self._position = 0.0
        self._duration = 0.0
        self._counted = False             # has this play been counted yet
        # A "pass" is mpv working through the playlist one rebuild gave it,
        # until it goes idle; what the pass has seen decides what that idle
        # means (see _on_idle).
        self._pass_busy = False           # mpv has been busy since the rebuild
        self._pass_played = False         # ...and a song in it played (to its end, or until skipped)
        self._file_open = False           # the pass has a file open right now
        self._last_end = ""               # why mpv's last file ended (eof, stop, error...)
        self._loading = False             # rebuilt, and no file of it has opened yet
        self._queue_ended = False         # the last pass ran off the end of the queue
        self._seek_pending = False        # a seek of ours has not restarted playback yet
        self._in_order = True             # an album front to back
        self._context: dict | None = None  # where the queue came from ("Playing from")
        self._shuffle = bool(settings.get("music_shuffle", False))
        self._repeat = str(settings.get("music_repeat", "off"))
        self._closing = False
        self._low_power = False
        # In background mode the position isn't streamed; it's asked for this
        # often instead, which is all play counting and Discord need.
        self._position_poll = QTimer(self)
        self._position_poll.setInterval(5000)
        self._position_poll.timeout.connect(self._poll_position)

        # The position as a clock: where the song was at a moment, running on
        # from there while it plays. Polled every 5 s in the tray, the position
        # alone is too stale to fade out on or to save.
        self._clock_position = 0.0
        self._clock_stamp = time.monotonic()

        # Resume: a restored queue is paused with no mpv behind it, until a
        # moment later it is handed to a paused mpv (see _preload_restored).
        self._restored = False
        self._resume_at = 0.0             # where the next rebuild of this song starts
        # (queue index, position) mpv was given paused, while nobody has played it.
        self._preload: tuple[int, float] | None = None
        self._preload_timer = QTimer(self)
        self._preload_timer.setSingleShot(True)
        self._preload_timer.setInterval(_PRELOAD_DELAY_MS)
        self._preload_timer.timeout.connect(self._preload_restored)
        # A preload's mpv while it starts on its own thread: (mpv, thread, the
        # device it was started on, the device it plays on once started, or
        # nothing if it failed).
        self._starting: tuple[AudioMpv, threading.Thread, str, list[str]] | None = None
        self._started_in_background.connect(self._on_started_in_background)

        # Sleep timer.
        self._sleep_mode: str | None = None   # minutes | track | queue
        self._sleep_deadline = 0.0            # monotonic, minutes mode
        self._sleep_hold = False              # the timer ended the queue: its idle must not start it over
        self._fade = 1.0                      # share of the user's volume mpv is playing at
        self._sent_volume: float | None = None
        self._sleep_clock = QTimer(self)
        self._sleep_clock.setSingleShot(True)
        self._sleep_clock.setTimerType(Qt.TimerType.PreciseTimer)
        self._sleep_clock.timeout.connect(self._sleep_update)
        self._volume_restore = QTimer(self)
        self._volume_restore.setSingleShot(True)
        self._volume_restore.setInterval(_SLEEP_RESTORE_MS)
        self._volume_restore.timeout.connect(self._restore_volume_after_sleep)

        # Output device: what mpv was last told, which is the setting unless
        # that device was missing and mpv fell back to the system default.
        self._device_in_use = "auto"
        # (index, position, counted, paused) at the pass's first failure on a
        # chosen device.
        self._device_error: tuple[int, float, bool, bool] | None = None
        # ...and that device was confirmed gone then: until the pass goes idle
        # and is restarted on the default, mpv is only failing the rest of the
        # queue, and none of what it reports about those songs is shown.
        self._device_lost = False
        # When each distinct mpv audio-output warning was last logged.
        self._mpv_log_seen: dict[str, float] = {}

        # Session file.
        self._session_last: dict | None = None
        self._session_debounce = QTimer(self)
        self._session_debounce.setSingleShot(True)
        self._session_debounce.setInterval(_SESSION_DEBOUNCE_MS)
        self._session_debounce.timeout.connect(self._save_session_if_changed)
        self._session_heartbeat = QTimer(self)
        self._session_heartbeat.setInterval(_SESSION_HEARTBEAT_MS)
        self._session_heartbeat.timeout.connect(self._save_session_if_changed)
        self.queue_changed.connect(self._on_own_queue_changed)
        self.track_changed.connect(self._on_own_track_changed)
        self.state_changed.connect(self._on_own_state_changed)

    # --- state ---------------------------------------------------------------

    @property
    def current(self) -> dict | None:
        return self._queue[self._index] if 0 <= self._index < len(self._queue) else None

    @property
    def queue(self) -> list[dict]:
        return list(self._queue)

    @property
    def index(self) -> int:
        return self._index

    @property
    def upcoming(self) -> list[dict]:
        return self._queue[self._index + 1:] if self._index >= 0 else []

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def has_queue(self) -> bool:
        return bool(self._queue)

    @property
    def is_idle(self) -> bool:
        """Nothing loaded in mpv: the queue ran out, was stopped, or never
        started. Not playing and not idle is a paused song. A restored session
        is idle until mpv is given it paused a moment later, and a paused song
        from then on; `restored` tells it apart either way."""
        mpv = self._mpv
        return mpv is None or not mpv.is_running or bool(mpv.cached("idle-active", False))

    @property
    def position(self) -> float:
        return self._position

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def repeat(self) -> str:
        return self._repeat

    @property
    def context(self) -> dict | None:
        """Where the queue came from: {"kind", "title", "id"}, or None. Set by
        play_tracks / shuffle_tracks and kept until the next one."""
        return dict(self._context) if self._context else None

    @property
    def restored(self) -> bool:
        """The queue on show came from the last session and hasn't played yet."""
        return self._restored

    @property
    def match_mode(self) -> str:
        """Album levelling keeps an album's own quiet and loud songs as the
        band intended and moves the whole record; track levelling evens out a
        mix of records. Which one is right depends on what you are playing, so
        it follows the queue: an album in order gets album levelling, a shuffle
        gets track levelling. ReplayGain was designed to be used this way."""
        return "album" if self._in_order and not self._shuffle else "track"

    @property
    def normalize(self) -> str:
        return str(settings.get("music_normalize", "loud"))

    def set_normalize(self, value: str) -> None:
        settings.set("music_normalize", str(value))
        self.apply_sound()

    def apply_sound(self) -> None:
        """Put the current sound settings on the song playing and the ones queued.

        The song playing is changed in place, so a switch is heard immediately;
        the rest of mpv's playlist is rewritten, because each entry carries the
        chain it was queued with.
        """
        mpv = self._mpv
        if mpv is None or not mpv.is_running:
            return
        current = self.current
        mpv.set_property("af", self._chain_for(current) if current else "")
        if self._index >= 0:
            self._resync_upcoming()
        self.sound_changed.emit()

    def _chain_for(self, item: dict | None) -> str:
        """The filter chain for one song: its levelling gain, then the effects."""
        if not item:
            return ""
        album_mode = self.match_mode == "album"
        target = loudness.target_db(self.normalize)
        level = item.get("album_loudness") if album_mode else item.get("loudness")
        # A peak for the whole album keeps every song on it at one gain; the
        # song's own peak is the honest answer for whether it needs a limiter.
        ceiling = item.get("album_peak") if album_mode else item.get("peak")
        tag = item.get("rg_album") if album_mode else item.get("rg_track")
        if level is None:                      # not measured yet: fall back
            level, ceiling = item.get("loudness"), item.get("peak")
        gain = loudness.gain_for(level, ceiling, target, tag)
        return audio_fx.chain(None, gain, item.get("peak"))

    def _file_options(self, item: dict, start: float = 0.0) -> str:
        """mpv per-file options, in mpv's length-escaped form.

        The option list is comma separated and a filter graph is full of commas,
        so the value is given as %<bytes>%<value>. Every entry carries one, even
        an empty one: without it a song would inherit whatever chain was set
        live for the song before it, gain and all.

        `start` opens the file at that point instead of seeking after it opens,
        so a resumed song is never heard from 0 first.
        """
        chain = self._chain_for(item)
        options = f"af=%{len(chain.encode('utf-8'))}%{chain}" if chain else "af="
        if start > 0:
            offset = f"+{start:.3f}"
            options += f",start=%{len(offset)}%{offset}"
        return options

    # --- engine --------------------------------------------------------------

    def _ensure_mpv(self) -> bool:
        # mpv is wanted now, so a preload still to come would only be in the way.
        self._preload_timer.stop()
        if self._starting is not None:
            # A preload's mpv is starting on its thread: wait for it rather
            # than start a second one beside it.
            self._adopt_started()
        if self._mpv is not None and self._mpv.is_running:
            return True
        if self._mpv is not None:
            # Dead, but its `exited` has not been handled yet. Its process may
            # even outlive a broken pipe, and a dropped handle to a live mpv is
            # a second song playing that nothing can pause.
            self._mpv.terminate()
        mpv = self._new_mpv()
        self._mpv = mpv
        try:
            self._device_in_use = self._start_mpv(mpv, _device_setting())
        except MpvUnavailable as exc:
            self.error.emit(str(exc))
            self._mpv = None
            return False
        self._sent_volume = float(self.volume)      # what --volume started it at
        if self._fade < 1.0:
            self._apply_volume()
        self._apply_power_mode()        # a restart while in the tray stays quiet
        return True

    def _new_mpv(self) -> AudioMpv:
        mpv = AudioMpv(self)
        # Every slot is tied to this instance. The pump's signals are queued, so
        # a dead mpv's `exited` could land after its replacement was started
        # and throw the new one's handle away, leaving it playing out of reach.
        mpv.property_changed.connect(self._for(mpv, self._on_property))
        mpv.file_loaded.connect(self._for(mpv, self._on_file_loaded))
        mpv.end_file.connect(self._for(mpv, self._on_end_file))
        mpv.playback_restart.connect(self._for(mpv, self._on_playback_restart))
        mpv.exited.connect(self._for(mpv, self._on_exited))
        mpv.log_message.connect(self._on_mpv_log)
        return mpv

    def _on_mpv_log(self, text: str) -> None:
        """mpv's warnings and errors: the audio output's go into Mistery's log.

        What mpv says about the output device is the only record of what a real
        unplug looked like to it, and it was thrown away. Everything else it
        warns about (a mistagged file, a skipped frame) stays at debug level.
        The same line is written at most once a minute, so a device that keeps
        failing can't fill the log.
        """
        prefix = text[1:text.find("]")] if text.startswith("[") and "]" in text else ""
        lowered = text.lower()
        if any(harmless in lowered for harmless in _MPV_LOG_HARMLESS) or not (
                prefix.startswith("ao") or "audio device" in lowered or "audio output" in lowered):
            _log.debug("mpv: %s", text)
            return
        now = time.monotonic()
        seen = self._mpv_log_seen
        if now - seen.get(text, -_MPV_LOG_REPEAT) < _MPV_LOG_REPEAT:
            return
        if len(seen) > 100:
            seen.clear()
        seen[text] = now
        _log.warning("mpv: %s", text)

    @staticmethod
    def _start_mpv(mpv: AudioMpv, device: str) -> str:
        """Start `mpv` on `device`; returns the device it really plays on.

        This is all the waiting in starting mpv (~150 ms measured, the process
        and its pipe), and it touches nothing of the player's, so a preload can
        run it on a thread of its own. Raises MpvUnavailable.
        """
        mpv.start(None)
        if device != "auto" and not MusicPlayer._device_present(mpv, device):
            # A USB DAC or headset that isn't plugged in. mpv doesn't fall back
            # by itself: every song ends in an error and nothing plays at all.
            _log.warning("audio device %s is not connected; playing on the system default", device)
            mpv.set_property("audio-device", "auto")
            return "auto"
        return device

    def _for(self, mpv: AudioMpv, slot):
        """`slot`, called only while `mpv` is still the player's mpv."""
        return lambda *args: slot(*args) if mpv is self._mpv else None

    def _apply_loop_modes(self) -> None:
        """Repeat one and the sleep timer's stop, as mpv options.

        "End of track" sets keep-open=always: mpv then pauses when the song
        finishes instead of moving on, so it stops at the true end of the song.
        Measured on a gapless 24/96 FLAC album: mpv held the first song at
        189.76 s of 189.83 s, once the audio already sent to the device had
        played, and the second song's position never moved off 0. Anything that
        listens for the end instead is already too late there, because gapless
        playback has begun the next song by the time an event about the old one
        arrives.

        While a track or queue timer is set, Repeat one stops looping, or the
        song it waits for would never end.
        """
        mpv = self._mpv
        if mpv is None or not mpv.is_running:
            return
        waiting_for_end = self._sleep_mode in ("track", "queue")
        mpv.set_property("loop-file", "inf" if self._repeat == "one" and not waiting_for_end else "no")
        mpv.set_property("keep-open", "always" if self._sleep_mode == "track" else "no")

    # --- background playback ---------------------------------------------------

    @property
    def low_power(self) -> bool:
        return self._low_power

    def set_low_power(self, enabled: bool) -> None:
        """Keep playing while asking as little of the machine as possible.

        With a window, mpv streams the playback position ~15 times a second
        for seek bars and lyrics. With no window nothing needs that, so the
        stream is switched off and the pipe is allowed to doze; the position is
        fetched every few seconds instead. Songs changing, pausing and the end
        of the queue are still reported the moment they happen.
        """
        enabled = bool(enabled)
        if enabled == self._low_power:
            return
        self._low_power = enabled
        self._apply_power_mode()

    def _apply_power_mode(self) -> None:
        mpv = self._mpv
        if mpv is None or not mpv.is_running:
            self._position_poll.stop()
            return
        if self._low_power:
            mpv.unobserve_property("time-pos")
            mpv.max_idle_wait = 0.1
            self._position_poll.start()
        else:
            # A restored session's preload streams nothing either until it
            # plays, and a media key heard 100 ms late is soon enough: paused,
            # the pump used 94-125 ms of CPU in 40 s at 20 ms, 16-31 ms at 100.
            # _refresh_playing puts the window's pace back once it plays.
            mpv.max_idle_wait = 0.1 if self._restored else 0.02
            self._position_poll.stop()
            if not mpv.is_observing("time-pos"):
                mpv.observe_property("time-pos")
            self._poll_position()       # catch the interface up at once

    def _poll_position(self) -> None:
        mpv = self._mpv
        if mpv is None or not mpv.is_running or self._loading or self._device_lost:
            return
        reply = mpv.command_sync("get_property", "time-pos", timeout=1.0)
        if reply.get("error") == "success" and reply.get("data") is not None:
            # Up to a whole poll interval since the last position: a loop may
            # have started any time in it. (The catch-up poll on leaving the
            # tray follows a poll too, so the same allowance holds for it.)
            self._update_position(float(reply["data"]),
                                  since_start=self._position_poll.interval() / 1000.0 + 0.5)

    def _on_exited(self, code: int) -> None:
        if self._closing:
            return
        _log.warning("music mpv exited (%s); it will restart on next play", code)
        self._anchor(self._estimated_position())
        if self._restored:
            # Never played: Play opens the song at the point on show, as it
            # would have before mpv was started for it.
            self._resume_at = self._clock_position
        self._preload = None
        self._mpv = None
        self._device_error = None       # its pass is over; the next start checks the device again
        self._device_lost = False
        self._playing = False
        self.state_changed.emit()

    # --- playing ---------------------------------------------------------------

    def play_tracks(self, tracks: list, start: int = 0, in_order: bool = True,
                    context: dict | None = None) -> None:
        """Replace the queue. `in_order` means an album or list played as laid out.

        `context` says where the songs came from, for "Playing from":
        {"kind": "album" | "artist" | "songs" | "liked" | "search" | "queue",
        "title": str, "id": int | str | None}.
        """
        items = [_as_dict(t) for t in tracks if (_as_dict(t).get("state") or "ready") == "ready"]
        if not items:
            return
        start_item = _as_dict(tracks[start]) if 0 <= start < len(tracks) else items[0]
        start = next((i for i, t in enumerate(items) if t["id"] == start_item.get("id")), 0)

        self._original = list(items)
        self._in_order = in_order
        self._context = dict(context) if isinstance(context, dict) else None
        self._restored = False
        if self._shuffle:
            first = items[start]
            rest = [t for i, t in enumerate(items) if i != start]
            items = [first] + smart_shuffle(rest, lambda t: t.get("album_id"))
            start = 0
        self._queue = items
        self._rebuild(start)
        self.queue_changed.emit()

    def shuffle_tracks(self, tracks: list, context: dict | None = None) -> None:
        """The Shuffle button: turn shuffle on and start somewhere random."""
        items = [_as_dict(t) for t in tracks if (_as_dict(t).get("state") or "ready") == "ready"]
        if not items:
            return
        self.set_shuffle(True, rebuild=False)
        self._original = list(items)
        self._in_order = False
        self._context = dict(context) if isinstance(context, dict) else None
        self._restored = False
        self._queue = smart_shuffle(items, lambda t: t.get("album_id"))
        self._rebuild(0)
        self.queue_changed.emit()

    def _rebuild(self, start: int, resume_at: float = 0.0, *, same_song: bool = False,
                 paused: bool = False) -> None:
        """Give mpv the queue from `start`, and play.

        With `resume_at`, or `same_song` for a song picked up at 0:00, the
        song on show carries on: not announced again, play count kept.
        `paused` gives mpv the queue without playing it: a restored session's
        preload (see _preload_restored).
        """
        if not self._ensure_mpv() or not (0 <= start < len(self._queue)):
            return
        mpv = self._mpv
        self._base = start
        # A new pass. If mpv is idle now, the pass has not really begun until it
        # says otherwise: its idle-active=true for the old state can still be in
        # the pipe (a fresh mpv's reply to being observed always is).
        self._pass_busy = not mpv.cached("idle-active", True)
        self._pass_played = False
        self._file_open = False
        self._last_end = ""
        if not paused:
            # Not for a preload, which starts nothing: a session saved after
            # its queue ran out has still ended, and Play moves on to the songs
            # added since, as it does with no mpv behind the restored queue. A
            # media key can only resume the song mpv holds, the one on show.
            self._queue_ended = False
        self._seek_pending = False
        self._sleep_hold = False
        self._device_error = None
        self._device_lost = False
        self._resume_at = 0.0
        self._preload = None
        # Until a file of this rebuild opens, time-pos events are the old song's.
        # Taken for the new one, a jump from past the old song's halfway point
        # counted the new song as played before a second of it was heard.
        self._loading = True
        item = self._queue[start]
        resuming = ((resume_at > 0 or same_song) and start == self._index
                    and self._announced == (item.get("id"), item.get("path")))
        if resuming:
            # The song on show, picked up where it was left: already announced,
            # and its position and play count stay as they are.
            self._set_index(start)
            self._position = resume_at
            self._anchor(resume_at)
        else:
            # Forced: a rebuild always starts a song, even at the same queue
            # position as before — Play on one album, then Play on another, both
            # start at 0, and comparing positions made the second one invisible.
            self._set_index(start, force=True)
        self._apply_loop_modes()
        opened_at = resume_at if resuming else 0.0
        if paused:
            # Before the file is opened, so none of it is played: mpv opens it
            # at `start` and holds it there. Measured, its first time-pos was
            # already the saved second, and time-pos stayed on it until Play.
            mpv.set_property("pause", True)
        mpv.command("loadfile", item["path"], "replace", 0, self._file_options(item, opened_at))
        for entry in self._queue[start + 1:]:
            mpv.command("loadfile", entry["path"], "append", 0, self._file_options(entry))
        if paused:
            # Nothing changed that anyone can see, so nothing is announced; if
            # this mpv goes before the song is played, Play opens it here again.
            self._preload = (start, opened_at)
            self._resume_at = opened_at
            return
        mpv.set_property("pause", False)
        # No optimistic "playing = True": mpv's own pause/idle events say when
        # it really starts, and guessing ahead of them made the button flicker.
        self.state_changed.emit()

    def _resync_upcoming(self) -> None:
        """mpv's playlist after the current track, rewritten to match the queue."""
        if self._mpv is None or not self._mpv.is_running or self._index < 0:
            return
        self._base = self._index
        self._mpv.command("playlist-clear")        # keeps only the playing entry
        for item in self._queue[self._index + 1:]:
            self._mpv.command("loadfile", item["path"], "append", 0, self._file_options(item))

    def _set_index(self, index: int, force: bool = False) -> None:
        """Move to a queue entry, announcing it if it is a different song.

        Identity, not position: the same position in a new queue is a new song.
        """
        self._index = index
        item = self.current
        identity = (item.get("id"), item.get("path")) if item else None
        if force or identity != self._announced:
            self._announced = identity
            self._position = 0.0
            self._anchor(0.0)
            self._counted = False
            self._resume_at = 0.0
            self.track_changed.emit(item)

    def toggle_pause(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def play(self) -> None:
        if not self._queue:
            return
        start = max(0, self._index)
        resume_at = self._resume_at
        if self._queue_ended and start + 1 < len(self._queue):
            # The queue played to its end and songs were added since: those are
            # what Play means, not the last song over again.
            start += 1
            resume_at = 0.0
        if self._sleep_mode is None and self._fade < 1.0:
            # Play straight after the sleep timer paused: full volume now, not
            # a moment into the song.
            self._volume_restore.stop()
            self._set_fade(1.0)
        mpv = self._mpv
        if self._preload is not None and mpv is not None and mpv.is_running:
            # The restored song is open in mpv already, paused at its point,
            # even if mpv has not finished opening it (then idle-active still
            # says idle, and opening it a second time is what that used to
            # cause). Only a queue that had ended, with songs added since,
            # means another song.
            if start == self._index:
                self._preload = None
                mpv.set_property("pause", False)
            else:
                self._rebuild(start, resume_at)
            return
        if mpv is None or not mpv.is_running or mpv.cached("idle-active"):
            self._rebuild(start, resume_at)
            return
        mpv.set_property("pause", False)

    def pause(self) -> None:
        if self._mpv is not None and self._mpv.is_running:
            self._mpv.set_property("pause", True)

    def next(self) -> None:
        if not self._queue:
            return
        if self._index + 1 < len(self._queue):
            # Not on a preload: mpv would move on paused, and Next on a
            # restored queue plays the next song, as it did with no mpv behind it.
            if (self._mpv is not None and self._mpv.is_running and not self._mpv.cached("idle-active")
                    and self._preload is None):
                self._mpv.command("playlist-next", "force")
            else:
                self._rebuild(self._index + 1)
        elif self._repeat == "all":
            self._rebuild(0)

    def previous(self) -> None:
        if not self._queue:
            return
        if self._position > _RESTART_THRESHOLD or self._index <= 0:
            self.seek(0)
            return
        self._rebuild(self._index - 1)

    def jump_to(self, index: int) -> None:
        if 0 <= index < len(self._queue):
            self._rebuild(index)

    def seek(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        mpv = self._mpv
        if self._preload is not None and self._loading and mpv is not None and mpv.is_running:
            # mpv refuses a seek until the file is open ("error running
            # command", measured), and the preloaded song is still opening:
            # it is opened again at the new point, still paused.
            self._rebuild(self._index, seconds, same_song=True, paused=True)
        elif mpv is not None and mpv.is_running:
            mpv.command("seek", seconds, "absolute", "exact")
            self._seek_pending = True
        elif self.current is not None:
            # Nothing loaded (a restored session): remember it for Play, which
            # opens the song at this point.
            self._resume_at = seconds
        else:
            return
        if self._restored:
            self._schedule_session_save()
        self._position = seconds
        self._anchor(seconds)
        self.position_changed.emit(self._position, self._duration)
        if self._sleep_mode in ("track", "queue"):
            self._sleep_update()

    def set_volume(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        changed = value != self.volume
        settings.set("music_volume", value)
        self._sent_volume = None        # always tell mpv, as before
        self._apply_volume()
        if changed:
            # The bar and Now Playing each have a slider; without this the one
            # not touched kept its old level, and nudging it jumped back to it.
            self.volume_changed.emit(value)

    @property
    def volume(self) -> int:
        return int(settings.get("music_volume", 70))

    def _apply_volume(self) -> None:
        """mpv's volume: the user's level, times the sleep timer's fade.

        The fade only ever touches mpv, never the setting, so the level saved
        and shown on the sliders is the one the music comes back at. Moving a
        slider mid-fade moves the level the fade is taken from.
        """
        mpv = self._mpv
        if mpv is None or not mpv.is_running:
            return
        value = round(self.volume * self._fade, 1)
        if value != self._sent_volume:
            mpv.set_property("volume", value)
            self._sent_volume = value

    # --- queue editing ----------------------------------------------------------

    def play_next(self, track) -> None:
        item = _as_dict(track)
        if not self._queue:
            self.play_tracks([item], context=dict(_QUEUE_CONTEXT))
            return
        self._queue.insert(self._index + 1, item)
        # Straight after the current song in the unshuffled order too, or turning
        # shuffle off would send it to the back of the queue.
        current = self.current
        after = next((i + 1 for i, t in enumerate(self._original) if t is current), len(self._original))
        self._original.insert(after, item)
        if self._mpv is not None and self._mpv.is_running:
            self._mpv.command("loadfile", item["path"], "append", 0, self._file_options(item))
            last = len(self._queue) - 1 - self._base
            self._mpv.command("playlist-move", last, self._index + 1 - self._base)
        self.queue_changed.emit()

    def add_to_queue(self, track) -> None:
        item = _as_dict(track)
        if not self._queue:
            self.play_tracks([item], context=dict(_QUEUE_CONTEXT))
            return
        self._queue.append(item)
        self._original.append(item)
        if self._mpv is not None and self._mpv.is_running:
            self._mpv.command("loadfile", item["path"], "append", 0, self._file_options(item))
        self.queue_changed.emit()

    def remove_upcoming(self, index: int) -> None:
        if index <= self._index or index >= len(self._queue):
            return
        item = self._queue.pop(index)
        # From the unshuffled order as well: turning shuffle off rebuilds Up next
        # from it, and a removed song used to come straight back.
        for i, t in enumerate(self._original):
            if t is item:
                del self._original[i]
                break
        self._resync_upcoming()
        self.queue_changed.emit()

    def set_shuffle(self, enabled: bool, rebuild: bool = True) -> None:
        enabled = bool(enabled)
        if enabled == self._shuffle:
            return
        self._shuffle = enabled
        settings.set("music_shuffle", enabled)
        if rebuild and self._index >= 0:
            current = self.current
            if enabled:
                rest = [t for t in self._queue[self._index + 1:]]
                self._queue[self._index + 1:] = smart_shuffle(rest, lambda t: t.get("album_id"))
            else:
                # Back to the original order, picking up after the current track:
                # that very entry if it is there (a "Play next" can repeat a song
                # that is also further down the album), else the same song.
                position = next((i for i, t in enumerate(self._original) if t is current), -1)
                if position < 0:
                    position = next((i for i, t in enumerate(self._original)
                                     if t["id"] == current["id"]), -1)
                self._queue[self._index + 1:] = self._original[position + 1:]
            # Shuffling also switches album levelling to track levelling, so the
            # song playing needs its gain recomputed, not just the queue.
            self.apply_sound()
            self.queue_changed.emit()
        self.state_changed.emit()

    def cycle_repeat(self) -> str:
        order = ["off", "all", "one"]
        self._repeat = order[(order.index(self._repeat) + 1) % 3] if self._repeat in order else "off"
        settings.set("music_repeat", self._repeat)
        self._apply_loop_modes()
        self.state_changed.emit()
        return self._repeat

    # --- liked songs --------------------------------------------------------------

    def set_liked(self, track, liked: bool) -> None:
        """Like or unlike a song, everywhere it is on show.

        The same song can be in the queue more than once and in the unshuffled
        order as well, as separate dicts; each one is updated in place so the
        heart doesn't flip back when the song comes round again or shuffle is
        turned off. `track_updated` then fires once, with the entry that is
        playing if it is that song, else a queue entry of it, else the row
        passed in: the caller's dict, updated in place, or for a sqlite Row
        (which can't be changed) a dict copy of it with the new values.
        """
        item = _as_dict(track)
        try:
            track_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            return
        liked = bool(liked)
        try:
            stored_at = library.set_liked(track_id, liked)
        except Exception:
            _log.exception("could not save a liked song")
            return
        # The library's own time, so every copy sorts as Liked songs does.
        liked_at = stored_at if isinstance(stored_at, (int, float)) else time.time()

        def mark(entry: dict) -> None:
            entry["liked"] = 1 if liked else 0
            entry["liked_at"] = liked_at if liked else None

        updated: dict | None = None
        seen: set[int] = set()
        for entry in (*self._queue, *self._original):
            if id(entry) in seen or entry.get("id") != track_id:
                continue
            seen.add(id(entry))
            mark(entry)
            updated = updated or entry
        if id(item) not in seen:
            # The caller's row, not a queue entry. A dict is theirs and changes
            # in place; a Row became a fresh copy above, and marking only dicts
            # sent that copy out with the old value, so a heart fed library
            # rows never changed.
            mark(item)
        current = self.current
        if current is not None and current.get("id") == track_id:
            updated = current
        self.track_updated.emit(updated if updated is not None else item)

    # --- output device ------------------------------------------------------------

    @property
    def audio_device(self) -> str:
        """The chosen output device: an mpv device name, or "auto" for Windows' default."""
        return _device_setting()

    def audio_devices(self) -> list[tuple[str, str]]:
        """(mpv device name, description) for every output, System default first.

        Asked of the running mpv when there is one (15 ms, measured), otherwise
        of a throwaway mpv that lists them and exits (126 ms); either is capped
        at a second. Bare driver names such as 'openal' are left out: each is
        that driver's own default device, the same speakers as System default
        under a second, confusing name.
        """
        entries: list[tuple[str, str]] | None = None
        mpv = self._mpv
        if mpv is not None and mpv.is_running:
            reply = mpv.command_sync("get_property", "audio-device-list", timeout=_DEVICE_TIMEOUT)
            if reply.get("error") == "success" and isinstance(reply.get("data"), list):
                entries = [(str(d.get("name") or ""), str(d.get("description") or ""))
                           for d in reply["data"] if isinstance(d, dict)]
        if entries is None:
            entries = _devices_from_help() or []
        devices = [("auto", "System default")]
        for name, description in entries:
            if name and name != "auto" and "/" in name:
                devices.append((name, description or name))
        return devices

    def set_audio_device(self, name: str) -> None:
        """Choose the output device, saved and applied at once.

        mpv reopens its audio output on the new device and carries on from the
        same point; measured, the song kept playing through a switch to a
        device and back. The setting is also passed when mpv starts.
        """
        name = str(name or "auto")
        if name != _device_setting():
            settings.set("music_audio_device", name)
        mpv = self._mpv
        if mpv is not None and mpv.is_running and name != self._device_in_use:
            mpv.set_property("audio-device", name)
            self._device_in_use = name
            if self._device_error is not None:
                # Picked while a device loss was being held back: the rest of
                # the pass may play on this one, so it is no longer the old
                # device's failure to recover from, and what mpv is on shows.
                self._device_error = None
                self._device_lost = False
                self._resolve_current()
                self._refresh_playing()

    @staticmethod
    def _device_present(mpv: AudioMpv, name: str) -> bool:
        """Is `name` among mpv's output devices? True when mpv can't say."""
        reply = mpv.command_sync("get_property", "audio-device-list", timeout=_DEVICE_TIMEOUT)
        if reply.get("error") != "success" or not isinstance(reply.get("data"), list):
            return True
        return any(isinstance(d, dict) and d.get("name") == name for d in reply["data"])

    def _recover_audio_device(self) -> bool:
        """The pass failed on a chosen device that has gone: carry on on the default.

        Unplugging a USB headset mid-song ends the song in an error, and every
        song after it fails the same way at once, so the music just stopped.
        This runs once mpv has gone idle, after the last of those failures, so
        none of their events can land on top of the new start; the song that
        failed first starts again from where it was. Only the running mpv is
        switched: the setting stays, so the device is used again next time it
        is there. True when it restarted the music, or put it back paused on
        the default when the song was paused as the device went.

        Quietly, when the loss was confirmed at the first failure (see
        _on_end_file): the failing songs were never announced, so the song on
        show simply carries on. Otherwise mpv's list may have lagged the
        failure and those songs were announced; the first one is announced
        again. Either way its play count is put back as it was: announcing a
        song starts its count over, and a song already counted past its
        halfway point was counted a second time.
        """
        failed_at, self._device_error = self._device_error, None
        confirmed, self._device_lost = self._device_lost, False
        mpv = self._mpv
        if failed_at is None or self._device_in_use == "auto":
            return False
        if mpv is None or not mpv.is_running:
            _log.warning("songs failed on audio device %s and mpv has gone; not recovering",
                         self._device_in_use)
            return False
        if not confirmed and self._device_present(mpv, self._device_in_use):
            _log.warning("songs failed on audio device %s, which is still connected: "
                         "the files' own failure, not a lost device", self._device_in_use)
            return False
        index, position, counted, paused = failed_at
        _log.warning("audio device %s went away; %s on the system default at %.1f s",
                     self._device_in_use, "paused" if paused else "playing", position)
        mpv.set_property("audio-device", "auto")
        self._device_in_use = "auto"
        if not 0 <= index < len(self._queue):
            return False
        self._set_index(index)          # a no-op when the failures were held back
        # Paused when it went, paused now: headphones unplugged from a paused
        # song started it on the speakers half a second later (measured).
        self._rebuild(index, position, same_song=True, paused=paused)
        self._counted = counted
        return True

    # --- sleep timer ----------------------------------------------------------------

    @property
    def sleep_mode(self) -> str | None:
        return self._sleep_mode

    @property
    def sleep_remaining(self) -> float | None:
        """Seconds until the sleep timer stops the music, or None with no timer.

        Minutes run on the wall clock, paused or not. "Track" is what is left
        of the song playing and "queue" what is left of the whole queue, so both
        stand still while the music is paused.
        """
        mode = self._sleep_mode
        if mode is None:
            return None
        if mode == "minutes":
            return max(0.0, self._sleep_deadline - time.monotonic())
        if self.current is None:
            return 0.0
        remaining = max(0.0, self._track_length() - self._estimated_position())
        if mode == "queue":
            remaining += sum(float(t.get("duration") or 0.0) for t in self._queue[self._index + 1:])
        return remaining

    def set_sleep_timer(self, minutes: float | None = None, *, end_of: str | None = None) -> None:
        """Stop the music after `minutes`, or at the end of the "track" or "queue".

        The last ~8 s fade out, then the music pauses and mpv's volume goes back
        to the user's level. Setting a timer replaces any timer already set. An
        end-of timer with nothing loaded has nothing to wait for and is ignored.

        It works the same from the tray, where the position isn't streamed:
        minutes run on a timer of their own, the fade on the position clock,
        and the end of a song or of the queue arrives as an mpv event.
        """
        if (minutes is None) == (end_of is None):
            raise ValueError("set_sleep_timer takes minutes or end_of, not both or neither")
        if minutes is not None:
            minutes = float(minutes)
            if not math.isfinite(minutes) or minutes <= 0:
                raise ValueError(f"sleep timer minutes must be positive, not {minutes}")
            mode = "minutes"
        else:
            if end_of not in ("track", "queue"):
                raise ValueError(f"end_of must be 'track' or 'queue', not {end_of!r}")
            if self.current is None:
                return
            mode = end_of
        self._volume_restore.stop()
        self._sleep_mode = mode
        self._sleep_deadline = time.monotonic() + minutes * 60.0 if minutes is not None else 0.0
        self._sleep_hold = False
        if minutes is not None:
            remembered = int(minutes) if minutes.is_integer() else minutes
            if settings.get("music_sleep_last") != remembered:
                settings.set("music_sleep_last", remembered)
        self._apply_loop_modes()
        self._fade = 1.0                # a fresh timer; what mpv is sent is decided just below
        self._sleep_update()
        self.sleep_timer_changed.emit()

    def cancel_sleep_timer(self) -> None:
        if self._sleep_mode is None:
            return
        self._sleep_mode = None
        self._sleep_hold = False
        self._sleep_clock.stop()
        self._apply_loop_modes()
        self._set_fade(1.0)
        self.sleep_timer_changed.emit()

    def _sleep_update(self) -> None:
        """Fade by what is left, fire a minutes timer that has run out, and plan
        the next look: every 50 ms in the fade, otherwise when the fade is due
        (at most 15 s away, so a drifting estimate is caught)."""
        mode = self._sleep_mode
        if mode is None:
            self._sleep_clock.stop()
            return
        remaining = self.sleep_remaining or 0.0
        if mode == "minutes" and remaining <= 0.0:
            self._sleep_fire()
            return
        target = remaining / _SLEEP_FADE if remaining < _SLEEP_FADE else 1.0
        if mode != "minutes" and self._fade < target < 1.0 \
                and (target - self._fade) * _SLEEP_FADE < _CLOCK_SLACK:
            # The position clock was put back a little by mpv's own reading
            # (it runs ahead after a seek, and in the tray it is only read every
            # 5 s): hold the level rather than swell back up in the middle of
            # a fade. A real seek back is more than that, and is followed.
            target = self._fade
        self._set_fade(target)
        if mode != "minutes" and not self._playing:
            self._sleep_clock.stop()    # nothing moves until Play; state_changed looks again
            return
        if remaining <= _SLEEP_FADE:
            interval = _SLEEP_FADE_STEP_MS
        else:
            interval = int(min(remaining - _SLEEP_FADE, _SLEEP_CHECK_MAX) * 1000) + 1
        self._sleep_clock.start(max(_SLEEP_FADE_STEP_MS, interval))

    def _check_sleep_end(self) -> None:
        """"End of track": keep-open has paused mpv at the end of the song."""
        mpv = self._mpv
        if (self._sleep_mode == "track" and mpv is not None and mpv.cached("eof-reached")
                and mpv.cached("pause") and not mpv.cached("idle-active")):
            self._sleep_fire()

    def _sleep_fire(self) -> None:
        """The timer ran out: pause, put the volume back, forget the timer.

        After "end of track" keep-open goes back to normal, and mpv moves to
        the next song and holds it paused at 0:00, so Play carries on with it;
        on the last song the queue ends as it would have. Either way the idle
        this may cause must not start the queue over under Repeat all.
        """
        mode = self._sleep_mode
        if mode is None:
            return
        self._sleep_mode = None
        self._sleep_clock.stop()
        if mode == "minutes":
            self._set_fade(0.0)         # silent at the pause even if the last step came late
            self.pause()
        else:
            self._sleep_hold = True
        self._apply_loop_modes()
        self._volume_restore.start()
        _log.info("sleep timer (%s) stopped the music", mode)
        self.sleep_timer_changed.emit()
        self.sleep_timer_fired.emit()

    def _restore_volume_after_sleep(self) -> None:
        if self._sleep_mode is None:
            self._set_fade(1.0)

    def _set_fade(self, factor: float) -> None:
        self._fade = max(0.0, min(1.0, factor))
        self._apply_volume()

    def _track_length(self) -> float:
        """The current song's length: mpv's once it has the file open, else the library's."""
        current = self.current
        if current is None:
            return 0.0
        mpv = self._mpv
        if mpv is not None and mpv.is_running and mpv.cached("path") == current.get("path"):
            length = mpv.cached("duration")
            if isinstance(length, (int, float)) and length > 0:
                return float(length)
        return float(current.get("duration") or self._duration or 0.0)

    def _on_own_track_changed(self, _track) -> None:
        self._schedule_session_save()
        if self._sleep_mode in ("track", "queue"):
            self._sleep_update()
            self.sleep_timer_changed.emit()     # retargeted to the new song

    def _on_own_queue_changed(self) -> None:
        self._schedule_session_save()
        if self._sleep_mode == "queue":
            self._sleep_update()
            self.sleep_timer_changed.emit()     # the queue grew or shrank

    def _on_own_state_changed(self) -> None:
        self._schedule_session_save()
        if self._playing and not self._closing:
            if not self._session_heartbeat.isActive():
                self._session_heartbeat.start()
        else:
            self._session_heartbeat.stop()
        if self._sleep_mode is not None:
            self._sleep_update()

    # --- position clock -----------------------------------------------------------

    def _anchor(self, seconds: float) -> None:
        self._clock_position = float(seconds)
        self._clock_stamp = time.monotonic()

    def _estimated_position(self) -> float:
        """Where the song is now: the last position heard, run on while playing."""
        position = self._clock_position
        if self._playing and not self._loading:
            position += time.monotonic() - self._clock_stamp
        length = self._track_length()
        return min(position, length) if length > 0 else position

    # --- session ----------------------------------------------------------------------

    def save_session(self) -> None:
        """Write the queue, the song and where it is to music-session.json.

        The player saves by itself, a moment after the queue, the song or the
        play state changes, and every 15 s while playing; this is for saving
        now (at quit). Never raises, and never replaces a saved session with an
        empty queue: a start that played nothing keeps the last one.
        """
        try:
            state = self._session_state()
            if state is not None:
                self._write_session(state)
        except Exception:
            _log.exception("could not save the music session")

    def _save_session_if_changed(self) -> None:
        try:
            state = self._session_state()
            if state is not None and state != self._session_last:
                self._write_session(state)
        except Exception:
            _log.exception("could not save the music session")

    def _write_session(self, state: dict) -> None:
        if session.write({**state, "saved_at": round(time.time(), 1)}):
            self._session_last = state

    def _schedule_session_save(self) -> None:
        if not self._closing and self._queue:
            self._session_debounce.start()

    def _session_state(self) -> dict | None:
        if not self._queue or self.current is None:
            return None
        return {
            "queue": [int(t["id"]) for t in self._queue],
            "original": [int(t["id"]) for t in self._original],
            "index": self._index,
            "position": round(self._estimated_position(), 1),
            "shuffle": self._shuffle,
            "repeat": self._repeat,
            "in_order": self._in_order,
            "context": session.clean_context(self._context),
            "counted": self._counted,
            "ended": self._queue_ended,
        }

    def restore_session(self) -> bool:
        """Put the last session's queue back, paused on its song and position.

        Nothing plays and no mpv is started here, so a startup pays nothing for
        it: the bar shows the song and a Play button. A moment later mpv is
        given the queue, paused at that point, so the media keys can resume it
        (see _preload_restored); a Play before then opens the file at the saved
        point itself (no blip from 0). Songs deleted or no longer ready since
        are dropped; if the song itself went, the next one that is still there
        is chosen, from its start. Returns False, touching nothing, when there
        is nothing to restore, resuming is off, or something is already queued.
        """
        try:
            return self._restore_session()
        except Exception:
            _log.exception("could not restore the music session")
            return False

    def _restore_session(self) -> bool:
        if not settings.get("music_resume", True) or self._queue:
            return False
        saved = session.read()
        if saved is None:
            return False
        rows = library.tracks_by_id(saved["queue"] + saved["original"])
        if not rows:
            return False

        # Queue entries share their dicts with the unshuffled order, as they do
        # when played (turning shuffle off and Play next look entries up by
        # identity), so each queue id takes the next unused original of that id.
        original: list[dict] = []
        pool: dict[int, list[dict]] = {}
        for track_id in saved["original"]:
            if track_id in rows:
                entry = dict(rows[track_id])
                original.append(entry)
                pool.setdefault(track_id, []).append(entry)
        queue: list[dict] = []
        kept: list[int] = []            # each entry's position in the saved queue
        taken: dict[int, int] = {}
        for position, track_id in enumerate(saved["queue"]):
            if track_id not in rows:
                continue
            entries = pool.get(track_id)
            used = taken.get(track_id, 0)
            if entries and used < len(entries):
                entry = entries[used]
                taken[track_id] = used + 1
            elif entries:
                entry = entries[-1]     # an unshuffle can hold one entry twice
            else:
                entry = dict(rows[track_id])
            queue.append(entry)
            kept.append(position)
        if not queue:
            return False
        if not original:
            original = list(queue)

        wanted = saved["index"]
        in_range = 0 <= wanted < len(saved["queue"])
        wanted = min(max(wanted, 0), len(saved["queue"]) - 1)
        index = next((i for i, position in enumerate(kept) if position >= wanted), len(queue) - 1)
        same_song = in_range and kept[index] == wanted
        current = queue[index]
        length = float(current.get("duration") or 0.0)
        position = saved["position"] if same_song else 0.0
        if length and position >= length - _RESTORE_END_MARGIN:
            position = 0.0

        self._queue = queue
        self._original = original
        self._index = index
        self._base = index
        self._announced = (current.get("id"), current.get("path"))
        self._in_order = saved["in_order"]
        self._context = saved["context"]
        if saved["shuffle"] != self._shuffle:
            self._shuffle = saved["shuffle"]
            settings.set("music_shuffle", self._shuffle)
        if saved["repeat"] != self._repeat:
            self._repeat = saved["repeat"]
            settings.set("music_repeat", self._repeat)
        self._playing = False
        self._position = position
        self._duration = length
        self._anchor(position)
        self._counted = saved["counted"] and same_song
        self._queue_ended = saved["ended"] and same_song
        self._resume_at = position
        self._restored = True
        self._session_last = self._session_state()      # nothing new to write yet
        self.queue_changed.emit()
        self.track_changed.emit(current)
        self.state_changed.emit()
        self.position_changed.emit(position, length)
        self._preload_timer.start()
        return True

    def _preload_restored(self) -> None:
        """Give mpv the restored queue, paused on its song and second.

        Windows' media keys and media overlay talk to mpv, not to Mistery
        (--media-controls), so a restored session with no mpv behind it was
        out of their reach: Play/Pause on the keyboard did nothing until Play
        had been pressed in the app once. With the song open and paused, mpv
        is a paused media session showing it, and a media key resumes it.

        Nothing is heard and nothing on show changes: the file is opened paused
        at the saved point (see _rebuild), the player stays `restored`, and so
        the song is not announced again, Discord is told nothing and no play is
        counted, until the music really plays, from Play here or from a media
        key that unpauses mpv itself (see _refresh_playing). Paused, mpv used
        0-31 ms of CPU in 30-40 s (Windows counts it in 15.6 ms steps), and
        the pipe to it dozes as it does in the tray (see _apply_power_mode).
        What it does hold on to: from then on mpv has the song's file open
        (only that one, and it could still be renamed, measured) and the audio
        output set up for it, paused (WASAPI, at the song's format), and
        quitting waits for mpv to exit (~85 ms measured).

        mpv is started on a thread of its own, the ~150 ms of waiting for its
        process and pipe that Play would otherwise spend in the window, and the
        queue is handed over back here once it is up (_on_started_in_background).
        """
        if (not self._restored or self._closing or self.current is None or self._starting is not None
                or (self._mpv is not None and self._mpv.is_running)):
            return
        mpv = self._new_mpv()
        device = _device_setting()
        outcome: list[str] = []
        thread = threading.Thread(target=self._start_in_background, args=(mpv, device, outcome),
                                  name="music-mpv-start", daemon=True)
        self._starting = (mpv, thread, device, outcome)
        thread.start()

    def _start_in_background(self, mpv: AudioMpv, device: str, outcome: list[str]) -> None:
        """The preload's thread: start mpv, then say so to the GUI thread."""
        try:
            outcome.append(self._start_mpv(mpv, device))
        except Exception as exc:        # nobody asked for music: Play reports it if it happens again
            _log.warning("could not start mpv for the restored session: %s", exc)
        try:
            self._started_in_background.emit(mpv)
        except RuntimeError:
            pass                        # the player itself is gone

    def _on_started_in_background(self, mpv: AudioMpv) -> None:
        if self._starting is None or self._starting[0] is not mpv:
            return                      # already taken over by Play, a skip or shutdown
        self._adopt_started()
        if self._mpv is mpv and self._restored and self.current is not None and not self._closing:
            self._rebuild(self._index, self._resume_at, same_song=True, paused=True)

    def _adopt_started(self) -> None:
        """Make a preload's mpv the player's, waiting for it to finish starting.

        Called when its thread reports back, and by anything that needs mpv
        before that: Play pressed meanwhile waits out the rest of the start,
        never longer than starting an mpv of its own would take, and plays on
        this one instead of a second mpv beside it.
        """
        starting, self._starting = self._starting, None
        if starting is None:
            return
        mpv, thread, requested, outcome = starting
        thread.join()
        if self._closing or not outcome or not mpv.is_running:
            mpv.terminate()
            return
        if self._mpv is not None:
            self._mpv.terminate()       # a dead one whose `exited` has not been handled yet
        self._mpv = mpv
        self._device_in_use = outcome[0]
        # The volume and the device were read on the thread; either may have
        # been changed since, and with no mpv then, only the setting changed.
        self._sent_volume = None
        self._apply_volume()
        if _device_setting() != requested:
            self.set_audio_device(_device_setting())
        self._apply_power_mode()        # a preload while in the tray stays quiet

    def _drop_failed_preload(self) -> None:
        """The preloaded queue went idle unplayed: none of it could be opened.

        Most likely the drive the music is on isn't there yet. Nobody asked for
        music, so nothing is said and the session stays as restored, on its
        song and second; Play tries the files again and reports what it finds
        then, as it did before there was a preload. The idle mpv is let go,
        or the media overlay would show an empty "Mistery", and it quits on a
        thread of its own so the window never waits for it.
        """
        index, position = self._preload
        self._preload = None
        self._device_error = None
        self._device_lost = False
        if 0 <= index < len(self._queue):
            self._set_index(index)      # back from any songs mpv tried after it
            self._duration = float(self._queue[index].get("duration") or self._duration)
        self._position = position
        self._anchor(position)
        self._resume_at = position
        self.position_changed.emit(position, self._duration)
        mpv, self._mpv = self._mpv, None
        if mpv is not None:
            threading.Thread(target=mpv.terminate, name="music-mpv-quit", daemon=True).start()

    # --- mpv events ---------------------------------------------------------------

    def _on_property(self, name: str, value) -> None:
        if name in ("path", "playlist-pos", "time-pos", "duration") and self._device_lost:
            # mpv failing its way through the queue after the chosen device
            # went: each song was announced (a lyrics lookup, a Discord update
            # and a repaint apiece, 17 in a row measured) and its 0:00 and
            # length shown, just before the recovery put the first one back.
            return
        if name in ("path", "playlist-pos"):
            self._resolve_current()
        elif name in ("pause", "idle-active"):
            # "Playing" depends on both, and mpv sends them in no fixed order:
            # judging on the pause event alone read a stale idle flag and left
            # the play button showing ▶ over a song that was plainly playing.
            self._refresh_playing()
            if name == "idle-active":
                if value:
                    self._on_idle()
                else:
                    self._pass_busy = True
            elif value:
                self._check_sleep_end()
        elif name == "eof-reached":
            if value:
                self._check_sleep_end()
        elif name == "time-pos" and value is not None:
            if self._loading:
                return                  # the song from before a rebuild
            self._update_position(float(value))
        elif name == "duration" and value:
            self._duration = float(value)
            self.position_changed.emit(self._position, self._duration)
            if self._sleep_mode in ("track", "queue"):
                self._sleep_update()

    def _refresh_playing(self) -> None:
        mpv = self._mpv
        if mpv is None:
            playing = False
        else:
            # A new mpv is idle until it reports otherwise. Its first pause=no
            # can arrive before its first idle-active, and read as "not idle"
            # it looked like playing for a moment: enough, on a preload, to
            # end `restored` for a song nobody had played (3 runs in 18
            # measured from the tray, where nothing waits on mpv before the
            # queue goes in; none in 20 since).
            playing = not bool(mpv.cached("pause", False)) and not bool(mpv.cached("idle-active", True))
            if not playing and self._device_lost and not mpv.cached("pause", False):
                # The lost device's pass going idle: _on_idle restarts the song
                # on the default, or says the music stopped if it can't.
                return
        if playing != self._playing:
            self._anchor(self._estimated_position())
            self._playing = playing
            if playing:
                if self._restored:
                    # However it started: Play here, or a media key unpausing
                    # the preload in mpv, which is only heard of from mpv. The
                    # session is live now, and a queue saved as ended is not.
                    self._queue_ended = False
                self._restored = False
                self._preload = None
                # The preload's resume point is used up once the song plays. A
                # rebuild clears it, but a preload starts playing without one,
                # and left set, every later start of this song from nothing
                # (Play after a Stop, after the queue ran out, after mpv died)
                # went back to the saved second instead of 0:00.
                self._resume_at = 0.0
                if not self._low_power:
                    mpv.max_idle_wait = 0.02    # the window's pace, for the position stream
            self.state_changed.emit()

    def _resolve_current(self) -> None:
        """Work out which queue entry mpv is playing — only when mpv agrees with itself.

        mpv's `path` and `playlist-pos` arrive as separate events in no fixed
        order, and events from before a queue rebuild can still be in the pipe.
        So neither event is trusted alone: both are read from the latest cached
        values, and a song is accepted only when the file at that playlist
        position really is the file mpv says it is playing. Any stale or
        half-updated pair simply fails that test, and the next event settles it.

        (The first version searched the queue for the path on its own, which let
        a late event for the previous song win if that song was also in the new
        queue.)
        """
        mpv = self._mpv
        if mpv is None:
            return
        path = mpv.cached("path")
        position = mpv.cached("playlist-pos")
        if not path or not isinstance(position, int) or position < 0:
            return
        index = self._base + position
        if self._preload is not None and self._loading and index != self._index:
            # mpv moving past a song held paused that never opened: it failed,
            # and its end-file (which drops the preload) can arrive after this.
            # Announced, the bar showed the next song for a moment and fetched
            # its lyrics. Once the song has opened, a move is a real Next from
            # the media overlay and is shown.
            return
        if 0 <= index < len(self._queue) and self._queue[index]["path"] == path:
            self._set_index(index)

    def _on_idle(self) -> None:
        """mpv ran out of playlist — if this idle really is the end of the pass.

        Only once mpv has been busy since the rebuild. A fresh mpv answers being
        observed with idle-active=true, and that answer lands after _rebuild has
        queued the new songs: taken as the end of the queue, Repeat all started
        over from track 1 on top of the song just picked — the first pick of
        every session, and again after any mpv restart.

        And the queue only starts over if a song in the pass played to its end.
        When nothing can play (the album folder moved since the last scan, a
        drive gone, no audio output) every entry fails at once, and starting
        over regardless looped ~60 times a second, from the tray, until repeat
        was switched off. Both failures end every file in "error" — with no
        audio device mpv even loads and starts each one first — so a song that
        ended in "eof" is what tells a finished queue from a broken one.

        Neither applies when mpv was told to stop. Windows' media Stop key and
        the media overlay send `stop` to mpv directly, not through this class,
        and mpv reports it the same way: the file ends, then mpv goes idle. Read
        as the end of the pass, a Stop after skipping songs would say nothing
        could be played and go back to the pass's first song, a Stop after a
        song had finished would make Play skip the stopped one, and under Repeat
        all the queue would start over instead of stopping. mpv sends the file's
        end-file before the idle, so its reason tells a stop from the rest.

        A sleep timer set to the end of the queue fires here, and a timer that
        has just stopped the music keeps Repeat all from starting it again.

        A restored session's preload that goes idle before anyone played it is
        not any of these: see _drop_failed_preload. A Stop from the media
        overlay is still a Stop, back to the start of the song.
        """
        mpv = self._mpv
        if mpv is not None and not mpv.cached("idle-active", True):
            return                      # busy again already: an old event
        if not self._queue or self._closing or not self._pass_busy:
            if self._device_lost:
                # Not a pass that can be recovered (Stop was pressed): stop
                # holding the lost device's failures back, and show the pause.
                self._device_lost = False
                self._refresh_playing()
            return
        self._pass_busy = False
        stopped = self._last_end in ("stop", "quit")
        if self._preload is not None:
            if not stopped:
                self._drop_failed_preload()
                return
            self._preload = None
            self._resume_at = 0.0
        if not stopped and self._device_error is not None and self._recover_audio_device():
            return
        self._device_lost = False       # not recovered: what follows says how the pass ended
        if not stopped and self._sleep_mode == "queue":
            self._sleep_fire()
        held, self._sleep_hold = self._sleep_hold, False
        if not stopped and self._pass_played and self._repeat == "all" and not held:
            self._rebuild(0)
            return
        failed = not stopped and not self._pass_played
        if failed:
            # Back on the song the pass started from, so Play tries it all again.
            self._set_index(self._base)
        elif not stopped and self._last_end == "eof":
            # Ran off the end of the queue: Play moves on to anything added
            # since. Not when the pass ended on a file that failed (a missing
            # last song after skipping): Play then replays the song you were on
            # instead of trying the broken one again.
            self._queue_ended = True
        # A stop leaves the current song where it is, so Play starts it again.
        self._playing = False
        self._position = 0.0
        self._anchor(0.0)
        self.state_changed.emit()
        self.position_changed.emit(0.0, self._duration)
        if failed:
            self.error.emit(_NOTHING_PLAYED)

    def _on_file_loaded(self) -> None:
        self._loading = False
        self._file_open = True
        # A song opened after a sleep timer's stop: that stop is over, and a
        # later end of the queue may start it over again as usual.
        self._sleep_hold = False

    def _on_end_file(self, reason: str) -> None:
        self._last_end = reason
        # A song that opened and was then skipped away from ("stop") proves the
        # queue plays just as well as one that reached its end. Without it,
        # skipping onto a missing last song said nothing could be played and
        # went back to the start. The "stop" a rebuild's loadfile gives the old
        # song arrives after _rebuild cleared _file_open, so it does not count;
        # a file that fails ends in "error" whether or not it opened.
        if reason == "eof" or (reason == "stop" and self._file_open):
            self._pass_played = True
        self._file_open = False
        mpv = self._mpv
        if reason == "error" and self._preload is not None:
            # The song held paused could not be opened (a restored file moved
            # or deleted since, or the output device gone). mpv would move on to the next
            # song by itself, paused, and the bar changed songs and the session
            # file was rewritten before anyone touched anything. Dropped as a
            # preload that opened nothing is: Play tries again and says why.
            _log.info("the song held paused could not be opened; letting mpv go until Play")
            self._drop_failed_preload()
            return
        if (reason == "error" and self._device_in_use != "auto" and self._device_error is None
                and self.current is not None and mpv is not None and mpv.is_running):
            # Where the music was when the first song failed, in case it was the
            # chosen device that went (see _recover_audio_device). Asked now,
            # not once mpv is idle, so the songs it fails meanwhile are held
            # back instead of announced: one IPC round trip (10-26 ms measured),
            # and only on a pass's first failure while a device is chosen.
            paused = bool(mpv.cached("pause", False))
            self._device_error = (self._index, self._estimated_position(), self._counted, paused)
            self._device_lost = not self._device_present(mpv, self._device_in_use)
            _log.warning("a song failed on audio device %s at %.1f s (%s); %s",
                         self._device_in_use, self._device_error[1], "paused" if paused else "playing",
                         "the device is gone from the device list" if self._device_lost
                         else "the device is still listed")

    def _on_playback_restart(self) -> None:
        # mpv has finished a seek (or started a file): the positions from here
        # on are after any seek of ours.
        self._seek_pending = False

    def _update_position(self, seconds: float, since_start: float = 1.0) -> None:
        """A new playback position, streamed or polled.

        On Repeat one this is also where a loop shows. mpv loops a file by
        seeking back to its start and the song never changes, so nothing reset
        the play count: an hour on repeat was one play. A position that jumps
        back to the start of the song, with no seek of ours under way, is the
        song starting over. (mpv sends time-pos 0 before the loop's
        playback-restart, so that event cannot mark the loop; and polled in the
        tray, a jump back between two polls is all there is to see.)

        Only a jump to the start, though: the Windows media overlay seeks mpv
        directly, so a seek of ours is not the only other way back, and taking
        a drag from 80% back to 60% for a loop would count another play at once.
        "The start" is `since_start` seconds: about a second when the position
        is streamed, one poll interval when it is polled.
        """
        if (self._repeat == "one" and not self._seek_pending
                and seconds <= since_start and self._position > seconds + 1.0):
            self._counted = False
        # A jump the clock didn't expect (the media overlay seeking mpv
        # directly) re-plans a track or queue sleep timer's fade.
        jumped = (self._sleep_mode in ("track", "queue")
                  and abs(seconds - self._estimated_position()) > _CLOCK_SLACK)
        self._position = seconds
        self._anchor(seconds)
        self.position_changed.emit(self._position, self._duration)
        self._maybe_count_play()
        if jumped:
            self._sleep_update()

    def _maybe_count_play(self) -> None:
        """The scrobbling rule: half the track, or four minutes, whichever is first.

        Not while restored: a song mpv only holds open, paused, has not been
        listened to, even when the second it was saved at is past halfway.
        """
        current = self.current
        if self._counted or self._restored or current is None or not self._duration:
            return
        if self._position >= min(self._duration * 0.5, 240.0):
            self._counted = True
            try:
                library.mark_played(int(current["id"]))
            except Exception:
                _log.exception("could not record play")

    def stop(self) -> None:
        if self._mpv is not None and self._mpv.is_running:
            self._mpv.command("stop")
        # The idle this causes is not the queue running out: with Repeat all it
        # used to start the queue over, so Stop never stopped.
        self._pass_busy = False
        self._anchor(self._estimated_position())
        if self._preload is not None:
            # A restored song stopped before it played stays where it is on
            # show, as it does with no mpv behind it.
            self._preload = None
            self._resume_at = self._clock_position
        self._playing = False
        self.state_changed.emit()

    def shutdown(self) -> None:
        if not self._closing:
            self.save_session()         # where the music was, for the next start
        self._closing = True
        self._preload_timer.stop()
        starting, self._starting = self._starting, None
        if starting is not None:
            mpv, thread, _requested, _outcome = starting
            thread.join()               # no more than the rest of its start
            mpv.terminate()
        self._position_poll.stop()
        self._sleep_clock.stop()
        self._volume_restore.stop()
        self._session_debounce.stop()
        self._session_heartbeat.stop()
        if self._mpv is not None:
            self._mpv.terminate()
            self._mpv = None
