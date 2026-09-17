"""The lyrics tab, taking over the screen while a song plays.

A record, the words, and the song's own shape in a band underneath, on black.
It starts by itself after a few minutes of stillness, and it is built to be left
running: the whole composition drifts, it dims as the hours pass, and it ends
only when somebody actually moves the mouse.

It is a child of NowPlayingView rather than a page of its own, because
NowPlayingView stops feeding the lyrics the moment it is not the visible page
(now_playing._on_position early-returns on `not self.isVisible()`). As a child
it simply covers the page, which keeps still running behind it — except for the
three things `entered` asks it to hide, whose own hideEvents stop the record's
33 ms timer, the lyrics' breathing dots and the sleep timer's clock.

Nothing behind the overlay draws a pixel, and that took two things rather than
one. The obvious one is WA_OpaquePaintEvent, which the theme's style sheet
silently took away again (see __init__). The other is that hiding a widget does
not stop a QVariantAnimation it started, so LyricsView.hideEvent stops its own
scroll. Measured full screen at 2560x1440, 8 s: NowPlayingView.paintEvent 46.0
times a second and 10.52 MP/s before, 0.0 and 0.00 after.

OLED, concretely:

* Mostly black. The Now Playing backdrop is a mid-tone gradient over a blurred
  cover at 0.42 opacity — at full screen that is a high average picture level
  held for hours, which is the worst thing to show an OLED. This draws black
  with one soft glow behind the record.
* The record is 38% of the screen's height, not all of it. A full-screen disc
  is both the burn-in problem (a big fixed sheen — vinyl.py keeps the sheen
  deliberately still, which is what makes it read as spinning) and the CPU
  problem: the rotated label scales with the diameter, and VinylView's cached
  body would be ~24 MB of pixmap at 4K instead of ~3.5.
* Everything orbits: a Lissajous of ±24 px over six minutes, which is 0.4 px a
  second — invisible while you watch it, 48 px of travel while you do not. It
  MOVES a container; it never resizes the views, because a resize re-keys
  VinylView._body and nulls NowPlayingView._background, and rebuilding either
  at full-screen size costs far more than the drift is worth.
* It dims: full at the start, 70% at two minutes, 55% at ten, and 34% if the
  music has been paused for a minute (a stopped record is a still picture).
* 20 fps, not 33 — and the record and the band share one clock, because two
  50 ms timers of their own land in different passes of the event loop and each
  pass is a flush of the window's backing store, which is most of what a
  screensaver like this costs. Shared, they cost one paint of this widget
  between them (measured: one dirty rectangle covering both, 20.3 frames a
  second each). The lyrics' own breathing dots are a third clock, at 40 ms,
  while the dots are up; they are 88x47 px and were left alone.
* It does NOT hold the display awake. Letting Windows blank an OLED after its
  own timeout is the best burn-in protection there is, and the app has never
  called SetThreadExecutionState. When the panel comes back, this is still here.

Leaving is in ExitIntent below, which is the part that had to be right.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ..config import settings
from ..music import library, peaks
from .vinyl import VinylView, readable_accent
from .waveform import WaveformView

# How far the pointer has to travel, and how that travel is measured. See
# ExitIntent — the re-arm is the difference between this and the film player's
# _WAKE_DISTANCE, which never re-anchors.
EXIT_TRAVEL = 96        # px, Manhattan, from the anchor
REARM_STILL = 2.0       # seconds of no movement re-anchors
REARM_WINDOW = 3.0      # ...and so does an anchor this old
MIN_MOVES = 2           # one event is a teleport, not a hand
MIN_SPAN = 0.050        # seconds between the first move and the one that exits

ORBIT = 24.0            # px of drift either way
ORBIT_PERIOD = 360.0    # seconds for the horizontal sweep
ORBIT_SKEW = 1.37       # the vertical sweep's period against it: never quite repeats

WATCH_MS = 2000         # how often stillness is checked against the setting
_SETTLE_MS = 350        # let a window state change finish before believing it
# ...and then ask once more before believing the window really lost the focus.
# A toast, a balloon or an installer that takes the focus and gives it straight
# back would otherwise end the screensaver and put the bright page back for the
# three minutes the idle clock needs to try again.
_CONFIRM_MS = 1200
SLOW_MS = 1000          # the orbit, the dim and the peaks poll

# Elapsed seconds -> how bright. A screensaver is not being read closely, and
# every stop below 1.0 is wear that is not happening.
DIM_STEPS = ((0.0, 1.0), (120.0, 0.70), (600.0, 0.55))
PAUSED_DIM = 0.34       # after a minute paused: the record has stopped turning
PAUSED_AFTER = 60.0

LYRIC_WHITE = 0.82      # the sung line's ceiling: pure white wears the blue subpixel hardest
DISC_HEIGHT = 0.38      # the record's diameter, as a share of the screen's height

# Keys that act without ending the screensaver. They are window shortcuts
# (main_window._install_shortcuts), so Qt usually claims them before this view
# ever sees them; they are listed anyway for the times the shortcuts are off.
_TRANSPORT_KEYS = frozenset({
    Qt.Key.Key_Space, Qt.Key.Key_MediaTogglePlayPause, Qt.Key.Key_MediaPlay,
    Qt.Key.Key_MediaPause, Qt.Key.Key_MediaStop, Qt.Key.Key_MediaNext,
    Qt.Key.Key_MediaPrevious, Qt.Key.Key_VolumeUp, Qt.Key.Key_VolumeDown,
    Qt.Key.Key_VolumeMute,
})

# What counts as somebody being at the machine, for the idle clock.
_INPUT_EVENTS = frozenset({
    QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick, QEvent.Type.Wheel, QEvent.Type.KeyPress,
    QEvent.Type.KeyRelease, QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
})


class ExitIntent:
    """Did a person move that mouse, or did the desk?

    Windows offers a screensaver plenty of movement nobody made. An optical
    mouse on a shiny desk drifts a pixel or two; a knock to the desk gives a few
    more; a precision touchpad reports a resting palm; a 16000 DPI gaming mouse
    turns a millimetre of vibration into 16 px; a remote-desktop client re-sends
    the cursor position on reconnect; a game warps the pointer; a resolution or
    DPI change moves it outright. Qt adds one of its own — a widget appearing
    under a resting pointer delivers a synthetic Enter with no movement at all,
    which is exactly what this overlay does when it starts, so Enter is never
    offered to this class.

    The rule is travel from an anchor, and the anchor re-arms both after two
    seconds of stillness and three seconds after it was set. That makes it "96
    px within about three seconds of continuous movement". Drift cannot get
    there: two pixels a second all night still only accumulates six per window.
    A hand reaching for the mouse crosses it in well under half a second.
    player_overlay's _WAKE_DISTANCE (16 px) is the same idea without the re-arm,
    which is right for a two-hour film and wrong for eight hours — with a fixed
    anchor, one pixel of drift a minute crosses any threshold before morning.

    On top of that, two moves at least 50 ms apart, so a single teleport is
    never enough however far it went.
    """

    def __init__(self, travel: int = EXIT_TRAVEL) -> None:
        self._travel = travel
        self._anchor: QPoint | None = None
        self._first_at = 0.0
        self._last_at = 0.0
        self._moves = 0

    @property
    def anchor(self) -> QPoint | None:
        return self._anchor

    @property
    def moves(self) -> int:
        """Moves counted since the anchor was last set (tests, and measuring)."""
        return self._moves

    def reset(self, point: QPoint | None = None, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._anchor = QPoint(point) if point is not None else None
        self._first_at = now
        self._last_at = now
        self._moves = 0

    def moved(self, point: QPoint, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if (self._anchor is None
                or now - self._last_at >= REARM_STILL
                or now - self._first_at >= REARM_WINDOW):
            self._anchor = QPoint(point)
            self._first_at = now
            self._moves = 0
        self._moves += 1
        self._last_at = now
        travel = (point - self._anchor).manhattanLength()
        return (travel >= self._travel
                and self._moves >= MIN_MOVES
                and now - self._first_at >= MIN_SPAN)


def dim_at(elapsed: float) -> float:
    """How bright the picture is `elapsed` seconds in, between the DIM_STEPS."""
    previous_at, previous = DIM_STEPS[0]
    for at, level in DIM_STEPS[1:]:
        if elapsed < at:
            share = (elapsed - previous_at) / (at - previous_at)
            return previous + (level - previous) * share
        previous_at, previous = at, level
    return previous


class ScreensaverView(QWidget):
    """Covers its parent. enter() and leave() are the whole public API; the
    window hears `left` when it has ended so it can undo full screen."""

    entered = Signal()
    left = Signal()

    def __init__(self, player, lyrics: QWidget, ready=None, parent=None) -> None:
        # `lyrics` is handed in rather than built here: LyricsView lives in
        # now_playing.py, which imports this module, and a screensaver that
        # imported it back would be a cycle. `ready` answers "is the lyrics tab
        # of a visible Now Playing on screen", which only the page can know.
        super().__init__(parent)
        self._player = player
        self._ready = ready or (lambda: False)
        self._active = False
        self._watching = False
        self._since = 0.0
        self._shown_second = -1
        self._paused_since = 0.0
        self._last_input = time.monotonic()
        self._dim = 1.0
        self._orbit_at = (0, 0)
        self._glow: QPixmap | None = None
        self._glow_for: int | None = None
        self._intent = ExitIntent()
        self._watched_window: QWidget | None = None
        self._confirming = False
        self._caption_style: tuple | None = None
        self._caption_on = False
        self._unnamed = False       # the song has not been named on screen yet
        self.exit_reason = ""       # what ended it last; the tests read this

        # The object name and the rule are what make the attribute stick, and
        # the attribute is the whole point of the overlay. The window carries a
        # 5.3 kB style sheet, and QStyleSheetStyle::polish() clears
        # WA_OpaquePaintEvent on any widget whose background it cannot see: set
        # here alone it read True at the end of __init__ and False on the live
        # widget, and Qt then repainted the page under every rectangle this
        # dirtied. Measured with a #Screensaver rule and without, 8 s each, both
        # full screen at 2560x1440: NowPlayingView.paintEvent 46.0 times a
        # second and 10.52 MP/s without it, 0.0 and 0.00 with it. The rule is
        # never actually drawn — paintEvent below fills black itself and never
        # calls the style — it is there to be seen by the polish.
        self.setObjectName("Screensaver")
        self.setStyleSheet("#Screensaver { background: #000000; }")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.hide()

        # Everything that orbits lives in here. Its SIZE only changes when the
        # window does; the drift only ever moves it.
        self._stage = QWidget(self)
        self._stage.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.vinyl = VinylView(self._stage)
        self.vinyl.set_frame_interval(50)       # 20 fps
        self.vinyl.set_sheen(0.35)              # the one permanently bright, permanently still thing
        self.lyrics = lyrics
        self.lyrics.setParent(self._stage)
        self.lyrics.set_screensaver(True)
        # Whether the caption is wanted depends on whether there are words, and
        # the words arrive long after the track changed — LRCLIB is a network
        # round trip. Without these two the only periodic check was _on_slow's,
        # which is inside `if the dim moved`, and dim_at() is flat from ten
        # minutes on: after that the caption was never re-checked again and the
        # song's title stayed painted over the lyrics for the rest of the night.
        self.lyrics.lyrics_changed.connect(self._on_words)
        self.lyrics.status_changed.connect(self._on_words)
        self.wave = WaveformView(self._stage)
        # One 50 ms clock for the whole picture instead of three. Every timer
        # that fires in a pass of its own is another flush of the window's
        # backing store, and the flush is most of what this costs: measured full
        # screen with the record, the band and the lyrics' intro dots all
        # running, 37.4 paints of this widget a second, of which 17.0 were the
        # dots' own 88x47 rectangle.
        self.wave.set_driven(True)
        self.lyrics.set_driven(True)
        self.vinyl.frame.connect(self.wave.tick)
        self.vinyl.frame.connect(self.lyrics.tick)
        self._caption = QLabel(self._stage)
        self._caption.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._caption.setWordWrap(True)
        for child in (self.vinyl, self.lyrics, self.wave, self._caption):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        # Space and the media keys act without ending this, so something has to
        # say so. It is here for a couple of seconds and then gone: no clock, no
        # pinned title, nothing that sits in one place all night.
        self._transient = QLabel(self)
        self._transient.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._transient.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._transient.setStyleSheet(
            "background: rgba(255,255,255,0.07); border-radius: 18px; padding: 10px 22px;"
            "color: rgba(255,255,255,0.80); font-size: 13pt; font-weight: 600;")
        self._transient.hide()
        self._transient_timer = QTimer(self)
        self._transient_timer.setSingleShot(True)
        self._transient_timer.timeout.connect(self._transient.hide)

        self._watch = QTimer(self)
        self._watch.setInterval(WATCH_MS)
        self._watch.timeout.connect(self._on_watch)
        self._slow = QTimer(self)
        self._slow.setInterval(SLOW_MS)
        self._slow.timeout.connect(self._on_slow)

        player.track_changed.connect(self._on_track)
        player.state_changed.connect(self._on_state)
        player.position_changed.connect(self._on_position)

    # --- public -------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def still_for(self) -> float:
        """Seconds since anything reached the keyboard, mouse or touchscreen."""
        return time.monotonic() - self._last_input

    def set_watching(self, watching: bool) -> None:
        """Arm or disarm the idle clock. The page calls this as it is shown and
        hidden, so nothing is watched while Now Playing is not on screen."""
        watching = bool(watching)
        if watching == self._watching:
            return
        self._watching = watching
        application = QApplication.instance()
        if watching:
            self._last_input = time.monotonic()
            if application is not None:
                application.installEventFilter(self)
            self._watch.start()
        else:
            self._watch.stop()
            if application is not None:
                application.removeEventFilter(self)

    def enter(self) -> None:
        if self._active:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        self._active = True
        self._since = time.monotonic()
        self._paused_since = 0.0 if self._player.is_playing else self._since
        self._dim = 1.0
        self._confirming = False
        self._watch.stop()
        self.entered.emit()                 # the page hides its furniture and asks for the screen

        self.setGeometry(parent.rect())
        self._relayout()
        self._seed()
        self._sync_caption()
        self.raise_()
        self.show()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self._intent.reset(QCursor.pos())
        window = self.window()
        if window is not None:
            window.installEventFilter(self)
            self._watched_window = window
        self._slow.start()

    def leave(self, reason: str = "") -> None:
        if not self._active:
            return
        self.exit_reason = reason
        self._active = False
        self._confirming = False
        self._slow.stop()
        self._transient_timer.stop()
        self._transient.hide()
        if self._watched_window is not None:
            self._watched_window.removeEventFilter(self)
            self._watched_window = None
        self.unsetCursor()
        self.hide()
        # Otherwise the idle clock is already past the threshold and the
        # screensaver comes straight back over whoever just dismissed it.
        self._last_input = time.monotonic()
        if self._watching:
            self._watch.start()
        self.left.emit()

    # --- starting -------------------------------------------------------------------

    def _eligible(self) -> bool:
        if not settings.get("music_screensaver", True):
            return False
        if not self._player.is_playing or not self._ready():
            return False
        window = self.window()
        if window is None or not window.isActiveWindow() or window.isMinimized():
            return False
        # A menu open over Now Playing is somebody in the middle of something.
        return QApplication.activePopupWidget() is None

    def _on_watch(self) -> None:
        if self._active:
            return
        if not self._eligible():
            self._last_input = time.monotonic()
            return
        after = max(10.0, float(settings.get("music_screensaver_after", 180) or 180))
        if self.still_for >= after:
            self.enter()

    # --- while it runs ---------------------------------------------------------------

    def _on_slow(self) -> None:
        if not self._active:
            return
        now = time.monotonic()
        dim = dim_at(now - self._since)
        if self._paused_since and now - self._paused_since >= PAUSED_AFTER:
            dim = min(dim, PAUSED_DIM)
        if abs(dim - self._dim) > 0.004:
            self._dim = dim
            for part in (self.lyrics, self.wave, self.vinyl):
                part.set_dim(dim)
            self._sync_caption()
            self.update()
        self._orbit(now)
        self.wave.poll_levels()

    def _orbit(self, now: float) -> None:
        """Drift the whole composition. Moving, never resizing: a resize would
        re-key VinylView._body (a ~3.5 MB pixmap at 4K) once a second."""
        phase = now - self._since
        x = ORBIT * math.sin(2 * math.pi * phase / ORBIT_PERIOD)
        y = ORBIT * math.sin(2 * math.pi * phase / (ORBIT_PERIOD * ORBIT_SKEW) + 1.1)
        step = (int(round(x)), int(round(y)))
        if step != self._orbit_at:
            self._orbit_at = step
            self._stage.move(int(ORBIT) + step[0], int(ORBIT) + step[1])

    def _seed(self) -> None:
        """Fill the parts from the player, whatever Now Playing happens to show.

        The record is fed here regardless of music_cover_style: NowPlayingView
        only calls set_progress while the style is "disc", so somebody on the
        flat cover would otherwise get a record with a frozen tonearm.
        """
        self._on_track(self._player.current)
        self.vinyl.set_playing(self._player.is_playing, animate=False)
        self.wave.set_playing(self._player.is_playing)
        self.lyrics.set_playing(self._player.is_playing)
        position, duration = self._player.position, self._player.duration
        self.vinyl.set_progress(position, duration)
        self.wave.set_position(position, duration)
        self.lyrics.set_position(position)
        for part in (self.lyrics, self.wave, self.vinyl):
            part.set_dim(1.0)

    def _on_track(self, track=None) -> None:
        track = track if track is not None else self._player.current
        if not track:
            return
        if settings.get("music_screensaver", True):
            # Measured while the song plays rather than when the screensaver
            # starts: 0.29 s of background ffmpeg now means the band is already
            # drawn the moment it appears.
            peaks.request(track)
        if not self._active:
            return          # a cover decode and a palette parse per song, for nothing
        accent = library.parse_palette(_field(track, "palette") or None)["accent"]
        self._shown_second = -1
        self.vinyl.set_cover(_field(track, "cover") or None)
        self.vinyl.set_accent(accent)
        self.lyrics.set_accent(accent)
        self.wave.set_accent(accent)
        self.wave.set_track(track)
        self._glow = None
        self._glow_for = readable_accent(accent).rgb()
        self._unnamed = True
        self._sync_caption()
        self._name_song()
        self.update()

    def _on_state(self) -> None:
        if not self._active:
            return
        playing = self._player.is_playing
        self.vinyl.set_playing(playing)
        self.lyrics.set_playing(playing)
        self.wave.set_playing(playing)
        self._paused_since = 0.0 if playing else (self._paused_since or time.monotonic())
        if playing:
            self._dim = dim_at(time.monotonic() - self._since)
            for part in (self.lyrics, self.wave, self.vinyl):
                part.set_dim(self._dim)
        self._show_transient("Playing" if playing else "Paused")

    def _on_position(self, position: float, duration: float) -> None:
        if not self._active:
            return
        self.lyrics.set_position(position)
        self.wave.set_position(position, duration)
        second = int(position)
        if second != self._shown_second:
            # The arm walks in once a second, as Now Playing does it: at 33 rpm
            # a tenth of a degree either way is not a picture anybody can see.
            self._shown_second = second
            self.vinyl.set_progress(position, duration or self._player.duration)

    def _show_transient(self, text: str) -> None:
        if not text:
            return
        self._transient.setText(text)
        self._transient.adjustSize()
        size = self._transient.sizeHint()
        self._transient.setGeometry((self.width() - size.width()) // 2,
                                    int(self.height() * 0.86) - size.height() // 2,
                                    size.width(), size.height())
        self._transient.show()
        self._transient.raise_()
        self._transient_timer.start(2200)

    def _on_words(self, *_) -> None:
        """The page forwarded set_status/set_lyrics to our copy of the view."""
        if self._active:
            self._sync_caption()

    def _name_song(self) -> None:
        """Say what is playing, once. Not while the caption is already saying
        it in the lyrics column — that showed the same line twice at once, in
        two places, at every track change. The words usually arrive a second
        later and take the caption away with them; the chip goes up then."""
        track = self._player.current
        if not self._unnamed or track is None or self._caption_on:
            return
        self._unnamed = False
        self._show_transient(_track_line(track))

    def _sync_caption(self) -> None:
        """The song's name, but only when there are no words to show. A wall of
        lyrics nobody can follow is not a screensaver, and neither is an empty
        black rectangle where they should be."""
        track = self._player.current
        wanted = bool(not self.lyrics.synced and track is not None)
        was, self._caption_on = self._caption_on, wanted
        self._caption.setVisible(wanted)
        if not wanted:
            if was:
                self._name_song()       # the words arrived: name the song on the way past
            return
        alpha = 0.40 * self._dim
        size = max(18, self.height() // 42)
        text = _track_line(track)
        style = (alpha, size, text)
        if style == self._caption_style:
            return          # called on every lyric and every dim step; a restyle is not free
        self._caption_style = style
        self._caption.setStyleSheet(
            f"color: rgba(255,255,255,{alpha:.3f}); font-size: {size}px;"
            "font-weight: 600;")
        self._caption.setText(text)

    # --- input ----------------------------------------------------------------------

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind in _INPUT_EVENTS:
            self._last_input = time.monotonic()
        elif self._active and watched is self._watched_window:
            if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.WindowStateChange):
                # Not straight away: showFullScreen() itself deactivates the
                # window on Windows for a frame or two, and leaving on that
                # would end the screensaver the instant it asked for the
                # screen. Ask again once the state has settled.
                QTimer.singleShot(_SETTLE_MS, self._check_window)
        return False

    def _check_window(self) -> None:
        window = self.window()
        if not self._active or window is None:
            return
        if window.isMinimized():
            self.leave("minimised")         # put away on purpose: no second look needed
        elif not window.isActiveWindow() and not self._confirming:
            self._confirming = True
            QTimer.singleShot(_CONFIRM_MS, self._confirm_deactivated)

    def _confirm_deactivated(self) -> None:
        self._confirming = False
        window = self.window()
        if not self._active or window is None:
            return
        if window.isMinimized():
            self.leave("minimised")
        elif not window.isActiveWindow():
            self.leave("deactivated")       # the window is not what you are looking at

    def mouseMoveEvent(self, event) -> None:
        if self._active and self._intent.moved(event.globalPosition().toPoint()):
            self.leave("moved")

    def mousePressEvent(self, event) -> None:
        self.leave("clicked")

    def wheelEvent(self, event) -> None:
        self.leave("wheel")

    def enterEvent(self, event) -> None:
        # Deliberately nothing. Showing this widget under a resting pointer
        # delivers a synthetic Enter with no movement behind it, so an Enter
        # that ended the screensaver would end it the instant it started —
        # player_overlay.enterEvent carries the same note for the same reason.
        super().enterEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in _TRANSPORT_KEYS:
            event.accept()                      # it acted; the screensaver stays
            return
        self.leave("escape" if event.key() == Qt.Key.Key_Escape else "key")
        event.accept()

    # --- geometry and painting --------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Polish happens on the way to the screen, so this is the last moment
        # the style sheet can take the attribute away. The #Screensaver rule in
        # __init__ is what keeps it; this is the belt to that pair of braces,
        # and it is free.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    @property
    def opaque(self) -> bool:
        """Is Qt treating this as covering what is under it? False means the
        page repaints behind the overlay (the tests assert this)."""
        return self.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        """Place the parts inside the stage. Called on resize only — the orbit
        moves the stage and touches none of this."""
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            return
        margin = int(ORBIT)
        stage = QSize(width - 2 * margin, height - 2 * margin)
        self._stage.resize(stage)
        self._stage.move(margin + self._orbit_at[0], margin + self._orbit_at[1])

        band = max(64, int(height * 0.11))
        gap = max(28, int(height * 0.035))
        upper = stage.height() - band - gap
        box = VinylView.box_for_radius(height * DISC_HEIGHT / 2.0)
        box = QSize(min(box.width(), int(stage.width() * 0.40)), min(box.height(), upper))
        # The record and the words are centred as a pair, not pinned to the
        # edges: a full-width column of lyrics wraps so late that a line of a
        # song looks like a paragraph.
        words_width = int(stage.width() * 0.40)
        between = int(stage.width() * 0.05)
        left = max(int(stage.width() * 0.03),
                   (stage.width() - box.width() - between - words_width) // 2)
        self.vinyl.setGeometry(left, (upper - box.height()) // 2, box.width(), box.height())
        words = QRect(left + box.width() + between, 0, words_width, upper)
        self.lyrics.setGeometry(words)
        self.lyrics.set_text_size(max(34, min(96, int(height * 0.044))))
        self._caption.setGeometry(words.adjusted(0, words.height() // 2 - 40, 0, 0))
        self.wave.setGeometry(int(stage.width() * 0.06), stage.height() - band,
                              stage.width() - int(stage.width() * 0.12), band)
        self._sync_caption()

    def _build_glow(self, colour: QColor) -> QPixmap:
        """One soft pool of the album's colour behind the record.

        Drawn at 256 px and scaled up, the way NowPlayingView._build_backdrop
        blurs at 120: a gradient this soft is indistinguishable enlarged, and a
        full-screen one would be 33 MB at 4K.
        """
        size = 256
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        gradient = QRadialGradient(size / 2.0, size / 2.0, size / 2.0)
        for stop, alpha in ((0.0, 64), (0.45, 30), (0.75, 9), (1.0, 0)):
            tint = QColor(colour)
            tint.setAlpha(alpha)
            gradient.setColorAt(stop, tint)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(0, 0, size, size)
        painter.end()
        return pixmap

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(event.rect(), Qt.GlobalColor.black)
        if self._glow_for is None:
            return
        if self._glow is None:
            self._glow = self._build_glow(QColor(self._glow_for))
        centre = self.vinyl.geometry().center() + self._stage.pos()
        reach = int(self.vinyl.height() * 1.45)
        area = QRect(centre.x() - reach, centre.y() - reach, 2 * reach, 2 * reach)
        if not area.intersects(event.rect()):
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setOpacity(self._dim)
        painter.drawPixmap(area, self._glow)


def _field(track, name: str):
    """One column of a track, whether it arrived as a dict (the play queue) or
    a sqlite3.Row (library.tracks)."""
    try:
        return track[name] or ""
    except (TypeError, KeyError, IndexError):
        return ""


def _track_line(track) -> str:
    title = _field(track, "title")
    artist = _field(track, "artist") or _field(track, "album_artist")
    return "  ·  ".join(part for part in (title, artist) if part)
