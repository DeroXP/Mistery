"""The hero at the top of Home: what to pick up, on a big rounded card.

The film or episode you were last part way through (else the newest arrival
with a backdrop), its picture filling the card and fading to the page on the
left, where the words sit: a small yellow line saying why it is here, the
title, what it is, a line of its story, how far along you are, and Resume,
More info, Watch together and Play in VR.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ... import db
from ...images import load_async
from ...metadata import categories as cat
from ...models import MediaItem
from ...util import elide, fmt_clock, fmt_duration, fmt_remaining
from ..theme import HERO_H, C, display_family
from .icons import icon_pixmap

HERO_RADIUS = 28
_TITLE_W = 640                  # the title's room before it steps down a size
_TITLE_SIZES = (44, 38, 32)     # pt, largest first


def _show_row(episode: MediaItem):
    if episode.show_id is None:
        return None
    try:
        return db.get_show(int(episode.show_id))
    except Exception:                       # noqa: BLE001 - the line without it
        return None


class ProgressBar(QWidget):
    """How far along, as a short rounded bar."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._fraction = 0.0
        self.setFixedSize(240, 6)

    def set_fraction(self, fraction: float) -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QRectF(self.rect())
        painter.setBrush(QColor(246, 240, 230, 46))
        painter.drawRoundedRect(track, 3, 3)
        if self._fraction > 0:
            painter.setBrush(QColor(C.ACCENT))
            painter.drawRoundedRect(QRectF(0, 0, max(6.0, track.width() * self._fraction), track.height()), 3, 3)


