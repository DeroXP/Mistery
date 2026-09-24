"""Poster and still cards — fully custom-painted for hover states and overlays."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer,
    QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import QMenu, QWidget

from ...images import art_pixmap, load_async
from ...metadata import thumbs as thumbs_module
from ...models import MediaItem, ShowItem
from ...util import elide, fmt_duration, fmt_remaining
from ..theme import POSTER_H, POSTER_W, RADIUS, WIDE_H, WIDE_W, C
from .flow import FlowLayout
from .icons import paint_icon

_TITLE_BLOCK = 56
_PLAY_RADIUS = 25.0

# Hover preview: wait before starting, so sweeping the pointer across a row
# doesn't set every tile going.
_PREVIEW_DWELL_MS = 650
_PREVIEW_FRAME_MS = 130
_PREVIEW_FRAMES = 24

# The tile is inset inside its widget at rest and fills it on hover, so it can
# grow without needing to paint outside its own bounds (which would mean either
# a popup window or a layout that reserves the space and leaves gaps).
_REST_INSET = 8.0


class _BaseCard(QWidget):
    clicked = Signal(object)
    play_requested = Signal(object)
    # "play" | "vr" | "details" | "watched" | "folder" | "playlist", plus
    # whatever extra_actions carries ("up" | "down" | "unlist" on a playlist).
    action_requested = Signal(str, object)

    ART_W = POSTER_W
    ART_H = POSTER_H
    FRAMED_ART = False      # a tall picture shown whole rather than cropped (images.framed_pixmap)

    def __init__(self, item, parent=None) -> None:
        super().__init__(parent)
        self._item = item
        self._pixmap: QPixmap | None = None
        self._hover = 0.0
        # (label, action) pairs the page underneath wants in this card's menu.
        # A card still knows nothing about the database: it emits the action and
        # the page does the work, the same as every other entry here.
        self.extra_actions: list[tuple[str, str]] = []
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setFixedSize(QSize(self.ART_W, self.ART_H + _TITLE_BLOCK))

        self._animation = QVariantAnimation(self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_hover_value)

        # Hover preview, off unless a row asks for it.
        self._hovered = False
        self._preview_enabled = False
        self._preview_index: dict | None = None
        self._preview_sheet = QImage()
        self._preview_frame = 0
        self._preview_start = 0
        self._preview_tile: QPixmap | None = None
        self._preview_dwell = QTimer(self)
        self._preview_dwell.setSingleShot(True)
        self._preview_dwell.setInterval(_PREVIEW_DWELL_MS)
        self._preview_dwell.timeout.connect(self._begin_preview)
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(_PREVIEW_FRAME_MS)
        self._preview_timer.timeout.connect(self._next_preview_frame)

        self._request_art()

    # --- hover preview ------------------------------------------------------

    def set_preview_enabled(self, enabled: bool) -> None:
        """Play a preview from the resume point while the pointer rests here."""
        self._preview_enabled = bool(enabled)
        if not enabled:
            self._stop_preview()

    def _begin_preview(self) -> None:
        """Dwell elapsed: load the sprite this file already has, then animate it."""
        item = self._item
        source = getattr(item, "thumbs", None)
        # _hovered rather than underMouse(): the latter goes stale while rows
        # are rebuilt underneath the pointer, and enter/leave already know.
        if not self._preview_enabled or not source or not self._hovered:
            return
        if self._preview_index is None:
            self._preview_index = thumbs_module.load_index(source)
            if self._preview_index is None:
                return
            load_async(self._preview_index["sprite"], self._on_preview_sheet)
        # Start where they left off — that is the frame they care about.
        interval = float(self._preview_index.get("interval") or 1.0)
        position = float(getattr(item, "position", 0.0) or 0.0)
        self._preview_start = int(position / interval) if interval > 0 else 0
        self._preview_frame = 0
        if not self._preview_sheet.isNull():
            self._preview_timer.start()

    def _on_preview_sheet(self, image: QImage) -> None:
        self._preview_sheet = image
        if self._preview_enabled and self._hovered and not image.isNull():
            self._preview_timer.start()

    def _next_preview_frame(self) -> None:
        index = self._preview_index
        if index is None or self._preview_sheet.isNull():
            self._stop_preview()
            return
        count = int(index.get("count") or 1)
        columns = int(index.get("columns") or 10)
        tile_w = int(index.get("tile_width") or 208)
        tile_h = int(index.get("tile_height") or 117)

        # Loop over a window of frames rather than running to the end of the file.
        self._preview_frame = (self._preview_frame + 1) % _PREVIEW_FRAMES
        frame = min(count - 1, self._preview_start + self._preview_frame)
        left, top = (frame % columns) * tile_w, (frame // columns) * tile_h
        self._preview_tile = QPixmap.fromImage(
            self._preview_sheet.copy(left, top, tile_w, tile_h)
        )
        self.update()

    def _stop_preview(self) -> None:
        self._preview_dwell.stop()
        self._preview_timer.stop()
        if self._preview_tile is not None:
            self._preview_tile = None
            self.update()

    @property
    def previewing(self) -> bool:
        return self._preview_tile is not None

    def snapshot(self) -> tuple[QPixmap, QRect]:
        """What this tile currently shows, and where it is on screen.

        Whatever is on the tile — artwork or the frame the preview reached — is
        what grows into the player, so the handover has no visible seam.
        """
        art = self._art_rect()
        top_left = self.mapToGlobal(QPoint(int(art.left()), int(art.top())))
        rect = QRect(top_left, QSize(int(art.width()), int(art.height())))
        source = self._preview_tile if self._preview_tile is not None else self._pixmap
        return (source if source is not None else QPixmap()), rect

    # --- data ---------------------------------------------------------------

    @property
    def item(self):
        return self._item

    def set_item(self, item) -> None:
        self._item = item
        self._pixmap = None
        self._request_art()
        self.update()

    def _art_path(self) -> str | None:
        return getattr(self._item, "art", None)

    def _request_art(self) -> None:
        path = self._art_path()
        title = getattr(self._item, "title", "") or ""
        # Square corners: the rounding is done by this widget's clip path, which
        # follows the tile as it grows. Baking it into the pixmap would scale it.
        self._pixmap = art_pixmap(
            path, title, self.ART_W, self.ART_H, 0,
            self.devicePixelRatioF(), self._on_art_ready, framed=self.FRAMED_ART,
        )

    def _on_art_ready(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    # --- interaction --------------------------------------------------------

    def _on_hover_value(self, value) -> None:
        self._hover = float(value)
        self.update()

    def _animate_to(self, target: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._hover)
        self._animation.setEndValue(target)
        self._animation.start()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self._animate_to(1.0)
        if self._preview_enabled:
            self._preview_dwell.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._animate_to(0.0)
        self._stop_preview()
        super().leaveEvent(event)

    def _play_button_rect(self) -> QRectF:
        art = self._art_rect()
        return QRectF(
            art.center().x() - _PLAY_RADIUS, art.center().y() - _PLAY_RADIUS,
            _PLAY_RADIUS * 2, _PLAY_RADIUS * 2,
        )

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        if self._hover > 0.4 and self._play_button_rect().contains(QPointF(event.position())):
            self.play_requested.emit(self._item)
        else:
            self.clicked.emit(self._item)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self._item)

    def contextMenuEvent(self, event) -> None:
        item = self._item
        is_media = isinstance(item, MediaItem)
        menu = QMenu(self)
        # Otherwise every menu opened stays alive, actions and lambdas included,
        # for as long as the card does.
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        resume = getattr(item, "resume_position", 0.0)
        menu.addAction(
            "Resume" if resume > 0 else "Play",
            lambda: self.action_requested.emit("play", item),
        )
        if is_media:
            menu.addAction("Play in VR", lambda: self.action_requested.emit("vr", item))
        menu.addSeparator()
        menu.addAction(
            "Open show" if not is_media else "Show details",
            lambda: self.action_requested.emit("details", item),
        )
        if is_media:
            menu.addAction(
                "Add to playlist…",
                lambda: self.action_requested.emit("playlist", item),
            )
            menu.addAction(
                "Mark unwatched" if item.watched else "Mark watched",
                lambda: self.action_requested.emit("watched", item),
            )
            menu.addAction(
                "Open file location",
                lambda: self.action_requested.emit("folder", item),
            )
        if self.extra_actions:
            menu.addSeparator()
            for label, action in self.extra_actions:
                menu.addAction(label, lambda a=action: self.action_requested.emit(a, item))
        menu.exec(event.globalPos())

    # --- painting helpers ---------------------------------------------------

    def _art_rect(self) -> QRectF:
        """The artwork, growing about its own centre as the pointer arrives.

        Scaling rather than nudging is what makes a wall of tiles feel like a
        streaming app: the one you are pointing at comes forward.
        """
        inset = _REST_INSET * (1.0 - self._hover)
        return QRectF(0, 0, self.ART_W, self.ART_H).adjusted(
            inset, inset, -inset, -inset
        )

    def _text_top(self) -> float:
        """Fixed, so titles don't jitter as the tile grows under the pointer."""
        return self.ART_H - _REST_INSET + 10

    def _paint_art(self, painter: QPainter, rect: QRectF) -> None:
        art_rect = rect

        path = QPainterPath()
        path.addRoundedRect(art_rect, RADIUS, RADIUS)

        if self._hover > 0.01:
            shadow = QColor(0, 0, 0, int(150 * self._hover))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shadow)
            painter.drawRoundedRect(
                art_rect.adjusted(3, 5, 3, 7), RADIUS, RADIUS
            )

        source = self._preview_tile if self._preview_tile is not None else self._pixmap
        if source is not None:
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(art_rect, source, QRectF(source.rect()))
            painter.restore()

        # A gentle darkening keeps the play button readable over bright art.
        if self._hover > 0.01:
            painter.fillPath(path, QColor(0, 0, 0, int(72 * self._hover)))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        border = QColor(255, 255, 255, int(26 + 130 * self._hover))
        painter.setPen(QPen(border, 1.2))
        painter.drawPath(path)

    def _paint_play_button(self, painter: QPainter) -> None:
        if self._hover <= 0.02:
            return
        rect = self._play_button_rect()
        scale = 0.72 + 0.28 * self._hover
        shrunk = QRectF(0, 0, rect.width() * scale, rect.height() * scale)
        shrunk.moveCenter(rect.center())

        painter.setPen(QPen(QColor(255, 255, 255, int(210 * self._hover)), 1.6))
        plate = QColor(C.PLAY_BG)
        plate.setAlphaF(min(1.0, self._hover) * 0.94)
        painter.setBrush(QBrush(plate))
        painter.drawEllipse(shrunk)

        glyph = QColor(C.PLAY_FG)
        glyph.setAlphaF(min(1.0, self._hover))
        icon = QRectF(0, 0, shrunk.width() * 0.52, shrunk.height() * 0.52)
        icon.moveCenter(shrunk.center() + QPointF(shrunk.width() * 0.02, 0))
        paint_icon(painter, "play", icon, glyph)

    def _paint_progress(self, painter: QPainter, rect: QRectF, fraction: float) -> None:
        if fraction <= 0.001:
            return
        height = 4.0
        track = QRectF(rect.left(), rect.bottom() - height, rect.width(), height)
        clip = QPainterPath()
        clip.addRoundedRect(rect, RADIUS, RADIUS)
        painter.save()
        painter.setClipPath(clip)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(90, 90, 90, 220))
        painter.drawRect(track)
        painter.setBrush(QColor(C.ACCENT))
        painter.drawRect(QRectF(track.left(), track.top(),
                                track.width() * max(0.02, fraction), track.height()))
        painter.restore()

    def _paint_watched_badge(self, painter: QPainter, rect: QRectF) -> None:
        size = 24.0
        badge = QRectF(rect.right() - size - 8, rect.top() + 8, size, size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawEllipse(badge)
        inner = badge.adjusted(5, 5, -5, -5)
        paint_icon(painter, "check", inner, QColor(C.TEXT), stroke=2.4)

    def _paint_text(self, painter: QPainter, top: float, title: str, subtitle: str) -> None:
        left = _REST_INSET
        width = self.width() - left * 2
        painter.setPen(QColor(C.TEXT))
        font = QFont(painter.font())
        font.setPointSizeF(9.8)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        painter.drawText(
            QRectF(left, top, width, metrics.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(title, Qt.TextElideMode.ElideRight, int(width)),
        )

        if subtitle:
            font.setBold(False)
            font.setPointSizeF(8.6)
            painter.setFont(font)
            painter.setPen(QColor(C.TEXT_FAINT))
            sub_metrics = painter.fontMetrics()
            painter.drawText(
                QRectF(left, top + metrics.height() + 1, width, sub_metrics.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                sub_metrics.elidedText(subtitle, Qt.TextElideMode.ElideRight, int(width)),
            )


class PosterCard(_BaseCard):
    """A 2:3 poster with progress, watched state and a hover play button."""

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        art_rect = self._art_rect()

        self._paint_art(painter, art_rect)
        self._paint_progress(painter, art_rect, getattr(self._item, "progress", 0.0))
        if getattr(self._item, "watched", False):
            self._paint_watched_badge(painter, art_rect)
        self._paint_play_button(painter)

        title = getattr(self._item, "display_title", None) or self._item.title
        subtitle = getattr(self._item, "subtitle", "")
        self._paint_text(painter, self._text_top(), title, subtitle)


class WideCard(_BaseCard):
    """A 16:9 still — used for Continue Watching and episode lists."""

    ART_W = WIDE_W
    ART_H = WIDE_H

    def __init__(self, item, show_remaining: bool = True, parent=None) -> None:
        self._show_remaining = show_remaining
        super().__init__(item, parent)

    def _art_path(self) -> str | None:
        return getattr(self._item, "wide_art", None) or getattr(self._item, "art", None)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        art_rect = self._art_rect()

        self._paint_art(painter, art_rect)
        self._paint_progress(painter, art_rect, getattr(self._item, "progress", 0.0))
        if getattr(self._item, "watched", False):
            self._paint_watched_badge(painter, art_rect)
        self._paint_play_button(painter)

        item = self._item
        title = getattr(item, "display_title", None) or item.title
        if self._show_remaining and isinstance(item, MediaItem) and item.position > 0:
            subtitle = fmt_remaining(item.position, item.duration)
        else:
            subtitle = getattr(item, "subtitle", "")
        self._paint_text(painter, self._text_top(), title, subtitle)


class ShowCard(PosterCard):
    """Poster card for a series rather than a single file."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)


def card_flow(container):
    """The wrapping layout a CardGrid lays its cards out in, or None.

    Same reason as set_extra_actions below: the grid builds and owns its cards,
    and a playlist page wants to move one of them without rebuilding all of
    them. Asked for by type rather than reached for by name, so a grid that
    changes how it is put together says None here instead of lying.
    """
    return container.findChild(FlowLayout)


def set_extra_actions(container, actions: list[tuple[str, str]]) -> None:
    """Give every card inside `container` some page-specific menu entries.

    Set after a grid has been filled, because the grid builds its own cards
    (rows.CardGrid): a playlist page wants Move up / Move down / Remove on each
    card, and no other page wants anything. Cards with nothing extra show
    exactly the menu they always did.
    """
    for card in container.findChildren(_BaseCard):
        card.extra_actions = list(actions)


def make_card(item, wide: bool = False) -> _BaseCard:
    if getattr(item, "friend_id", None) is not None:
        # A friend's film or episode in one of this library's rows (Home's
        # Continue Watching): a card whose menu never reaches this library.
        from ..friend_library_view import friend_card

        return friend_card(item, wide)
    if wide:
        return WideCard(item)
    if isinstance(item, ShowItem):
        return ShowCard(item)
    return PosterCard(item)
