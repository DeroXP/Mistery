"""Vector icons drawn with QPainter.

Everything is authored in a 24x24 box and scaled to fit, so there is no icon
font to be missing and no bitmap to go soft on a high-DPI display.
"""

from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import QAbstractButton

from ..theme import C


def _tri(painter: QPainter, points: list[tuple[float, float]], color: QColor) -> None:
    path = QPainterPath()
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)
    path.closeSubpath()
    painter.fillPath(path, QBrush(color))


def _rounded(painter: QPainter, x, y, w, h, r, color: QColor) -> None:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    painter.fillPath(path, QBrush(color))


def _text(painter: QPainter, text: str, rect: QRectF, size: float, color: QColor) -> None:
    font = QFont(painter.font())
    font.setPixelSize(max(6, int(size)))
    font.setBold(True)
    painter.save()
    painter.setFont(font)
    painter.setPen(QPen(color))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


# --- individual glyphs ------------------------------------------------------

def _play(p: QPainter, c: QColor) -> None:
    _tri(p, [(7.5, 4.8), (19.2, 12.0), (7.5, 19.2)], c)


def _pause(p: QPainter, c: QColor) -> None:
    _rounded(p, 7.0, 5.0, 3.5, 14.0, 1.3, c)
    _rounded(p, 13.5, 5.0, 3.5, 14.0, 1.3, c)


def _stop(p: QPainter, c: QColor) -> None:
    _rounded(p, 6.5, 6.5, 11, 11, 1.8, c)


def _prev(p: QPainter, c: QColor) -> None:
    _rounded(p, 6.0, 5.5, 2.6, 13.0, 1.1, c)
    _tri(p, [(18.5, 5.5), (18.5, 18.5), (9.6, 12.0)], c)


def _next(p: QPainter, c: QColor) -> None:
    _rounded(p, 15.4, 5.5, 2.6, 13.0, 1.1, c)
    _tri(p, [(5.5, 5.5), (5.5, 18.5), (14.4, 12.0)], c)


def _skip_arc(p: QPainter, c: QColor, forward: bool, label: str) -> None:
    """A circular arrow with the jump size written inside."""
    rect = QRectF(3.6, 3.6, 16.8, 16.8)
    start, span = (108, -300) if forward else (72, 300)
    p.drawArc(rect, int(start * 16), int(span * 16))
    tip_x = 15.6 if forward else 8.4
    if forward:
        _tri(p, [(tip_x - 2.4, 2.0), (tip_x + 1.2, 4.6), (tip_x - 2.6, 6.6)], c)
    else:
        _tri(p, [(tip_x + 2.4, 2.0), (tip_x - 1.2, 4.6), (tip_x + 2.6, 6.6)], c)
    _text(p, label, QRectF(3.6, 8.2, 16.8, 11.0), 9.2, c)


def _back10(p: QPainter, c: QColor) -> None:
    _skip_arc(p, c, forward=False, label="10")


def _fwd10(p: QPainter, c: QColor) -> None:
    _skip_arc(p, c, forward=True, label="10")


def _speaker(p: QPainter, c: QColor) -> None:
    _tri(p, [(4.2, 9.2), (8.2, 9.2), (12.6, 5.0), (12.6, 19.0), (8.2, 14.8), (4.2, 14.8)], c)


def _volume(p: QPainter, c: QColor) -> None:
    _speaker(p, c)
    p.drawArc(QRectF(11.6, 8.0, 5.4, 8.0), int(-60 * 16), int(120 * 16))
    p.drawArc(QRectF(13.4, 5.4, 8.0, 13.2), int(-60 * 16), int(120 * 16))


def _volume_low(p: QPainter, c: QColor) -> None:
    _speaker(p, c)
    p.drawArc(QRectF(11.6, 8.0, 5.4, 8.0), int(-60 * 16), int(120 * 16))


