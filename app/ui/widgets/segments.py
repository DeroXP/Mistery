"""Choices in one rounded tray, the chosen one filled yellow (the theme's
#Segment): Movies' All / Unwatched / In progress / Watched, a show's seasons."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QWidget

from .flow import FlowLayout


class SegmentTray(QWidget):
    """`wrap` lets the choices run onto more rows (a show with twenty seasons);
    otherwise they stay on one, in a 42-high pill."""

    RADIUS = 21

    def __init__(self, wrap: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._wrap = wrap
        if wrap:
            self._layout = FlowLayout(self, margin=4, h_spacing=2, v_spacing=4)
            policy = QSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
        else:
            self._layout = QHBoxLayout(self)
            self._layout.setContentsMargins(4, 4, 4, 4)
            self._layout.setSpacing(2)
            self.setFixedHeight(42)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        """Every choice on one row: the flow layout alone answers with its
        widest item, and the tray stayed one choice wide (a show's Season 1,
        and none of the other seven)."""
        if not self._wrap:
            return super().sizeHint()
        items = [self._layout.itemAt(index) for index in range(self._layout.count())]
        hints = [item.sizeHint() for item in items if item is not None]
        # +2: QRect.right() is one short of the width, and the flow layout
        # wraps an item that ends exactly on it.
        width = sum(hint.width() for hint in hints) + 2 * max(0, len(hints) - 1) + 8 + 2
        height = max((hint.height() for hint in hints), default=34) + 8
        return QSize(width, height)

    def add(self, button: QPushButton) -> None:
        button.setObjectName("Segment")
        self._layout.addWidget(button)
        self.updateGeometry()

    def clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(self.RADIUS, rect.height() / 2)
        painter.setPen(QPen(QColor("#2B2520"), 1))
        painter.setBrush(QColor("#161311"))
        painter.drawRoundedRect(rect, radius, radius)
