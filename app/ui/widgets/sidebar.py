"""The sidebar: every page as an icon down the left, opening into names on hover.

It floats over the page at the window's left edge rather than taking part in
the layout. Closed it is a rail as wide as MainWindow keeps free for it
(RAIL_W), so nothing is ever under it; opened it grows over the page for as
long as the pointer is on it, and the page does not move. A page reflowing
under the pointer each time it passed the edge would be the opposite of calm.

The pointer has to rest a moment before it opens (OPEN_DELAY_MS): crossing the
edge on the way to a card leaves it closed.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from ..theme import C, RAIL_OPEN_W, RAIL_W, display_font, ui_font
from .icons import IconButton, paint_icon

OPEN_DELAY_MS = 110
OPEN_MS = 260
ICON = 22
ITEM_H = 48


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


class SideItem(QAbstractButton):
    """One page: its icon, and its name while the sidebar is open.

    Hovered, it lifts onto a warm plate and its icon gives a small tilt and
    swell. The page you are on sits on the yellow plate with dark ink.
    """

    def __init__(self, icon: str, text: str, sidebar: "Sidebar",
                 painter: Callable[[QPainter, QRectF, QColor], None] | None = None) -> None:
        super().__init__(sidebar)
        self._icon = icon
        self._paint_glyph = painter
        self._sidebar = sidebar
        self._hover = 0.0
        self.setText(text)
        self.setAccessibleName(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(ITEM_H)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self._anim.valueChanged.connect(self._on_hover_value)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(RAIL_W - 28, ITEM_H)

    def _on_hover_value(self, value) -> None:
        self._hover = float(value)
        self.update()

    def _hover_to(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(target)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack if target > 0 else QEasingCurve.Type.OutCubic)
        self._anim.start()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().enterEvent(event)
        self._hover_to(1.0)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().leaveEvent(event)
        self._hover_to(0.0)

    def _ink(self) -> QColor:
        if self.isChecked():
            return QColor(C.ON_ACCENT)
        return _mix(QColor("#BDB2A5"), QColor(C.TEXT), max(0.0, min(1.0, self._hover)))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        hover = max(0.0, min(1.2, self._hover))
        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(C.ACCENT))
            painter.drawRoundedRect(rect, 15, 15)
        elif hover > 0.01:
            plate = QColor(C.SURFACE_HOVER)
            plate.setAlphaF(min(1.0, hover))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(plate)
            painter.drawRoundedRect(rect.translated(3 * min(1.0, hover), 0), 15, 15)
        ink = self._ink()
        # The icon sits where the closed rail centres it, open or not, so it
        # never moves as the names come in.
        x = (RAIL_W - 28 - ICON) / 2.0
        centre = QPointF(x + ICON / 2.0 + 3 * min(1.0, hover) * (not self.isChecked()), rect.center().y())
        painter.save()
        painter.translate(centre)
        painter.rotate(-5.0 * hover)
        painter.scale(1.0 + 0.12 * hover, 1.0 + 0.12 * hover)
        glyph = QRectF(-ICON / 2.0, -ICON / 2.0, ICON, ICON)
        if self._paint_glyph is not None:
            self._paint_glyph(painter, glyph, ink)
        else:
            paint_icon(painter, self._icon, glyph, ink, stroke=1.9)
        painter.restore()
        self._paint_extra(painter, centre)
        openness = self._sidebar.openness
        if openness > 0.02:
            words = QColor(ink)
            words.setAlphaF(ink.alphaF() * min(1.0, openness * 1.4))
            painter.setPen(words)
            font = ui_font(11, QFont.Weight.DemiBold)
            painter.setFont(font)
            left = x + ICON + 16 + 3 * min(1.0, hover) * (not self.isChecked())
            painter.drawText(QRectF(left, rect.top(), self.width() - left - 8, rect.height()),
                             int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.text())

    def _paint_extra(self, painter: QPainter, centre: QPointF) -> None:
        """For subclasses: anything drawn beside the icon."""


class MovieNightItem(SideItem):
    """Movie night: Join when none is on, its panel while one runs, and a dot
    on the mark to say one is running (party_dialog.MovieNightButton's job)."""

    LABEL = "Movie night"

    def __init__(self, sidebar: "Sidebar") -> None:
        super().__init__("ticket", self.LABEL, sidebar)
        self.setCheckable(False)
        self._live = False
        self.setToolTip("Movie night: join a friend's with their code")

    @property
    def live(self) -> bool:
        return self._live

    def set_live(self, live: bool, tooltip: str) -> None:
        self._live = bool(live)
        self.setToolTip(tooltip)
        self.update()

    def _paint_extra(self, painter: QPainter, centre: QPointF) -> None:
        if not self._live:
            return
        painter.setPen(QPen(QColor(C.RAIL), 2.0))
        painter.setBrush(QColor(C.SUCCESS))
        painter.drawEllipse(QPointF(centre.x() + 9, centre.y() - 8), 4.2, 4.2)


class _Brand(QWidget):
    """The mark (a yellow tile with the M) and the name, which fades in as the
    sidebar opens."""

    def __init__(self, sidebar: "Sidebar") -> None:
        super().__init__(sidebar)
        self._sidebar = sidebar
        self.setFixedHeight(40)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        side = 34.0
        x = (RAIL_W - 28 - side) / 2.0
        tile = QRectF(x, (self.height() - side) / 2.0, side, side)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C.ACCENT))
        painter.drawRoundedRect(tile, 11, 11)
        pen = QPen(QColor(C.ON_ACCENT), 2.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        m = QPainterPath()
        s = side / 24.0
        m.moveTo(tile.left() + 7 * s, tile.top() + 17 * s)
        m.lineTo(tile.left() + 7 * s, tile.top() + 7.5 * s)
        m.lineTo(tile.left() + 12 * s, tile.top() + 12.5 * s)
        m.lineTo(tile.left() + 17 * s, tile.top() + 7.5 * s)
        m.lineTo(tile.left() + 17 * s, tile.top() + 17 * s)
        painter.drawPath(m)
        openness = self._sidebar.openness
        if openness > 0.02:
            words = QColor(C.TEXT)
            words.setAlphaF(min(1.0, openness * 1.4))
            painter.setPen(words)
            painter.setFont(display_font(17, QFont.Weight.Bold))
            left = tile.right() + 14
            painter.drawText(QRectF(left, 0, self.width() - left, self.height()),
                             int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), "Mistery")


