"""The Now Playing cover as a record on a turntable.

A near-black record with fine grooves, the album cover as its centre label, and
a tonearm that swings onto it when the music plays and lifts off when it stops.
It turns at 33⅓ rpm, runs up to speed and coasts down like a platter does, and
the light on it stays where it is while the record turns underneath: that fixed
sheen is most of what makes it read as spinning rather than as a picture being
rotated.

Cost is what shaped the drawing. The user games with music on, so this has to
be close to free:

* Everything that looks rotationally the same — the grooves, the rim, the sheen,
  the accent ring, the shadow — is drawn once per size into a cached pixmap at
  the screen's pixel ratio and never rotated. A record's grooves look the same
  at every angle, so turning them would be work nobody could see.
* The only thing that visibly turns is the label, so a frame rotates one small
  pixmap (the label is 40% of the diameter, 16% of the area) and asks Qt to
  repaint just that square. The arm is repainted only when it has moved.
* Frames run only while the record is turning, running up, coasting down or
  the arm is moving, and only while it can be seen: a hidden page, a minimised
  window or the tray stop the timer outright.

Measured numbers are in the docstring of `VinylView`.
"""

from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QConicalGradient, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient, QTransform,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..images import load_async
from .widgets.icons import paint_icon

# 33⅓ revolutions a minute is 200 degrees a second.
RPM = 100.0 / 3.0
DEGREES_PER_SECOND = RPM * 6.0
SPIN_UP = 0.8          # seconds from standstill to speed
SPIN_DOWN = 1.2        # seconds to coast from speed to a stop
ARM_SWING = 0.5        # seconds for the arm to swing on or off the record
ARM_RETURN = 0.6       # seconds to glide back to the lead-in when the song changes
FRAME_MS = 33

LABEL = 0.40           # label radius as a share of the record's (40% of the diameter)

# Turntable geometry, in record radii from the record's centre. The pivot sits
# off the top-right of the record; the arm points straight down at rest and
# swings clockwise onto the record. Where it lands (the lead-in) and where it
# has got to by the end of a song are angles solved from these below, so the
# stylus sits on the grooves at any size.
_PIVOT = (0.99, -0.80)
_ARM = 1.16            # pivot to the headshell joint
_HEADSHELL = 0.21      # joint to stylus tip
_OFFSET = 24.0         # headshell angle against the arm, degrees
_REST = -8.0           # arm angle at rest, degrees (0 = straight down): clear of the rim
_LEAD_IN = 0.905       # stylus radius when a song starts
_RUN_OUT = 0.585       # ...and when it ends
_BASE = 0.125          # radius of the pivot's base
_SHADOW_REACH = (0.045, 0.075)   # furthest the arm's shadow falls (lifted), in radii
_BOTTOM_GAP = 26.0     # px under the record, for its shadow: the flat cover's gap plus a little


def _arm_outline(angle: float) -> list[tuple[float, float]]:
    """The corners of the arm (counterweight, tube, headshell, finger lift) with it
    at `angle`, in record radii from the record's centre."""
    a = math.radians(angle)
    cos_a, sin_a = math.cos(a), math.sin(a)
    b = math.radians(_OFFSET)
    cos_b, sin_b = math.cos(b), math.sin(b)
    local = [(-0.08, -0.29), (0.08, -0.29), (-0.02, 0.0), (0.02, 0.0), (-0.02, _ARM), (0.02, _ARM)]
    for x, y in ((-0.05, -0.03), (0.05, -0.03), (-0.045, _HEADSHELL + 0.015),
                 (0.045, _HEADSHELL + 0.015), (0.14, 0.0), (0.14, 0.03)):
        local.append((x * cos_b - y * sin_b, x * sin_b + y * cos_b + _ARM))
    return [(_PIVOT[0] + x * cos_a - y * sin_a, _PIVOT[1] + x * sin_a + y * cos_a) for x, y in local]


def _extent() -> tuple[float, float, float]:
    """(left, right, top) of the record, base and arm in any position, shadow included."""
    xs, ys = [-1.0, 1.0, _PIVOT[0] + _BASE + 0.03], [-1.0, _PIVOT[1] - _BASE]
    for step in range(9):
        angle = _REST + (_ANGLE_RUN_OUT - _REST) * step / 8
        for x, y in _arm_outline(angle):
            xs += [x, x + _SHADOW_REACH[0]]
            ys.append(y)
    return min(xs), max(xs), min(ys)


def _stylus(angle: float) -> tuple[float, float]:
    """Where the stylus tip is, in record radii, with the arm at `angle` degrees."""
    a = math.radians(angle)
    b = math.radians(angle + _OFFSET)
    x = _PIVOT[0] - _ARM * math.sin(a) - _HEADSHELL * math.sin(b)
    y = _PIVOT[1] + _ARM * math.cos(a) + _HEADSHELL * math.cos(b)
    return x, y


