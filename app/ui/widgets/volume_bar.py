"""The volume bar, in one place for the music bar, Now Playing and the film player.

Painted rather than a QSlider because everything worth having here is something
a QSlider will not do. The app runs Fusion (main.py), whose "jump to the click"
button is the middle one, so a left click on a QSlider groove is a repeating
10-unit page step towards the pointer — clicking a volume bar where you want it
simply did not work. A QSlider also has no state besides its value, so mute had
to be faked as "set it to 0 and remember the level somewhere else", and it has
nowhere to put a number.

Built in the manner of player_overlay.SeekBar: a QWidget that draws itself, a
26 px hit area around a thin track, and the mouse grabbed for the length of a
drag so running off the end of the bar pins to the end instead of stopping.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import C

# One wheel notch, and one with Shift held, in units of mpv's volume. The film
# picture (player_overlay.wheelEvent), the film keys (player_view.handle_key)
# and the music keys (main_window._nudge_music_volume) all move by these, so
# the volume steps the same amount however you ask for it. Before this, the
# wheel moved 5 over the picture and 3 over the bar — Qt's own default of
# singleStep(1) x wheelScrollLines(3) — which read as the bar being sticky.
WHEEL_STEP = 5
FINE_STEP = 1

_HEIGHT = 26                    # the hit area; the visible track is a few px of it
_TRACK_HEIGHT = 4.0
_TRACK_HOVER_HEIGHT = 6.0
_KNOB_RADIUS = 6.0

# How much quieter the far left of the bar is than the far right.
#
# mpv's volume property is softvol and cubic (gain = (volume/100)**3), so a bar
# that is linear in that number is not linear in loudness: 50 is -18 dB, 20 is
# -42, and everything under 10 is past -60. On the 104 px music bar that made
# the bottom ten pixels dead travel and the top of the bar hyper-fine (0.28 dB
# per unit at 95 against 2.75 at 10). Spreading a fixed 40 dB over the travel
# instead gives about 0.43 dB per percent of the bar the whole way along.
#
# The stored number does not change — only where the handle for it is drawn.
# It has to stay mpv's own volume, because the sleep timer's fade multiplies
# it. (The loudness levelling does not: it is a dB gain in mpv's af chain,
# worked out from the file's own measured LUFS, and never reads this.)
#
# What it costs: the handle moves for the same volume. Music at 70 sat at 70%
# of the bar and now sits at 78%; a film at 80 of 150 sat at 53% and now sits
# at 62%. Nothing sounds different until you next touch it.
_SPAN_DB = 40.0
# ...and the first 6% of the travel runs straight down to silence, so a level
# below the span's floor (22 for music, 32 for a film) still has a place to sit
# and drags out of it smoothly instead of jumping.
_FLOOR = 0.06


def _db(value: float) -> float:
    """mpv's volume number as dB of gain: 100 is 0 dB, 50 is -18, 20 is -42."""
    return 60.0 * math.log10(max(float(value), 1e-6) / 100.0)


def _floor_value(high: int) -> float:
    return 100.0 * 10.0 ** ((_db(high) - _SPAN_DB) / 60.0)


def value_at(fraction: float, low: int = 0, high: int = 100) -> int:
    """The volume a point along the bar means."""
    fraction = max(0.0, min(1.0, float(fraction)))
    if high <= low:
        return int(low)
    floor = _floor_value(high)
    if low > 0 or floor <= low:
        # Not a 0-based range: nothing to be perceptual about, draw it straight.
        return int(round(low + (high - low) * fraction))
    if fraction <= _FLOOR:
        return int(round(floor * fraction / _FLOOR))
    span = (fraction - _FLOOR) / (1.0 - _FLOOR)
    return int(round(100.0 * 10.0 ** ((_db(high) - _SPAN_DB * (1.0 - span)) / 60.0)))


