"""Home's hover preview: rest on a card and it grows, and plays.

Rest the pointer on a card on Home for a moment (the card's dwell) and a bigger
card opens over it: the film playing from where you left off, silent for the
first two seconds and then with its sound fading up, and under it the title,
how far along you are, and Resume / More info. A title you have not started
opens the same card with its picture and no film: nothing to pick up from, and
no spoiler from a minute chosen at random. A friend's title is a picture too:
the file is on their PC.

One card for the page, and one small mpv for it, started the first time it is
needed and kept, so the next hover costs the opening of a file rather than the
start of a player. The video surface is a native window (mpv draws into it),
which is why it only appears once the first frame is ready (the picture holds
the place until then) and why the card has no fade: Qt's opacity effects do
not reach native windows.
"""

from __future__ import annotations

import logging
import math
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap, QRegion
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ... import db
from ...config import settings
from ...images import art_pixmap
from ...metadata import categories as cat
from ...models import MediaItem, ShowItem
from ...util import fmt_clock, fmt_duration, fmt_remaining
from ..theme import C, display_family

_log = logging.getLogger("ui")

SOUND_AFTER_MS = 2000       # silent this long after the first frame
SOUND_RAMP_MS = 1600        # then up to the preview's volume over this long
SHADOW = 26                 # room around the card for its shadow
GROW = 1.34                 # a wide card's preview, against the card


def _started(item) -> bool:
    return isinstance(item, MediaItem) and item.resume_position > 0 and bool(item.path)


def _show_episode(item: ShowItem) -> MediaItem | None:
    """The episode of a show you are part way through, first in the show's order."""
    try:
        rows = db.episodes_for_show(int(item.id))
    except Exception:                       # noqa: BLE001 - a picture instead
        return None
    for row in rows:
        episode = MediaItem.from_row(row)
        if _started(episode) and not row["missing"]:
            return episode
    return None


def _show_of(episode: MediaItem) -> tuple[str, str | None]:
    """An episode's show: its name, and its genres (an episode has none of its own)."""
    if episode.show_id is None:
        return "", None
    try:
        row = db.get_show(int(episode.show_id))
    except Exception:                       # noqa: BLE001 - the words without it
        return "", None
    if row is None:
        return "", None
    return row["title"] or "", row["genres"]