def _mute(p: QPainter, c: QColor) -> None:
    _speaker(p, c)
    p.drawLine(QPointF(15.2, 9.2), QPointF(20.4, 14.8))
    p.drawLine(QPointF(20.4, 9.2), QPointF(15.2, 14.8))


def _fullscreen(p: QPainter, c: QColor) -> None:
    for dx, dy, hx, hy in ((0, 0, 1, 1), (1, 0, -1, 1), (0, 1, 1, -1), (1, 1, -1, -1)):
        x = 4.5 + dx * 15.0
        y = 4.5 + dy * 15.0
        p.drawLine(QPointF(x, y), QPointF(x + hx * 4.4, y))
        p.drawLine(QPointF(x, y), QPointF(x, y + hy * 4.4))


def _exit_fullscreen(p: QPainter, c: QColor) -> None:
    for dx, dy, hx, hy in ((0, 0, 1, 1), (1, 0, -1, 1), (0, 1, 1, -1), (1, 1, -1, -1)):
        x = 9.2 + dx * 5.6
        y = 9.2 + dy * 5.6
        p.drawLine(QPointF(x, y), QPointF(x + hx * 4.4, y))
        p.drawLine(QPointF(x, y), QPointF(x, y + hy * 4.4))


def _close(p: QPainter, c: QColor) -> None:
    p.drawLine(QPointF(6.4, 6.4), QPointF(17.6, 17.6))
    p.drawLine(QPointF(17.6, 6.4), QPointF(6.4, 17.6))


def _chevron_left(p: QPainter, c: QColor) -> None:
    p.drawPolyline(QPolygonF([QPointF(15, 5), QPointF(8.4, 12), QPointF(15, 19)]))


def _chevron_right(p: QPainter, c: QColor) -> None:
    p.drawPolyline(QPolygonF([QPointF(9, 5), QPointF(15.6, 12), QPointF(9, 19)]))


def _chevron_down(p: QPainter, c: QColor) -> None:
    p.drawPolyline(QPolygonF([QPointF(5.5, 9), QPointF(12, 15.6), QPointF(18.5, 9)]))


def _back(p: QPainter, c: QColor) -> None:
    p.drawLine(QPointF(5.2, 12), QPointF(18.8, 12))
    p.drawPolyline(QPolygonF([QPointF(11.2, 5.6), QPointF(5.0, 12), QPointF(11.2, 18.4)]))


def _home(p: QPainter, c: QColor) -> None:
    p.drawPolyline(QPolygonF([
        QPointF(3.6, 11.4), QPointF(12, 4.2), QPointF(20.4, 11.4),
    ]))
    p.drawPolyline(QPolygonF([
        QPointF(5.8, 10.2), QPointF(5.8, 19.6), QPointF(18.2, 19.6), QPointF(18.2, 10.2),
    ]))


def _film(p: QPainter, c: QColor) -> None:
    p.drawRoundedRect(QRectF(3.4, 5.0, 17.2, 14.0), 2.2, 2.2)
    p.drawLine(QPointF(8.0, 5.0), QPointF(8.0, 19.0))
    p.drawLine(QPointF(16.0, 5.0), QPointF(16.0, 19.0))
    for y in (8.0, 12.0, 16.0):
        p.drawLine(QPointF(3.4, y), QPointF(8.0, y))
        p.drawLine(QPointF(16.0, y), QPointF(20.6, y))


def _tv(p: QPainter, c: QColor) -> None:
    p.drawRoundedRect(QRectF(3.2, 6.6, 17.6, 11.6), 2.0, 2.0)
    p.drawLine(QPointF(8.4, 21.0), QPointF(15.6, 21.0))
    p.drawPolyline(QPolygonF([QPointF(8.2, 2.8), QPointF(12, 6.4), QPointF(15.8, 2.8)]))


def _search(p: QPainter, c: QColor) -> None:
    p.drawEllipse(QRectF(4.4, 4.4, 12.0, 12.0))
    p.drawLine(QPointF(15.4, 15.4), QPointF(20.2, 20.2))


