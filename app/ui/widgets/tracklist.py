"""A list of songs, as album pages, the Songs tab and the queue show them.

Model/view rather than a widget per row, so a library of thousands of tracks
scrolls as smoothly as an album of ten. The delegate paints everything: the
number that turns into a play arrow under the pointer, the bars that bounce
beside whatever is playing, the heart beside a liked song, and the greyed rows
of tracks still downloading.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QMenu, QStyle, QStyledItemDelegate, QTableView,
)

from ...util import fmt_clock
from ..theme import C
from .icons import paint_icon

TRACK_ROLE = Qt.ItemDataRole.UserRole + 1
_ROW_HEIGHT = 46
_HEART_SIDE = 17.0

COLUMN_LABELS = {
    "number": "#", "title": "Title", "artist": "Artist", "album": "Album", "like": "", "time": "",
}


def apply_liked(rows: list, track: dict) -> bool:
    """Copy a song's liked state onto every row of it in `rows`, in place.

    Pages keep their own copies of the rows they list, and those copies are what
    Play queues: a copy left saying "not liked" put a liked song in the queue
    with an empty heart. True when anything changed.
    """
    track_id = track.get("id") if isinstance(track, dict) else None
    if track_id is None:
        return False
    liked = 1 if track.get("liked") else 0
    changed = False
    for row in rows:
        if isinstance(row, dict) and row.get("id") == track_id and (
                int(row.get("liked") or 0) != liked or row.get("liked_at") != track.get("liked_at")):
            row["liked"] = liked
            row["liked_at"] = track.get("liked_at")
            changed = True
    return changed


class TrackModel(QAbstractTableModel):
    def __init__(self, columns: list[str], numbering: str = "track", parent=None) -> None:
        super().__init__(parent)
        self._columns = columns
        # An album numbers songs by their place on the record; a list that mixes
        # albums (Songs, the queue) numbers by position, or the column is noise.
        self._numbering = numbering
        self._rows: list[dict] = []

    def set_rows(self, rows: list) -> None:
        self.beginResetModel()
        self._rows = [dict(r) for r in rows]
        self.endResetModel()

    @property
    def rows(self) -> list[dict]:
        return self._rows

    @property
    def columns(self) -> list[str]:
        return self._columns

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == TRACK_ROLE:
            return row
        if role == Qt.ItemDataRole.ToolTipRole and self._columns[index.column()] == "like":
            if row.get("liked"):
                return "Remove from Liked Songs"
            return "Save to Liked Songs" if (row.get("state") or "ready") == "ready" else None
        if role == Qt.ItemDataRole.DisplayRole:
            column = self._columns[index.column()]
            if column == "number":
                if self._numbering == "track" and row.get("track_no"):
                    return str(row["track_no"])
                return str(index.row() + 1)
            if column == "title":
                return row.get("title") or ""
            if column == "artist":
                return row.get("artist") or row.get("album_artist") or ""
            if column == "album":
                return row.get("album_title") or row.get("album") or ""
            if column == "time":
                return fmt_clock(row.get("duration") or 0) if row.get("duration") else ""
        return None


class _TrackDelegate(QStyledItemDelegate):
    def __init__(self, view: "TrackList") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt API
        return QSize(40, _ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        view = self._view
        row = index.data(TRACK_ROLE) or {}
        column = view.model_.columns[index.column()]
        rect = QRect(option.rect)
        hovered = index.row() == view.hover_row
        current = row.get("id") is not None and row.get("id") == view.current_id
        ready = (row.get("state") or "ready") == "ready"

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Row background, painted per cell but reading as one rounded bar: each
        # cell clips to itself and lets its inner corners fall outside the clip,
        # so a translucent fill is drawn once rather than doubling up at seams.
        if hovered or current:
            first = index.column() == 0
            last = index.column() == view.model_.columnCount() - 1
            background = QRectF(rect).adjusted(0 if first else -12, 2, 0 if last else 12, -2)
            painter.save()
            painter.setClipRect(rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 22 if hovered else 12))
            painter.drawRoundedRect(background, 6, 6)
            painter.restore()

        dim = QColor(C.TEXT_DIM)
        bright = QColor(view.accent if current else C.TEXT)
        if not ready:
            dim = QColor(C.TEXT_FAINT)
            bright = QColor(C.TEXT_FAINT)

        font = QFont(option.font)
        font.setPointSizeF(10.0)

        if column == "number":
            centre = QRectF(rect)
            if current and ready:
                self._paint_bars(painter, centre, QColor(view.accent), view.playing)
            elif hovered and ready:
                glyph = QRectF(0, 0, 14, 14)
                glyph.moveCenter(centre.center())
                paint_icon(painter, "play", glyph, QColor(C.TEXT))
            else:
                painter.setFont(font)
                painter.setPen(dim)
                painter.drawText(centre, Qt.AlignmentFlag.AlignCenter, index.data())
        elif column == "title":
            text_rect = QRectF(rect).adjusted(6, 0, -8, 0)
            title_font = QFont(font)
            title_font.setWeight(QFont.Weight.DemiBold if current else QFont.Weight.Medium)
            painter.setFont(title_font)
            painter.setPen(bright)
            title = index.data() or ""
            if not ready:
                note = "  ·  Downloading" if row.get("state") == "incomplete" else "  ·  Unavailable"
                width = int(text_rect.width()) - painter.fontMetrics().horizontalAdvance(note)
                elided = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, max(20, width))
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 elided + note)
            else:
                painter.drawText(
                    text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight,
                                                     int(text_rect.width())))
        elif column == "like":
            # Always there on a liked song; on the others only under the
            # pointer, as an offer, or every row would carry an empty outline.
            glyph = QRectF(0, 0, _HEART_SIDE, _HEART_SIDE)
            glyph.moveCenter(QRectF(rect).center())
            if row.get("liked"):
                paint_icon(painter, "heart_filled", glyph, QColor(view.accent if ready else C.TEXT_FAINT))
            elif hovered and ready:
                over = view.hover_like
                paint_icon(painter, "heart", glyph, QColor(C.TEXT if over else C.TEXT_DIM))
        else:
            painter.setFont(font)
            painter.setPen(dim)
            align = (Qt.AlignmentFlag.AlignRight if column == "time" else Qt.AlignmentFlag.AlignLeft)
            text_rect = QRectF(rect).adjusted(6, 0, -10, 0)
            painter.drawText(
                text_rect, align | Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(index.data() or "", Qt.TextElideMode.ElideRight,
                                                 int(text_rect.width())))
        painter.restore()

    @staticmethod
    def _paint_bars(painter: QPainter, area: QRectF, colour: QColor, moving: bool) -> None:
        """Three bouncing bars: the universal sign for 'this one is playing'."""
        now = time.monotonic()
        width, gap, tallest = 3.0, 2.6, 14.0
        left = area.center().x() - (width * 3 + gap * 2) / 2
        bottom = area.center().y() + tallest / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        for bar, phase in enumerate((0.0, 1.7, 3.1)):
            level = 0.45 + 0.55 * abs(math.sin(now * (5.2 + bar) + phase)) if moving else 0.35 + 0.2 * bar
            height = max(3.0, tallest * level)
            painter.drawRoundedRect(QRectF(left + bar * (width + gap), bottom - height, width, height),
                                    1.2, 1.2)


class TrackList(QTableView):
    """Songs, click to play. `auto_height` lets an outer page do the scrolling."""

    play_requested = Signal(int)                   # row
    context_requested = Signal(object, QPoint)      # track dict, global position
    like_requested = Signal(object, bool)           # track dict, liked (the "like" column)

    def __init__(self, columns: list[str], auto_height: bool = False,
                 numbering: str = "track", parent=None) -> None:
        super().__init__(parent)
        self.model_ = TrackModel(columns, numbering, self)
        self.setModel(self.model_)
        self.setItemDelegate(_TrackDelegate(self))
        self.hover_row = -1
        self.hover_like = False             # the pointer is on the heart itself
        self.current_id: int | None = None
        self.playing = False
        self.accent = C.ACCENT
        self._auto_height = auto_height

        self.setMouseTracking(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setFrameShape(QTableView.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setStyleSheet("QTableView { background: transparent; border: none; }")

        header = self.horizontalHeader()
        header.setVisible(False)
        header.setMinimumSectionSize(40)
        for position, column in enumerate(columns):
            if column == "number":
                header.setSectionResizeMode(position, QHeaderView.ResizeMode.Fixed)
                header.resizeSection(position, 52)
            elif column == "time":
                header.setSectionResizeMode(position, QHeaderView.ResizeMode.Fixed)
                header.resizeSection(position, 70)
            elif column == "like":
                header.setSectionResizeMode(position, QHeaderView.ResizeMode.Fixed)
                header.resizeSection(position, 44)
            else:
                header.setSectionResizeMode(position, QHeaderView.ResizeMode.Stretch)
        if auto_height:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Bars only animate while something here is actually playing.
        self._animation = QTimer(self)
        self._animation.setInterval(90)
        self._animation.timeout.connect(self._tick)

    # --- data ----------------------------------------------------------------

    def set_tracks(self, rows: list) -> None:
        self.model_.set_rows(rows)
        self.hover_row = -1
        if self._auto_height:
            self.setFixedHeight(max(1, len(self.model_.rows)) * _ROW_HEIGHT + 4)
        self._sync_animation()

    @property
    def tracks(self) -> list[dict]:
        return self.model_.rows

    def set_current(self, track_id: int | None, playing: bool) -> None:
        changed = track_id != self.current_id or playing != self.playing
        self.current_id, self.playing = track_id, playing
        if changed:
            self._sync_animation()
            self.viewport().update()

    def set_accent(self, colour: str) -> None:
        self.accent = colour
        self.viewport().update()

    def update_track(self, track: dict) -> None:
        """A song was liked or unliked somewhere: its hearts here follow."""
        if apply_liked(self.model_.rows, track):
            self.viewport().update()

    def _sync_animation(self) -> None:
        showing = (self.playing and self.isVisible()
                   and any(r.get("id") == self.current_id for r in self.model_.rows))
        if showing and not self._animation.isActive():
            self._animation.start()
        elif not showing:
            self._animation.stop()

    def hideEvent(self, event) -> None:
        # Nobody can see the bars bounce; stop waking up to draw them.
        self._animation.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_animation()

    def _tick(self) -> None:
        if not self.isVisible():
            self._animation.stop()
            return
        for row, data in enumerate(self.model_.rows):
            if data.get("id") == self.current_id:
                self.viewport().update(self.visualRect(self.model_.index(row, 0)))
                return

    # --- interaction ------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        point = event.position().toPoint()
        row = self.rowAt(point.y())
        column = self.columnAt(point.x())
        on_like = row >= 0 and 0 <= column < len(self.model_.columns) \
            and self.model_.columns[column] == "like"
        if row != self.hover_row or on_like != self.hover_like:
            self.hover_row = row
            self.hover_like = on_like
            self.viewport().update()
        cursor = Qt.CursorShape.PointingHandCursor if row >= 0 else Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(cursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_row = -1
        self.hover_like = False
        self.viewport().update()
        super().leaveEvent(event)

    def _toggle_like(self, index: QModelIndex) -> bool:
        """A click on the heart column: like or unlike that row. True if it was one."""
        if self.model_.columns[index.column()] != "like":
            return False
        row = self.model_.rows[index.row()]
        if row.get("id") is not None and ((row.get("state") or "ready") == "ready" or row.get("liked")):
            self.like_requested.emit(row, not row.get("liked"))
        return True

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                if self._toggle_like(index):
                    return
                row = self.model_.rows[index.row()]
                if (row.get("state") or "ready") == "ready":
                    # The number column is a play button; so is a double click.
                    if self.model_.columns[index.column()] == "number":
                        self.play_requested.emit(index.row())
                        return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return
        # A double click on the heart is two clicks on it, not a request to play
        # the song. Qt 6 delivers the second press to mousePressEvent before
        # this event, so that press has already counted; toggling here too made
        # a double click like the song three times over.
        if self.model_.columns[index.column()] == "like":
            return
        if (self.model_.rows[index.row()].get("state") or "ready") == "ready":
            self.play_requested.emit(index.row())

    def contextMenuEvent(self, event) -> None:
        index = self.indexAt(event.pos())
        if index.isValid():
            self.context_requested.emit(self.model_.rows[index.row()], event.globalPos())

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if self._auto_height:
            return QSize(hint.width(), max(1, len(self.model_.rows)) * _ROW_HEIGHT + 4)
        return hint


def track_menu(parent, track: dict, player, extra: list | None = None,
               on_playlist_change=None) -> QMenu:
    """Play next / Add to queue / Like / Add to playlist / Show in folder — the
    same everywhere.

    `extra` is for the page underneath: Move up, Move down and Remove from this
    playlist only make sense on a playlist page, and only it knows which entry
    the row is. `on_playlist_change` is called with a line of text after a
    playlist has been added to or taken from, so that page can reload itself —
    db.data_version deliberately does not report our own writes, so nothing
    else will tell it.
    """
    from ...util import reveal_in_explorer
    from .playlist_menu import add_to_playlist_menu

    menu = QMenu(parent)
    # Callers exec() it and drop it, and its parent is a page that lives for
    # the session: every right-click kept a menu, its actions and their lambdas
    # (measured, 100 menus opened and closed left 100 behind). Deleted on close
    # instead, which covers choosing an action, Esc and clicking elsewhere.
    menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    ready = (track.get("state") or "ready") == "ready"
    play_next = menu.addAction("Play next", lambda: player.play_next(track))
    add = menu.addAction("Add to queue", lambda: player.add_to_queue(track))
    play_next.setEnabled(ready)
    add.setEnabled(ready)
    liked = bool(track.get("liked"))
    like = menu.addAction("Remove from Liked Songs" if liked else "Save to Liked Songs",
                          lambda: player.set_liked(track, not liked))
    like.setEnabled(ready or liked)
    if track.get("id") is not None:
        # Parented to this menu so it goes when it does (see WA_DeleteOnClose
        # above); a submenu parented to the page outlived every right-click.
        playlists = add_to_playlist_menu(menu, "music", [int(track["id"])], on_playlist_change)
        playlists.setEnabled(ready)
        menu.addMenu(playlists)
    for label, callback in extra or []:
        menu.addAction(label, callback)
    menu.addSeparator()
    menu.addAction("Show in folder", lambda: reveal_in_explorer(track["path"]))
    return menu
