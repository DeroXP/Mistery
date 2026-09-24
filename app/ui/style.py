"""The app's widget style: Fusion, with the check box drawn by hand.

A style sheet can only put an image in a check box, and an image goes soft on
a 150 % display; here it is a rounded box, filled yellow with a dark tick when
ticked, drawn as vectors at whatever size the screen wants. The style sheet
(theme.STYLESHEET) says nothing about QCheckBox::indicator, so Qt hands the
indicator to this style.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

from .theme import C

_BOX = 18


class CozyStyle(QProxyStyle):
    def pixelMetric(self, metric, option=None, widget=None) -> int:  # noqa: N802 - Qt API
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth, QStyle.PixelMetric.PM_IndicatorHeight):
            return _BOX
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None) -> None:  # noqa: N802 - Qt API
        if element in (QStyle.PrimitiveElement.PE_IndicatorCheckBox,
                       QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck):
            self._check_box(option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)

    @staticmethod
    def _check_box(option, painter: QPainter) -> None:
        state = option.state
        on = bool(state & QStyle.StateFlag.State_On)
        partly = bool(state & QStyle.StateFlag.State_NoChange)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        side = min(option.rect.width(), option.rect.height(), _BOX)
        box = QRectF(0, 0, side, side)
        box.moveCenter(QRectF(option.rect).center())
        box.adjust(0.5, 0.5, -0.5, -0.5)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not enabled:
            painter.setOpacity(0.45)
        if on or partly:
            painter.setPen(QPen(QColor(C.ACCENT_HOVER if hovered else C.ACCENT), 1))
            painter.setBrush(QColor(C.ACCENT_HOVER if hovered else C.ACCENT))
        else:
            painter.setPen(QPen(QColor(C.ACCENT if hovered else C.BORDER_STRONG), 1))
            painter.setBrush(QColor(C.SURFACE))
        painter.drawRoundedRect(box, 6, 6)

        ink = QPen(QColor(C.ON_ACCENT), 2.2)
        ink.setCapStyle(Qt.PenCapStyle.RoundCap)
        ink.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(ink)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        x, y, w, h = box.x(), box.y(), box.width(), box.height()
        if on:
            painter.drawPolyline(QPolygonF([QPointF(x + w * 0.26, y + h * 0.53),
                                            QPointF(x + w * 0.43, y + h * 0.70),
                                            QPointF(x + w * 0.75, y + h * 0.33)]))
        elif partly:
            painter.drawLine(QPointF(x + w * 0.28, y + h * 0.5), QPointF(x + w * 0.72, y + h * 0.5))
        painter.restore()


def install() -> None:
    """Put the style on the app, once (the window calls this before styling)."""
    app = QApplication.instance()
    if app is not None and not isinstance(app.style(), CozyStyle):
        app.setStyle(CozyStyle("Fusion"))