def _settings(p: QPainter, c: QColor) -> None:
    """Sliders rather than a gear — a small gear turns to mush at 19px."""
    for y, knob_x in ((6.6, 15.4), (12.0, 8.6), (17.4, 16.6)):
        p.drawLine(QPointF(3.4, y), QPointF(20.6, y))
        p.save()
        p.setBrush(QBrush(c))
        p.drawEllipse(QPointF(knob_x, y), 2.5, 2.5)
        p.restore()


def _ticket(p: QPainter, c: QColor) -> None:
    """A cinema ticket, notched at the sides, with its tear line: movie night."""
    path = QPainterPath()
    path.moveTo(3.4, 6.4)
    path.lineTo(20.6, 6.4)
    path.lineTo(20.6, 9.6)
    path.arcTo(QRectF(18.2, 9.6, 4.8, 4.8), 90, 180)
    path.lineTo(20.6, 17.6)
    path.lineTo(3.4, 17.6)
    path.lineTo(3.4, 14.4)
    path.arcTo(QRectF(1.0, 9.6, 4.8, 4.8), -90, 180)
    path.closeSubpath()
    p.drawPath(path)
    for y in (8.6, 11.0, 13.4, 15.8):
        p.drawLine(QPointF(14.6, y), QPointF(14.6, y + 0.4))


def _people(p: QPainter, c: QColor) -> None:
    """Two people, the one in front a little larger: the movie night mark."""
    # The friend behind: a smaller head, and only the shoulder that shows.
    p.drawEllipse(QPointF(16.4, 7.9), 2.7, 2.7)
    behind = QPainterPath()
    behind.moveTo(15.2, 12.9)
    behind.cubicTo(15.7, 12.7, 16.1, 12.6, 16.6, 12.6)
    behind.cubicTo(19.2, 12.6, 21.2, 14.5, 21.2, 18.2)
    p.drawPath(behind)
    # The one in front.
    p.drawEllipse(QPointF(9.0, 8.6), 3.4, 3.4)
    front = QPainterPath()
    front.moveTo(2.8, 20.0)
    front.cubicTo(2.8, 15.6, 5.6, 13.6, 9.0, 13.6)
    front.cubicTo(12.4, 13.6, 15.2, 15.6, 15.2, 20.0)
    p.drawPath(front)


def _vr(p: QPainter, c: QColor) -> None:
    """A headset from the front: one visor, a notch for the nose."""
    path = QPainterPath()
    path.moveTo(5.0, 7.0)
    path.lineTo(19.0, 7.0)
    path.quadTo(21.4, 7.0, 21.4, 9.4)
    path.lineTo(21.4, 14.8)
    path.quadTo(21.4, 17.2, 19.0, 17.2)
    path.lineTo(15.6, 17.2)
    path.lineTo(13.9, 14.9)
    path.quadTo(12.0, 12.9, 10.1, 14.9)
    path.lineTo(8.4, 17.2)
    path.lineTo(5.0, 17.2)
    path.quadTo(2.6, 17.2, 2.6, 14.8)
    path.lineTo(2.6, 9.4)
    path.quadTo(2.6, 7.0, 5.0, 7.0)
    path.closeSubpath()
    p.drawPath(path)


def _mug(p: QPainter, c: QColor) -> None:
    """A cup with steam rising: cozy."""
    body = QPainterPath()
    body.moveTo(4.6, 10.0)
    body.lineTo(15.4, 10.0)
    body.lineTo(15.4, 16.0)
    body.quadTo(15.4, 20.0, 11.4, 20.0)
    body.lineTo(8.6, 20.0)
    body.quadTo(4.6, 20.0, 4.6, 16.0)
    body.closeSubpath()
    p.drawPath(body)
    p.drawArc(QRectF(13.6, 11.2, 5.6, 5.6), int(-90 * 16), int(180 * 16))
    for x in (8.2, 11.8):
        steam = QPainterPath()
        steam.moveTo(x, 7.6)
        steam.cubicTo(x - 1.3, 6.4, x + 1.3, 5.2, x, 3.6)
        p.drawPath(steam)


