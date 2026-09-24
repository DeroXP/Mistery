"""Auto-hiding transport controls drawn over the video.

mpv renders into a native child window, and native children always sit above
Qt's own painting inside the same top-level. So the controls live in a separate
frameless, translucent tool window that tracks the video area's geometry.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal,
)  # noqa: F401  (QRect is used by the Up Next snapshot)
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QCursor, QGuiApplication, QImage, QLinearGradient,
    QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from ..images import cover_pixmap, load_async
from ..metadata import thumbs as thumbs_module
from ..util import elide, fmt_clock
from .theme import C
from .widgets.icons import IconButton
from .widgets.volume_bar import FINE_STEP, WHEEL_STEP, VolumeBar

_HIDE_DELAY_MS = 2800
_TRACK_HEIGHT = 5.0
_TRACK_HOVER_HEIGHT = 8.0

# How far the pointer must travel before it counts as someone reaching for the
# controls. An idle optical mouse still reports a pixel or two of drift, and a
# knock to the desk a few more, none of which should light up the screen
# mid-film. Measured from where the pointer came to rest, so a slow deliberate
# move still gets there — it just has to actually go somewhere.
_WAKE_DISTANCE = 16


def _global_point(event) -> QPoint | None:
    """Where on screen an event happened, or None if it does not say."""
    getter = getattr(event, "globalPosition", None)
    return getter().toPoint() if getter is not None else None


class ThumbPreview(QWidget):
    """Floating frame preview shown while scrubbing."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._sprite = QImage()
        self._index: dict | None = None
        self._tile = QPixmap()
        self._time_text = ""
        self._chapter = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setVisible(False)

    def set_source(self, index_path: str | None) -> None:
        self._index = None
        self._sprite = QImage()
        if not index_path:
            self.setVisible(False)
            return
        data = thumbs_module.load_index(index_path)
        if not data:
            self.setVisible(False)
            return
        self._index = data
        load_async(data["sprite"], self._on_sprite)

    def _on_sprite(self, image: QImage) -> None:
        self._sprite = image
        self._resize_to_tile()

    def _resize_to_tile(self) -> None:
        if self._index is None:
            return
        width = int(self._index.get("tile_width", 208))
        height = int(self._index.get("tile_height", 117))
        self.setFixedSize(QSize(width + 8, height + 34))

    @property
    def has_frames(self) -> bool:
        return self._index is not None and not self._sprite.isNull()

    def show_at(self, position: float, time_text: str, chapter: str, anchor: QPoint) -> None:
        self._time_text = time_text
        self._chapter = chapter
        if self.has_frames:
            left, top, width, height = thumbs_module.tile_rect(self._index, position)
            self._tile = QPixmap.fromImage(self._sprite.copy(left, top, width, height))
            self.setFixedSize(QSize(width + 8, height + 34))
        else:
            self._tile = QPixmap()
            self.setFixedSize(QSize(120, 34))

        parent = self.parentWidget()
        x = anchor.x() - self.width() // 2
        if parent is not None:
            x = max(10, min(parent.width() - self.width() - 10, x))
        self.move(x, anchor.y() - self.height() - 12)
        self.setVisible(True)
        self.raise_()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(255, 255, 255, 46), 1))
        painter.setBrush(QColor(8, 10, 14, 235))
        painter.drawRoundedRect(rect, 8, 8)

        text_top = 4.0
        if not self._tile.isNull():
            painter.drawPixmap(QRect(4, 4, self._tile.width(), self._tile.height()), self._tile)
            text_top = self._tile.height() + 6

        painter.setPen(QColor(C.TEXT))
        font = painter.font()
        font.setPointSizeF(9.5)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(0, text_top, self.width(), 16),
            Qt.AlignmentFlag.AlignCenter, self._time_text,
        )
        if self._chapter:
            font.setBold(False)
            font.setPointSizeF(8.2)
            painter.setFont(font)
            painter.setPen(QColor(C.TEXT_FAINT))
            painter.drawText(
                QRectF(4, text_top + 15, self.width() - 8, 14),
                Qt.AlignmentFlag.AlignCenter,
                elide(self._chapter, 30),
            )


class SeekBar(QWidget):
    seek_requested = Signal(float)
    scrub_preview = Signal(float, QPoint)
    scrub_finished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._duration = 0.0
        self._position = 0.0
        self._chapters: list[dict] = []
        self._hover_x: float | None = None
        self._dragging = False
        self._accent = QColor(C.ACCENT)
        self.setFixedHeight(26)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_duration(self, duration: float) -> None:
        self._duration = max(0.0, float(duration or 0))
        self.update()

    def set_position(self, position: float) -> None:
        if self._dragging:
            return
        self._position = max(0.0, float(position or 0))
        self.update()

    def set_chapters(self, chapters: list[dict]) -> None:
        self._chapters = chapters or []
        self.update()

    def set_accent(self, colour: str) -> None:
        """The music player tints this with the colour of the record."""
        self._accent = QColor(colour)
        self.update()

    @property
    def is_dragging(self) -> bool:
        return self._dragging

    def _track_rect(self) -> QRectF:
        height = _TRACK_HOVER_HEIGHT if (self.underMouse() or self._dragging) else _TRACK_HEIGHT
        return QRectF(0, (self.height() - height) / 2, self.width(), height)

    def _time_at(self, x: float) -> float:
        if self._duration <= 0 or self.width() <= 0:
            return 0.0
        return max(0.0, min(self._duration, (x / self.width()) * self._duration))

    def _chapter_at(self, position: float) -> str:
        name = ""
        for chapter in self._chapters:
            start = chapter.get("time")
            if start is None or start > position:
                continue
            name = chapter.get("title") or ""
        return name

    # --- interaction --------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._duration <= 0:
            return
        self._dragging = True
        self._update_scrub(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        self._hover_x = event.position().x()
        if self._dragging:
            self._update_scrub(self._hover_x)
        elif self._duration > 0:
            self._emit_preview(self._hover_x)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self.seek_requested.emit(self._time_at(event.position().x()))
        self.scrub_finished.emit()
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover_x = None
        if not self._dragging:
            self.scrub_finished.emit()
        self.update()
        super().leaveEvent(event)

    def _update_scrub(self, x: float) -> None:
        self._position = self._time_at(x)
        self._emit_preview(x)
        self.update()

    def _emit_preview(self, x: float) -> None:
        anchor = self.mapTo(self.window(), QPoint(int(x), int(self._track_rect().top())))
        self.scrub_preview.emit(self._time_at(x), anchor)

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = self._track_rect()
        radius = track.height() / 2

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 48))
        painter.drawRoundedRect(track, radius, radius)

        if self._duration > 0:
            fraction = max(0.0, min(1.0, self._position / self._duration))
            played = QRectF(track.left(), track.top(), track.width() * fraction, track.height())
            painter.setBrush(self._accent)
            painter.drawRoundedRect(played, radius, radius)

            painter.setBrush(QColor(255, 255, 255, 120))
            for chapter in self._chapters:
                start = chapter.get("time")
                if not start or start <= 0 or start >= self._duration:
                    continue
                x = track.left() + track.width() * (start / self._duration)
                painter.drawRect(QRectF(x - 0.9, track.top() - 1, 1.8, track.height() + 2))

            if self.underMouse() or self._dragging:
                knob = 7.0
                painter.setBrush(self._accent)
                painter.setPen(QPen(QColor(0, 0, 0, 120), 1))
                painter.drawEllipse(
                    QRectF(played.right() - knob, track.center().y() - knob, knob * 2, knob * 2)
                )


