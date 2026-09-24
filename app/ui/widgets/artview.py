"""A plain rounded artwork panel with async loading (no hover behaviour)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ...images import art_pixmap


class ArtView(QWidget):
    def __init__(self, width: int, height: int, radius: int = 12, parent=None) -> None:
        super().__init__(parent)
        self._radius = radius
        self._pixmap: QPixmap | None = None
        self._title = ""
        self._path: str | None = None
        self.setFixedSize(QSize(width, height))

    def set_art(self, path: str | None, title: str = "", *, framed: bool = False) -> None:
        """`framed` shows a poster whole in a wide panel (images.framed_pixmap)."""
        self._path, self._title = path, title
        self._pixmap = art_pixmap(
            path, title, self.width(), self.height(), self._radius,
            self.devicePixelRatioF(), lambda pixmap, wanted=path: self._on_ready(pixmap, wanted),
            framed=framed,
        )
        self.update()

    def _on_ready(self, pixmap: QPixmap, wanted: str | None = None) -> None:
        # Reads finish in any order. Skip from one album to the next quickly and
        # the first cover can arrive after the second — without this check the
        # player bar would show the previous album's art for the new song.
        if wanted != self._path:
            return
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event) -> None:
        if self._pixmap is None:
            return
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        painter.drawPixmap(QRectF(self.rect()).topLeft(), self._pixmap)
