"""The song's shape, as a band of bars under the screensaver's record.

The levels come from the file itself (music/peaks.py), so this is the real
song and not a decoration that moves to a timer. That has one very useful
consequence for a thing meant to run all night: the bars never change height.
Only two things move — which side of the playhead a bar is on, and a soft glow
that travels with it — so a frame repaints a few hundred pixels around the
playhead instead of the whole band.

Sizes, on this machine: at 1920 wide the band asks for 240 bars, at 3840 for
480 (music/peaks.py stores 480, so 4K is the resolution it was measured at).
The glow window is 21 bars, which is 336 px at 4K; at 20 fps that is about
1.5 MP a second, against the ~2 MP a second the Now Playing background was
cached to avoid.

While a song has not been measured yet the band draws a flat line with a slow
swell moving along it. It is deliberately nothing like a waveform: faking a
beat would be a lie, and this player measures loudness for a living.

The frames can come from here or from somebody else's clock (`set_driven`). In
the screensaver they come from the record's, because two 50 ms timers on one
screen flush the window's backing store 36.6 times a second between them for a
picture that changes 20 times a second.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..music import peaks
from .vinyl import readable_accent

FRAME_MS = 50           # 20 fps: nothing here needs more, and this runs for hours

_MIN_BAR = 8.0          # px per bar, including its gap
_GLOW_BARS = 10         # bars either side of the playhead that the glow reaches
_GLOW_PERIOD = 5.0      # seconds for the glow to breathe once
_IDLE_PERIOD = 6.0      # seconds for the unmeasured swell to cross the band
_PLAYED = 0.92          # alpha of a bar behind the playhead
_AHEAD = 0.20           # ...and in front of it


class WaveformView(QWidget):
    """A band of bars across the whole song. `set_track` asks peaks.py for the
    levels and orders a measurement if there are none yet."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._track = None
        self._levels: list[float] = []
        self._accent = readable_accent("#E50914")
        self._dim = 1.0
        self._playing = False
        self._position = 0.0
        self._duration = 0.0
        self._position_at = time.monotonic()
        self._glow_at = -1.0            # where the glow was last painted, in bars
        self._played_bars = -1
        self._driven = False            # somebody else is calling tick()
        self.frames = 0                 # frames drawn by the timer (for measuring)

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)

    # --- public -------------------------------------------------------------------

    def set_track(self, track) -> None:
        self._track = track
        self._levels = []
        self._played_bars = -1
        self._position = 0.0
        self._position_at = time.monotonic()
        self._pull_levels()
        if not self._levels and track is not None:
            peaks.request(track)
        self.update()

    def set_accent(self, colour: str) -> None:
        accent = readable_accent(colour)
        if accent != self._accent:
            self._accent = accent
            self.update()

    def set_dim(self, level: float) -> None:
        level = max(0.0, min(1.0, float(level)))
        if abs(level - self._dim) > 0.004:
            self._dim = level
            self.update()

    def set_playing(self, playing: bool) -> None:
        self._position = self.position
        self._position_at = time.monotonic()
        self._playing = bool(playing)
        self._sync_timer()

    def set_position(self, position: float, duration: float) -> None:
        self._position = max(0.0, float(position))
        self._position_at = time.monotonic()
        if duration and duration > 0:
            self._duration = float(duration)
        if not self._frames_coming():
            # Paused, or off screen: the seek still has to show. While frames
            # are coming this must NOT repaint — position_changed arrives ~15
            # times a second and would undo the whole point of one clock.
            self._refresh(self.position)

    @property
    def position(self) -> float:
        """Where the song is now, extrapolated between the ~15 position updates
        a second, and clamped the way LyricsView clamps its own: a stream that
        has stopped arriving must not run the playhead away on its own."""
        if self._playing:
            return self._position + min(1.0, time.monotonic() - self._position_at)
        return self._position

    @property
    def measured(self) -> bool:
        return bool(self._levels)

    @property
    def bars(self) -> int:
        return len(self._levels) if self._levels else self._bar_count()

    def poll_levels(self) -> bool:
        """Ask peaks.py again for a song that was still being measured. Called
        about once a second by the screensaver, so it costs one 500 byte read."""
        if self._levels or self._track is None:
            return False
        self._pull_levels()
        if self._levels:
            self._played_bars = -1
            self.update()
            return True
        return False

    # --- frames -------------------------------------------------------------------

    def set_driven(self, driven: bool) -> None:
        """Take frames from somebody else's clock instead of running one here.

        The screensaver shows this under a record that already ticks at 50 ms.
        Two timers of the same period do not land in the same pass of the event
        loop, so the window's backing store was flushed 36.6 times a second for
        a picture that changes 20 times a second. Driven, both updates arrive in
        one pass and the flush happens once.
        """
        self._driven = bool(driven)
        self._sync_timer()

    def tick(self) -> None:
        """One frame, from the clock that is driving this (see set_driven)."""
        if self._driven and self._wants_frames():
            self._tick()

    def _wants_frames(self) -> bool:
        window = self.window()
        on_screen = self.isVisible() and (window is None or not window.isMinimized())
        return bool(self._playing and on_screen)

    def _frames_coming(self) -> bool:
        return self._timer.isActive() or (self._driven and self._wants_frames())

    def _sync_timer(self) -> None:
        if self._wants_frames() and not self._driven:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        self.frames += 1
        if self._levels:
            self._refresh(self.position)
        else:
            self._idle_step()

    def _refresh(self, position: float) -> None:
        """Repaint the glow where it was and where it is, plus any bar that has
        just changed side. Everything else on the band is exactly as it was."""
        bars = self.bars
        if bars <= 0 or self.width() <= 0:
            return
        at = self._bar_at(position)
        dirty = self._span(at - _GLOW_BARS, at + _GLOW_BARS)
        if self._glow_at >= 0:
            dirty = dirty.united(self._span(self._glow_at - _GLOW_BARS, self._glow_at + _GLOW_BARS))
        played = int(at)
        if played != self._played_bars:
            dirty = dirty.united(self._span(min(played, self._played_bars), max(played, self._played_bars)))
            self._played_bars = played
        self._glow_at = at
        self.update(dirty)

    def _bar_at(self, position: float) -> float:
        if not self._duration:
            return 0.0
        return max(0.0, min(1.0, position / self._duration)) * self.bars

    def _bar_count(self) -> int:
        return max(80, min(peaks.STORED_BARS, int(self.width() / _MIN_BAR)))

    def _span(self, first: float, last: float) -> QRect:
        step = self.width() / max(1, self.bars)
        left = int(max(0.0, first * step) - 2)
        right = int(min(float(self.width()), (last + 1) * step) + 2)
        return QRect(left, 0, max(0, right - left), self.height())

    def _pull_levels(self) -> None:
        if self._track is None or self.width() <= 0:
            return
        found = peaks.peaks_for(self._track, self._bar_count())
        self._levels = found or []

    # --- events -------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._levels:
            self._pull_levels()
        self._sync_timer()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # More bars fit, or fewer: re-reduce rather than stretch what we had.
        self._levels = []
        self._pull_levels()
        self._played_bars = -1
        self._glow_at = -1.0

    # --- painting -----------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        clip = QRectF(event.rect())
        height = float(self.height())
        middle = height / 2.0
        if not self._levels:
            self._paint_idle(painter, clip, middle)
            return

        bars = len(self._levels)
        step = self.width() / bars
        width = max(1.0, step * 0.62)
        reach = middle - 3.0
        at = self._bar_at(self.position)
        glow_phase = 0.5 + 0.5 * math.sin(time.monotonic() * 2 * math.pi / _GLOW_PERIOD)
        first = max(0, int(clip.left() / step) - 1)
        last = min(bars - 1, int(clip.right() / step) + 1)
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(first, last + 1):
            level = self._levels[index]
            tall = max(1.5, level * reach)
            x = index * step + (step - width) / 2.0
            distance = abs(index + 0.5 - at)
            if index < at:
                colour = QColor(self._accent)
                alpha = _PLAYED
            else:
                colour = QColor(255, 255, 255)
                alpha = _AHEAD
            if distance <= _GLOW_BARS:
                # The one thing that moves: a soft swell around the playhead,
                # breathing over five seconds so it is never a fixed bright mark.
                lift = math.exp(-(distance / (_GLOW_BARS * 0.55)) ** 2) * (0.45 + 0.55 * glow_phase)
                alpha = alpha + (1.0 - alpha) * lift
                colour = _mix(colour, QColor(255, 255, 255), 0.35 * lift)
            colour.setAlphaF(max(0.0, min(1.0, alpha * self._dim)))
            painter.setBrush(colour)
            painter.drawRect(QRectF(x, middle - tall, width, tall * 2.0))

    def _paint_idle(self, painter: QPainter, clip: QRectF, middle: float) -> None:
        """No measurement yet: a line with a swell moving along it. Not the song,
        and not pretending to be."""
        phase = (time.monotonic() % _IDLE_PERIOD) / _IDLE_PERIOD
        centre = phase * self.width()
        bars = self._bar_count()
        step = self.width() / bars
        width = max(1.0, step * 0.62)
        first = max(0, int(clip.left() / step) - 1)
        last = min(bars - 1, int(clip.right() / step) + 1)
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(first, last + 1):
            x = index * step + (step - width) / 2.0
            distance = abs(x + width / 2.0 - centre) / max(1.0, self.width() * 0.12)
            swell = math.exp(-distance * distance)
            tall = 1.5 + swell * (middle - 3.0) * 0.22
            colour = QColor(255, 255, 255)
            colour.setAlphaF(max(0.0, min(1.0, (0.10 + 0.30 * swell) * self._dim)))
            painter.setBrush(colour)
            painter.drawRect(QRectF(x, middle - tall, width, tall * 2.0))

    def _idle_step(self) -> None:
        """Move the unmeasured swell along, so a song still being measured is
        not a dead band."""
        if self._levels or self.width() <= 0:
            return
        phase = (time.monotonic() % _IDLE_PERIOD) / _IDLE_PERIOD
        centre = phase * self.width()
        reach = self.width() * 0.34
        self.update(QRect(int(centre - reach), 0, int(2 * reach), self.height()))


def _mix(a: QColor, b: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, amount))
    return QColor(int(a.red() + (b.red() - a.red()) * amount),
                  int(a.green() + (b.green() - a.green()) * amount),
                  int(a.blue() + (b.blue() - a.blue()) * amount))
