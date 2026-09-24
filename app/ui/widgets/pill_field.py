"""A page's own "Find in…" field: a rounded pill with a glass in it, whose edge
turns yellow while it has the keys."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget

from ..theme import C
from .icons import paint_icon


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


class PillField(QWidget):
    """`edit` is the QLineEdit inside; everything else is the look."""

    HEIGHT = 44

    def __init__(self, placeholder: str, width: int = 280, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(width, self.HEIGHT)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("PillFieldEdit")
        self.edit.setPlaceholderText(placeholder)
        self.edit.setAccessibleName(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.setStyleSheet(
            f"QLineEdit#PillFieldEdit {{ background: transparent; border: none; padding: 0; margin: 0;"
            f" font-size: 10.5pt; color: {C.TEXT}; selection-background-color: {C.ACCENT};"
            f" selection-color: {C.ON_ACCENT}; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(40, 0, 10, 0)
        row.addWidget(self.edit)
        self._focus = 0.0
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(170)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._on_fade)
        self.edit.installEventFilter(self)

    def _on_fade(self, value) -> None:
        self._focus = float(value)
        self.update()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.edit and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self._fade.stop()
            self._fade.setStartValue(self._focus)
            self._fade.setEndValue(1.0 if event.type() == QEvent.Type.FocusIn else 0.0)
            self._fade.start()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.edit.setFocus()
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        focus = self._focus
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(_mix(QColor("#2B2520"), QColor(C.ACCENT), focus), 1 + focus))
        painter.setBrush(QColor("#161311"))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        paint_icon(painter, "search", QRectF(14, (self.height() - 18) / 2, 18, 18),
                   _mix(QColor(C.TEXT_FAINT), QColor(C.ACCENT), focus))
