"""The top of Home: a greeting for the time of day, and the way into Search."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..theme import C, display_family, ui_font
from .icons import paint_icon

# English whatever the machine's locale: the rest of the app is.
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_COUNTS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
           "Eleven", "Twelve")


def part_of_day(hour: int) -> str:
    if hour < 5:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "night"


def greeting(now: datetime) -> str:
    # Not "Good night", which is a goodbye.
    return "Up late" if now.hour < 5 else f"Good {part_of_day(now.hour if now.hour < 22 else 21)}"


def day_line(now: datetime, waiting: int) -> str:
    """"A quiet Thursday evening. Four things are waiting where you left them." """
    line = f"A quiet {_DAYS[now.weekday()]} {part_of_day(now.hour)}."
    if waiting == 1:
        return line + " One thing is waiting where you left it."
    if waiting > 1:
        count = _COUNTS[waiting] if waiting < len(_COUNTS) else str(waiting)
        return line + f" {count} things are waiting where you left them."
    return line


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


class SearchPill(QAbstractButton):
    """What looks like a search field and opens Search, where the real one is.

    On hover it warms: the edge and the glass turn yellow and the glass leans
    in a little.
    """

    PLACEHOLDER = "Search films, shows, music…"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(380, 48)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Search  ( / or Ctrl+F )")
        self.setAccessibleName("Search films, shows, music")
        self._warm = 0.0
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(170)
        self._fade.valueChanged.connect(self._on_fade)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._toward(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._toward(0.0)
        super().leaveEvent(event)

    def _toward(self, value: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._warm)
        self._fade.setEndValue(value)
        self._fade.start()

    def _on_fade(self, value) -> None:
        self._warm = float(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        warm = self._warm
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        edge = _mix(QColor("#2B2520"), QColor(255, 210, 63, 120), warm)
        painter.setPen(QPen(edge, 1))
        painter.setBrush(_mix(QColor("#161311"), QColor(C.SURFACE_HOVER), warm))
        painter.drawRoundedRect(rect, 24, 24)

        paint_icon(painter, "search", QRectF(16 + 2 * warm, 14, 20, 20),
                   _mix(QColor(C.TEXT_FAINT), QColor(C.ACCENT), warm))
        painter.setFont(ui_font(11))
        painter.setPen(_mix(QColor(C.TEXT_FAINT), QColor(C.TEXT_DIM), warm))
        painter.drawText(QRectF(48, 0, self.width() - 48 - 52, self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.PLACEHOLDER)

        key = QRectF(self.width() - 12 - 28, (self.height() - 24) / 2, 28, 24)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#221E1A"))
        painter.drawRoundedRect(key, 8, 8)
        painter.setFont(ui_font(10, QFont.Weight.DemiBold))
        painter.setPen(QColor(C.TEXT_DIM))
        painter.drawText(key, Qt.AlignmentFlag.AlignCenter, "/")


class ControllerPill(QWidget):
    """"Controller ready": shown while a game controller is connected."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(48)
        self._words = "Controller ready"
        font = ui_font(10.5)
        self.setFixedWidth(16 + 24 + 8 + QFontMetrics(font).horizontalAdvance(self._words) + 18)
        self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#161311"))
        painter.drawRoundedRect(rect, 24, 24)
        paint_icon(painter, "gamepad", QRectF(16, 12, 24, 24), QColor(C.ACCENT))
        painter.setFont(ui_font(10.5))
        painter.setPen(QColor(C.TEXT_DIM))
        painter.drawText(QRectF(48, 0, self.width() - 48 - 12, self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._words)


class HomeHeader(QWidget):
    """"Good evening", a line about the day, and the search pill."""

    search_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._waiting = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)

        words = QVBoxLayout()
        words.setSpacing(2)
        self.greeting = QLabel()
        self.greeting.setStyleSheet(
            f'color: {C.TEXT}; font-family: "{display_family()}"; font-size: 24pt; font-weight: 700;')
        words.addWidget(self.greeting)
        self.day_line = QLabel()
        self.day_line.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 10.5pt;")
        words.addWidget(self.day_line)
        row.addLayout(words, 1)

        self.search = SearchPill()
        self.search.clicked.connect(self.search_requested.emit)
        row.addWidget(self.search, 0, Qt.AlignmentFlag.AlignVCenter)
        self.controller = ControllerPill()
        row.addWidget(self.controller, 0, Qt.AlignmentFlag.AlignVCenter)

        # The greeting follows the clock: evening comes while Home is open.
        self._clock = QTimer(self)
        self._clock.setInterval(60_000)
        self._clock.timeout.connect(self.refresh)
        self._clock.start()
        self.refresh()

    def set_controller(self, kind: str) -> None:
        """A controller's kind ("xbox", "playstation", "generic"), or "" for none."""
        self.controller.setVisible(bool(kind))
        self.controller.setToolTip({"playstation": "A PlayStation controller is connected",
                                    "xbox": "An Xbox controller is connected"}.get(kind, "A controller is connected"))

    def set_waiting(self, count: int) -> None:
        """How many things are part way through, for the day line."""
        self._waiting = max(0, int(count))
        self.refresh()

    def refresh(self) -> None:
        now = datetime.now()
        self.greeting.setText(greeting(now))
        self.day_line.setText(day_line(now, self._waiting))