def fraction_of(value: float, low: int = 0, high: int = 100) -> float:
    """Where a volume sits along the bar — the inverse of value_at."""
    value = max(low, min(high, float(value)))
    if high <= low:
        return 0.0
    floor = _floor_value(high)
    if low > 0 or floor <= low:
        return (value - low) / (high - low)
    if value <= 0:
        return 0.0
    if value <= floor:
        return _FLOOR * value / floor
    span = 1.0 - (_db(high) - _db(value)) / _SPAN_DB
    return _FLOOR + max(0.0, min(1.0, span)) * (1.0 - _FLOOR)


class VolumeBar(QWidget):
    """A level, and whether it is muted. The value is always mpv's own volume
    number (0-100 for music, 0-150 for a film), never a reshaped one: the sleep
    timer's fade multiplies that number."""

    value_changed = Signal(int)          # a real change only, never from set_value

    def __init__(self, low: int = 0, high: int = 100, parent=None) -> None:
        super().__init__(parent)
        self._low = int(low)
        self._high = int(high)
        self._value = int(high)
        self._muted = False
        self._dragging = False
        self._hover = False
        self._hover_x: float | None = None
        self._accent = QColor(C.TEXT)
        self.setFixedHeight(_HEIGHT)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # The film overlay refuses focus as a whole window and the music keys
        # are window shortcuts: this must never be what the keyboard is talking
        # to, the same reason IconButton is NoFocus.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def sizeHint(self) -> QSize:
        return QSize(110, _HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(40, _HEIGHT)

    # --- state --------------------------------------------------------------

    def set_range(self, low: int, high: int) -> None:
        self._low, self._high = int(low), int(high)
        self._value = max(self._low, min(self._high, self._value))
        self.update()

    def value(self) -> int:
        return int(self._value)

    def set_value(self, value: int) -> None:
        """Show a level chosen elsewhere. Never emits, and never fights a drag:
        the film's mpv echoes its volume property back while the handle is
        still under the finger."""
        if self._dragging:
            return
        value = max(self._low, min(self._high, int(value)))
        if value != self._value:
            self._value = value
            self.update()

    def set_muted(self, muted: bool) -> None:
        muted = bool(muted)
        if muted != self._muted:
            self._muted = muted
            self.update()

    def is_muted(self) -> bool:
        return self._muted

    def set_accent(self, colour: str) -> None:
        """The music player tints this with the colour of the record."""
        self._accent = QColor(colour)
        self.update()

    @property
    def is_dragging(self) -> bool:
        return self._dragging

    def nudge(self, step: int) -> None:
        """A wheel notch or a key, in mpv's units."""
        self._commit(self._value + int(step))

    # --- geometry -----------------------------------------------------------

    def _track_rect(self) -> QRectF:
        height = _TRACK_HOVER_HEIGHT if (self._hover or self._dragging) else _TRACK_HEIGHT
        return QRectF(0.0, (self.height() - height) / 2.0, float(self.width()), height)

    def _knob_x(self, fraction: float) -> float:
        """Kept a knob's radius inside the ends, so the circle is never clipped
        and the bar keeps its whole width as travel."""
        width = float(self.width())
        return max(_KNOB_RADIUS, min(width - _KNOB_RADIUS, width * fraction))

    def _commit(self, value: int) -> None:
        value = max(self._low, min(self._high, int(value)))
        if value == self._value:
            return
        self._value = value
        self.update()
        self.value_changed.emit(value)

    def _set_from_x(self, x: float) -> None:
        width = float(self.width())
        if width <= 0:
            return
        # The travel is width - 1, not width: the largest x a mouse event on a
        # 104 px widget carries is 103, so x / width never reached 1.0 and the
        # top of the range could not be clicked at all — the now bar stopped at
        # 98, the film bar at 148 — while the fill painted the whole width and
        # the handle sat hard right, so nothing on screen said so.
        self._commit(value_at(x / max(1.0, width - 1.0), self._low, self._high))

    # --- interaction --------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._dragging = True
        self._hover_x = event.position().x()
        self._set_from_x(self._hover_x)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and not (event.buttons() & Qt.MouseButton.LeftButton):
            # The button came up somewhere we never heard about — the grab lost
            # to another window. This move is a hover, not a drag.
            self._end_drag()
            self._hover = self.rect().contains(event.position().toPoint())
        self._hover_x = event.position().x()
        if self._dragging:
            # Qt grabs the mouse for the length of a press, so these keep
            # arriving once the pointer has left the widget: a drag that runs
            # off the right-hand end pins to the top instead of stopping
            # wherever it happened to cross the edge.
            self._set_from_x(self._hover_x)
        else:
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._set_from_x(event.position().x())
        self._hover = self.rect().contains(event.position().toPoint())
        self.update()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        step = FINE_STEP if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else WHEEL_STEP
        self.nudge(step if delta > 0 else -step)
        event.accept()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self._hover_x = None
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        # A drag can end without ever sending a release: the widget hidden under
        # the pointer (the film's chrome going away, a page change), or the grab
        # lost. Left set, _dragging freezes set_value for good and keeps the
        # film's controls from ever auto-hiding again (player_overlay.hide_chrome).
        self._end_drag()
        super().hideEvent(event)

    def _end_drag(self) -> None:
        self._dragging = False
        self._hover = False
        self._hover_x = None

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # The film overlay is a layered window that Windows hit-tests per pixel:
        # anywhere alpha is 0 the mouse falls through to mpv's own child window,
        # which ignores it. One step above nothing keeps the whole 26 px hit
        # area clickable and is invisible over anything.
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))

        track = self._track_rect()
        radius = track.height() / 2.0
        fraction = fraction_of(self._value, self._low, self._high)
        active = self._hover or self._dragging

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 48))
        painter.drawRoundedRect(track, radius, radius)

        fill_colour = QColor(self._accent)
        if self._muted:
            # Muted reads as a state, not as a level of zero: the fill stays
            # where the level is, dimmed, so you can see what unmuting returns.
            fill_colour.setAlpha(90)
        if fraction > 0:
            filled = QRectF(track.left(), track.top(), track.width() * fraction, track.height())
            painter.setBrush(fill_colour)
            painter.drawRoundedRect(filled, radius, radius)

        # Where the file's own level is, on a bar that goes past it. Only the
        # film does: 150 exists for quiet films, 100 is "as recorded".
        if self._high > 100:
            mark = self._knob_x(fraction_of(100, self._low, self._high))
            painter.setBrush(QColor(255, 255, 255, 110))
            painter.drawRect(QRectF(mark - 0.9, track.top() - 1.5, 1.8, track.height() + 3))

        knob_x = self._knob_x(fraction)
        if active or self._muted:
            painter.setBrush(fill_colour)
            painter.setPen(QPen(QColor(0, 0, 0, 120), 1))
            painter.drawEllipse(QPointF(knob_x, track.center().y()), _KNOB_RADIUS, _KNOB_RADIUS)
            if self._muted:
                # Crossed out, so a dimmed fill is not mistaken for a theme.
                painter.setPen(QPen(QColor(12, 12, 12, 235), 2.0))
                offset = _KNOB_RADIUS * 0.72
                painter.drawLine(
                    QPointF(knob_x - offset, track.center().y() + offset),
                    QPointF(knob_x + offset, track.center().y() - offset),
                )

        if active:
            # The number, only while you are working the bar: it is the only way
            # to tell 62 from 65, and it makes a wheel notch legible. The level
            # you are at, not the one under the pointer — you look at a volume
            # readout to answer "what am I on", and the fill already shows where
            # a click would land.
            font = QFont(self.font())
            font.setPointSizeF(7.0)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255, 235))
            text = "off" if self._muted else str(self._value)
            metrics = QFontMetricsF(font)
            # Sat on a baseline just above the track rather than centred in the
            # 10 px band: digits are cap height (6.3 px at 7pt, measured), so
            # centring a 12 px line box in 10 px clipped their tops off.
            width = metrics.horizontalAdvance(text)
            left = max(0.0, min(self.width() - width, knob_x - width / 2))
            painter.drawText(QPointF(left, track.top() - 2.0), text)
