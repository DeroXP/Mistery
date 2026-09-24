"""News from the library, in a small card at the bottom right of the page.

What the top bar's status line used to say: a scan's progress, "Added to X.",
why a friend's film did not open. It shows for a few seconds after the last
thing it was told, then fades. When there is nothing to tell, its text() is the
library in a line (MainWindow puts the same line at the foot of the sidebar),
which is what the status line read at rest.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget

from ..theme import C

SHOW_MS = 6000
MAX_W = 520
MARGIN = 24


class StatusToast(QLabel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._message = ""
        self._idle = ""
        self._bottom_gap = 0
        self.setWordWrap(True)
        self.setMaximumWidth(MAX_W)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            f"background: {C.BG_ELEV}; color: {C.TEXT}; border: 1px solid {C.BORDER_STRONG};"
            " border-radius: 14px; padding: 11px 16px; font-size: 10pt;")
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(220)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._faded)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SHOW_MS)
        self._timer.timeout.connect(self._fade_out)
        self.hide()

    # --- the status line's words -------------------------------------------------------------

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        """A message to show (the status line's setText)."""
        self.show_message(text)

    def text(self) -> str:
        """What the status line would say: the message, or the library at rest."""
        return self._message or self._idle

    def show_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self.set_idle(self._idle)
            return
        self._message = text
        super().setText(text)
        # One line when it fits: word wrap alone sizes a label to its
        # narrowest, and a short message came out three words to a line.
        words = self.fontMetrics().horizontalAdvance(text)
        self.setFixedWidth(min(MAX_W, words + 2 * 17 + 4))
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._timer.start()

    def set_idle(self, text: str) -> None:
        """Nothing to tell: the library in a line, and the card goes."""
        self._idle = text or ""
        self._message = ""
        self._timer.stop()
        if self.isVisible():
            self._fade_out()

    # --- showing -----------------------------------------------------------------------------

    def set_bottom_gap(self, pixels: int) -> None:
        """Room to leave below it: the Now Playing bar, while it is up."""
        self._bottom_gap = max(0, int(pixels))
        self.place()

    def place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        x = parent.width() - self.width() - MARGIN
        y = parent.height() - self.height() - MARGIN - self._bottom_gap
        self.move(max(MARGIN, x), max(MARGIN, y))

    def _fade_out(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _faded(self) -> None:
        if self._opacity.opacity() <= 0.01:
            self.hide()
            self._message = ""
