"""The hero banner at the top of the home screen."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ...images import load_async
from ...models import MediaItem
from ...util import elide, fmt_clock, fmt_duration
from ..theme import HERO_H, C, display_font, ui_font
from .icons import icon_pixmap


class HeroBanner(QWidget):
    play_requested = Signal(object)
    play_in_vr_requested = Signal(object)
    details_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._item: MediaItem | None = None
        self._image = QImage()
        self._art_path: str | None = None
        self.setFixedHeight(HERO_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(56, 0, 56, 56)
        outer.addStretch(1)

        self._title = QLabel()
        self._title.setFont(display_font(42, weight=display_font().weight()))
        self._title.setStyleSheet(
            f"color: {C.TEXT}; font-size: 42pt; font-weight: 800; letter-spacing: -1px;"
        )
        outer.addWidget(self._title)

        self._meta = QLabel()
        self._meta.setStyleSheet(
            f"color: {C.TEXT}; font-size: 10.5pt; font-weight: 600;"
        )
        outer.addSpacing(10)
        outer.addWidget(self._meta)

        self._overview = QLabel()
        self._overview.setWordWrap(True)
        self._overview.setMaximumWidth(560)
        self._overview.setStyleSheet(f"color: {C.TEXT}; font-size: 10.5pt;")
        outer.addSpacing(12)
        outer.addWidget(self._overview)

        buttons = QHBoxLayout()
        buttons.setSpacing(11)
        outer.addSpacing(24)
        outer.addLayout(buttons)

        ratio = self.devicePixelRatioF()
        self._play = QPushButton("Play")
        self._play.setObjectName("Primary")
        self._play.setIcon(QIcon(icon_pixmap("play", 21, C.PLAY_FG, ratio)))
        self._play.setIconSize(QSize(21, 21))
        self._play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play.clicked.connect(self._emit_play)
        buttons.addWidget(self._play)

        self._details = QPushButton("More info")
        self._details.setObjectName("Ghost")
        self._details.setIcon(QIcon(icon_pixmap("info", 20, C.TEXT, ratio)))
        self._details.setIconSize(QSize(20, 20))
        self._details.setCursor(Qt.CursorShape.PointingHandCursor)
        self._details.clicked.connect(self._emit_details)
        buttons.addWidget(self._details)

        self._vr = QPushButton("Play in VR")
        self._vr.setObjectName("Ghost")
        self._vr.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vr.clicked.connect(self._emit_vr)
        buttons.addWidget(self._vr)
        buttons.addStretch(1)

        self.setVisible(False)

    # --- data ---------------------------------------------------------------

    def set_item(self, item: MediaItem | None) -> None:
        self._item = item
        if item is None or not item.id:
            self.setVisible(False)
            return
        self.setVisible(True)

        self._title.setText(elide(item.title, 60))

        meta_bits = []
        if item.year:
            meta_bits.append(str(item.year))
        if item.duration:
            meta_bits.append(fmt_duration(item.duration))
        if item.rating:
            meta_bits.append(f"★ {item.rating:.1f}")
        if item.genres:
            meta_bits.append(item.genres.split(",")[0].strip())
        meta_bits.extend(item.badges[:3])
        self._meta.setText("   ·   ".join(b for b in meta_bits if b))

        text = item.overview or item.tagline or ""
        self._overview.setText(elide(text, 260))
        self._overview.setVisible(bool(text))

        resume = item.resume_position
        if resume > 0:
            self._play.setText(f"Resume  {fmt_clock(resume)}")
        else:
            self._play.setText("Play")

        path = item.wide_art
        # The same path is asked for again while there's no picture for it. The
        # artwork retry clears a failed read and rebuilds the pages, and the
        # hero, seeing an unchanged path, used to skip it: a backdrop that a
        # backup tool held for a moment stayed blank for the session. A read
        # still in progress is shared (images.load_async), not repeated.
        if path != self._art_path or self._image.isNull():
            self._art_path = path
            self._image = QImage()
            if path:
                load_async(path, lambda image, wanted=path: self._on_image(image, wanted))
            self.update()

    def _on_image(self, image: QImage, wanted: str | None = None) -> None:
        if wanted != self._art_path:
            return                  # a read for the title the hero showed before
        self._image = image
        self.update()

    def _emit_play(self) -> None:
        if self._item:
            self.play_requested.emit(self._item)

    def _emit_vr(self) -> None:
        if self._item:
            self.play_in_vr_requested.emit(self._item)

    def _emit_details(self) -> None:
        if self._item:
            self.details_requested.emit(self._item)

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        rect = QRectF(self.rect())
        painter.fillRect(rect, QColor(C.BG))

        if not self._image.isNull():
            scale = max(rect.width() / self._image.width(),
                        rect.height() / self._image.height())
            width = self._image.width() * scale
            height = self._image.height() * scale
            painter.drawImage(
                QRectF(rect.width() - width, (rect.height() - height) / 2, width, height),
                self._image,
            )

        # Left-to-right scrim so the copy stays legible over any artwork.
        base = QColor(C.BG)
        red, green, blue = base.red(), base.green(), base.blue()
        horizontal = QLinearGradient(0, 0, rect.width(), 0)
        horizontal.setColorAt(0.0, QColor(red, green, blue, 252))
        horizontal.setColorAt(0.30, QColor(red, green, blue, 224))
        horizontal.setColorAt(0.62, QColor(red, green, blue, 96))
        horizontal.setColorAt(1.0, QColor(red, green, blue, 0))
        painter.fillRect(rect, horizontal)

        # Fade into the page below, so the rows appear to sit on the artwork.
        vertical = QLinearGradient(0, rect.height() * 0.35, 0, rect.height())
        vertical.setColorAt(0.0, QColor(red, green, blue, 0))
        vertical.setColorAt(0.72, QColor(red, green, blue, 170))
        vertical.setColorAt(1.0, base)
        painter.fillRect(rect, vertical)