def _rain(p: QPainter, c: QColor) -> None:
    """A cloud, and rain from it."""
    cloud = QPainterPath()
    cloud.moveTo(7.0, 15.0)
    cloud.cubicTo(4.2, 15.0, 3.0, 13.0, 3.4, 11.2)
    cloud.cubicTo(3.8, 9.4, 5.6, 8.6, 7.2, 9.0)
    cloud.cubicTo(8.0, 6.0, 11.0, 4.6, 13.8, 5.4)
    cloud.cubicTo(16.2, 6.1, 17.6, 8.2, 17.4, 10.2)
    cloud.cubicTo(19.6, 10.2, 21.0, 11.8, 20.8, 13.2)
    cloud.cubicTo(20.6, 14.4, 19.6, 15.0, 18.4, 15.0)
    cloud.closeSubpath()
    p.drawPath(cloud)
    for x in (8.4, 12.4, 16.4):
        p.drawLine(QPointF(x, 17.8), QPointF(x - 1.0, 20.4))


def _clock(p: QPainter, c: QColor) -> None:
    """A stopwatch: not much time, or a while ago."""
    p.drawEllipse(QRectF(4.0, 5.4, 15.6, 15.6))
    p.drawLine(QPointF(11.8, 9.4), QPointF(11.8, 13.2))
    p.drawLine(QPointF(11.8, 13.2), QPointF(14.2, 14.8))
    p.drawLine(QPointF(9.8, 2.6), QPointF(13.8, 2.6))
    p.drawLine(QPointF(11.8, 2.6), QPointF(11.8, 5.4))


def _smile(p: QPainter, c: QColor) -> None:
    """A smiling face: feel-good."""
    p.drawEllipse(QRectF(3.4, 3.4, 17.2, 17.2))
    p.drawArc(QRectF(7.6, 8.0, 8.8, 8.4), int(200 * 16), int(140 * 16))
    p.save()
    p.setBrush(QBrush(c))
    p.drawEllipse(QPointF(9.3, 9.9), 0.55, 0.55)
    p.drawEllipse(QPointF(14.7, 9.9), 0.55, 0.55)
    p.restore()


def _resume(p: QPainter, c: QColor) -> None:
    """Round and back to where you were: what you stopped part way."""
    p.drawArc(QRectF(4.0, 4.0, 16.0, 16.0), int(180 * 16), int(270 * 16))
    p.drawPolyline(QPolygonF([QPointF(4.0, 4.0), QPointF(4.0, 9.0), QPointF(9.0, 9.0)]))


def _gamepad(p: QPainter, c: QColor) -> None:
    """A game controller: its body, a d-pad on the left, two buttons on the right."""
    body = QPainterPath()
    body.moveTo(7.0, 8.0)
    body.lineTo(17.0, 8.0)
    body.quadTo(21.0, 8.0, 21.0, 12.0)
    body.lineTo(21.6, 16.2)
    body.quadTo(21.8, 18.4, 19.6, 18.6)
    body.quadTo(18.2, 18.7, 17.5, 18.0)
    body.lineTo(15.0, 15.0)
    body.lineTo(9.0, 15.0)
    body.lineTo(6.5, 18.0)
    body.quadTo(5.8, 18.7, 4.4, 18.6)
    body.quadTo(2.2, 18.4, 2.4, 16.2)
    body.lineTo(3.0, 12.0)
    body.quadTo(3.0, 8.0, 7.0, 8.0)
    body.closeSubpath()
    p.drawPath(body)
    p.drawLine(QPointF(7.0, 10.9), QPointF(7.0, 14.1))
    p.drawLine(QPointF(5.4, 12.5), QPointF(8.6, 12.5))
    for x, y in ((15.6, 11.8), (17.6, 13.6)):
        p.drawLine(QPointF(x, y), QPointF(x + 0.01, y))