class _Surface(QWidget):
    """The native window mpv draws the preview into."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        QPainter(self).fillRect(self.rect(), QColor("#000000"))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        # Rounded top corners: a native window is clipped by a region, which
        # has no anti-aliasing, so the radius is kept small.
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.width(), self.height() + 20), 14, 14)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))


def _preview_mpv():
    """A small mpv for previews: silent to begin with, no subtitles."""
    from ...player.mpv_process import MpvProcess

    class PreviewMpv(MpvProcess):
        volume_max = 100

        def observed_properties(self) -> list[str]:
            return ["time-pos"]

        def _base_arguments(self, window_id):
            args = [a for a in super()._base_arguments(window_id)
                    if not a.startswith(("--volume=", "--sub-visibility=", "--audio-file-auto"))]
            return args + ["--volume=0", "--sub-visibility=no", "--audio-file-auto=no",
                           "--hr-seek=no", "--title=Mistery preview"]

    return PreviewMpv()


class HoverPreview(QWidget):
    """The card that opens over a card on Home (rest_on), and plays."""

    play_requested = Signal(object)
    open_requested = Signal(object)

    def __init__(self, parent: QWidget, sound_allowed: Callable[[], bool] | None = None) -> None:
        super().__init__(parent)
        self._sound_allowed = sound_allowed or (lambda: True)
        self._item = None
        self._card = None
        self._episode: MediaItem | None = None
        self._still: QPixmap | None = None
        self._playing = False
        self._mpv = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)
        outer.setSpacing(0)
        self._media = QWidget(self)
        outer.addWidget(self._media)
        self._surface = _Surface(self._media)
        self._surface.hide()

        info = QWidget(self)
        body = QVBoxLayout(info)
        body.setContentsMargins(18, 14, 18, 16)
        body.setSpacing(6)
        self._title = QLabel(info)
        self._title.setStyleSheet(
            f'color: {C.TEXT}; font-family: "{display_family()}"; font-size: 15pt; font-weight: 700;')
        body.addWidget(self._title)
        self._meta = QLabel(info)
        self._meta.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 9.5pt;")
        body.addWidget(self._meta)
        self._genres = QLabel(info)
        self._genres.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9pt;")
        body.addWidget(self._genres)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 6, 0, 0)
        buttons.setSpacing(8)
        self._play = QPushButton(info)
        self._play.setObjectName("Primary")
        self._play.setStyleSheet("padding: 8px 18px; font-size: 10pt; border-radius: 17px;")
        self._play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play.clicked.connect(lambda: self._emit(self.play_requested))
        buttons.addWidget(self._play)
        self._more = QPushButton("More info", info)
        self._more.setObjectName("Ghost")
        self._more.setStyleSheet("padding: 8px 16px; font-size: 10pt; border-radius: 17px;")
        self._more.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more.clicked.connect(lambda: self._emit(self.open_requested))
        buttons.addWidget(self._more)
        buttons.addStretch(1)
        self._hint = QLabel(info)
        self._hint.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 8.5pt;")
        buttons.addWidget(self._hint)
        body.addLayout(buttons)
        body.addStretch(1)              # any room the card is given past its words
        outer.addWidget(info)
        self._info = info

        self._grow = QPropertyAnimation(self, b"geometry", self)
        self._grow.setDuration(230)
        self._grow.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._grow.finished.connect(self._grown)
        self._sound_wait = QTimer(self)
        self._sound_wait.setSingleShot(True)
        self._sound_wait.setInterval(SOUND_AFTER_MS)
        self._sound_wait.timeout.connect(self._fade_sound_in)
        self._sound = QVariantAnimation(self)
        self._sound.setDuration(SOUND_RAMP_MS)
        self._sound.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._sound.valueChanged.connect(self._set_volume)
        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._update_time)
        self.hide()                     # last: hiding runs hideEvent, which needs the timers

    # --- opening and closing -----------------------------------------------------------------

    @property
    def item(self):
        return self._item

    @property
    def is_playing(self) -> bool:
        return self._playing

    def rest_on(self, card) -> None:
        """Open over `card` (a cards._BaseCard), its item playing if it can."""
        parent = self.parentWidget()
        if parent is None or not parent.isVisible():
            return
        if self.isVisible() and self._card is card:
            return
        self.close_preview()
        self._card = card
        self._item = card.item
        self._episode = self._playable(card.item)
        self._fill_words()
        self._still = self._picture(card)
        art = card.art_rect_in(parent)
        final = self._final_rect(art, QRect(card.mapTo(parent, QPoint(0, 0)), card.size()),
                                 self._heading_band(card, parent), parent)
        start = QRect(art.topLeft() - QPoint(SHADOW, SHADOW), art.size() + QSize(2 * SHADOW, 2 * SHADOW))
        self._info.hide()
        self.setGeometry(start)
        self.show()
        self.raise_()
        self._grow.stop()
        self._grow.setStartValue(start)
        self._grow.setEndValue(final)
        self._grow.start()

    def close_preview(self) -> None:
        self._grow.stop()
        self._stop_video()
        self._card = None
        self._item = None
        self._episode = None
        self.hide()

    def card_left(self, card) -> None:
        """The pointer left the card underneath: close, unless it went onto this."""
        if card is not self._card or not self.isVisible():
            return
        from PySide6.QtGui import QCursor

        if not self.geometry().adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW).contains(
                self.parentWidget().mapFromGlobal(QCursor.pos())):
            self.close_preview()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().leaveEvent(event)
        self.close_preview()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.close_preview()            # the page is scrolling out from under it
        event.ignore()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().hideEvent(event)
        self._stop_video()

    def _grown(self) -> None:
        if not self.isVisible():
            return
        self._info.show()
        if self._episode is not None:
            self._start_video(self._episode)

    @staticmethod
    def _heading_band(card, parent: QWidget) -> tuple[int, int] | None:
        """The top and foot, in `parent`, of the heading over the card's row."""
        widget = card.parentWidget()
        while widget is not None and widget is not parent:
            heading = widget.findChild(QLabel, "SectionTitle", Qt.FindChildOption.FindDirectChildrenOnly)
            if heading is not None and heading.isVisible():
                top = heading.mapTo(parent, QPoint(0, 0)).y()
                return top, top + heading.height()
            widget = widget.parentWidget()
        return None

    def _final_rect(self, art: QRect, whole: QRect, band: tuple[int, int] | None, parent: QWidget) -> QRect:
        """Where the card ends up: `art` is the picture on the card underneath,
        `whole` that card with its words, `band` the row's heading."""
        wide = art.width() >= art.height()
        info_h = self._info.sizeHint().height()
        if wide:
            width = round(art.width() * GROW)
        else:
            # Over a poster: wide enough that its picture and words reach past
            # the poster's own words, rather than padding out under the buttons.
            reach = whole.bottom() + 8 - (art.top() - 8)
            width = max(360, round(art.height() * 1.25), math.ceil((reach - info_h) * 16 / 9))
        media_h = round(width * 9 / 16)
        left = art.center().x() - width // 2
        # From the card's top, lifted a little: centred on the card it would
        # rise into the row's heading, and over a poster leave the poster's top
        # showing above it.
        top = art.top() - max(8, min(12, (media_h - art.height()) // 4))
        if band is not None and art.top() >= band[1]:
            top = max(top, band[1])
        # Down past the card's own words, so none of them show underneath.
        height = max(media_h + info_h, whole.bottom() + 8 - top)
        margin = 12
        left = max(margin, min(left, parent.width() - width - margin))
        top = max(margin, min(top, parent.height() - height - margin))
        if band is not None and band[0] - 4 < top < band[1]:
            # Pushed up by the window's foot into the heading: over it whole,
            # rather than through the middle of its words.
            top = max(margin, band[0] - 6)
        self._media.setFixedHeight(media_h)
        return QRect(left - SHADOW, top - SHADOW, width + 2 * SHADOW, height + 2 * SHADOW)

    # --- what it shows -----------------------------------------------------------------------

    @staticmethod
    def _playable(item) -> MediaItem | None:
        if isinstance(item, ShowItem):
            return _show_episode(item)
        return item if _started(item) else None

    def _fill_words(self) -> None:
        item = self._item
        episode = self._episode
        target = episode or (item if isinstance(item, MediaItem) else None)
        bits: list[str] = []
        genres = getattr(item, "genres", None)
        if isinstance(item, ShowItem):
            title = item.title
            if episode is not None:
                bits.extend(bit for bit in (episode.code, episode.title) if bit)
        elif isinstance(item, MediaItem) and item.is_episode:
            # The episode's name as the title, and its show and code in the line
            # under it: the plain face, whose figures sit on the line (the title
            # face's old-style ones make S01E02 read "So1Eo2").
            title = item.title or item.code
            show, genres = _show_of(item)
            bits.extend(bit for bit in (show, item.code if item.title else "") if bit)
        else:
            title = getattr(item, "title", "") or getattr(item, "display_title", "") or ""
            if isinstance(item, MediaItem) and item.year:
                bits.append(str(item.year))
        self._title.setText(title)
        if target is not None and target.resume_position > 0 and target.duration:
            bits.append(fmt_remaining(target.position, target.duration))
        elif isinstance(item, MediaItem) and item.duration:
            # Not started (a few stray seconds are not a start): how long it is.
            bits.append(fmt_duration(item.duration))
        elif isinstance(item, ShowItem) and item.subtitle:
            bits.append(item.subtitle)
        if not bits and getattr(item, "subtitle", ""):
            bits.append(item.subtitle)
        self._meta.setText("  ·  ".join(bits))
        names = cat.split(genres)[:3]
        self._genres.setText("  ·  ".join(names))
        self._genres.setVisible(bool(names))
        if episode is not None:
            self._play.setText(f"Resume · {fmt_clock(episode.resume_position)}")
            self._say("")
        else:
            self._play.setText("Play")
            self._say("Not started, so no preview" if isinstance(item, (MediaItem, ShowItem)) else "")

    def _picture(self, card) -> QPixmap | None:
        path = getattr(card.item, "wide_art", None) or getattr(card.item, "art", None)
        if not path:
            return None
        return art_pixmap(path, "", 640, 360, 0, self.devicePixelRatioF(), self._on_picture)

    def _on_picture(self, pixmap: QPixmap) -> None:
        self._still = pixmap
        self.update()

    def _emit(self, signal) -> None:
        item = self._item
        self.close_preview()
        if item is not None:
            signal.emit(item)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self._media.geometry().contains(event.position().toPoint()):
            self._emit(self.play_requested)
            return
        super().mousePressEvent(event)

    # --- the film ----------------------------------------------------------------------------

    def _ensure_mpv(self) -> bool:
        if self._mpv is not None and self._mpv.is_running:
            return True
        try:
            self._mpv = _preview_mpv()
            self._mpv.setParent(self)
            self._surface.winId()
            self._mpv.start(int(self._surface.winId()))
            self._mpv.playback_restart.connect(self._first_frame)
        except Exception as problem:            # noqa: BLE001 - a picture instead
            _log.info("hover preview: no mpv for it (%s)", problem)
            self._mpv = None
            return False
        return True

    def warm_up(self) -> None:
        """Start the preview's mpv now, idle, so the first hover need not."""
        self._ensure_mpv()

    def _start_video(self, episode: MediaItem) -> None:
        if not self._ensure_mpv():
            return
        self._playing = True
        self._position = float(episode.resume_position)
        self._mpv.set_volume(0)
        self._mpv.load(episode.path, start_at=self._position)
        self._tick.start()
        self._say(f"Previewing from {fmt_clock(self._position)}")

    def _first_frame(self) -> None:
        if not self._playing or not self.isVisible():
            return
        self._surface.setGeometry(self._media.rect())
        self._surface.show()
        self._surface.raise_()
        self._sound_wait.start()

    def _fade_sound_in(self) -> None:
        if not self._playing or not self._sound_allowed():
            return
        target = max(0.0, min(100.0, float(settings.get("volume", 80)) * 0.8))
        self._sound.stop()
        self._sound.setStartValue(0.0)
        self._sound.setEndValue(target)
        self._sound.start()
        self._say("Previewing · sound on")

    def _say(self, text: str) -> None:
        """The line beside the buttons, in yellow while the preview plays."""
        self._hint.setText(text)
        self._hint.setStyleSheet(f"color: {C.ACCENT if self._playing else C.TEXT_FAINT}; font-size: 8.5pt;")

    def _set_volume(self, value) -> None:
        if self._mpv is not None and self._playing:
            self._mpv.set_volume(float(value))

    def _update_time(self) -> None:
        self.update()

    def _stop_video(self) -> None:
        self._sound_wait.stop()
        self._sound.stop()
        self._tick.stop()
        self._surface.hide()
        if self._playing and self._mpv is not None and self._mpv.is_running:
            self._mpv.set_volume(0)
            self._mpv.stop()
        self._playing = False

    def shutdown(self) -> None:
        self._stop_video()
        if self._mpv is not None:
            self._mpv.terminate()
            self._mpv = None

    # --- painting ----------------------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self._surface.isVisible():
            self._surface.setGeometry(self._media.rect())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        card = QRectF(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)
        # A soft shadow, layered by hand: Qt's shadow effect cannot reach the
        # native video window inside.
        for step in range(SHADOW // 2, 0, -1):
            spread = step * 2.0
            shade = QColor(0, 0, 0, round(9 * (1 - step / (SHADOW / 2)) + 3))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shade)
            painter.drawRoundedRect(card.adjusted(-spread, -spread + 6, spread, spread + 10), 18 + spread, 18 + spread)
        path = QPainterPath()
        path.addRoundedRect(card, 18, 18)
        painter.fillPath(path, QColor(C.BG_ELEV))
        media = QRectF(self._media.geometry())
        painter.save()
        painter.setClipPath(path)
        if self._still is not None and not self._still.isNull():
            painter.drawPixmap(media, self._still, QRectF(self._still.rect()))
        else:
            painter.fillRect(media, QColor(C.SURFACE))
        painter.restore()
        # How far along, in yellow, along the picture's lower edge: on the
        # card rather than over the picture, where the video would cover it.
        target = self._episode
        if target is not None and target.duration:
            fraction = max(0.02, min(1.0, target.position / target.duration))
            bar = QRectF(media.left(), media.bottom(), media.width(), 4)
            painter.fillRect(bar, QColor(246, 240, 230, 40))
            painter.fillRect(QRectF(bar.left(), bar.top(), bar.width() * fraction, 4), QColor(C.ACCENT))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        edge = QColor(C.ACCENT)
        edge.setAlpha(90)
        painter.setPen(edge)
        painter.drawPath(path)
