"""A shared empty-state panel: icon, headline, explanation, optional action."""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QTextDocument
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..theme import C, display_family
from .icons import paint_icon

COLUMN_WIDTH = 560


class WrapLabel(QLabel):
    """A word-wrapped label that reports its height honestly.

    QLabel.heightForWidth under-reports wrapped rich text — measured here, a
    paragraph needing 103px comes back as 95px, so the tail gets clipped. Laying
    the same text out in a QTextDocument gives the true height.
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        # heightForWidth must be flagged on the policy or the layout never asks.
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _document_height(self, width: int) -> int:
        document = QTextDocument()
        document.setDefaultFont(self.font())
        document.setTextWidth(max(1, width))
        if self.textFormat() == Qt.TextFormat.PlainText:
            document.setPlainText(self.text())
        else:
            document.setHtml(self.text())
        return int(math.ceil(document.size().height())) + 2

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        return self._document_height(width)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        width = self.width() or COLUMN_WIDTH
        return QSize(width, self._document_height(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.sizeHint()


class _Glyph(QWidget):
    def __init__(self, name: str, size: int = 64, parent=None) -> None:
        super().__init__(parent)
        self._name = name
        self.setFixedSize(QSize(size, size))

    def set_name(self, name: str) -> None:
        self._name = name
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(246, 240, 230, 14))
        painter.drawRoundedRect(rect, 22, 22)
        paint_icon(painter, self._name, rect.adjusted(17, 17, -17, -17),
                   QColor(C.TEXT_FAINT), stroke=1.7)


class EmptyState(QWidget):
    action_clicked = Signal()

    def __init__(
        self,
        icon: str = "film",
        headline: str = "",
        body: str = "",
        action: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Everything sits in a fixed-width column so the wrapped body has a
        # definite width to measure against; the column itself is centred.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 56, 0, 40)
        outer.setSpacing(0)
        outer.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        column = QWidget()
        column.setFixedWidth(COLUMN_WIDTH)
        outer.addWidget(column, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._glyph = _Glyph(icon)
        layout.addWidget(self._glyph, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(20)

        self._headline = QLabel(headline)
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._headline.setStyleSheet(
            f'color: {C.TEXT}; font-family: "{display_family()}"; font-size: 19pt; font-weight: 700;'
        )
        layout.addWidget(self._headline)
        layout.addSpacing(12)

        self._body = WrapLabel(body)
        self._body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body.setTextFormat(Qt.TextFormat.RichText)
        # Size comes from the QFont, not the stylesheet: WrapLabel measures with
        # self.font(), and a stylesheet size isn't applied until the widget is
        # polished — so it would measure at one size and paint at another.
        body_font = QFont(self.font())
        body_font.setPointSizeF(10.5)
        self._body.setFont(body_font)
        self._body.setStyleSheet(f"color: {C.TEXT_DIM};")
        layout.addWidget(self._body)          # no alignment: keeps heightForWidth
        layout.addSpacing(26)

        self._action = QPushButton(action)
        self._action.setObjectName("Primary")
        self._action.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action.clicked.connect(self.action_clicked.emit)
        self._action.setVisible(bool(action))
        layout.addWidget(self._action, alignment=Qt.AlignmentFlag.AlignHCenter)

    def _sync_body_height(self) -> None:
        """Give the body exactly the height its wrapped text needs.

        The surrounding layout caches a stale hint for this label (measured 64px
        for text that needs 100px), so the height is pinned directly instead.
        """
        if not self._body.text():
            return
        needed = self._body.heightForWidth(COLUMN_WIDTH)
        if needed > 0 and self._body.height() != needed:
            self._body.setFixedHeight(needed)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_body_height()          # fonts and styles are applied by now

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_body_height()

    def configure(
        self,
        icon: str | None = None,
        headline: str | None = None,
        body: str | None = None,
        action: str | None = None,
    ) -> None:
        if icon is not None:
            self._glyph.set_name(icon)
        if headline is not None:
            self._headline.setText(headline)
        if body is not None:
            self._body.setText(body)
            self._body.setVisible(bool(body))
            self._body.updateGeometry()
            self._sync_body_height()
        if action is not None:
            self._action.setText(action)
            self._action.setVisible(bool(action))