def _refresh(p: QPainter, c: QColor) -> None:
    p.drawArc(QRectF(4.2, 4.2, 15.6, 15.6), int(60 * 16), int(280 * 16))
    _tri(p, [(17.6, 2.4), (20.8, 6.6), (15.4, 7.4)], c)


def _cc(p: QPainter, c: QColor) -> None:
    p.drawRoundedRect(QRectF(2.8, 5.6, 18.4, 12.8), 2.6, 2.6)
    _text(p, "CC", QRectF(2.8, 5.6, 18.4, 12.8), 8.6, c)


def _audio(p: QPainter, c: QColor) -> None:
    _speaker(p, c)
    p.drawArc(QRectF(12.4, 7.2, 6.2, 9.6), int(-58 * 16), int(116 * 16))
    p.drawArc(QRectF(14.6, 4.4, 8.0, 15.2), int(-58 * 16), int(116 * 16))


def _boost(p: QPainter, c: QColor) -> None:
    """Waveform with an up-arrow: the dialogue-boost toggle."""
    for index, height in enumerate((5.0, 9.0, 13.4, 8.0, 4.2)):
        x = 3.6 + index * 3.3
        _rounded(p, x, 12 - height / 2, 2.0, height, 1.0, c)
    p.drawPolyline(QPolygonF([QPointF(18.0, 9.4), QPointF(20.6, 6.4), QPointF(23.0, 9.4)]))
    p.drawLine(QPointF(20.6, 6.6), QPointF(20.6, 15.4))


def _music(p: QPainter, c: QColor) -> None:
    """Two beamed quavers."""
    p.drawLine(QPointF(9.0, 17.0), QPointF(9.0, 5.6))
    p.drawLine(QPointF(18.6, 15.0), QPointF(18.6, 3.8))
    p.drawLine(QPointF(9.0, 5.6), QPointF(18.6, 3.8))
    p.save()
    p.setBrush(QBrush(c))
    p.drawEllipse(QPointF(6.6, 17.4), 2.7, 2.2)
    p.drawEllipse(QPointF(16.2, 15.4), 2.7, 2.2)
    p.restore()


def _shuffle(p: QPainter, c: QColor) -> None:
    p.drawPolyline(QPolygonF([QPointF(3.6, 7.4), QPointF(8.0, 7.4), QPointF(15.2, 16.6),
                              QPointF(19.6, 16.6)]))
    p.drawPolyline(QPolygonF([QPointF(3.6, 16.6), QPointF(8.0, 16.6), QPointF(10.4, 13.5)]))
    p.drawPolyline(QPolygonF([QPointF(12.8, 10.5), QPointF(15.2, 7.4), QPointF(19.6, 7.4)]))
    _tri(p, [(18.4, 4.6), (21.6, 7.4), (18.4, 10.2)], c)
    _tri(p, [(18.4, 13.8), (21.6, 16.6), (18.4, 19.4)], c)


def _repeat(p: QPainter, c: QColor) -> None:
    p.drawPolyline(QPolygonF([QPointF(5.0, 12.4), QPointF(5.0, 8.4), QPointF(18.0, 8.4)]))
    p.drawPolyline(QPolygonF([QPointF(19.0, 11.6), QPointF(19.0, 15.6), QPointF(6.0, 15.6)]))
    _tri(p, [(16.6, 5.4), (20.0, 8.4), (16.6, 11.4)], c)
    _tri(p, [(7.4, 12.6), (4.0, 15.6), (7.4, 18.6)], c)


def _repeat_one(p: QPainter, c: QColor) -> None:
    _repeat(p, c)
    _text(p, "1", QRectF(8.0, 8.6, 8.0, 7.0), 7.0, c)