def _angle_for(radius: float) -> float:
    """The arm angle that puts the stylus `radius` from the centre."""
    best, best_error = 0.0, 1e9
    for step in range(0, 1200):
        angle = step * 0.05
        x, y = _stylus(angle)
        error = abs(math.hypot(x, y) - radius)
        if error < best_error:
            best, best_error = angle, error
    return best


_ANGLE_LEAD_IN = _angle_for(_LEAD_IN)
_ANGLE_RUN_OUT = _angle_for(_RUN_OUT)
_LEFT, _RIGHT, _TOP = _extent()


def _ease_in_out(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return 4 * u * u * u if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def readable_accent(colour: str) -> QColor:
    """The album's accent, lifted if it would vanish against black vinyl."""
    accent = QColor(colour or "#E50914")
    if not accent.isValid():
        accent = QColor("#E50914")
    hue, saturation, lightness, _ = accent.getHsl()
    if lightness < 110:
        accent.setHsl(max(0, hue), saturation, 110)
    return accent


class VinylView(QWidget):
    """The spinning record. A drop-in for CoverView: `set_cover(path)`.

    Also `set_accent(colour)` for the ring round the label, `set_playing()` to
    spin up or coast down, and `set_progress(position, duration)`, which walks
    the arm from the rim towards the label over the song.

    Measured with Now Playing on screen (1500x950 window at 150% scaling, record
    424 px across, 33 ms frames, lyrics hidden so the record's own share shows):
    the GUI process used ~4.2% of a core with the record turning against ~1.6%
    with the flat cover, i.e. about 2.6% for ~30 small frames a second. Paused,
    and with the page hidden or the window minimised, the record draws nothing.

    `frame` is emitted on every frame the timer runs, so something else on the
    same screen can share this clock instead of starting a second timer of its
    own — two 50 ms timers flush the window's backing store twice as often as
    one for the same picture. The screensaver's waveform rides on it.
    """

    frame = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(160, 160)

        self._path: str | None = None
        self._image = QImage()              # the cover, pre-shrunk once to label scale
        self._accent = readable_accent("#E50914")
        self._sheen = 1.0                   # the screensaver turns the fixed highlight down
        self._dim = 1.0                     # ...and the whole record, as the hours pass

        # Cached drawings, rebuilt only when their key (size, ratio, colour, cover) changes.
        self._body: QPixmap | None = None
        self._body_key: tuple | None = None
        self._label: QPixmap | None = None
        self._label_key: tuple | None = None
        self._gloss: QPixmap | None = None
        self._arm_paths: dict | None = None

        # Geometry, worked out on resize.
        self._radius = 100.0
        self._centre = QPointF(0, 0)

        # Motion. Speed is a share of 33⅓ rpm, eased between two values over time,
        # so a frame that arrives late (or after the window was hidden) still
        # lands where the platter would really be.
        self._angle = 0.0
        self._speed = 0.0
        self._speed_from = 0.0
        self._speed_to = 0.0
        self._speed_t0 = 0.0
        self._speed_duration = 0.0
        self._arm_on = 0.0                  # 0 resting off the record, 1 playing
        self._arm_from = 0.0
        self._arm_to = 0.0
        self._arm_t0 = 0.0
        self._arm_duration = 0.0
        self._progress = 0.0                # through the song, as shown by the arm
        self._progress_from = 0.0
        self._progress_to = 0.0
        self._progress_t0 = 0.0
        self._progress_duration = 0.0
        self._arm_angle = _REST             # what was last painted
        self._playing = False

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._last_frame = time.monotonic()
        self._watched_window: QWidget | None = None
        self.frames = 0                     # frames drawn by the timer (for measuring)

    # --- public -------------------------------------------------------------------

    def set_cover(self, path: str | None) -> None:
        if path == self._path:
            return
        self._path = path
        self._image = QImage()
        self._label = None
        if path:
            load_async(path, lambda image, wanted=path: self._on_image(image, wanted))
        self.update(self._label_rect())

    def set_accent(self, colour: str) -> None:
        accent = readable_accent(colour)
        if accent != self._accent:
            self._accent = accent
            self._body = None
            self._arm_paths = None
            self.update()

    def set_sheen(self, strength: float) -> None:
        """How strong the fixed highlight is, 0 to 1.

        It is the one thing on the record that never moves — that is what makes
        the record read as spinning rather than as a picture being turned — and
        on an OLED asked to hold this for hours it is also the one thing that
        would wear a shape into the panel. The screensaver asks for 0.35.
        """
        strength = max(0.0, min(1.0, float(strength)))
        if abs(strength - self._sheen) > 0.01:
            self._sheen = strength
            self._body = None
            self.update()

    def set_dim(self, level: float) -> None:
        """Fade the whole record. Painter opacity over the cached pixmaps, not a
        redraw: against black, opacity is the dimming, and rebuilding the body
        at screensaver size would cost a ~3.5 MB pixmap every step."""
        level = max(0.0, min(1.0, float(level)))
        if abs(level - self._dim) > 0.004:
            self._dim = level
            self.update()

    def set_frame_interval(self, milliseconds: int) -> None:
        """Slower frames for a record nobody is watching closely (the
        screensaver runs at 50 ms, i.e. 20 fps, against the usual 33)."""
        self._timer.setInterval(max(16, int(milliseconds)))

    @staticmethod
    def box_for_radius(radius: float) -> QSize:
        """How big this view has to be for the record to come out `radius`
        across. _layout() takes the smaller of the two fits, so give it both
        exactly — a caller that wants a particular diameter cannot get there by
        guessing at the tonearm's share of the width."""
        across = _RIGHT - _LEFT
        tall = 1.0 - _TOP
        return QSize(int(math.ceil(radius * across + 8.0)),
                     int(math.ceil(radius * tall + _BOTTOM_GAP + 4.0)))

    def set_playing(self, playing: bool, animate: bool = True) -> None:
        """Spin up and lower the arm, or lift it and coast to a stop."""
        playing = bool(playing)
        if animate and playing == self._playing:
            return              # state_changed also fires for shuffle and repeat
        now = time.monotonic()
        speed = self._speed_at(now)
        arm = self._arm_at(now)
        self._playing = playing
        target = 1.0 if playing else 0.0
        self._speed_from, self._speed_to, self._speed_t0 = speed, target, now
        self._arm_from, self._arm_to, self._arm_t0 = arm, target, now
        if animate:
            self._speed_duration = (SPIN_UP * (1.0 - speed)) if playing else (SPIN_DOWN * speed)
            self._arm_duration = ARM_SWING * abs(target - arm)
        else:
            self._speed_duration = self._arm_duration = 0.0
            self._speed = target
            self._arm_on = target
            self._arm_angle = self._arm_angle_for(target, self._progress)
            self.update()
        self._kick()

    def set_progress(self, position: float, duration: float) -> None:
        """Walk the arm inwards as the song plays; glide back when it jumps."""
        target = max(0.0, min(1.0, position / duration)) if duration and duration > 0 else 0.0
        now = time.monotonic()
        shown = self._progress_at(now)
        if abs(target - shown) > 0.03:
            # A new song or a seek: glide there instead of teleporting.
            self._progress_from, self._progress_to, self._progress_t0 = shown, target, now
            self._progress_duration = ARM_RETURN
            self._kick()
        else:
            self._progress_from = self._progress_to = target
            self._progress_duration = 0.0
            self._progress = target
            if not self._timer.isActive():
                self._repaint_arm_if_moved(now)

    @property
    def is_animating(self) -> bool:
        return self._timer.isActive()

    @property
    def speed(self) -> float:
        return self._speed_at(time.monotonic())

    @property
    def arm_position(self) -> float:
        """0 resting off the record … 1 on it."""
        return self._arm_at(time.monotonic())

    @property
    def angle(self) -> float:
        return self._angle

    # --- motion ---------------------------------------------------------------------

    def _speed_at(self, now: float) -> float:
        if self._speed_duration <= 0:
            return self._speed_to
        u = (now - self._speed_t0) / self._speed_duration
        if u >= 1.0:
            return self._speed_to
        if self._speed_to > self._speed_from:
            eased = u * u * (3 - 2 * u)             # a motor: gentle start, gentle arrival
        else:
            eased = 1 - (1 - u) ** 2                # friction: sheds speed fast, then glides
        return self._speed_from + (self._speed_to - self._speed_from) * eased

    def _arm_at(self, now: float) -> float:
        if self._arm_duration <= 0:
            return self._arm_to
        u = (now - self._arm_t0) / self._arm_duration
        if u >= 1.0:
            return self._arm_to
        return self._arm_from + (self._arm_to - self._arm_from) * _ease_in_out(u)

    def _progress_at(self, now: float) -> float:
        if self._progress_duration <= 0:
            return self._progress_to
        u = (now - self._progress_t0) / self._progress_duration
        if u >= 1.0:
            return self._progress_to
        return self._progress_from + (self._progress_to - self._progress_from) * _ease_in_out(u)

    @staticmethod
    def _arm_angle_for(arm_on: float, progress: float) -> float:
        playing = _ANGLE_LEAD_IN + (_ANGLE_RUN_OUT - _ANGLE_LEAD_IN) * progress
        return _REST + (playing - _REST) * arm_on

    def _moving(self, now: float) -> bool:
        return (self._speed_at(now) > 0.0
                or (self._speed_duration > 0 and now - self._speed_t0 < self._speed_duration)
                or (self._arm_duration > 0 and now - self._arm_t0 < self._arm_duration)
                or (self._progress_duration > 0 and now - self._progress_t0 < self._progress_duration))

    def _can_animate(self) -> bool:
        if not self.isVisible():
            return False
        window = self.window()
        return window is None or not window.isMinimized()

    def _kick(self) -> None:
        """Start frames if something is moving and can be seen; otherwise stop them."""
        now = time.monotonic()
        if self._moving(now) and self._can_animate():
            if not self._timer.isActive():
                self._last_frame = now
                self._timer.start()
        else:
            self._timer.stop()
            self._settle(now)

    def _settle(self, now: float) -> None:
        """Bring the stored state up to date without drawing a frame."""
        self._speed = self._speed_at(now)
        self._arm_on = self._arm_at(now)
        self._progress = self._progress_at(now)

    def _tick(self) -> None:
        if not self._can_animate():
            self._timer.stop()
            return
        now = time.monotonic()
        elapsed = min(0.1, max(0.0, now - self._last_frame))
        self._last_frame = now
        before = self._speed
        after = self._speed_at(now)
        self._speed = after
        dirty = QRect()
        if before > 0.0 or after > 0.0:
            self._angle = (self._angle + DEGREES_PER_SECOND * (before + after) / 2.0 * elapsed) % 360.0
            dirty = dirty.united(self._label_rect())
        self._arm_on = self._arm_at(now)
        self._progress = self._progress_at(now)
        dirty = dirty.united(self._arm_dirty_rect())
        if not dirty.isEmpty():
            self.frames += 1
            self.update(dirty)
        # Emitted whether or not this frame drew anything: a passenger's own
        # picture may have moved when the record's has not, and a beat that
        # skips is worse than one that sometimes costs nothing.
        self.frame.emit()
        if not self._moving(now):
            self._timer.stop()

    def _arm_dirty_rect(self) -> QRect:
        """The arm's old and new places, if it moved enough to show (a third of a pixel)."""
        angle = self._arm_angle_for(self._arm_on, self._progress)
        reach = (_ARM + _HEADSHELL) * self._radius * math.pi / 180.0
        if abs(angle - self._arm_angle) * reach < 0.33:
            return QRect()
        rect = self._arm_bounds(self._arm_angle).united(self._arm_bounds(angle))
        self._arm_angle = angle
        return rect

    def _repaint_arm_if_moved(self, now: float) -> None:
        self._settle(now)
        dirty = self._arm_dirty_rect()
        if not dirty.isEmpty():
            self.update(dirty)

    # --- events ---------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        window = self.window()
        if window is not None and window is not self._watched_window:
            if self._watched_window is not None:
                self._watched_window.removeEventFilter(self)
            window.installEventFilter(self)
            self._watched_window = window
        self._kick()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()
        self._settle(time.monotonic())

    def eventFilter(self, watched, event) -> bool:
        if watched is self._watched_window and event.type() == QEvent.Type.WindowStateChange:
            # Minimised stops the frames; restored starts them again.
            QTimer.singleShot(0, self._kick)
        return False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout()

    def sizeHint(self) -> QSize:
        return QSize(420, 420)

    def _on_image(self, image: QImage, wanted: str | None = None) -> None:
        if wanted != self._path:
            return              # a slower read for the song before; not ours any more
        if not image.isNull():
            # Shrunk once to comfortably above any label size, so a resize redraws
            # the label from a few hundred pixels rather than the full cover.
            side = min(image.width(), image.height())
            if side > 720:
                image = image.scaled(720, 720, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                     Qt.TransformationMode.SmoothTransformation)
        self._image = image
        self._label = None
        self.update(self._label_rect())

    # --- geometry --------------------------------------------------------------------

    def _layout(self) -> None:
        """As big as fits: the record with the arm beside it, centred together,
        sitting on the title below the way the flat cover does."""
        width, height = float(self.width()), float(self.height())
        across = _RIGHT - _LEFT
        tall = 1.0 - _TOP
        radius = max(30.0, min((width - 8.0) / across, (height - _BOTTOM_GAP - 4.0) / tall))
        self._radius = radius
        left = (width - across * radius) / 2.0
        self._centre = QPointF(left - _LEFT * radius, height - _BOTTOM_GAP - radius)
        self._arm_paths = None

    def _label_rect(self) -> QRect:
        r = self._radius * LABEL + 2
        c = self._centre
        return QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r).toAlignedRect()

    def _pivot(self) -> QPointF:
        return QPointF(self._centre.x() + _PIVOT[0] * self._radius,
                       self._centre.y() + _PIVOT[1] * self._radius)

    def _arm_bounds(self, angle: float) -> QRect:
        """Where the arm and its shadow are drawn at `angle`, in widget pixels."""
        R = self._radius
        c = self._centre
        points = _arm_outline(angle)
        xs = [c.x() + x * R for x, _ in points]
        ys = [c.y() + y * R for _, y in points]
        return QRectF(min(xs) - 4, min(ys) - 4,
                      max(xs) - min(xs) + _SHADOW_REACH[0] * R + 8,
                      max(ys) - min(ys) + _SHADOW_REACH[1] * R + 8).toAlignedRect()

    # --- painting ---------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        clip = event.rect()
        ratio = self.devicePixelRatioF()
        R = self._radius
        c = self._centre

        if self._dim < 1.0:
            painter.setOpacity(self._dim)
        key = (round(R, 2), ratio, self._accent.rgba(), round(self._sheen, 2))
        if self._body is None or self._body_key != key:
            self._body = self._build_body(R, ratio)
            self._body_key = key
            self._gloss = self._build_gloss(R, ratio)
        margin = 0.14 * R
        painter.drawPixmap(QPointF(c.x() - R - margin, c.y() - R - margin), self._body)

        label_rect = self._label_rect()
        if clip.intersects(label_rect):
            label_key = (round(R, 2), ratio, self._image.cacheKey(), self._path)
            if self._label is None or self._label_key != label_key:
                self._label = self._build_label(R, ratio)
                self._label_key = label_key
            radius = R * LABEL + 1.0
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.save()
            painter.translate(c)
            painter.rotate(self._angle)
            painter.drawPixmap(QRectF(-radius, -radius, 2 * radius, 2 * radius), self._label,
                               QRectF(self._label.rect()))
            painter.restore()
            painter.drawPixmap(QPointF(c.x() - radius, c.y() - radius), self._gloss)

        self._paint_arm(painter, clip)

    def _build_body(self, R: float, ratio: float) -> QPixmap:
        """Shadow, record, grooves, accent ring and the fixed sheen, drawn once."""
        margin = 0.14 * R
        side = 2 * (R + margin)
        pixels = max(1, int(math.ceil(side * ratio)))
        image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        centre = QPointF(R + margin, R + margin)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        # A soft shadow, a touch below: the record sits a little above the page.
        # It reaches (0.035 + 1.05) R below the centre, which stays inside the
        # gap left under the record at every size the view allows.
        spread = min(1.05, 1.0 + (_BOTTOM_GAP - 4.0) / max(R, 1.0) - 0.035)
        shadow = QRadialGradient(centre + QPointF(0.0, 0.035 * R), R * spread)
        shadow.setColorAt(0.0, QColor(0, 0, 0, 150))
        shadow.setColorAt(0.90, QColor(0, 0, 0, 130))
        shadow.setColorAt(0.955, QColor(0, 0, 0, 55))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setBrush(shadow)
        painter.drawEllipse(centre + QPointF(0.0, 0.035 * R), R * spread, R * spread)

        # The record: one radial gradient carries every groove, so a size change
        # redraws it in a single fill rather than hundreds of circles.
        rings = self._groove_rings(R * ratio)
        grooves = QRadialGradient(centre, R)
        grooves.setStops(self._groove_stops(rings))
        painter.setBrush(grooves)
        painter.drawEllipse(centre, R, R)

        # The accent ring round the label: the album's colour, as a pressing's
        # label ring would be.
        ring = QPen(self._accent, max(1.2, 0.011 * R))
        painter.setPen(ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, R * (LABEL + 0.007), R * (LABEL + 0.007))
        faint = QColor(self._accent)
        faint.setAlpha(60)
        painter.setPen(QPen(faint, 1.0))
        painter.drawEllipse(centre, R * 0.442, R * 0.442)
        painter.setPen(Qt.PenStyle.NoPen)

        # The sheen: light caught by the grooves in two opposite wedges, the
        # bow tie a lamp makes on real vinyl. It is drawn into its own layer and
        # masked by the groove pattern, so it glitters along the rings instead
        # of lying on top of them like a sticker.
        sheen = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
        sheen.setDevicePixelRatio(ratio)
        sheen.fill(Qt.GlobalColor.transparent)
        layer = QPainter(sheen)
        layer.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        layer.setPen(Qt.PenStyle.NoPen)
        tint = QColor(255, 255, 255)
        mix = 0.14
        tint = QColor(int(255 * (1 - mix) + self._accent.red() * mix),
                      int(255 * (1 - mix) + self._accent.green() * mix),
                      int(255 * (1 - mix) + self._accent.blue() * mix))
        cone = QConicalGradient(centre, 0.0)
        steps = 90
        for step in range(steps + 1):
            degrees = step * 360.0 / steps
            # A bright narrow core inside a broad soft glow, strongest upper
            # left where the lamp is, weaker in the opposite wedge, and a faint
            # cross light so the rest of the record isn't dead flat.
            alpha = (0.34 * self._lobe(degrees, 128.0, 11.0) + 0.22 * self._lobe(degrees, 128.0, 30.0)
                     + 0.20 * self._lobe(degrees, 308.0, 12.0) + 0.14 * self._lobe(degrees, 308.0, 32.0)
                     + 0.05 * self._lobe(degrees, 38.0, 40.0) + 0.04 * self._lobe(degrees, 218.0, 40.0))
            alpha *= self._sheen
            colour = QColor(tint)
            colour.setAlphaF(min(1.0, alpha))
            cone.setColorAt(step / steps, colour)
        layer.setBrush(cone)
        layer.drawEllipse(centre, R, R)
        layer.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        mask = QRadialGradient(centre, R)
        mask.setStops(self._sheen_mask_stops(rings))
        layer.setBrush(mask)
        layer.drawEllipse(centre, R, R)
        layer.end()
        painter.drawImage(QPointF(0, 0), sheen)

        # A hairline where the rim turns over.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 20), 1.0))
        painter.drawEllipse(centre, R - 0.5, R - 0.5)
        painter.end()
        return QPixmap.fromImage(image)

    @staticmethod
    def _lobe(degrees: float, centre: float, width: float) -> float:
        distance = abs((degrees - centre + 180.0) % 360.0 - 180.0)
        return math.exp(-(distance / width) ** 2)

    @staticmethod
    def _groove_rings(radius_px: float) -> list[tuple[float, float, float]]:
        """(start, end, brightness) for each groove, in shares of the radius.

        A groove every ~2.4 device pixels whatever the size, so the rings stay
        fine and never beat against the pixel grid. Brightness wanders slowly
        (quiet and loud passages cut differently) with a little noise on top,
        and four smooth bands mark the gaps between songs.
        """
        rng = random.Random(33)
        pitch = max(2.3, radius_px / 150.0) / max(1.0, radius_px)
        gaps = ((0.612, 0.622), (0.705, 0.713), (0.792, 0.801), (0.872, 0.879))
        rings = []
        t = 0.505
        while t < 0.95:
            gap = next((g for g in gaps if g[0] <= t < g[1]), None)
            if gap:
                t = gap[1]
                continue
            wander = 0.5 + 0.5 * math.sin(t * 41.0) * math.sin(t * 13.0 + 1.0)
            rings.append((t, min(0.95, t + pitch), 0.35 + 0.45 * wander + 0.2 * rng.random()))
            t += pitch
        return rings

    @staticmethod
    def _groove_stops(rings) -> list:
        def grey(level: int, blue: int = 2) -> QColor:
            return QColor(level, level, level + blue)

        stops = [(0.0, grey(9, 1)), (LABEL - 0.004, grey(9, 1)),
                 # dead wax by the label: matte, a shade lighter
                 (LABEL + 0.012, grey(22)), (0.438, grey(19)),
                 # the run-out: glossy black, with the locked groove's two wide turns
                 (0.446, grey(10, 1)), (0.462, grey(12, 1)), (0.466, grey(24)),
                 (0.470, grey(11, 1)), (0.484, grey(12, 1)), (0.488, grey(23)),
                 (0.492, grey(11, 1)), (0.503, grey(12, 1))]
        for start, end, brightness in rings:
            middle = (start + end) / 2.0
            stops.append((start + 0.0001, grey(8, 1)))
            stops.append((middle, grey(int(12 + 10 * brightness))))
        # the lead-in: a smooth band before the first groove
        stops += [(0.951, grey(12, 1)), (0.956, grey(20)), (0.960, grey(13, 1)),
                  (0.982, grey(15)),
                  # the rim rolls over: a bright edge, then dark
                  (0.989, grey(36, 3)), (0.995, grey(20)), (1.0, grey(7, 1))]
        cleaned, last = [], -1.0
        for position, colour in stops:
            position = max(last + 1e-5, min(1.0, position))
            if position > 1.0 or position <= last:
                continue
            cleaned.append((position, colour))
            last = position
        return cleaned

    @staticmethod
    def _sheen_mask_stops(rings) -> list:
        def alpha(value: float) -> QColor:
            return QColor(0, 0, 0, int(max(0.0, min(1.0, value)) * 255))

        stops = [(0.0, alpha(0.0)), (0.425, alpha(0.0)), (0.47, alpha(0.55)), (0.503, alpha(0.7))]
        for start, end, brightness in rings:
            stops.append((start + 0.0001, alpha(0.42)))
            stops.append(((start + end) / 2.0, alpha(0.75 + 0.25 * brightness)))
        stops += [(0.951, alpha(0.6)), (0.975, alpha(0.8)), (0.989, alpha(1.0)), (1.0, alpha(0.0))]
        cleaned, last = [], -1.0
        for position, colour in stops:
            position = max(last + 1e-5, min(1.0, position))
            if position <= last:
                continue
            cleaned.append((position, colour))
            last = position
        return cleaned

    def _build_label(self, R: float, ratio: float) -> QPixmap:
        """The cover as a round label. This is the one drawing that turns."""
        radius = R * LABEL + 1.0
        pixels = max(1, int(math.ceil(2 * radius * ratio)))
        image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        disc = QPainterPath()
        disc.addEllipse(QRectF(0, 0, 2 * radius, 2 * radius))
        painter.setClipPath(disc)
        if not self._image.isNull():
            scale = max(2 * radius / self._image.width(), 2 * radius / self._image.height())
            width, height = self._image.width() * scale, self._image.height() * scale
            painter.drawImage(QRectF(radius - width / 2, radius - height / 2, width, height), self._image)
        else:
            # No art yet: a plain label in the accent, with a note set off-centre
            # so the turning still shows.
            base = QColor(self._accent).darker(260)
            painter.fillPath(disc, base)
            glyph = QRectF(0, 0, radius * 0.62, radius * 0.62)
            glyph.moveCenter(QPointF(radius, radius * 0.52))
            paint_icon(painter, "music", glyph, QColor(255, 255, 255, 120), stroke=1.6)
        # Printed labels darken a little towards their edge.
        edge = QRadialGradient(QPointF(radius, radius), radius)
        edge.setColorAt(0.0, QColor(0, 0, 0, 0))
        edge.setColorAt(0.80, QColor(0, 0, 0, 0))
        edge.setColorAt(1.0, QColor(0, 0, 0, 90))
        painter.fillPath(disc, edge)
        painter.end()
        return QPixmap.fromImage(image)

    def _build_gloss(self, R: float, ratio: float) -> QPixmap:
        """What sits still on top of the turning label: a soft light and the spindle."""
        radius = R * LABEL + 1.0
        pixels = max(1, int(math.ceil(2 * radius * ratio)))
        image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        centre = QPointF(radius, radius)
        disc = QPainterPath()
        disc.addEllipse(centre, radius, radius)
        light = QRadialGradient(centre + QPointF(-0.45 * radius, -0.55 * radius), radius * 1.25)
        light.setColorAt(0.0, QColor(255, 255, 255, 34))
        light.setColorAt(0.55, QColor(255, 255, 255, 8))
        light.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(disc, light)

        # The spindle through the hole: steel, lit from the top left.
        hole = radius * 0.085
        painter.setBrush(QColor(0, 0, 0, 200))
        painter.drawEllipse(centre + QPointF(0.0, hole * 0.18), hole * 1.12, hole * 1.12)
        painter.setBrush(QColor(6, 6, 7))
        painter.drawEllipse(centre, hole, hole)
        pin = hole * 0.66
        steel = QRadialGradient(centre + QPointF(-pin * 0.35, -pin * 0.4), pin * 1.4)
        steel.setColorAt(0.0, QColor(245, 245, 248))
        steel.setColorAt(0.45, QColor(170, 170, 176))
        steel.setColorAt(1.0, QColor(70, 70, 76))
        painter.setBrush(steel)
        painter.drawEllipse(centre, pin, pin)
        painter.end()
        return QPixmap.fromImage(image)

    def _build_arm_paths(self) -> dict:
        R = self._radius
        tube_width = max(3.0, 0.026 * R)
        paths = {}

        tube = QPainterPath()
        tube.addRoundedRect(QRectF(-tube_width / 2, -0.10 * R, tube_width, (_ARM + 0.10) * R + 1),
                            tube_width / 2, tube_width / 2)
        paths["tube"] = tube
        paths["tube_width"] = tube_width

        weight = QPainterPath()
        weight.addRoundedRect(QRectF(-0.075 * R, -0.28 * R, 0.15 * R, 0.14 * R), 0.02 * R, 0.02 * R)
        paths["weight"] = weight

        head = QTransform()
        head.translate(0.0, _ARM * R)
        head.rotate(_OFFSET)
        # The headshell narrows a little towards the stylus, like a real one.
        half_back, half_front = 0.046 * R, 0.040 * R
        shell = QPainterPath()
        shell.moveTo(-half_back, -0.02 * R)
        shell.lineTo(half_back, -0.02 * R)
        shell.lineTo(half_front, (_HEADSHELL + 0.01) * R)
        shell.lineTo(-half_front, (_HEADSHELL + 0.01) * R)
        shell.closeSubpath()
        rounded = QPainterPath()
        rounded.addRoundedRect(shell.boundingRect(), 0.012 * R, 0.012 * R)
        shell = shell.intersected(rounded)
        lift = QPainterPath()
        lift.moveTo(half_back - 0.004 * R, 0.035 * R)
        lift.lineTo(half_back + 0.085 * R, 0.005 * R)
        lift.lineTo(half_back + 0.088 * R, 0.017 * R)
        lift.lineTo(half_back - 0.004 * R, 0.055 * R)
        lift.closeSubpath()
        cartridge = QPainterPath()
        cartridge.addRoundedRect(QRectF(-0.032 * R, (_HEADSHELL - 0.095) * R, 0.064 * R, 0.10 * R),
                                 0.006 * R, 0.006 * R)
        paths["shell"] = head.map(shell)
        paths["lift"] = head.map(lift)
        paths["cartridge"] = head.map(cartridge)
        paths["stripe"] = head.map(QPointF(-0.032 * R, (_HEADSHELL - 0.07) * R)), \
            head.map(QPointF(0.032 * R, (_HEADSHELL - 0.07) * R))

        silhouette = QPainterPath(tube)
        silhouette = silhouette.united(weight).united(paths["shell"]).united(paths["lift"])
        paths["silhouette"] = silhouette
        return paths

    def _paint_arm(self, painter: QPainter, clip: QRect) -> None:
        R = self._radius
        pivot = self._pivot()
        angle = self._arm_angle
        base_rect = QRectF(pivot.x() - 0.16 * R, pivot.y() - 0.16 * R, 0.32 * R + 8, 0.32 * R + 8)
        if not clip.intersects(self._arm_bounds(angle)) and not clip.intersects(base_rect.toAlignedRect()):
            return
        if self._arm_paths is None:
            self._arm_paths = self._build_arm_paths()
        paths = self._arm_paths
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        # The base the arm turns on.
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.drawEllipse(pivot + QPointF(0.015 * R, 0.03 * R), (_BASE + 0.01) * R, (_BASE + 0.01) * R)
        plinth = QRadialGradient(pivot + QPointF(-0.04 * R, -0.05 * R), 0.16 * R)
        plinth.setColorAt(0.0, QColor(40, 40, 44))
        plinth.setColorAt(1.0, QColor(16, 16, 18))
        painter.setBrush(plinth)
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
        painter.drawEllipse(pivot, _BASE * R, _BASE * R)
        painter.setPen(Qt.PenStyle.NoPen)

        # Shadow: lifted off the record it falls further away.
        lifted = 1.0 - self._arm_on
        offset = QPointF((0.025 + 0.02 * lifted) * R, (0.045 + 0.03 * lifted) * R)
        for spread, alpha in ((1.0, 55), (0.55, 60)):
            painter.save()
            painter.translate(pivot + offset * spread)
            painter.rotate(angle)
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawPath(paths["silhouette"])
            painter.restore()

        painter.save()
        painter.translate(pivot)
        painter.rotate(angle)
        # Counterweight: dark machined metal with a couple of turned ridges.
        weight = QLinearGradient(-0.075 * R, 0, 0.075 * R, 0)
        weight.setColorAt(0.0, QColor(30, 30, 34))
        weight.setColorAt(0.45, QColor(92, 92, 100))
        weight.setColorAt(1.0, QColor(22, 22, 25))
        painter.setBrush(weight)
        painter.drawPath(paths["weight"])
        painter.setPen(QPen(QColor(0, 0, 0, 90), 1.0))
        for y in (-0.24, -0.21, -0.18):
            painter.drawLine(QPointF(-0.075 * R, y * R), QPointF(0.075 * R, y * R))
        painter.setPen(Qt.PenStyle.NoPen)
        # The tube: brushed aluminium, lit across its width.
        width = paths["tube_width"]
        metal = QLinearGradient(-width / 2, 0, width / 2, 0)
        metal.setColorAt(0.0, QColor(120, 120, 126))
        metal.setColorAt(0.4, QColor(236, 236, 240))
        metal.setColorAt(1.0, QColor(110, 110, 116))
        painter.setBrush(metal)
        painter.drawPath(paths["tube"])
        # Headshell, finger lift, cartridge and a stripe of the album's colour.
        painter.setBrush(QColor(170, 170, 176))
        painter.drawPath(paths["lift"])
        painter.setBrush(QColor(34, 34, 38))
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
        painter.drawPath(paths["shell"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(10, 10, 12))
        painter.drawPath(paths["cartridge"])
        start, end = paths["stripe"]
        painter.setPen(QPen(self._accent, max(1.2, 0.012 * R)))
        painter.drawLine(start, end)
        painter.restore()

        # The cap over the pivot, on top of the arm.
        cap = QRadialGradient(pivot + QPointF(-0.025 * R, -0.03 * R), 0.08 * R)
        cap.setColorAt(0.0, QColor(150, 150, 158))
        cap.setColorAt(0.5, QColor(70, 70, 76))
        cap.setColorAt(1.0, QColor(30, 30, 34))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(cap)
        painter.drawEllipse(pivot, 0.07 * R, 0.07 * R)
