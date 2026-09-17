"""The card-to-player transition: artwork that grows into the video.

Starting playback has an unavoidable gap — mpv has to launch or load, open the
file and decode a first frame. Cutting to black for that long feels broken. So
the artwork you clicked expands from where it sits on screen to fill the video
area and holds there until real video is actually on screen, which turns the
gap into the transition rather than a stall.

It lives in its own top-level window for the same reason the transport controls
do: mpv renders into a native child window, and native children always paint
above Qt's own drawing inside the same top-level.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QPropertyAnimation, QRect, QRectF, Qt, QTimer, Property, Signal,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget

from ..theme import C

_GROW_MS = 300
_FADE_MS = 260
# However badly playback goes, the cover comes off. Nothing about a transition
# is worth leaving a picture stuck over a working player.
_SAFETY_MS = 6000


class HeroTransition(QWidget):
    """A pixmap that flies from one screen rect to another, then fades out."""

    finished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._pixmap = QPixmap()
        self._radius = 6.0
        self._opacity = 1.0
        self._running = False

        self._grow = QPropertyAnimation(self, b"geometry", self)
        self._grow.setDuration(_GROW_MS)
        self._grow.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fade = QPropertyAnimation(self, b"fade", self)
        self._fade.setDuration(_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade.finished.connect(self._teardown)

        self._safety = QTimer(self)
        self._safety.setSingleShot(True)
        self._safety.setInterval(_SAFETY_MS)
        self._safety.timeout.connect(self.reveal)

    # `fade` is animated by QPropertyAnimation, so it has to be a Qt property.
    def _get_fade(self) -> float:
        return self._opacity

    def _set_fade(self, value: float) -> None:
        self._opacity = float(value)
        self.setWindowOpacity(max(0.0, min(1.0, self._opacity)))
        self.update()

    fade = Property(float, _get_fade, _set_fade)

    @property
    def running(self) -> bool:
        return self._running

    def start(self, pixmap: QPixmap, source: QRect, target: QRect) -> None:
        """Grow `pixmap` from `source` to `target` — both in screen coordinates."""
        if pixmap.isNull() or target.isEmpty():
            self.finished.emit()
            return
        self._pixmap = pixmap
        self._running = True
        self._set_fade(1.0)

        # An empty source (no card to grow from) becomes a straight cross-fade.
        self.setGeometry(source if not source.isEmpty() else target)
        self.show()
        self.raise_()
        self._grow.stop()
        self._grow.setStartValue(source if not source.isEmpty() else target)
        self._grow.setEndValue(target)
        self._grow.start()
        self._safety.start()

    def retarget(self, target: QRect) -> None:
        """Follow the video area if it moves — going fullscreen, for instance."""
        if not self._running or target.isEmpty():
            return
        if self._grow.state() == QPropertyAnimation.State.Running:
            self._grow.setEndValue(target)
        else:
            self.setGeometry(target)

    def reveal(self) -> None:
        """Playback is up (or we gave up waiting): uncover it."""
        if not self._running or self._fade.state() == QPropertyAnimation.State.Running:
            return
        self._safety.stop()
        self._fade.stop()
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def cancel(self) -> None:
        """Stop immediately, without the fade."""
        self._grow.stop()
        self._fade.stop()
        self._teardown()

    def _teardown(self) -> None:
        was_running, self._running = self._running, False
        self._safety.stop()
        self._pixmap = QPixmap()
        self.hide()
        self.setWindowOpacity(1.0)
        self._opacity = 1.0
        if was_running:
            self.finished.emit()

    def paintEvent(self, event) -> None:
        if self._pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        rect = QRectF(self.rect())

        # Corners round at the start and square off as it fills the screen, so
        # it reads as the tile becoming the picture.
        span = max(1.0, float(self._grow.endValue().width() - self._grow.startValue().width()))
        progress = min(1.0, max(0.0, (rect.width() - self._grow.startValue().width()) / span))
        radius = self._radius * (1.0 - progress)

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        painter.fillRect(rect, QColor(C.SCRIM))
        painter.drawPixmap(rect, self._pixmap, QRectF(self._pixmap.rect()))