def _lyrics(p: QPainter, c: QColor) -> None:
    """A speech bubble holding lines of text."""
    path = QPainterPath()
    path.addRoundedRect(QRectF(3.4, 4.2, 17.2, 12.6), 3.0, 3.0)
    p.drawPath(path)
    p.drawPolyline(QPolygonF([QPointF(7.4, 16.8), QPointF(6.4, 20.4), QPointF(11.0, 16.8)]))
    p.drawLine(QPointF(7.2, 8.6), QPointF(16.8, 8.6))
    p.drawLine(QPointF(7.2, 12.4), QPointF(13.4, 12.4))


def _queue(p: QPainter, c: QColor) -> None:
    for y in (6.4, 11.6):
        p.drawLine(QPointF(3.6, y), QPointF(20.4, y))
    p.drawLine(QPointF(3.6, 16.8), QPointF(11.6, 16.8))
    _tri(p, [(14.6, 13.6), (20.6, 16.8), (14.6, 20.0)], c)


def _quality(p: QPainter, c: QColor) -> None:
    """Rising bars: how much work is going into the picture."""
    for index, height in enumerate((5.5, 9.5, 13.5)):
        _rounded(p, 4.6 + index * 5.4, 19.0 - height, 3.4, height, 1.3, c)


def _autoplay(p: QPainter, c: QColor) -> None:
    """A play mark inside a loop: keep going on your own."""
    p.drawArc(QRectF(4.2, 4.2, 15.6, 15.6), int(60 * 16), int(280 * 16))
    _tri(p, [(17.6, 2.4), (20.8, 6.6), (15.4, 7.4)], c)
    _tri(p, [(9.8, 8.4), (15.2, 12.0), (9.8, 15.6)], c)


def _chapters(p: QPainter, c: QColor) -> None:
    for y in (7.0, 12.0, 17.0):
        p.drawEllipse(QRectF(4.0, y - 1.1, 2.2, 2.2))
        p.drawLine(QPointF(8.8, y), QPointF(20.0, y))


def _info(p: QPainter, c: QColor) -> None:
    p.drawEllipse(QRectF(3.6, 3.6, 16.8, 16.8))
    p.drawLine(QPointF(12, 11.0), QPointF(12, 16.6))
    p.drawEllipse(QRectF(11.2, 7.0, 1.7, 1.7))


def _check(p: QPainter, c: QColor) -> None:
    p.drawPolyline(QPolygonF([QPointF(5.0, 12.4), QPointF(10.0, 17.4), QPointF(19.2, 6.8)]))


def _star(p: QPainter, c: QColor) -> None:
    points = []
    for index in range(10):
        radius = 9.2 if index % 2 == 0 else 4.0
        angle = math.radians(-90 + index * 36)
        points.append((12 + radius * math.cos(angle), 12 + radius * math.sin(angle)))
    _tri(p, points, c)


def _folder(p: QPainter, c: QColor) -> None:
    p.drawPolyline(QPolygonF([
        QPointF(3.4, 18.6), QPointF(3.4, 6.2), QPointF(9.4, 6.2),
        QPointF(11.4, 8.8), QPointF(20.6, 8.8), QPointF(20.6, 18.6), QPointF(3.4, 18.6),
    ]))


def _plus(p: QPainter, c: QColor) -> None:
    p.drawLine(QPointF(12, 5.4), QPointF(12, 18.6))
    p.drawLine(QPointF(5.4, 12), QPointF(18.6, 12))


def _trash(p: QPainter, c: QColor) -> None:
    p.drawLine(QPointF(4.4, 6.8), QPointF(19.6, 6.8))
    p.drawPolyline(QPolygonF([
        QPointF(6.6, 6.8), QPointF(7.6, 19.8), QPointF(16.4, 19.8), QPointF(17.4, 6.8),
    ]))
    p.drawPolyline(QPolygonF([QPointF(9.4, 6.6), QPointF(9.8, 4.0),
                              QPointF(14.2, 4.0), QPointF(14.6, 6.6)]))


def _speed(p: QPainter, c: QColor) -> None:
    p.drawArc(QRectF(3.4, 5.4, 17.2, 17.2), 0, int(180 * 16))
    p.drawLine(QPointF(12, 14.0), QPointF(16.6, 9.6))