class SkipPill(QPushButton):
    """The floating 'Skip intro' button, visible even when the chrome is hidden."""

    def __init__(self, parent=None) -> None:
        super().__init__("Skip intro", parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background: rgba(10, 12, 16, 0.88);
                border: 1px solid rgba(255, 255, 255, 0.45);
                border-radius: 6px;
                color: {C.TEXT};
                font-size: 10.5pt;
                font-weight: 600;
                padding: 10px 22px;
            }}
            QPushButton:hover {{
                background: {C.TEXT};
                color: {C.SCRIM};
                border-color: {C.TEXT};
            }}
        """)
        self.setVisible(False)


class NextUpCard(QWidget):
    """End-of-episode card: what's next, a countdown, and a way out."""

    play_now = Signal()
    cancelled = Signal()

    ART_W, ART_H = 232, 131

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._remaining = 0
        self._held = False
        self.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        # The still doubles as the thing that grows into the picture when the
        # next episode starts, so the handover has something to come *from*.
        self._art = QLabel()
        self._art.setFixedSize(self.ART_W, self.ART_H)
        self._art.setScaledContents(False)
        self._art.setVisible(False)
        layout.addWidget(self._art)
        layout.addSpacing(4)

        self._eyebrow = QLabel("UP NEXT")
        self._eyebrow.setStyleSheet(
            f"color: {C.ACCENT}; font-size: 8.5pt; font-weight: 700; letter-spacing: 1.4px;"
        )
        layout.addWidget(self._eyebrow)

        self._title = QLabel()
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 12pt; font-weight: 600;")
        layout.addWidget(self._title)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        layout.addSpacing(8)
        layout.addLayout(buttons)

        self._play = QPushButton("Play now")
        self._play.setObjectName("Primary")
        self._play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play.clicked.connect(self._on_play)
        buttons.addWidget(self._play)

        self._cancel = QPushButton("Cancel")
        self._cancel.setObjectName("Ghost")
        self._cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel.clicked.connect(self.dismiss)
        buttons.addWidget(self._cancel)
        buttons.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    def present(self, title: str, countdown: int | None, art: str = "") -> None:
        """Show the card. countdown=None means wait for a click."""
        self._title.setText(elide(title, 46))
        self._art.clear()
        self._art.setVisible(False)
        if art:
            load_async(art, self._on_art)
        self._timer.stop()
        self._held = False
        if countdown is not None and countdown > 0:
            self._remaining = int(countdown)
            self._play.setText(f"Play now ({self._remaining})")
            self._timer.start()
        else:
            self._play.setText("Play now")
        self.setVisible(True)
        self.raise_()

    def _on_art(self, image: QImage) -> None:
        if image.isNull():
            return
        self._art.setPixmap(
            cover_pixmap(image, self.ART_W, self.ART_H, 5, self.devicePixelRatioF())
        )
        self._art.setVisible(True)
        self.adjustSize()

    def snapshot(self) -> tuple[QPixmap, QRect]:
        """The still and where it is on screen, for the handover to playback."""
        pixmap = self._art.pixmap()
        if not self.isVisible() or pixmap is None or pixmap.isNull():
            return QPixmap(), QRect()
        return pixmap, QRect(self._art.mapToGlobal(QPoint(0, 0)), self._art.size())

    def dismiss(self) -> None:
        self._timer.stop()
        self._held = False
        self.setVisible(False)
        self.cancelled.emit()

    def hide_quietly(self) -> None:
        """Hide without signalling — used when playback moves on by itself."""
        self._timer.stop()
        self._held = False
        self.setVisible(False)

    def hold(self, held: bool) -> None:
        """Stop the countdown while playback is paused, and pick it up after.

        Pausing during the ending theme means "hang on"; without this the next
        episode started eight seconds later anyway, with nobody watching. While
        held, the card still offers Play now — it just stops deciding for you.
        """
        if held:
            if self._timer.isActive():
                self._timer.stop()
                self._held = True
                self._play.setText("Play now")
        elif self._held:
            self._held = False
            if self.isVisible() and self._remaining > 0:
                self._play.setText(f"Play now ({self._remaining})")
                self._timer.start()

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._on_play()
        else:
            self._play.setText(f"Play now ({self._remaining})")

    def _on_play(self) -> None:
        self._timer.stop()
        self._held = False
        self.setVisible(False)
        self.play_now.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.setBrush(QColor(10, 12, 16, 235))
        painter.drawRoundedRect(rect, 12, 12)


# The overlay is a window of its own, outside the main window's stylesheet, so
# what it draws carries its own look (as SkipPill does).
_CARD_BUTTONS = f"""
    QPushButton#Primary {{
        background: {C.PLAY_BG}; color: {C.PLAY_FG}; border: none; border-radius: 5px;
        font-size: 10pt; font-weight: 700; padding: 8px 18px;
    }}
    QPushButton#Primary:hover {{ background: {C.PLAY_BG_HOVER}; }}
    QPushButton#Ghost {{
        background: rgba(109, 109, 110, 0.35); color: {C.TEXT}; border: none;
        border-radius: 5px; font-size: 10pt; font-weight: 600; padding: 8px 18px;
    }}
    QPushButton#Ghost:hover {{ background: rgba(109, 109, 110, 0.55); }}
"""