class Sidebar(QWidget):
    """The rail. MainWindow puts the pages' buttons in its QButtonGroup, and
    lays the rail over the left edge of the window (place)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self._openness = 0.0
        self.setFixedWidth(RAIL_W)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 22, 14, 14)
        layout.setSpacing(2)
        self.brand = _Brand(self)
        layout.addWidget(self.brand)
        layout.addSpacing(22)
        self._top = QVBoxLayout()
        self._top.setSpacing(2)
        layout.addLayout(self._top)
        layout.addStretch(1)
        self._bottom = QVBoxLayout()
        self._bottom.setSpacing(2)
        layout.addLayout(self._bottom)

        # The library in a line, and the rescan: shown while the sidebar is open.
        foot = QWidget(self)
        foot.setFixedHeight(46)
        row = QHBoxLayout(foot)
        row.setContentsMargins(6, 10, 0, 0)
        row.setSpacing(6)
        self.stats = QLabel(foot)
        self.stats.setObjectName("NavStats")
        self.stats.setWordWrap(False)
        row.addWidget(self.stats, 1)
        self.rescan = IconButton("refresh", size=32, icon_size=17, tooltip="Rescan the library  (Ctrl+R)")
        row.addWidget(self.rescan)
        self._foot = foot
        layout.addWidget(foot)
        self._set_foot_visible(False)

        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(56)
        self._shadow.setOffset(18, 0)
        self._shadow.setColor(QColor(0, 0, 0, 0))
        self.setGraphicsEffect(self._shadow)

        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(OPEN_DELAY_MS)
        self._delay.timeout.connect(lambda: self._animate(1.0))
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(OPEN_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_openness)

    # --- what goes in it ---------------------------------------------------------------------

    def add_item(self, item: SideItem, bottom: bool = False) -> SideItem:
        (self._bottom if bottom else self._top).addWidget(item)
        return item

    def set_stats(self, text: str) -> None:
        self.stats.setText(text)
        self.stats.setToolTip(text)

    # --- opening and closing -----------------------------------------------------------------

    @property
    def openness(self) -> float:
        """0 closed, 1 open, in between while it moves."""
        return self._openness

    @property
    def is_open(self) -> bool:
        return self._openness > 0.5

    def open_now(self) -> None:
        self._delay.stop()
        self._animate(1.0)

    def close_now(self) -> None:
        self._delay.stop()
        self._animate(0.0)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().enterEvent(event)
        self._delay.start()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().leaveEvent(event)
        self.close_now()

    def _animate(self, target: float) -> None:
        if abs(self._openness - target) < 0.001:
            return
        self._anim.stop()
        self._anim.setStartValue(self._openness)
        self._anim.setEndValue(target)
        self._anim.setDuration(max(80, int(OPEN_MS * abs(target - self._openness))))
        self._anim.start()

    def _set_openness(self, value) -> None:
        self._openness = float(value)
        self.setFixedWidth(round(RAIL_W + (RAIL_OPEN_W - RAIL_W) * self._openness))
        self._shadow.setColor(QColor(0, 0, 0, round(200 * self._openness)))
        self._set_foot_visible(self._openness > 0.6)
        for child in self.findChildren(QWidget):
            child.update()
        self.update()

    def _set_foot_visible(self, shown: bool) -> None:
        self.stats.setVisible(shown)
        self.rescan.setVisible(shown)

    # --- where it sits -----------------------------------------------------------------------

    def place(self) -> None:
        """Over the left edge of its parent, top to bottom, above the pages."""
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(0, 0, self.width(), parent.height())
            self.raise_()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(C.RAIL))
        painter.setPen(QColor(C.BORDER))
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