def _sound(p: QPainter, c: QColor) -> None:
    """Three sliders: the sound settings."""
    for index, (y, knob) in enumerate(((6.0, 15.4), (12.0, 8.6), (18.0, 13.0))):
        p.drawLine(QPointF(3.4, y), QPointF(20.6, y))
        p.setBrush(QBrush(c))
        p.drawEllipse(QPointF(knob, y), 2.3, 2.3)
        p.setBrush(Qt.BrushStyle.NoBrush)


def _spatial(p: QPainter, c: QColor) -> None:
    """A head between two arcs: sound arriving from either side."""
    p.drawEllipse(QPointF(12.0, 12.0), 3.4, 3.4)
    for side in (-1, 1):
        for radius in (6.2, 9.0):
            box = QRectF(12.0 - radius, 12.0 - radius, radius * 2, radius * 2)
            start = 0 if side > 0 else 180
            p.drawArc(box, int((start - 34) * 16), int(68 * 16))


def _heart_path() -> QPainterPath:
    path = QPainterPath()
    path.moveTo(12.0, 20.2)
    path.cubicTo(12.0, 20.2, 3.2, 14.8, 3.2, 8.9)
    path.cubicTo(3.2, 6.2, 5.3, 4.2, 7.8, 4.2)
    path.cubicTo(9.6, 4.2, 11.1, 5.2, 12.0, 6.7)
    path.cubicTo(12.9, 5.2, 14.4, 4.2, 16.2, 4.2)
    path.cubicTo(18.7, 4.2, 20.8, 6.2, 20.8, 8.9)
    path.cubicTo(20.8, 14.8, 12.0, 20.2, 12.0, 20.2)
    path.closeSubpath()
    return path


def _heart(p: QPainter, c: QColor) -> None:
    """Liked songs: an outline heart."""
    p.drawPath(_heart_path())


def _heart_filled(p: QPainter, c: QColor) -> None:
    path = _heart_path()
    p.fillPath(path, QBrush(c))
    p.drawPath(path)            # the same outline, so filling in doesn't shrink it


def _moon(p: QPainter, c: QColor) -> None:
    """The sleep timer: a crescent, cut from a circle by a second one."""
    outer = QPainterPath()
    outer.addEllipse(QPointF(12.0, 12.4), 8.2, 8.2)
    bite = QPainterPath()
    bite.addEllipse(QPointF(16.6, 8.0), 6.8, 6.8)
    p.drawPath(outer.subtracted(bite))


def _disc(p: QPainter, c: QColor) -> None:
    """A record: the Now Playing cover style."""
    p.drawEllipse(QPointF(12.0, 12.0), 8.6, 8.6)
    p.drawEllipse(QPointF(12.0, 12.0), 3.0, 3.0)


def _device(p: QPainter, c: QColor) -> None:
    """A loudspeaker cabinet: the output device."""
    p.drawRoundedRect(QRectF(6.0, 3.2, 12.0, 17.6), 2.2, 2.2)
    p.drawEllipse(QPointF(12.0, 14.4), 3.3, 3.3)
    p.save()
    p.setBrush(QBrush(c))
    p.drawEllipse(QPointF(12.0, 7.4), 1.1, 1.1)
    p.restore()