class PartyPill(QPushButton):
    """The top bar's movie night pill.

    On a film or episode of your own that is playing: "Watch together", which
    asks for a movie night to be started with it. While one runs: "Movie night
    · 3", red dot and all, and a click lists who is watching, with the invite
    to copy (the host) and the way out.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._live = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(f"""
            QPushButton {{
                background: rgba(10, 12, 16, 0.72);
                border: 1px solid rgba(255, 255, 255, 0.28);
                border-radius: 15px;
                color: {C.TEXT};
                font-size: 9.5pt;
                font-weight: 600;
                padding: 6px 14px 6px 27px;
            }}
            QPushButton:hover {{
                background: rgba(44, 46, 52, 0.92);
                border-color: rgba(255, 255, 255, 0.5);
            }}
        """)
        self.setVisible(False)

    @property
    def live(self) -> bool:
        return self._live

    def set_live(self, live: bool, text: str, tooltip: str) -> None:
        self._live = bool(live)
        self.setText(text)
        self.setToolTip(tooltip)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C.ACCENT) if self._live else QColor(C.TEXT_DIM))
        radius = 3.6
        painter.drawEllipse(QRectF(14 - radius, self.height() / 2 - radius, radius * 2, radius * 2))


class PartyCard(QWidget):
    """A movie night's question over the picture: the host's Next episode
    together, a guest's offer of a lighter stream. No countdown: in a movie
    night nothing moves everyone on by itself."""

    accepted = Signal(str)      # the card's kind: "next", "quality:1080p", …

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._kind = ""
        self.setVisible(False)
        self.setStyleSheet(_CARD_BUTTONS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)
        self._eyebrow = QLabel()
        self._eyebrow.setStyleSheet(
            f"color: {C.ACCENT}; font-size: 8.5pt; font-weight: 700; letter-spacing: 1.4px;"
        )
        layout.addWidget(self._eyebrow)
        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setMaximumWidth(300)
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 12pt; font-weight: 600;")
        layout.addWidget(self._title)
        layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        self._accept = QPushButton()
        self._accept.setObjectName("Primary")
        self._accept.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accept.clicked.connect(self._on_accept)
        buttons.addWidget(self._accept)
        self._dismiss = QPushButton()
        self._dismiss.setObjectName("Ghost")
        self._dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dismiss.clicked.connect(self.dismiss)
        buttons.addWidget(self._dismiss)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    @property
    def kind(self) -> str:
        return self._kind if self.isVisible() else ""

    def present(self, kind: str, eyebrow: str, title: str, accept: str, dismiss: str) -> None:
        self._kind = kind
        self._eyebrow.setText(eyebrow)
        self._title.setText(elide(title, 90))
        self._accept.setText(accept)
        self._dismiss.setText(dismiss)
        self.setVisible(True)
        self.adjustSize()
        self.raise_()

    def dismiss(self) -> None:
        self.setVisible(False)

    def _on_accept(self) -> None:
        kind = self._kind
        self.setVisible(False)
        self.accepted.emit(kind)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.setBrush(QColor(10, 12, 16, 235))
        painter.drawRoundedRect(rect, 12, 12)


class PlayerOverlay(QWidget):
    """Transport chrome. Lives in its own window above the mpv surface."""

    play_pause = Signal()
    seek_relative = Signal(float)
    seek_absolute = Signal(float)
    volume_changed = Signal(int)
    mute_toggled = Signal()
    audio_track_selected = Signal(object)
    sub_track_selected = Signal(object)
    speed_selected = Signal(float)
    quality_selected = Signal(str)
    chapter_selected = Signal(int)
    boost_toggled = Signal(bool)
    fullscreen_toggled = Signal()
    close_requested = Signal()
    next_requested = Signal()       # the >| button or the N key: skip ahead
    next_from_card = Signal()       # the Up Next card: this episode is over
    previous_requested = Signal()
    autoplay_toggled = Signal(bool)
    skip_intro = Signal()
    next_cancelled = Signal()
    # "skip_now" | "auto_toggle" | "intro_start" | "intro_end" | "intro_clear"
    # | "credits_here" | "credits_clear"
    tv_action = Signal(str)
    # Movie night: "Watch together" (start one with what is playing), the host's
    # panel, End or Leave from the pill's menu, a guest's own stream quality
    # (original | 1080p | 720p), and a card's button ("next", "quality:<q>").
    party_requested = Signal()
    party_panel_requested = Signal()
    party_leave_requested = Signal()
    stream_quality_selected = Signal(str)
    party_card_accepted = Signal(str)

    def __init__(self, parent=None, owner: QWidget | None = None) -> None:
        super().__init__(parent)
        # Not a Qt parent (this has to stay a separate top-level window) — just
        # the widget whose window owns the keyboard.
        self._owner = owner
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
            # Clicking the video must not pull activation off the main window,
            # or the keyboard shortcuts (routed from there) go dead.
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        self._chrome_visible = True
        self._wake_anchor: QPoint | None = None
        self._duration = 0.0
        self._audio_tracks: list[dict] = []
        self._sub_tracks: list[dict] = []
        self._chapters: list[dict] = []
        self._speed = 1.0
        self._quality = "balanced"
        self._source_size = ""
        # The movie night this player is in, as PlayerView describes it
        # (set_party), or None.
        self._party: dict | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- top bar ---
        self._top = QWidget()
        self._top.setMouseTracking(True)
        top_layout = QHBoxLayout(self._top)
        top_layout.setContentsMargins(18, 14, 18, 14)
        top_layout.setSpacing(12)

        self._back = IconButton("back", size=40, icon_size=22, tooltip="Back to library  (Esc)")
        self._back.clicked.connect(self.close_requested.emit)
        top_layout.addWidget(self._back)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        self._title = QLabel()
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 13pt; font-weight: 600;")
        titles.addWidget(self._title)
        self._subtitle = QLabel()
        self._subtitle.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9.5pt;")
        titles.addWidget(self._subtitle)
        top_layout.addLayout(titles)
        top_layout.addStretch(1)
        self._party_pill = PartyPill()
        self._party_pill.clicked.connect(self._on_party_pill)
        top_layout.addWidget(self._party_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self._top)

        root.addStretch(1)

        # --- bottom bar ---
        self._bottom = QWidget()
        self._bottom.setMouseTracking(True)
        bottom = QVBoxLayout(self._bottom)
        bottom.setContentsMargins(22, 10, 22, 16)
        bottom.setSpacing(6)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(12)
        self._elapsed = QLabel("0:00")
        self._elapsed.setFixedWidth(66)
        self._elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._elapsed.setStyleSheet(f"color: {C.TEXT}; font-size: 9.5pt;")
        seek_row.addWidget(self._elapsed)

        self.seek_bar = SeekBar()
        self.seek_bar.seek_requested.connect(self.seek_absolute.emit)
        self.seek_bar.scrub_preview.connect(self._on_scrub_preview)
        self.seek_bar.scrub_finished.connect(lambda: self._preview.setVisible(False))
        seek_row.addWidget(self.seek_bar, 1)

        self._total = QLabel("0:00")
        self._total.setFixedWidth(66)
        self._total.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9.5pt;")
        seek_row.addWidget(self._total)
        bottom.addLayout(seek_row)

        controls = QHBoxLayout()
        controls.setSpacing(4)

        self._play = IconButton("play", size=44, icon_size=24, tooltip="Play/Pause  (Space)")
        self._play.clicked.connect(self.play_pause.emit)
        controls.addWidget(self._play)

        self._back10 = IconButton("back10", size=40, icon_size=22, tooltip="Back 10s  (←)")
        self._back10.clicked.connect(lambda: self.seek_relative.emit(-10))
        controls.addWidget(self._back10)

        self._fwd10 = IconButton("fwd10", size=40, icon_size=22, tooltip="Forward 10s  (→)")
        self._fwd10.clicked.connect(lambda: self.seek_relative.emit(10))
        controls.addWidget(self._fwd10)

        # Episode navigation, not chapter navigation: on a file with no chapters
        # the chapter buttons only ever jumped to the start or the end, which is
        # not what a |< >| pair means to anyone. Chapters live in their own menu.
        self._prev_episode = IconButton("prev", size=38, icon_size=19,
                                        tooltip="Previous episode  (P)")
        self._prev_episode.clicked.connect(self.previous_requested.emit)
        controls.addWidget(self._prev_episode)

        self._next_episode = IconButton("next", size=38, icon_size=19,
                                        tooltip="Next episode  (N)")
        self._next_episode.clicked.connect(self.next_requested.emit)
        controls.addWidget(self._next_episode)

        controls.addSpacing(10)
        self._volume_button = IconButton("volume", size=38, icon_size=20, tooltip="Mute  (M)")
        self._volume_button.clicked.connect(self.mute_toggled.emit)
        controls.addWidget(self._volume_button)

        # The same ceiling as mpv's --volume-max and the arrow keys. At 130 the
        # bar clipped a key-set 150, and the next wheel step dropped it to 130.
        self._volume = VolumeBar(0, 150)
        self._volume.setFixedWidth(112)
        self._volume.value_changed.connect(self.volume_changed.emit)
        controls.addWidget(self._volume)

        controls.addStretch(1)

        self._autoplay = IconButton(
            "autoplay", size=38, icon_size=20, checkable=True,
            tooltip="Autoplay — start the next episode when this one ends",
        )
        self._autoplay.toggled.connect(self.autoplay_toggled.emit)
        self._autoplay.setVisible(False)
        controls.addWidget(self._autoplay)

        self._tv_button = IconButton("tv", size=38, icon_size=20,
                                     tooltip="Intro and credits for this show")
        self._tv_button.clicked.connect(self._show_tv_menu)
        self._tv_button.setVisible(False)
        controls.addWidget(self._tv_button)

        self._boost = IconButton(
            "boost", size=38, icon_size=21, checkable=True,
            tooltip="Dialogue boost — evens out quiet speech and loud action  (B)",
        )
        self._boost.toggled.connect(self.boost_toggled.emit)
        controls.addWidget(self._boost)

        self._audio_button = IconButton("audio", size=38, icon_size=20, tooltip="Audio track")
        self._audio_button.clicked.connect(self._show_audio_menu)
        controls.addWidget(self._audio_button)

        self._sub_button = IconButton("cc", size=38, icon_size=20, tooltip="Subtitles  (S)")
        self._sub_button.clicked.connect(self._show_sub_menu)
        controls.addWidget(self._sub_button)

        self._chapters_button = IconButton("chapters", size=38, icon_size=20, tooltip="Chapters")
        self._chapters_button.clicked.connect(self._show_chapter_menu)
        controls.addWidget(self._chapters_button)

        self._speed_button = IconButton("speed", size=38, icon_size=20, tooltip="Playback speed")
        self._speed_button.clicked.connect(self._show_speed_menu)
        controls.addWidget(self._speed_button)

        self._quality_button = IconButton("quality", size=38, icon_size=20,
                                          tooltip="Video quality")
        self._quality_button.clicked.connect(self._show_quality_menu)
        controls.addWidget(self._quality_button)

        self._fullscreen = IconButton("fullscreen", size=38, icon_size=20,
                                      tooltip="Fullscreen  (F)")
        self._fullscreen.clicked.connect(self.fullscreen_toggled.emit)
        controls.addWidget(self._fullscreen)

        bottom.addLayout(controls)
        root.addWidget(self._bottom)

        self._preview = ThumbPreview(self)

        self.skip_pill = SkipPill(self)
        self.skip_pill.clicked.connect(self.skip_intro.emit)

        self.next_card = NextUpCard(self)
        self.next_card.play_now.connect(self.next_from_card.emit)
        self.next_card.cancelled.connect(self.next_cancelled.emit)

        # "Opening…" and playback errors. They used to be a label on the video
        # surface, which mpv's own child window covers edge to edge — so a file
        # that would not play was a black screen with no word of why. This
        # window is the only thing that sits above mpv.
        self._message = QLabel(self)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._message.setStyleSheet(f"""
            color: {C.TEXT}; font-size: 12pt;
            background: rgba(10, 12, 16, 0.82); border-radius: 8px; padding: 12px 22px;
        """)
        self._message.setVisible(False)
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(self.clear_message)

        # A movie night's news: "Sam paused", "Alex joined", "Waiting for Sam…".
        # A line near the top rather than the middle of the picture, and it stays
        # when the controls hide: what the room did is worth seeing mid-film.
        self._notice = QLabel(self)
        self._notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._notice.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._notice.setStyleSheet(f"""
            color: {C.TEXT}; font-size: 10.5pt; font-weight: 600;
            background: rgba(10, 12, 16, 0.84); border: 1px solid rgba(255, 255, 255, 0.18);
            border-radius: 15px; padding: 7px 18px;
        """)
        self._notice.setVisible(False)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self.clear_notice)

        self.party_card = PartyCard(self)
        self.party_card.accepted.connect(self.party_card_accepted.emit)

        self._tv_state = {"has_intro": False, "has_credits": False, "auto": True}

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self.hide_chrome)

        self._watch_children()

    # --- chrome visibility --------------------------------------------------

    def wake_on_movement(self, position: QPoint | None = None) -> None:
        """Wake, but only for movement someone meant to make.

        Deliberate interaction — a click, a key, a button — goes through wake()
        directly and is never filtered. This is only for the pointer drifting.
        """
        if position is None:
            position = QCursor.pos()
        anchor = self._wake_anchor
        if anchor is not None:
            travelled = (position - anchor).manhattanLength()
            if travelled < _WAKE_DISTANCE:
                return
        self._wake_anchor = position
        self.wake()

    def wake(self) -> None:
        """Show the controls and restart the auto-hide countdown."""
        if not self._chrome_visible:
            self._chrome_visible = True
            self._top.setVisible(True)
            self._bottom.setVisible(True)
            self._place_floaters()
            self.update()
        self.unsetCursor()
        self._hide_timer.start()

    def hide_chrome(self) -> None:
        if self._menu_open():
            self._hide_timer.start()
            return
        if self.seek_bar.is_dragging or self.seek_bar.underMouse():
            self._hide_timer.start()
            return
        # A volume drag can leave the bottom bar and keep going (the mouse is
        # grabbed); hiding the chrome under it would take the bar away mid-drag.
        if self._volume.is_dragging:
            self._hide_timer.start()
            return
        # Never pull the controls out from under a resting pointer.
        if self._top.underMouse() or self._bottom.underMouse():
            self._hide_timer.start()
            return
        # Measure the next movement from wherever the pointer has settled.
        self._wake_anchor = QCursor.pos()
        self._chrome_visible = False
        self._top.setVisible(False)
        self._bottom.setVisible(False)
        self._preview.setVisible(False)
        self._place_floaters()
        # The pill and Up Next card stay: they are exactly what you want
        # visible while leaning back with the controls hidden.
        if self.next_card.isVisible() or self.skip_pill.isVisible() or self.party_card.isVisible():
            self.unsetCursor()
        else:
            self.setCursor(Qt.CursorShape.BlankCursor)
        self.update()

    def _menu_open(self) -> bool:
        from PySide6.QtWidgets import QApplication
        return isinstance(QApplication.activePopupWidget(), QMenu)

    def freeze_chrome(self, frozen: bool) -> None:
        """Keep the controls up (used while a menu is open)."""
        if frozen:
            self._hide_timer.stop()
        else:
            self._hide_timer.start()

    # --- state from the player ---------------------------------------------

    def set_title(self, title: str, subtitle: str = "") -> None:
        self._title.setText(elide(title, 80))
        self._subtitle.setText(subtitle)
        self._subtitle.setVisible(bool(subtitle))

    def set_paused(self, paused: bool) -> None:
        self._play.set_icon_name("play" if paused else "pause")

    def set_position(self, position: float) -> None:
        self.seek_bar.set_position(position)
        self._elapsed.setText(fmt_clock(position))

    def set_duration(self, duration: float) -> None:
        self._duration = duration or 0.0
        self.seek_bar.set_duration(self._duration)
        self._total.setText(fmt_clock(self._duration))

    def set_volume(self, volume: float, muted: bool) -> None:
        self._volume.set_value(int(volume or 0))
        # Muted is a state the bar shows (dimmed fill, crossed handle), not a
        # level of zero: the fill still says what unmuting comes back to.
        self._volume.set_muted(bool(muted))
        if muted or not volume:
            name = "mute"
        elif volume < 55:
            name = "volume_low"
        else:
            name = "volume"
        self._volume_button.set_icon_name(name)

    def set_tracks(self, audio: list[dict], subs: list[dict]) -> None:
        self._audio_tracks = audio
        self._sub_tracks = subs
        self._audio_button.setEnabled(len(audio) > 0)
        self._sub_button.setEnabled(True)

    def set_chapters(self, chapters: list[dict]) -> None:
        self._chapters = chapters or []
        self.seek_bar.set_chapters(self._chapters)
        self._chapters_button.setEnabled(len(self._chapters) > 1)

    def set_boost(self, enabled: bool) -> None:
        self._boost.blockSignals(True)
        self._boost.setChecked(enabled)
        self._boost.blockSignals(False)

    def set_speed(self, speed: float) -> None:
        self._speed = speed or 1.0

    def set_fullscreen(self, fullscreen: bool) -> None:
        self._fullscreen.set_icon_name("exit_fullscreen" if fullscreen else "fullscreen")

    def set_subs_active(self, active: bool) -> None:
        self._sub_button.setChecked(active)

    def set_episode_nav(self, is_episode: bool, has_prev: bool, has_next: bool) -> None:
        """Show the previous/next buttons and the autoplay tick when there is
        somewhere to go: the next episode of a show, or the next item of a
        playlist. set_tv_state below is still about episodes only — a film has
        no fingerprinted intro or credits behind that button."""
        for button, enabled in ((self._prev_episode, has_prev),
                                (self._next_episode, has_next)):
            button.setVisible(is_episode)
            button.setEnabled(enabled)
        # Autoplay stays out of a movie night: what comes next is the host's to
        # put on, for everyone, never a countdown's.
        self._autoplay.setVisible(is_episode and self._party is None)

    def set_autoplay(self, enabled: bool) -> None:
        self._autoplay.blockSignals(True)
        self._autoplay.setChecked(bool(enabled))
        self._autoplay.blockSignals(False)

    def set_tv_state(self, is_episode: bool, has_intro: bool,
                     has_credits: bool, auto_skip: bool) -> None:
        self._tv_button.setVisible(is_episode)
        self._tv_state = {
            "has_intro": has_intro, "has_credits": has_credits, "auto": auto_skip,
        }

    def show_skip_pill(self, visible: bool) -> None:
        if visible == self.skip_pill.isVisible():
            return
        self.skip_pill.setVisible(visible)
        if visible:
            self._place_floaters()
            self.skip_pill.raise_()

    def present_next_card(self, title: str, countdown: int | None,
                          art: str = "") -> None:
        self.next_card.present(title, countdown, art)
        self._place_floaters()

    def next_card_snapshot(self) -> tuple[QPixmap, QRect]:
        return self.next_card.snapshot()

    def show_message(self, text: str, timeout_ms: int = 0) -> None:
        """A line in the middle of the picture; timeout_ms=0 keeps it up."""
        self._message.setText(text)
        self._message.setVisible(bool(text))
        self._place_floaters()
        self._message.raise_()
        if text and timeout_ms > 0:
            self._message_timer.start(timeout_ms)
        else:
            self._message_timer.stop()

    def clear_message(self) -> None:
        self._message_timer.stop()
        self._message.setVisible(False)
        self._message.clear()

    # --- movie night ---------------------------------------------------------

    def set_party(self, info: dict | None, can_start: bool = False, over: bool = False) -> None:
        """The movie night this player is in, or None.

        info: {"role": "host" | "guest", "people": [{"id", "name", "host",
        "buffering"}], "me": this person's id, "code", "link" (the host's
        invite), "quality" (a guest's stream), "transcode" (the host can make
        1080p and 720p), "panel" (something shows the host's panel)}.
        can_start: outside one, whether "Watch together" is offered.
        over: the title on screen was a movie night's, and that is over: still
        nothing to choose a speed for.
        """
        self._party = info
        pill = self._party_pill
        if info is None:
            pill.set_live(False, "Watch together",
                          "Start a movie night with this: friends with Mistery watch it with you, in sync")
            pill.setVisible(can_start)
        else:
            count = len(info.get("people") or [])
            pill.set_live(True, f"Movie night · {count}" if count else "Movie night",
                          "Who is watching, and the invite" if info.get("role") == "host"
                          else "Who is watching")
            pill.setVisible(True)
        # The speed is the room's: the follower nudges it to keep this player in
        # step, and a choice here would only be undone.
        self._speed_button.setVisible(info is None and not over)
        if info is not None:
            self._autoplay.setVisible(False)
            together = info.get("role") == "host"
            self._next_episode.setToolTip("Next episode together  (N)" if together else "Next episode  (N)")
            self._prev_episode.setToolTip("Previous episode together  (P)" if together
                                          else "Previous episode  (P)")
        else:
            self._next_episode.setToolTip("Next episode  (N)")
            self._prev_episode.setToolTip("Previous episode  (P)")

    @property
    def party_info(self) -> dict | None:
        return self._party

    def show_notice(self, text: str, timeout_ms: int = 3500) -> None:
        """A movie night line near the top of the picture; timeout_ms=0 keeps it up."""
        self._notice.setText(text)
        self._notice.setVisible(bool(text))
        self._place_floaters()
        self._notice.raise_()
        if text and timeout_ms > 0:
            self._notice_timer.start(timeout_ms)
        else:
            self._notice_timer.stop()

    def clear_notice(self) -> None:
        self._notice_timer.stop()
        self._notice.setVisible(False)
        self._notice.clear()

    @property
    def notice_text(self) -> str:
        return self._notice.text() if self._notice.isVisible() else ""

    def present_party_card(self, kind: str, eyebrow: str, title: str, accept: str,
                           dismiss: str) -> None:
        self.party_card.present(kind, eyebrow, title, accept, dismiss)
        self._place_floaters()

    def hide_party_card(self) -> None:
        self.party_card.dismiss()

    def _on_party_pill(self) -> None:
        if self._party is None:
            self.party_requested.emit()
            return
        self._popup(self.party_menu(), self._party_pill, below=True)

    def party_menu(self) -> QMenu:
        """Who is watching, the host's invite to copy, and the way out."""
        info = self._party or {}
        menu = QMenu(self)
        header = QAction("Watching together", menu)
        header.setEnabled(False)
        menu.addAction(header)
        for person in info.get("people") or []:
            notes = []
            if person.get("id") == info.get("me"):
                notes.append("you")
            elif person.get("host"):
                notes.append("host")
            if person.get("buffering"):
                notes.append("loading…")
            name = elide(str(person.get("name") or "Friend"), 32)
            line = QAction(f"{name}   ·   {', '.join(notes)}" if notes else name, menu)
            line.setEnabled(False)
            menu.addAction(line)
        menu.addSeparator()
        if info.get("role") == "host":
            if info.get("code"):
                menu.addAction("Copy invite code", lambda: self._copy(info["code"], "Invite code copied"))
            if info.get("link"):
                menu.addAction("Copy invite link", lambda: self._copy(info["link"], "Invite link copied"))
            if info.get("panel"):
                menu.addAction("Movie night panel…", self.party_panel_requested.emit)
            menu.addSeparator()
            menu.addAction("End movie night", self.party_leave_requested.emit)
        else:
            # A guest has a code to pass on only in a movie night on a friend's
            # PC, which the one who started it hands to that friend's friends.
            if info.get("code"):
                menu.addAction("Copy invite code", lambda: self._copy(info["code"], "Invite code copied"))
            if info.get("link"):
                menu.addAction("Copy invite link", lambda: self._copy(info["link"], "Invite link copied"))
            if info.get("code") or info.get("link"):
                menu.addSeparator()
            menu.addAction("Leave movie night", self.party_leave_requested.emit)
        return menu

    def _copy(self, text: str, said: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.show_notice(said)

    def _place_floaters(self) -> None:
        """Bottom-right corner, clear of the control bar when it's visible."""
        margin = 26
        base = self.height() - margin
        if self._chrome_visible:
            base = min(base, self._bottom.y() - 12)
        pill = self.skip_pill
        pill.adjustSize()
        pill.move(self.width() - pill.width() - margin, base - pill.height())
        card = self.next_card
        card.adjustSize()
        card.move(self.width() - card.width() - margin, base - card.height())
        party_card = self.party_card
        party_card.adjustSize()
        party_card.move(self.width() - party_card.width() - margin, base - party_card.height())
        notice = self._notice
        notice.adjustSize()
        top = self._top.y() + self._top.height() + 6 if self._chrome_visible else 22
        notice.move((self.width() - notice.width()) // 2, top)
        message = self._message
        widest = max(200, min(640, self.width() - 80))
        # Wrap only a line too long for one row: a word-wrapping QLabel sizes
        # itself narrow, and put even "This file could not be played." on two.
        message.setWordWrap(False)
        if message.sizeHint().width() > widest:
            message.setWordWrap(True)
            message.resize(widest, message.heightForWidth(widest))
        else:
            message.resize(message.sizeHint())
        message.move((self.width() - message.width()) // 2,
                     (self.height() - message.height()) // 2)
        # Never under a card. A movie night's "Join again" comes up beside the
        # reason it answers, and in a 960 x 540 player the two met: the card,
        # 145 px tall above the control bar, covered the end of the sentence
        # (at 800 x 450 too; at 1280 x 720 they clear each other). The line
        # moves up just clear of any card it would meet, no higher than where
        # the notice goes (or below the notice, when there is one).
        y = message.y()
        for floater in (self.party_card, self.next_card, self.skip_pill):
            if floater.isVisibleTo(self) and floater.geometry().intersects(message.geometry()):
                y = min(y, floater.y() - message.height() - 12)
        if y != message.y():
            ceiling = notice.y() + notice.height() + 6 if notice.isVisibleTo(self) else top
            message.move(message.x(), max(ceiling, y))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_floaters()

    def _show_tv_menu(self) -> None:
        menu = QMenu(self)
        state = self._tv_state

        if state["has_intro"]:
            menu.addAction("Skip intro now  (I)",
                           lambda: self.tv_action.emit("skip_now"))
            auto = QAction("Skip intros automatically", menu)
            auto.setCheckable(True)
            auto.setChecked(state["auto"])
            auto.triggered.connect(lambda: self.tv_action.emit("auto_toggle"))
            menu.addAction(auto)
            menu.addSeparator()

        menu.addAction("Intro starts at this moment",
                       lambda: self.tv_action.emit("intro_start"))
        menu.addAction("Intro ends at this moment",
                       lambda: self.tv_action.emit("intro_end"))
        if state["has_intro"]:
            menu.addAction("Forget this show's intro",
                           lambda: self.tv_action.emit("intro_clear"))
        menu.addSeparator()
        menu.addAction("Credits start at this moment",
                       lambda: self.tv_action.emit("credits_here"))
        if state["has_credits"]:
            menu.addAction("Forget this show's credits marker",
                           lambda: self.tv_action.emit("credits_clear"))

        self._popup(menu, self._tv_button)

    def set_thumbnails(self, index_path: str | None) -> None:
        self._preview.set_source(index_path)

    # --- menus --------------------------------------------------------------

    def _popup(self, menu: QMenu, anchor: QWidget, below: bool = False) -> None:
        self.freeze_chrome(True)
        menu.aboutToHide.connect(lambda: self.freeze_chrome(False))
        # Every menu is built fresh when its button is clicked, and parented here
        # so it themes and stacks with the overlay — which lives all session, so
        # without this each one opened was kept (150 opens, 150 menus). Later
        # rather than now: the chosen action fires after the menu starts hiding.
        menu.aboutToHide.connect(menu.deleteLater)
        point = anchor.mapToGlobal(QPoint(0, 0))
        if below:
            # The top bar's menus drop down, right edges lined up.
            menu.popup(QPoint(point.x() + anchor.width() - menu.sizeHint().width(),
                              point.y() + anchor.height() + 8))
            return
        menu.popup(QPoint(point.x() - menu.sizeHint().width() // 2 + anchor.width() // 2,
                          point.y() - menu.sizeHint().height() - 8))

    def _track_label(self, track: dict, fallback: str) -> str:
        bits = []
        if track.get("title"):
            bits.append(str(track["title"]))
        if track.get("lang"):
            bits.append(str(track["lang"]).upper())
        channels = track.get("demux-channel-count")
        if channels:
            bits.append({1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, f"{channels}ch"))
        if track.get("codec"):
            bits.append(str(track["codec"]).upper())
        return " · ".join(dict.fromkeys(bits)) or fallback

    def _show_audio_menu(self) -> None:
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)
        for track in self._audio_tracks:
            action = QAction(self._track_label(track, f"Track {track.get('id')}"), menu)
            action.setCheckable(True)
            action.setChecked(bool(track.get("selected")))
            action.triggered.connect(
                lambda _checked, tid=track.get("id"): self.audio_track_selected.emit(tid)
            )
            group.addAction(action)
            menu.addAction(action)
        if not self._audio_tracks:
            empty = QAction("No audio tracks", menu)
            empty.setEnabled(False)
            menu.addAction(empty)
        self._popup(menu, self._audio_button)

    def _show_sub_menu(self) -> None:
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)

        none_selected = not any(t.get("selected") for t in self._sub_tracks)
        off = QAction("Off", menu)
        off.setCheckable(True)
        off.setChecked(none_selected)
        off.triggered.connect(lambda: self.sub_track_selected.emit("no"))
        group.addAction(off)
        menu.addAction(off)

        if self._sub_tracks:
            menu.addSeparator()
        for track in self._sub_tracks:
            action = QAction(self._track_label(track, f"Track {track.get('id')}"), menu)
            action.setCheckable(True)
            action.setChecked(bool(track.get("selected")))
            action.triggered.connect(
                lambda _checked, tid=track.get("id"): self.sub_track_selected.emit(tid)
            )
            group.addAction(action)
            menu.addAction(action)
        self._popup(menu, self._sub_button)

    def _show_chapter_menu(self) -> None:
        menu = QMenu(self)
        for index, chapter in enumerate(self._chapters):
            start = float(chapter.get("time") or 0)
            title = chapter.get("title") or f"Chapter {index + 1}"
            action = QAction(f"{fmt_clock(start)}   {elide(title, 44)}", menu)
            action.triggered.connect(lambda _checked, i=index: self.chapter_selected.emit(i))
            menu.addAction(action)
        if not self._chapters:
            empty = QAction("No chapters", menu)
            empty.setEnabled(False)
            menu.addAction(empty)
        self._popup(menu, self._chapters_button)

    def set_quality(self, preset: str, source_size: str = "") -> None:
        self._quality = preset
        self._source_size = source_size

    def _show_quality_menu(self) -> None:
        self._popup(self.quality_menu(), self._quality_button)

    def quality_menu(self) -> QMenu:
        """A guest's own stream first, in a movie night; then the scaling presets."""
        from ..player.mpv_process import QUALITY_LABELS, QUALITY_PRESETS

        menu = QMenu(self)
        party = self._party
        if party is not None and party.get("role") == "guest":
            # A guest's own stream. The original is the film as the host has it;
            # the others are made on the fly by the host's Mistery, for a
            # connection or a PC that cannot keep up with it (4K HEVC, say).
            header = QAction("Movie night stream", menu)
            header.setEnabled(False)
            menu.addAction(header)
            streams = QActionGroup(menu)
            streams.setExclusive(True)
            offered = party.get("transcode")
            for key, label in (("original", "Original  —  the film as the host has it"),
                               ("1080p", "Smoother  —  1080p"),
                               ("720p", "Low bandwidth  —  720p")):
                action = QAction(label, menu)
                action.setCheckable(True)
                action.setChecked(key == party.get("quality"))
                action.setEnabled(key == "original" or bool(offered))
                action.triggered.connect(
                    lambda _checked, name=key: self.stream_quality_selected.emit(name))
                streams.addAction(action)
                menu.addAction(action)
            menu.addSeparator()
        # The file's own resolution is the honest ceiling here: none of these
        # presets add detail, they decide how much work goes into showing it.
        if self._source_size:
            header = QAction(f"Source  ·  {self._source_size}", menu)
            header.setEnabled(False)
            menu.addAction(header)
            menu.addSeparator()

        notes = {
            "fast": "Cheapest scaling, lowest GPU load",
            "balanced": "Good scaling (recommended)",
            "high": "Best scaling, heaviest GPU load",
        }
        group = QActionGroup(menu)
        group.setExclusive(True)
        for key in QUALITY_PRESETS:
            action = QAction(f"{QUALITY_LABELS[key]}   —   {notes[key]}", menu)
            action.setCheckable(True)
            action.setChecked(key == self._quality)
            action.triggered.connect(
                lambda _checked, name=key: self.quality_selected.emit(name)
            )
            group.addAction(action)
            menu.addAction(action)
        return menu

    def _show_speed_menu(self) -> None:
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)
        for speed in (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0):
            label = "Normal" if speed == 1.0 else f"{speed:g}×"
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(abs(self._speed - speed) < 0.01)
            action.triggered.connect(lambda _checked, s=speed: self.speed_selected.emit(s))
            group.addAction(action)
            menu.addAction(action)
        self._popup(menu, self._speed_button)

    # --- scrubbing ----------------------------------------------------------

    def _on_scrub_preview(self, position: float, anchor: QPoint) -> None:
        chapter = ""
        for entry in self._chapters:
            start = entry.get("time")
            if start is not None and start <= position:
                chapter = entry.get("title") or ""
        self._preview.show_at(position, fmt_clock(position), chapter, anchor)

    # --- events -------------------------------------------------------------

    def _watch_children(self) -> None:
        """Wake on movement over the chrome too, not just the bare video.

        A QLabel with mouse tracking off swallows the move event instead of
        letting it reach us, so the controls could fade out from under a pointer
        resting on the title.
        """
        for child in self.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.MouseMove, QEvent.Type.Enter):
            self.wake_on_movement(_global_point(event))
        return False

    def _over_chrome(self, point: QPoint) -> bool:
        """Is this point on the control bars rather than on the picture?"""
        if not self._chrome_visible:
            return False
        return (self._top.geometry().contains(point)
                or self._bottom.geometry().contains(point))

    def mouseMoveEvent(self, event) -> None:
        self.wake_on_movement(_global_point(event))
        super().mouseMoveEvent(event)

    def enterEvent(self, event) -> None:
        # Hiding the control bars can hand us a synthetic Enter without the
        # pointer having moved at all; the distance check absorbs that.
        self.wake_on_movement(_global_point(event))
        super().enterEvent(event)

    def _focus_owner(self) -> None:
        """This window refuses focus, so clicking it while another app is in
        front would leave Mistery unable to hear the keyboard. Hand focus back."""
        from PySide6.QtWidgets import QApplication

        if self._owner is None or QApplication.activeWindow() is not None:
            return
        try:
            window = self._owner.window()
            window.raise_()
            window.activateWindow()
        except RuntimeError:
            self._owner = None

    def mousePressEvent(self, event) -> None:
        self.wake()
        self._focus_owner()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # Empty space in the control bar is not the picture — clicking it to
        # reach for a button should not pause the film.
        if self._over_chrome(event.position().toPoint()):
            return
        self.play_pause.emit()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._over_chrome(event.position().toPoint()):
            return
        # Qt delivers press, release, then double-click, so the press has
        # already toggled pause. Undo it here rather than delaying every single
        # click by the double-click interval: a click that pauses the instant
        # you make it is worth more than a tidier code path.
        self.play_pause.emit()
        self.fullscreen_toggled.emit()

    def wheelEvent(self, event) -> None:
        """The wheel anywhere on the picture is the volume — the same notch the
        bar itself uses, so the step no longer changes as the pointer crosses
        onto the bar (it was 5 over the picture and Qt's own 3 over the slider)."""
        self.wake()
        delta = event.angleDelta().y()
        if not delta:
            return
        step = FINE_STEP if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else WHEEL_STEP
        self._volume.nudge(step if delta > 0 else -step)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = QRectF(self.rect())

        # Windows hit-tests a layered window per pixel and lets the mouse fall
        # through wherever alpha is 0 — straight to mpv's native child window,
        # which ignores it. So every pixel we want to be clickable has to carry
        # *some* alpha. One step above nothing is 0.4% black: invisible on a
        # video frame, and enough to make the whole surface respond to the mouse.
        #
        # It is not free. A layer DWM cannot skip costs mpv about 1.3 points of
        # one core while the controls are hidden (5.1% -> 6.4%, three 20s runs
        # each on a 4K file); our own process is unchanged. That buys every Qt
        # mouse handler working normally, which is worth more than the point.
        painter.fillRect(rect, QColor(0, 0, 0, 1))
        if not self._chrome_visible:
            return

        top_height = max(90.0, float(self._top.height() + 20))
        top = QLinearGradient(0, 0, 0, top_height)
        top.setColorAt(0.0, QColor(0, 0, 0, 190))
        top.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, 0, rect.width(), top_height), top)

        bottom_height = max(190.0, float(self._bottom.height() + 84))
        bottom = QLinearGradient(0, rect.height() - bottom_height, 0, rect.height())
        bottom.setColorAt(0.0, QColor(0, 0, 0, 0))
        bottom.setColorAt(0.40, QColor(0, 0, 0, 130))
        bottom.setColorAt(0.70, QColor(0, 0, 0, 205))
        bottom.setColorAt(1.0, QColor(0, 0, 0, 240))
        painter.fillRect(
            QRectF(0, rect.height() - bottom_height, rect.width(), bottom_height), bottom
        )