class HeroBanner(QWidget):
    play_requested = Signal(object)
    play_in_vr_requested = Signal(object)
    details_requested = Signal(object)
    together_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._item: MediaItem | None = None
        self._image = QImage()
        self._art_path: str | None = None
        self.setFixedHeight(HERO_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 34, 48, 36)
        outer.setSpacing(0)
        outer.addStretch(1)

        self._eyebrow = QLabel()
        self._eyebrow.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(self._eyebrow)
        outer.addSpacing(8)

        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setMaximumWidth(_TITLE_W)
        outer.addWidget(self._title)
        outer.addSpacing(8)

        self._meta = QLabel()
        self._meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11pt;")
        outer.addWidget(self._meta)

        self._overview = QLabel()
        self._overview.setWordWrap(True)
        self._overview.setMaximumWidth(560)
        self._overview.setStyleSheet("color: #E8E0D4; font-size: 12pt;")
        outer.addSpacing(12)
        outer.addWidget(self._overview)

        self._progress_row = QWidget()
        progress = QHBoxLayout(self._progress_row)
        progress.setContentsMargins(0, 16, 0, 0)
        progress.setSpacing(14)
        self._progress = ProgressBar()
        progress.addWidget(self._progress, 0, Qt.AlignmentFlag.AlignVCenter)
        self._left = QLabel()
        self._left.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10.5pt;")
        progress.addWidget(self._left)
        progress.addStretch(1)
        outer.addWidget(self._progress_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        outer.addSpacing(22)
        outer.addLayout(buttons)

        ratio = self.devicePixelRatioF()
        self._play = QPushButton("Play")
        self._play.setObjectName("Primary")
        self._play.setIcon(QIcon(icon_pixmap("play", 20, C.PLAY_FG, ratio)))
        self._play.setIconSize(QSize(20, 20))
        self._play.setFixedHeight(52)
        # Half the height at most: past half, Qt draws the corners square.
        self._play.setStyleSheet("padding: 0 26px 0 20px; border-radius: 25px; font-size: 12.5pt;")
        self._play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play.clicked.connect(self._emit_play)
        buttons.addWidget(self._play)

        self._details = self._ghost("More info", "info")
        self._details.clicked.connect(self._emit_details)
        buttons.addWidget(self._details)

        self._together = self._ghost("Watch together", "people")
        self._together.setToolTip("Start a movie night with this, and send friends the code")
        self._together.clicked.connect(self._emit_together)
        buttons.addWidget(self._together)

        self._vr = self._ghost("", "vr")
        self._vr.setFixedWidth(52)
        self._vr.setStyleSheet("padding: 0; border-radius: 25px;")
        self._vr.setToolTip("Play in VR")
        self._vr.setAccessibleName("Play in VR")
        self._vr.clicked.connect(self._emit_vr)
        buttons.addWidget(self._vr)
        buttons.addStretch(1)

        self.setVisible(False)

    def _ghost(self, text: str, icon: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("Ghost")
        button.setIcon(QIcon(icon_pixmap(icon, 20, C.TEXT, self.devicePixelRatioF())))
        button.setIconSize(QSize(20, 20))
        button.setFixedHeight(52)
        button.setStyleSheet("padding: 0 22px 0 18px; border-radius: 25px; font-size: 12pt;")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    # --- data ---------------------------------------------------------------

    def set_item(self, item: MediaItem | None) -> None:
        self._item = item
        if item is None or not item.id:
            self.setVisible(False)
            return
        self.setVisible(True)

        resume = item.resume_position
        why = "Pick up where you left off" if resume > 0 else "New in your library"
        self._eyebrow.setText(
            f'<span style="color: {C.ACCENT}; font-size: 9.5pt; font-weight: 700; letter-spacing: 1.6px;">'
            f"{why.upper()}</span>")

        # Episodes by their own name (their code is in the line under it), in
        # the biggest size the name fits on one line at, down to a floor.
        title = elide(item.title or item.display_title, 70)
        size = _TITLE_SIZES[-1]
        for size in _TITLE_SIZES:
            font = QFont(display_family(), size, QFont.Weight.Bold)
            if QFontMetrics(font).horizontalAdvance(title) <= _TITLE_W:
                break
        self._title.setStyleSheet(
            f'color: {C.TEXT}; font-family: "{display_family()}"; font-size: {size}pt; font-weight: 700;')
        self._title.setText(title)
        one_line = QFontMetrics(QFont(display_family(), size, QFont.Weight.Bold)).horizontalAdvance(title) <= _TITLE_W

        meta_bits = []
        show = _show_row(item) if item.is_episode else None
        if show is not None:
            meta_bits.append(show["title"] or "")
        if item.is_episode and item.code:
            meta_bits.append(item.code)
        if item.year and not item.is_episode:
            meta_bits.append(str(item.year))
        # The one word the chips would use, not the one the service sent: this
        # line said "Sci-Fi & Fantasy" on the same screen whose only chip for it
        # reads Science Fiction. A hand-set category still can't show up here —
        # MediaItem has no user_genres field to carry it. An episode's are its
        # show's.
        first = (cat.categories_of(item) or (cat.categories_of(show) if show is not None else []))[:1]
        if first:
            meta_bits.append(first[0])
        if item.duration:
            meta_bits.append(fmt_duration(item.duration))
        if item.rating:
            meta_bits.append(f"★ {item.rating:.1f}")
        # How it looks (4K HDR10), not how it is packed: the codecs are on the
        # film's own page.
        quality = [bit for bit in (item.resolution_label, item.hdr or item.tags.get("hdr")) if bit]
        if quality:
            meta_bits.append(" ".join(quality))
        self._meta.setText("  ·  ".join(b for b in meta_bits if b))

        # Two lines of the story, or one when the title took two.
        text = item.overview or item.tagline or ""
        self._overview.setText(elide(text, 170 if one_line else 90))
        self._overview.setVisible(bool(text))

        started = resume > 0 and bool(item.duration)
        self._progress_row.setVisible(started)
        if started:
            self._progress.set_fraction(item.position / item.duration)
            self._left.setText(fmt_remaining(item.position, item.duration))
        self._play.setText(f"Resume · {fmt_clock(resume)}" if resume > 0 else "Play")

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

    def _emit_together(self) -> None:
        if self._item:
            self.together_requested.emit(self._item)

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        card = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(card, HERO_RADIUS, HERO_RADIUS)
        painter.fillPath(path, QColor(C.BG_ELEV))

        painter.save()
        painter.setClipPath(path)
        if not self._image.isNull():
            scale = max(card.width() / self._image.width(), card.height() / self._image.height())
            width = self._image.width() * scale
            height = self._image.height() * scale
            # Anchored right and a little above centre, where faces usually are.
            painter.drawImage(
                QRectF(card.width() - width, (card.height() - height) * 0.4, width, height), self._image)

        # From the page's own black on the left, where the words are, to the
        # picture by two-thirds of the way across.
        base = QColor(C.BG)
        red, green, blue = base.red(), base.green(), base.blue()
        across = QLinearGradient(0, 0, card.width(), 0)
        across.setColorAt(0.0, QColor(red, green, blue, 240))
        across.setColorAt(0.36, QColor(red, green, blue, 189))
        across.setColorAt(0.68, QColor(red, green, blue, 0))
        painter.fillRect(card, across)
        # And a little shade along the foot, under the buttons.
        down = QLinearGradient(0, card.height() * 0.5, 0, card.height())
        down.setColorAt(0.0, QColor(red, green, blue, 0))
        down.setColorAt(1.0, QColor(red, green, blue, 120))
        painter.fillRect(card, down)
        painter.restore()

        # A hairline, so the card's edge holds against the page's black.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(246, 240, 230, 20), 1))
        painter.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), HERO_RADIUS, HERO_RADIUS)