DRAWERS: dict[str, Callable[[QPainter, QColor], None]] = {
    "play": _play, "pause": _pause, "stop": _stop, "prev": _prev, "next": _next,
    "back10": _back10, "fwd10": _fwd10, "volume": _volume, "volume_low": _volume_low,
    "mute": _mute, "fullscreen": _fullscreen, "exit_fullscreen": _exit_fullscreen,
    "close": _close, "chevron_left": _chevron_left, "chevron_right": _chevron_right,
    "chevron_down": _chevron_down, "back": _back, "home": _home, "film": _film,
    "tv": _tv, "search": _search, "settings": _settings, "refresh": _refresh, "ticket": _ticket,
    "cc": _cc, "audio": _audio, "boost": _boost, "chapters": _chapters,
    "autoplay": _autoplay, "quality": _quality, "music": _music,
    "shuffle": _shuffle, "repeat": _repeat, "repeat_one": _repeat_one,
    "lyrics": _lyrics, "queue": _queue,
    "info": _info, "check": _check, "star": _star, "folder": _folder,
    "plus": _plus, "trash": _trash, "speed": _speed,
    "sound": _sound, "spatial": _spatial,
    "heart": _heart, "heart_filled": _heart_filled, "moon": _moon, "disc": _disc,
    "device": _device, "people": _people, "vr": _vr,
    "mug": _mug, "rain": _rain, "clock": _clock, "smile": _smile, "resume": _resume,
    "gamepad": _gamepad,
}


def paint_icon(
    painter: QPainter,
    name: str,
    rect: QRectF,
    color: QColor,
    stroke: float = 1.85,
) -> None:
    drawer = DRAWERS.get(name)
    if drawer is None:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = min(rect.width(), rect.height()) / 24.0
    painter.translate(rect.center().x() - 12 * scale, rect.center().y() - 12 * scale)
    painter.scale(scale, scale)
    pen = QPen(color, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    drawer(painter, color)
    painter.restore()


def icon_pixmap(name: str, size: int, color: str, ratio: float = 1.0) -> QPixmap:
    """A glyph as a pixmap, for QPushButton.setIcon.

    Letting Qt lay the icon out next to the label beats painting it ourselves at
    a guessed offset, which goes wrong as soon as the label length changes.
    """
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_icon(painter, name, QRectF(0, 0, size, size), QColor(color))
    painter.end()
    return pixmap


class IconButton(QAbstractButton):
    """A flat, hover-highlighted icon button."""

    def __init__(
        self,
        name: str,
        size: int = 38,
        icon_size: int = 21,
        tooltip: str = "",
        checkable: bool = False,
        accent_when_checked: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._icon_size = icon_size
        self._accent_when_checked = accent_when_checked
        self._pop: QVariantAnimation | None = None
        self._pop_scale = 1.0
        self.setCheckable(checkable)
        self.setFixedSize(QSize(size, size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if tooltip:
            self.setToolTip(tooltip)

    def icon_name(self) -> str:
        return self._name

    def set_icon_name(self, name: str) -> None:
        if name != self._name:
            self._name = name
            self.update()

    def sizeHint(self) -> QSize:
        return self.size()

    def pop(self) -> None:
        """A quick swell and settle of the glyph, to acknowledge a choice (a like).

        Only the glyph is repainted, a dozen times over a quarter of a second;
        nothing runs once it has settled.
        """
        if self._pop is None:
            self._pop = QVariantAnimation(self)
            self._pop.setStartValue(0.0)
            self._pop.setEndValue(1.0)
            self._pop.setDuration(260)
            self._pop.valueChanged.connect(self._on_pop)
        self._pop.stop()
        self._pop.start()

    def _on_pop(self, value) -> None:
        # Up to 1.3x in the first third, then an eased settle back to 1.
        t = float(value)
        self._pop_scale = 1.0 + 0.3 * (t / 0.33 if t < 0.33 else (1.0 - (t - 0.33) / 0.67) ** 2)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())

        hovered = self.underMouse() and self.isEnabled()
        if hovered or self.isDown():
            alpha = 34 if not self.isDown() else 56
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, alpha))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)

        if not self.isEnabled():
            color = QColor(C.TEXT_FAINT)
        elif self.isChecked() and self._accent_when_checked:
            color = QColor(C.ACCENT)
        elif hovered:
            color = QColor(C.TEXT)
        else:
            color = QColor(C.TEXT_DIM)

        side = float(self._icon_size) * self._pop_scale
        icon_rect = QRectF(0, 0, side, side)
        icon_rect.moveCenter(rect.center())
        paint_icon(painter, self._name, icon_rect, color)
