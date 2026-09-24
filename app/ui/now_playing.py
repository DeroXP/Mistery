"""Now Playing: the bar that follows you around, and the full view behind it.

The full view is where the "Mistery" part of the music player lives — it wears
the colours of whatever is playing (a gradient from the cover's own palette over
a heavily blurred copy of the art), and its right half is synced lyrics you can
click to jump to a line, the queue, or the details of the file.

The cover is a record turning on a turntable (vinyl.py) unless Settings asks for
the flat square. Around it: where the music is playing from, a heart for Liked
Songs, a sleep timer, and the song that comes next.
"""

from __future__ import annotations

import bisect
import math
import time
from html import escape as html_escape

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer,
    QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QImage, QKeySequence, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QToolTip, QVBoxLayout, QWidget,
)

from ..config import settings
from ..images import cover_pixmap, load_async
from ..music import audio_fx, library, loudness
from ..music.tags import quality_label
from ..util import fmt_clock, fmt_size, reveal_in_explorer
from .player_overlay import SeekBar
from .screensaver import LYRIC_WHITE, ScreensaverView
from .sound_panel import SoundPanel
from .theme import C
from .vinyl import VinylView
from .widgets.artview import ArtView
from .widgets.icons import IconButton, icon_pixmap, paint_icon
from .widgets.tracklist import TrackList
from .widgets.volume_bar import VolumeBar


# Small capitals over a title: "PLAYING FROM ALBUM", "NEXT UP", the Details sections.
_CAPTION_STYLE = "color: rgba(255,255,255,0.62); font-size: 7.5pt; font-weight: 700;"


def _on_screen(widget: QWidget) -> bool:
    """Really showing: visible, and its window not minimised.

    Minimising sends children a hide event but leaves isVisible() True, so a
    timer restarted by a later signal would animate a window nobody can see —
    while the user games with Mistery minimised on Now Playing.
    """
    window = widget.window()
    return widget.isVisible() and not (window is not None and window.isMinimized())


def _caption(text: str = "", extra_style: str = "") -> QLabel:
    """A small-capitals caption. Letter spacing is a font setting: style sheets
    have no property for it."""
    label = QLabel(text)
    label.setStyleSheet(_CAPTION_STYLE + extra_style)
    font = label.font()
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.3)
    label.setFont(font)
    return label


class ElidedLabel(QLabel):
    """A single line that ends in … instead of pushing its layout wider."""

    def __init__(self, text: str = "", parent=None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight) -> None:
        super().__init__(text, parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        self._full = text or ""
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = max(10, self.width() - 2)
        super().setText(self.fontMetrics().elidedText(self._full, self._mode, width))


class ClickableLabel(QLabel):
    """A label that is also a button: the time that toggles, a title that opens."""

    clicked = Signal()

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class CircleButton(QAbstractButton):
    """The play/pause button: a white disc with a black glyph."""

    def __init__(self, size: int = 40, parent=None) -> None:
        super().__init__(parent)
        self._icon = "play"
        self.setFixedSize(QSize(size, size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_playing(self, playing: bool) -> None:
        name = "pause" if playing else "play"
        if name != self._icon:
            self._icon = name
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        scale = 0.94 if self.isDown() else (1.04 if self.underMouse() else 1.0)
        disc = QRectF(0, 0, rect.width() * scale, rect.height() * scale)
        disc.moveCenter(rect.center())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C.PLAY_BG) if self.isEnabled() else QColor(C.TEXT_FAINT))
        painter.drawEllipse(disc)
        glyph = QRectF(0, 0, disc.width() * 0.5, disc.height() * 0.5)
        glyph.moveCenter(disc.center() + (QPoint(1, 0) if self._icon == "play" else QPoint(0, 0)))
        paint_icon(painter, self._icon, glyph, QColor(C.PLAY_FG))


class CoverView(QWidget):
    """A square cover that fills whatever space it's given, with a shadow."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._path: str | None = None
        self._cache: tuple[tuple, QPixmap] | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(160, 160)

    def set_cover(self, path: str | None) -> None:
        if path == self._path:
            return
        self._path = path
        self._image = QImage()
        self._cache = None
        if path:
            load_async(path, lambda image, wanted=path: self._on_image(image, wanted))
        self.update()

    def _on_image(self, image: QImage, wanted: str | None = None) -> None:
        if wanted != self._path:
            return          # a slower read for the song before; not ours any more
        self._image = image
        self._cache = None
        self.update()

    def side(self) -> int:
        return min(self.width(), self.height()) - 24

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        side = max(40, self.side())
        # Sit on the title below rather than hang from the top: spare height goes
        # above the cover, where it reads as breathing room, not as a gap.
        target = QRectF((self.width() - side) / 2, self.height() - side - 22, side, side)

        # A soft shadow built from stacked translucent rects.
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, alpha in ((18, 18), (11, 30), (5, 46)):
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(target.adjusted(-spread / 2, spread / 3, spread / 2, spread), 12, 12)

        if self._image.isNull():
            path = QPainterPath()
            path.addRoundedRect(target, 10, 10)
            painter.fillPath(path, QColor(255, 255, 255, 18))
            paint_icon(painter, "music", target.adjusted(side * 0.33, side * 0.33, -side * 0.33, -side * 0.33),
                       QColor(255, 255, 255, 90))
            return
        key = (int(side), self.devicePixelRatioF())
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, cover_pixmap(self._image, int(side), int(side), 10, self.devicePixelRatioF()))
        painter.drawPixmap(target.topLeft(), self._cache[1])


def _transport(player, size: int = 40) -> tuple[QHBoxLayout, dict]:
    """Shuffle · previous · play · next · repeat, wired to the player."""
    row = QHBoxLayout()
    row.setSpacing(10)
    shuffle = IconButton("shuffle", size=36, icon_size=18, checkable=True, tooltip="Smart shuffle")
    previous = IconButton("prev", size=38, icon_size=18, tooltip="Previous")
    play = CircleButton(size)
    following = IconButton("next", size=38, icon_size=18, tooltip="Next")
    repeat = IconButton("repeat", size=36, icon_size=18, checkable=True, tooltip="Repeat")
    shuffle.clicked.connect(lambda: player.set_shuffle(not player.shuffle))
    previous.clicked.connect(player.previous)
    play.clicked.connect(player.toggle_pause)
    following.clicked.connect(player.next)
    repeat.clicked.connect(player.cycle_repeat)
    for widget in (shuffle, previous, play, following, repeat):
        row.addWidget(widget, alignment=Qt.AlignmentFlag.AlignVCenter)
    return row, {"shuffle": shuffle, "previous": previous, "play": play,
                 "next": following, "repeat": repeat}


def _sync_transport(buttons: dict, player) -> None:
    buttons["play"].set_playing(player.is_playing)
    for key in ("shuffle", "repeat"):
        buttons[key].blockSignals(True)
    buttons["shuffle"].setChecked(player.shuffle)
    buttons["repeat"].setChecked(player.repeat != "off")
    buttons["repeat"].set_icon_name("repeat_one" if player.repeat == "one" else "repeat")
    buttons["repeat"].setToolTip({"off": "Repeat: off", "all": "Repeat: all",
                                  "one": "Repeat: this song"}[player.repeat])
    for key in ("shuffle", "repeat"):
        buttons[key].blockSignals(False)


def _volume_row(player, width: int = 110) -> tuple[QHBoxLayout, IconButton, VolumeBar]:
    """The level and the mute, for the bar and for Now Playing alike.

    The painted VolumeBar replaced a QSlider whose click was a 10-unit page
    step. Mute is now a state of its own (mpv's own property, kept in
    music_muted) instead of "set the level to 0 and remember it in this widget",
    which forgot where the bar was as soon as the app closed.
    """
    row = QHBoxLayout()
    row.setSpacing(8)
    icon = IconButton("volume", size=34, icon_size=18, tooltip="Mute  (Ctrl+M)")
    bar = VolumeBar(0, 100)
    bar.setFixedWidth(width)
    bar.set_value(player.volume)
    bar.set_muted(player.muted)

    def show_level(value: int, muted: bool) -> None:
        icon.set_icon_name("mute" if muted or value == 0
                           else ("volume_low" if value < 50 else "volume"))

    def changed(value: int) -> None:
        player.set_volume(value)
        # Moving the bar is never a way of asking to stay silent.
        if player.muted:
            player.set_muted(False)
        show_level(value, player.muted)

    def follow(value: int) -> None:
        # Set from the other row (the bar and Now Playing each have one). The
        # widget never emits from set_value, so this is a display of the level
        # rather than a new choice of it, and the two cannot ping-pong.
        bar.set_value(value)
        show_level(value, player.muted)

    def follow_mute(muted: bool) -> None:
        bar.set_muted(muted)
        show_level(bar.value(), muted)

    bar.value_changed.connect(changed)
    icon.clicked.connect(lambda: player.set_muted(not player.muted))
    player.volume_changed.connect(follow)
    player.muted_changed.connect(follow_mute)
    # Nothing is set at construction: two rows are built at every launch, and
    # calling set_volume here rewrote settings.json twice before a note played.
    show_level(player.volume, player.muted)
    row.addWidget(icon)
    row.addWidget(bar)
    return row, icon, bar


def _sound_button(player, owner: QWidget) -> IconButton:
    """The Sound button: opens the panel, and lights up while anything is on."""
    button = IconButton("sound", size=36, icon_size=19, checkable=True, tooltip="Sound")
    held: dict[str, SoundPanel | None] = {"panel": None}

    def sync() -> None:
        button.blockSignals(True)
        button.setChecked(audio_fx.is_active())
        button.blockSignals(False)
        button.setToolTip(f"Sound — {audio_fx.describe()}")
        if held["panel"] is not None and held["panel"].isVisible():
            held["panel"].reload()

    def open_panel() -> None:
        if held["panel"] is None:
            held["panel"] = SoundPanel(player, owner)
        held["panel"].popup_at(button)
        sync()

    button.clicked.connect(open_panel)
    player.sound_changed.connect(sync)
    sync()
    return button


def _remaining_mode() -> bool:
    return bool(settings.get("music_show_remaining", False))


def _total_text(position: float, duration: float) -> str:
    """The time after the seek bar: the song's length, or -what is left of it.

    Left is counted from the second on show, not the exact position, so the
    two labels always add up to the length (0:04 and -3:22 for a 3:26 song).
    """
    if _remaining_mode() and duration:
        return "-" + fmt_clock(max(0, int(duration) - int(position or 0)))
    return fmt_clock(duration)


def _time_toggle(label: ClickableLabel, refresh) -> None:
    """Clicking the total time switches it to the time left, and back; remembered."""
    def toggle() -> None:
        settings.set("music_show_remaining", not _remaining_mode())
        tip()
        refresh()

    def tip() -> None:
        label.setToolTip("Show the song's length" if _remaining_mode() else "Show time left")

    label.clicked.connect(toggle)
    tip()


class LikeButton(IconButton):
    """The heart: saves the song playing to Liked Songs, filled while it's there.

    It follows the player rather than keeping its own idea of the song: a like
    from anywhere else (the tracklist, the other heart) arrives as
    `track_updated` and every heart on show agrees.
    """

    def __init__(self, player, size: int = 36, icon_size: int = 20, parent=None) -> None:
        super().__init__("heart", size=size, icon_size=icon_size, checkable=True,
                         tooltip="Save to Liked Songs", parent=parent)
        self._player = player
        self.clicked.connect(self._toggle)
        player.track_changed.connect(lambda _track: self.sync())
        player.track_updated.connect(self._on_updated)
        self.sync()

    def _toggle(self) -> None:
        current = self._player.current
        if current is None:
            self.sync()
            return
        liked = not bool(current.get("liked"))
        self._player.set_liked(current, liked)
        self.sync()             # also undoes the click's own toggle if saving failed
        if liked and self.isChecked():
            self.pop()

    def _on_updated(self, track) -> None:
        current = self._player.current
        if current is None or not track or track.get("id") == current.get("id"):
            self.sync()

    def sync(self) -> None:
        current = self._player.current
        liked = bool(current and current.get("liked"))
        self.blockSignals(True)
        self.setChecked(liked)
        self.blockSignals(False)
        self.set_icon_name("heart_filled" if liked else "heart")
        friends = bool(current and current.get("friend"))
        self.setToolTip("A friend's song: Liked Songs is for your own" if friends else
                        "Remove from Liked Songs" if liked else "Save to Liked Songs")
        self.setEnabled(current is not None and not friends)
        self.update()


class SleepTimerButton(IconButton):
    """The moon: a menu of sleep timers, and accented while one is running.

    The time left is shown in the tooltip (and, in Now Playing, in a small
    label beside it). It is recounted once a second, and only while the button
    is on screen: a timer set from the bar and left running while you play a
    game costs nothing here.
    """

    remaining_changed = Signal(str)       # "12:34", or "" with no timer

    MINUTES = (15, 30, 45, 60)

    def __init__(self, player, size: int = 36, icon_size: int = 19, parent=None) -> None:
        super().__init__("moon", size=size, icon_size=icon_size, checkable=True,
                         tooltip="Sleep timer", parent=parent)
        self._player = player
        self._shown = ""
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._refresh)
        self.clicked.connect(self.open_menu)
        player.sleep_timer_changed.connect(self.sync)
        # Song and queue timers stand still while paused and move again on Play.
        player.state_changed.connect(self._refresh)
        self.sync()

    @property
    def remaining_text(self) -> str:
        return self._shown

    def sync(self) -> None:
        active = self._player.sleep_mode is not None
        self.blockSignals(True)
        self.setChecked(active)
        self.blockSignals(False)
        if active and _on_screen(self):
            if not self._clock.isActive():
                self._clock.start()
        else:
            self._clock.stop()
        self._refresh()
        self.update()

    def describe(self) -> str:
        mode = self._player.sleep_mode
        remaining = self._player.sleep_remaining
        if mode is None or remaining is None:
            return "Sleep timer"
        left = fmt_clock(math.ceil(remaining))
        if mode == "track":
            return f"Sleep timer — stops at the end of this song ({left})"
        if mode == "queue":
            return f"Sleep timer — stops at the end of the queue ({left})"
        return f"Sleep timer — music stops in {left}"

    def _refresh(self) -> None:
        mode = self._player.sleep_mode
        remaining = self._player.sleep_remaining
        text = "" if mode is None or remaining is None else fmt_clock(math.ceil(remaining))
        tip = self.describe()
        if tip != self.toolTip():
            self.setToolTip(tip)
            if self.underMouse() and QToolTip.isVisible():
                QToolTip.showText(QCursor.pos(), tip, self)
        if text != self._shown:
            self._shown = text
            self.remaining_changed.emit(text)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.sync()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._clock.stop()

    def build_menu(self) -> QMenu:
        player = self._player
        mode = player.sleep_mode
        last = settings.get("music_sleep_last", 30)
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)     # see PosterCard's menu
        if mode is not None:
            header = menu.addAction(self.describe().replace("Sleep timer — ", "").capitalize())
            header.setEnabled(False)
            menu.addSeparator()
        choices = list(self.MINUTES)
        if isinstance(last, (int, float)) and last > 0 and last not in choices:
            choices.insert(0, last)         # a length chosen elsewhere is offered too
        for minutes in choices:
            action = menu.addAction(f"{minutes:g} minutes",
                                    lambda m=minutes: player.set_sleep_timer(minutes=m))
            action.setCheckable(True)
            action.setChecked(mode == "minutes" and minutes == last)
            if minutes == last and mode is None:
                menu.setDefaultAction(action)   # bold: the length you picked last time
        menu.addSeparator()
        has_song = player.current is not None
        song = menu.addAction("End of this song", lambda: player.set_sleep_timer(end_of="track"))
        song.setCheckable(True)
        song.setChecked(mode == "track")
        song.setEnabled(has_song)
        queue = menu.addAction("End of queue", lambda: player.set_sleep_timer(end_of="queue"))
        queue.setCheckable(True)
        queue.setChecked(mode == "queue")
        queue.setEnabled(has_song)
        if mode is not None:
            menu.addSeparator()
            menu.addAction("Turn off timer", player.cancel_sleep_timer)
        return menu

    def open_menu(self) -> None:
        self.sync()                 # the click toggled the button; the timer decides
        menu = self.build_menu()
        hint = menu.sizeHint()
        corner = self.mapToGlobal(QPoint(self.width() // 2 - hint.width() // 2, -hint.height() - 6))
        screen = self.screen()
        if screen is not None and corner.y() < screen.availableGeometry().top():
            corner = self.mapToGlobal(QPoint(0, self.height() + 6))
        menu.popup(corner)


# --- Playing from -------------------------------------------------------------------

_CONTEXT_CAPTIONS = {
    "album": "PLAYING FROM ALBUM",
    "artist": "PLAYING FROM ARTIST",
    "liked": "PLAYING FROM YOUR LIBRARY",
    "songs": "PLAYING FROM YOUR LIBRARY",
    "search": "PLAYING FROM SEARCH",
    "playlist": "PLAYING FROM PLAYLIST",
    "queue": "PLAYING FROM",
    "friend": "PLAYING FROM A FRIEND'S LIBRARY",
}


class PlayingFrom(QWidget):
    """"PLAYING FROM ALBUM / Neon Hours" at the top of Now Playing, and a way back there.

    An album or an artist opens its page; your queue opens Up next; anything
    else (Liked Songs, Songs, a search) is handed to the window as
    `context_requested`, which knows those pages.
    """

    album_requested = Signal(int)
    artist_requested = Signal(str)
    context_requested = Signal(dict)
    queue_requested = Signal()

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._context: dict | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(1)
        self._caption = _caption()
        layout.addWidget(self._caption)
        self._title = ElidedLabel()
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 10.5pt; font-weight: 700;")
        layout.addWidget(self._title)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        player.queue_changed.connect(self.refresh)
        player.track_changed.connect(lambda _track: self.refresh())
        self.refresh()

    @property
    def context(self) -> dict | None:
        return dict(self._context) if self._context else None

    def refresh(self) -> None:
        context = self._player.context if self._player.has_queue else None
        self._context = context
        if not context:
            self.setVisible(False)
            return
        kind = str(context.get("kind") or "")
        title = str(context.get("title") or "")
        if kind == "search":
            # The caption already says SEARCH; show just what was searched for.
            searched = context.get("id")
            if isinstance(searched, str) and searched.strip():
                title = searched.strip()
            elif title.lower().startswith("search: "):
                title = title[len("search: "):]
            if title and title[0] not in "\"“'":
                title = f"“{title}”"
        self._caption.setText(_CONTEXT_CAPTIONS.get(kind, "PLAYING FROM"))
        self._title.setText(title)
        self.setToolTip({"album": "Open the album", "artist": "Open the artist",
                         "queue": "Show Up next"}.get(kind, f"Open {title}" if title else ""))
        self.setVisible(True)

    def enterEvent(self, event) -> None:
        font = self._title.font()
        font.setUnderline(True)
        self._title.setFont(font)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        font = self._title.font()
        font.setUnderline(False)
        self._title.setFont(font)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.rect().contains(event.position().toPoint()):
            return
        self.activate()

    def activate(self) -> None:
        context = self._context
        if not context:
            return
        kind = context.get("kind")
        if kind == "album":
            try:
                self.album_requested.emit(int(context.get("id")))
            except (TypeError, ValueError):
                current = self._player.current
                if current and current.get("album_id"):
                    self.album_requested.emit(int(current["album_id"]))
        elif kind == "artist":
            name = context.get("id") if isinstance(context.get("id"), str) else context.get("title")
            if name:
                self.artist_requested.emit(str(name))
        elif kind == "queue":
            self.queue_requested.emit()
        else:
            self.context_requested.emit(dict(context))


# --- Next up --------------------------------------------------------------------------

class NextUpCard(QWidget):
    """The song after this one, small, at the foot of the left column. Click to play it."""

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._target: tuple[int, dict] | None = None
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(64)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 9, 36, 9)
        layout.setSpacing(12)
        self._art = ArtView(46, 46, radius=5)
        self._art.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._art)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addStretch(1)
        text.addWidget(_caption("NEXT UP"))
        self._title = ElidedLabel()
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 10pt; font-weight: 700;")
        text.addWidget(self._title)
        self._artist = ElidedLabel()
        self._artist.setStyleSheet("color: rgba(255,255,255,0.66); font-size: 9pt;")
        text.addWidget(self._artist)
        text.addStretch(1)
        layout.addLayout(text, 1)

        player.queue_changed.connect(self.refresh)
        player.track_changed.connect(lambda _track: self.refresh())
        player.state_changed.connect(self.refresh)          # Repeat all brings the first song round
        player.track_updated.connect(lambda _track: self.refresh())
        self.refresh()

    def next_song(self) -> tuple[int, dict] | None:
        """(queue index, song) of what plays after this one, or None."""
        player = self._player
        if not player.has_queue or player.index < 0:
            return None
        upcoming = player.upcoming
        if upcoming:
            return player.index + 1, upcoming[0]
        if player.repeat == "all":
            return 0, player.queue[0]
        return None

    def refresh(self) -> None:
        target = self.next_song()
        self._target = target
        if target is None:
            self.setVisible(False)
            return
        _, track = target
        self._title.setText(track.get("title") or "")
        self._artist.setText(track.get("artist") or track.get("album_artist") or "")
        self._art.set_art((track.get("cover") or "").replace(".jpg", "-sm.jpg") or None,
                          track.get("album_title") or track.get("title") or "")
        self.setToolTip(f"Play {track.get('title') or 'the next song'} now")
        self.setVisible(True)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.rect().contains(event.position().toPoint()):
            return
        self.activate()

    def activate(self) -> None:
        # Checked again at the click: the queue may have moved on since it was drawn.
        target = self.next_song()
        if target is not None:
            self._player.jump_to(target[0])

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        painter.fillPath(path, QColor(255, 255, 255, 30 if self._hover else 16))
        painter.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        painter.drawPath(path)
        glyph = QRectF(self.width() - 30, (self.height() - 18) / 2, 18, 18)
        paint_icon(painter, "chevron_right", glyph, QColor(255, 255, 255, 210 if self._hover else 120))


# --- lyrics ----------------------------------------------------------------------

class _Row:
    """One row of the lyrics view.

    "line" and "break" come from the lyrics themselves (a break is an empty
    line, which LRC files use for an instrumental passage). "intro" and "gap"
    are added here: the wait before a first line that comes late, and a long
    silence after a line has been sung.
    """

    __slots__ = ("time", "text", "kind", "line", "end")

    def __init__(self, time_: float | None, text: str, kind: str, line: int, end: float | None) -> None:
        self.time = time_
        self.text = text
        self.kind = kind
        self.line = line            # index into lyrics.lines, -1 for added rows
        self.end = end              # when the next line starts


_DOT_KINDS = ("intro", "gap", "break")

# The screensaver's lyrics, by distance from the sung line. The top of it is
# LYRIC_WHITE rather than #FFFFFF: pure white is the worst case for an OLED's
# blue subpixel, and at this size nobody can tell the difference.
_SAVER_ALPHA = (int(255 * LYRIC_WHITE), 86, 38)


class LyricsView(QWidget):
    """Synced lyrics that follow the song. Click a line to jump to it.

    When the words are a while off — an intro before the first line (Fist's
    comes in at 2:00), or a long silence after one — three dots sit where the
    next line will be sung, breathing gently and filling in one by one as it
    gets closer. They are rows like any other, so following the song, clicking
    a line and scrolling ahead work exactly as before; they are just not
    clickable themselves.
    """

    seek_requested = Signal(float)
    # main_window fetches the words once and hands them to this view. The
    # screensaver's second copy hears about them here rather than through
    # another call site that could be missed.
    lyrics_changed = Signal(object)
    status_changed = Signal(str)

    _GAP = 18
    _FOCUS = 0.32          # the sung line sits about a third of the way down
    _INTRO_MIN = 3.0       # a first line at least this late gets dots before it
    _GAP_MIN = 6.0         # a silence at least this long after a sung line gets dots
    _DOTS_FRAME_MS = 40    # the dots' breathing, ~25 fps, drawn only in their own small rect
    _FADE = 0.35           # seconds for the dots to appear or leave
    _DOT_RADIUS = 5.5
    _DOT_SPACING = 20.0

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lyrics = None
        self._status = "Play something to see its lyrics"
        self._rows: list[_Row] = []
        self._times: list[float] = []
        self._current = -1
        self._scroll = 0.0
        self._layout: list[tuple[float, float, str]] = []
        self._layout_width: tuple | None = None
        self._hover = -1
        self._manual_until = 0.0
        self._accent = C.TEXT
        self._playing = False
        self._position = 0.0
        self._position_at = time.monotonic()
        self._screensaver = False   # the sung line and its neighbours, nothing else
        self._driven = False        # somebody else is calling tick()
        self._focus = self._FOCUS
        self._dim = 1.0
        self._dots_since = 0.0                          # when the current dots row arrived
        self._leaving: tuple[int, float] | None = None  # (row, since) for dots fading out
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._font = QFont("Segoe UI Variable Display")
        self._font.setPixelSize(30)
        self._font.setWeight(QFont.Weight.Bold)

        self._animation = QVariantAnimation(self)
        self._animation.setDuration(460)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_scroll_value)

        self._pulse = QTimer(self)
        self._pulse.setInterval(self._DOTS_FRAME_MS)
        self._pulse.timeout.connect(self._on_pulse)

    @property
    def current_line(self) -> int:
        """Index of the lyrics line being sung (the last one sung, in a gap), -1 before the first."""
        for index in range(min(self._current, len(self._rows) - 1), -1, -1):
            if self._rows[index].line >= 0:
                return self._rows[index].line
        return -1

    @property
    def current_row(self) -> _Row | None:
        return self._rows[self._current] if 0 <= self._current < len(self._rows) else None

    @property
    def waiting(self) -> bool:
        """The dots are up: the song is before its first line or in a long gap."""
        row = self.current_row
        return bool(row is not None and row.kind in _DOT_KINDS and self._synced())

    @property
    def lyrics(self):
        return self._lyrics

    @property
    def status(self) -> str:
        return self._status

    @property
    def synced(self) -> bool:
        """There are timed lines to follow (the screensaver shows nothing else)."""
        return self._synced()

    def set_text_size(self, pixels: int) -> None:
        """Bigger words for the screensaver. _ensure_layout caches on the widget
        size alone, so the cache has to be dropped by hand or every row keeps
        the metrics of the old size and the lines overlap."""
        pixels = max(12, int(pixels))
        if pixels == self._font.pixelSize():
            return
        self._font.setPixelSize(pixels)
        self._layout_width = None
        self._layout = []
        if self._synced():
            self._follow()
        self.update()

    def set_screensaver(self, on: bool) -> None:
        """The screensaver look: the sung line at 0.82 white, one line either
        side, and nothing else drawn at all — fewer lit pixels on a panel that
        is going to hold this for hours, and a much cheaper repaint at full
        screen, where a full page of wrapped lines is redrawn ~28 times per line
        change by the scroll animation."""
        on = bool(on)
        if on != self._screensaver:
            self._screensaver = on
            # Nearly centred, rather than a third down: on a page the sung line
            # sits high so the words to come are readable, and in a screensaver
            # there is nothing else in the column to read.
            self._focus = 0.44 if on else self._FOCUS
            self._layout_width = None
            if self._synced():
                self._follow()
            self.update()

    def set_dim(self, level: float) -> None:
        level = max(0.0, min(1.0, float(level)))
        if abs(level - self._dim) > 0.004:
            self._dim = level
            self.update()

    def set_status(self, text: str) -> None:
        self._lyrics = None
        self._status = text
        # A new song is on its way: lyrics that arrive before its first position
        # must not be placed at the old song's.
        self._position = 0.0
        self._position_at = time.monotonic()
        self._rows = []
        self._times = []
        self._layout = []
        self._layout_width = None
        self._current = -1
        self._scroll = 0.0
        self._leaving = None
        self._sync_pulse()
        self.status_changed.emit(text)
        self.update()

    def set_lyrics(self, lyrics) -> None:
        self._lyrics = lyrics
        self._current = -1
        self._scroll = 0.0
        self._layout_width = None
        self._manual_until = 0.0
        self._leaving = None
        if lyrics is None or not lyrics.available:
            source = getattr(lyrics, "source", "none")
            # "unavailable" is LRCLIB not answering, not a song without lyrics:
            # it is not remembered, so the song is looked up again next time.
            self._status = {
                "instrumental": "Instrumental",
                "unavailable": "Couldn't reach the lyrics service — try again later",
            }.get(source, "No lyrics found for this song")
            self._rows = []
        else:
            self._status = ""
            self._rows = self._build_rows(lyrics)
        self._times = [row.time for row in self._rows if row.time is not None]
        if self._synced():
            self.set_position(self._position_now())
        self._sync_pulse()
        self.lyrics_changed.emit(lyrics)
        self.update()

    def set_accent(self, colour: str) -> None:
        self._accent = colour
        self.update()

    def set_playing(self, playing: bool) -> None:
        self._position = self._position_now()
        self._position_at = time.monotonic()
        self._playing = bool(playing)
        self._sync_pulse()

    @classmethod
    def _sung_for(cls, text: str, interval: float) -> float:
        """How long a line is probably being sung for: a word takes about half a
        second, and slow songs stretch their lines, so never less than a third of
        the time to the next one. Measured against this library's synced lyrics:
        lines 13 s apart are often sung for 5 s or more."""
        words = len(text.split())
        return max(1.2 + 0.5 * words, 0.35 * interval)

    @classmethod
    def _build_rows(cls, lyrics) -> list[_Row]:
        if not lyrics.synced:
            return [_Row(None, text, "line" if text.strip() else "break", index, None)
                    for index, text in enumerate(lyrics.plain.splitlines())]
        lines = lyrics.lines
        rows: list[_Row] = []
        if lines and lines[0][0] >= cls._INTRO_MIN:
            rows.append(_Row(0.0, "", "intro", -1, lines[0][0]))
        for index, (stamp, text) in enumerate(lines):
            end = lines[index + 1][0] if index + 1 < len(lines) else None
            spoken = bool(text.strip())
            rows.append(_Row(stamp, text, "line" if spoken else "break", index, end))
            if spoken and end is not None and lines[index + 1][1].strip():
                starts = stamp + cls._sung_for(text, end - stamp)
                if end - starts >= cls._GAP_MIN:
                    rows.append(_Row(starts, "", "gap", -1, end))
        return rows

    def _synced(self) -> bool:
        return bool(self._lyrics and self._lyrics.synced and self._rows)

    def _position_now(self) -> float:
        if self._playing:
            return self._position + min(1.0, time.monotonic() - self._position_at)
        return self._position

    def _ensure_layout(self) -> None:
        width = self.width() - 40
        key = (width, self.height())
        if key == self._layout_width and self._layout:
            return
        self._layout_width = key
        metrics = QFontMetrics(self._font)
        # Synced lyrics start at the focus line rather than the top edge, so the
        # first line is sung in the same place as every other one. Without it
        # the scroll can't go above zero and the opening lines sit jammed at
        # the top while the rest of the song plays a third of the way down.
        synced = self._synced()
        top = self.height() * self._focus - 20 if synced else 24.0
        self._layout = []
        for row in self._rows:
            if row.kind in _DOT_KINDS:
                # Added rows are shorter than a line, so a gap that has gone
                # by leaves a modest space rather than a missing line.
                height = float(metrics.height()) if row.kind == "break" else 30.0
                self._layout.append((top, height, ""))
            else:
                bounds = metrics.boundingRect(QRect(0, 0, max(40, width), 100000),
                                              int(Qt.TextFlag.TextWordWrap), row.text)
                self._layout.append((top, float(bounds.height()), row.text))
            top += self._layout[-1][1] + self._GAP

    def set_position(self, position: float) -> None:
        self._position = float(position)
        self._position_at = time.monotonic()
        if not self._synced():
            return
        row = bisect.bisect_right(self._times, position + 0.15) - 1
        if row == self._current:
            if self.waiting and not self._pulse.isActive():
                self.update(self._dots_rect(row))       # the countdown moved on (paused, seeking)
            return
        previous = self._current
        self._current = row
        now = time.monotonic()
        if 0 <= previous < len(self._rows) and self._rows[previous].kind in ("intro", "gap"):
            self._leaving = (previous, now)
        if self.waiting:
            self._dots_since = now
            if self._leaving and self._leaving[0] == row:
                self._leaving = None
        if time.monotonic() >= self._manual_until:
            self._follow()
        self._sync_pulse()
        self.update()

    def _follow(self) -> None:
        self._ensure_layout()
        if not self._layout:
            return
        index = max(0, self._current)
        top, height, _ = self._layout[min(index, len(self._layout) - 1)]
        target = max(0.0, top + 20 + height / 2 - self.height() * self._focus)
        self._animation.stop()
        self._animation.setStartValue(self._scroll)
        self._animation.setEndValue(target)
        self._animation.start()

    def _on_scroll_value(self, value) -> None:
        self._scroll = float(value)
        self.update()

    # --- the dots ---------------------------------------------------------------

    def set_driven(self, driven: bool) -> None:
        """Take the dots' frames from somebody else's clock instead of this one.

        The screensaver drives the record, the band and this from the record's
        50 ms timer, because every timer that fires in a pass of its own is
        another flush of the window's backing store. Measured full screen with
        the intro dots up: 37.4 paints of the overlay a second, 17.0 of them the
        dots' own 88x47 rectangle.
        """
        self._driven = bool(driven)
        self._sync_pulse()

    def tick(self) -> None:
        """One dots frame, from the clock that is driving this (see set_driven).

        The test is deliberately looser than _pulse_wanted's: _on_pulse ends by
        calling _sync_pulse, which is what lets go of a row on its way out, so
        the last frame of the fade has to be drawn before that happens — the
        same order the timer gave it.
        """
        if not self._driven or not _on_screen(self):
            return
        if (self.waiting and self._playing) or self._leaving is not None:
            self._on_pulse()

    def _pulse_wanted(self) -> bool:
        """Also lets go of a row whose fade has finished."""
        leaving = self._leaving is not None and time.monotonic() - self._leaving[1] < self._FADE
        if not leaving:
            self._leaving = None
        return bool(_on_screen(self) and ((self.waiting and self._playing) or leaving))

    def _sync_pulse(self) -> None:
        """Breathe only while the dots are up, the music is playing and this is on screen."""
        wanted = self._pulse_wanted() and not self._driven
        if wanted and not self._pulse.isActive():
            self._pulse.start()
        elif not wanted and self._pulse.isActive():
            self._pulse.stop()

    def _on_pulse(self) -> None:
        if self.waiting and 0 <= self._current < len(self._layout):
            self.update(self._dots_rect(self._current))
        if self._leaving is not None:
            if 0 <= self._leaving[0] < len(self._layout):
                self.update(self._dots_rect(self._leaving[0]))
        self._sync_pulse()

    def _dots_rect(self, index: int) -> QRect:
        self._ensure_layout()
        if not 0 <= index < len(self._layout):
            return QRect()
        top, height, _ = self._layout[index]
        y = top - self._scroll + 20
        width = 20 + 2 * self._DOT_SPACING + 2 * self._DOT_RADIUS * 1.4 + 12
        return QRectF(8, y - 8, width, height + 16).toAlignedRect()

    def _paint_dots(self, painter: QPainter, index: int, y: float, height: float, alpha: int) -> None:
        row = self._rows[index]
        centre_y = y + height / 2
        now = time.monotonic()
        active = index == self._current and self._synced()
        fade = 1.0
        if self._leaving is not None and self._leaving[0] == index and not active:
            fade = max(0.0, 1.0 - (now - self._leaving[1]) / self._FADE)
            active = fade > 0
        if not active:
            if row.kind != "break":
                return                  # an added row is only there while it's waiting
            colour = QColor(C.TEXT)
            colour.setAlpha(alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            for k in range(3):
                painter.drawEllipse(QPointF(20 + 4.5 + k * 16.0, centre_y), 4.0, 4.0)
            return

        start = row.time or 0.0
        end = row.end if row.end is not None else start
        span = max(0.1, end - start)
        progress = max(0.0, min(1.0, (self._position_now() - start) / span))
        appear = min(1.0, (now - self._dots_since) / self._FADE) if index == self._current else 1.0
        breathing = self._playing and span >= 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        for k in range(3):
            wave = 0.5 + 0.5 * math.sin(now * 2 * math.pi / 1.6 - k * 0.7) if breathing else 0.6
            filled = max(0.0, min(1.0, progress * 3.0 - k))
            scale = (0.80 + 0.22 * wave) * (0.6 + 0.4 * appear)
            opacity = (0.34 + 0.66 * filled) * (0.8 + 0.2 * wave) * appear * fade
            colour = QColor(C.TEXT)
            colour.setAlphaF(max(0.0, min(1.0, opacity)))
            painter.setBrush(colour)
            radius = self._DOT_RADIUS * scale
            painter.drawEllipse(QPointF(20 + self._DOT_RADIUS * 1.2 + k * self._DOT_SPACING, centre_y),
                                radius, radius)

    # --- input ------------------------------------------------------------------

    def _line_at(self, y: float) -> int:
        for index, (top, height, _) in enumerate(self._layout):
            if top - self._GAP / 2 <= y + self._scroll - 20 <= top + height + self._GAP / 2:
                return index
        return -1

    def _clickable(self, index: int) -> bool:
        return 0 <= index < len(self._rows) and self._rows[index].kind in ("line", "break")

    def mouseMoveEvent(self, event) -> None:
        if self._synced():
            hover = self._line_at(event.position().y())
            if not self._clickable(hover):
                hover = -1
            if hover != self._hover:
                self._hover = hover
                self.setCursor(Qt.CursorShape.PointingHandCursor if hover >= 0 else Qt.CursorShape.ArrowCursor)
                self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._synced():
            return
        line = self._line_at(event.position().y())
        if self._clickable(line):
            self._manual_until = 0.0
            self.seek_requested.emit(self._rows[line].time)

    def wheelEvent(self, event) -> None:
        self._ensure_layout()
        if not self._layout:
            return
        last_top, last_height, _ = self._layout[-1]
        limit = max(0.0, last_top + last_height - self.height() * 0.5)
        self._animation.stop()
        self._scroll = max(0.0, min(limit, self._scroll - event.angleDelta().y() * 0.9))
        # Reading ahead shouldn't be yanked back by the next line change.
        self._manual_until = time.monotonic() + 3.0
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_width = None
        if self._synced():
            self._follow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_pulse()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._pulse.stop()
        # Hiding a widget does not stop an animation it started. _follow()'s is
        # 460 ms of QVariantAnimation driving update() at 60 Hz, and the page's
        # lyrics keep being fed while the screensaver covers them, so this went
        # on repainting a widget nobody could see (caught running in 1 of 20
        # half-second samples). Snap to where it was going, so the words are in
        # the right place when the page comes back.
        if self._animation.state() != QAbstractAnimation.State.Stopped:
            end = self._animation.endValue()
            self._animation.stop()
            if end is not None:
                self._scroll = float(end)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._dim < 1.0:
            painter.setOpacity(self._dim)
        if self._status:
            if self._screensaver:
                return      # no status string parked in one place all night
            font = QFont(self._font)
            font.setPixelSize(20)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255, 150))
            painter.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, self._status)
            return

        self._ensure_layout()
        synced = self._synced()
        if self._screensaver and not synced:
            return          # nothing to follow; the screensaver shows the song's name instead
        painter.setFont(self._font)
        clip = QRectF(event.rect())
        # Fade lines out towards the top and bottom edges.
        height = float(self.height())
        for index, (top, line_height, text) in enumerate(self._layout):
            y = top - self._scroll + 20
            if y + line_height < -40 or y > height + 40:
                continue
            if y + line_height + 10 < clip.top() or y - 10 > clip.bottom():
                continue
            if self._screensaver:
                away = abs(index - self._current)
                if away > 2:
                    continue
                alpha = (_SAVER_ALPHA[away] if self._current >= 0
                         else _SAVER_ALPHA[2])
            elif not synced:
                alpha = 215
            elif index == self._current:
                alpha = 255
            elif index == self._hover:
                alpha = 170
            elif index < self._current:
                alpha = 70
            else:
                alpha = 105
            edge = min(1.0, max(0.0, min(y + line_height, height - y) / 70.0))
            shown = int(alpha * (0.25 + 0.75 * edge))
            if self._rows[index].kind in _DOT_KINDS:
                self._paint_dots(painter, index, y, line_height, shown if not synced else min(shown, 150))
                painter.setFont(self._font)
                continue
            colour = QColor(C.TEXT)
            colour.setAlpha(shown)
            painter.setPen(colour)
            painter.drawText(QRectF(20, y, self.width() - 40, line_height + 4),
                             int(Qt.TextFlag.TextWordWrap), text)

        if not synced and self._layout:
            note = QFont(self._font)
            note.setPixelSize(12)
            note.setWeight(QFont.Weight.Normal)
            painter.setFont(note)
            painter.setPen(QColor(255, 255, 255, 110))
            painter.drawText(QRectF(20, 0, self.width() - 40, 16), Qt.AlignmentFlag.AlignLeft,
                             "These lyrics aren't time-synced")


class QueuePanel(QWidget):
    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        now = QLabel("Now playing")
        now.setObjectName("SectionTitle")
        layout.addWidget(now)
        self._now = TrackList(["number", "title", "artist", "time"], auto_height=True, numbering="index")
        layout.addWidget(self._now)
        layout.addSpacing(14)

        header = QHBoxLayout()
        up = QLabel("Up next")
        up.setObjectName("SectionTitle")
        header.addWidget(up)
        header.addStretch(1)
        self._mode = QLabel()
        self._mode.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9pt;")
        header.addWidget(self._mode)
        # The way people actually end up with a playlist: they built the queue
        # by hand and want to keep it. The queue is untouched by saving it.
        self._save = QPushButton("Save as playlist")
        self._save.setObjectName("Ghost")
        self._save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save.clicked.connect(self._save_as_playlist)
        header.addSpacing(10)
        header.addWidget(self._save)
        layout.addLayout(header)

        self._upcoming = TrackList(["number", "title", "artist", "time"], numbering="index")
        self._upcoming.play_requested.connect(lambda row: player.jump_to(player.index + 1 + row))
        self._upcoming.context_requested.connect(self._menu)
        layout.addWidget(self._upcoming, 1)

        player.queue_changed.connect(self.refresh)
        player.track_changed.connect(lambda _t: self.refresh())
        player.state_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        current = self._player.current
        self._now.set_tracks([current] if current else [])
        self._now.set_current(current["id"] if current else None, self._player.is_playing)
        self._upcoming.set_tracks(self._player.upcoming)
        self._save.setVisible(bool(current))
        mode = []
        if self._player.shuffle:
            mode.append("Smart shuffle")
        if self._player.repeat != "off":
            mode.append("Repeat " + ("all" if self._player.repeat == "all" else "one"))
        self._mode.setText("  ·  ".join(mode))

    def _save_as_playlist(self) -> None:
        """Keep the queue as it stands, in order — the song playing included,
        so the list you save is the list you are hearing."""
        from .widgets.playlist_menu import new_playlist_with

        queue = self._player.queue      # a copy: the real one moves under a menu
        ids = [int(track["id"]) for track in queue if track.get("id") is not None]
        if ids:
            new_playlist_with(self, "music", ids)

    def set_accent(self, colour: str) -> None:
        self._now.set_accent(colour)
        self._upcoming.set_accent(colour)

    def _menu(self, track: dict, position) -> None:
        rows = self._upcoming.tracks
        row = next((i for i, t in enumerate(rows) if t is track), -1)
        upcoming = self._player.upcoming
        if row < 0 or row >= len(upcoming) or upcoming[row].get("id") != track.get("id"):
            return
        # The list shows copies; the player's own entry is what can be found again.
        item = upcoming[row]
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)     # see PosterCard's menu
        menu.addAction("Play now", lambda: self._act_on(item, self._player.jump_to))
        menu.addAction("Remove from queue", lambda: self._act_on(item, self._player.remove_upcoming))
        menu.exec(position)

    def _act_on(self, item: dict, action) -> None:
        """Run a menu action on the song that was right-clicked, wherever it is now.

        The menu is modal, but the music is not: a song ending, a media key or
        the tray can move the queue on while it is open. A position worked out
        when the menu opened then pointed one song further along.
        """
        queue = self._player.queue
        index = next((i for i in range(self._player.index + 1, len(queue)) if queue[i] is item), -1)
        if index >= 0:              # gone from Up next (playing now, or removed): nothing to do
            action(index)


# --- details ---------------------------------------------------------------------------

_CHANNELS = {1: "Mono", 2: "Stereo", 3: "2.1", 4: "Quadraphonic", 6: "5.1 surround", 8: "7.1 surround"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _db(value: float, unit: str = "dB", signed: bool = True) -> str:
    text = f"{value:+.1f}" if signed else f"{value:.1f}"
    if text in ("+0.0", "-0.0"):
        text = "0.0"
    return text.replace("-", "−") + f" {unit}"


def _when(stamp: float | None) -> str:
    """Today at 21:05 / Yesterday at 21:05 / Tuesday at 21:05 / 3 Aug 2026."""
    if not stamp:
        return ""
    moment = time.localtime(float(stamp))
    today = time.localtime()
    days = (time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
            - time.mktime((moment.tm_year, moment.tm_mon, moment.tm_mday, 0, 0, 0, 0, 0, -1))) / 86400
    clock = f"{moment.tm_hour:02d}:{moment.tm_min:02d}"
    days = round(days)
    if days == 0:
        return f"Today at {clock}"
    if days == 1:
        return f"Yesterday at {clock}"
    if 1 < days < 7:
        return time.strftime("%A", moment) + f" at {clock}"
    return f"{moment.tm_mday} {_MONTHS[moment.tm_mon - 1]} {moment.tm_year}"


def levelling_gain(track: dict, player) -> tuple[float, str]:
    """(dB, how) of the levelling Mistery applies to `track` right now.

    The same choice MusicPlayer._chain_for makes: album loudness for an album
    played in order, the song's own when shuffled or mixed, a ReplayGain tag
    until the file is measured, capped by the peak so nothing clips.
    """
    target = loudness.target_db(player.normalize)
    album_mode = player.match_mode == "album"
    level = track.get("album_loudness") if album_mode else track.get("loudness")
    ceiling = track.get("album_peak") if album_mode else track.get("peak")
    tag = track.get("rg_album") if album_mode else track.get("rg_track")
    if level is None:
        level, ceiling = track.get("loudness"), track.get("peak")
    gain = loudness.gain_for(level, ceiling, target, tag)
    if target is None:
        return 0.0, "Volume matching is off: played as mastered"
    name = loudness.TARGET_LABELS.get(player.normalize, player.normalize).split(" — ")[0]
    basis = "album levelling" if album_mode else "track levelling"
    if level is None and tag is None:
        return 0.0, "Not measured yet"
    source = "" if level is not None else ", from its ReplayGain tag until measured"
    return gain, f"{basis.capitalize()} to {name} ({_db(target, 'LUFS', signed=False).replace('.0 ', ' ')}){source}"


class DetailsPanel(QWidget):
    """Everything known about the file playing, in two columns.

    Read from the library when the tab is on screen (a single-row query), and
    again when the song, its like, or the sound settings change while it is.
    """

    SECTIONS = (
        ("AUDIO", ("format", "sample_rate", "bit_depth", "bitrate", "channels", "duration", "size")),
        ("LOUDNESS", ("loudness", "peak", "album_loudness", "gain", "effects")),
        ("LISTENING", ("play_count", "last_played", "added_at", "liked_at")),
        ("FILE", ("location", "state")),
    )
    NAMES = {
        "format": "Format", "sample_rate": "Sample rate", "bit_depth": "Bit depth",
        "bitrate": "Bitrate", "channels": "Channels", "duration": "Duration", "size": "File size",
        "loudness": "Measured loudness", "peak": "True peak", "album_loudness": "Album",
        "gain": "Mistery applies", "effects": "Sound effects",
        "play_count": "Plays", "last_played": "Last played", "added_at": "Added",
        "liked_at": "Liked", "location": "Location", "state": "Status",
    }

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._details: dict | None = None
        self._stale = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 6, 18, 18)
        layout.setSpacing(0)

        heading = QLabel("Details")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        self._subtitle = ElidedLabel()
        self._subtitle.setStyleSheet("color: rgba(255,255,255,0.66); font-size: 10pt;")
        layout.addWidget(self._subtitle)

        self._values: dict[str, QWidget] = {}       # the widget showing each row's value
        self._names: dict[str, QLabel] = {}
        self._plain: dict[str, str] = {}             # each row's text, without markup
        self._captions: list[tuple[QLabel, tuple[str, ...]]] = []
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(9)
        grid.setColumnMinimumWidth(0, 150)
        grid.setColumnStretch(1, 1)
        row = 0
        for caption_text, keys in self.SECTIONS:
            caption = _caption(caption_text, " padding-top: 20px;")
            grid.addWidget(caption, row, 0, 1, 2)
            self._captions.append((caption, keys))
            row += 1
            for key in keys:
                name = QLabel(self.NAMES[key])
                name.setStyleSheet("color: rgba(255,255,255,0.62); font-size: 10pt;")
                name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                grid.addWidget(name, row, 0)
                if key == "location":
                    value = self._location_row()
                else:
                    value = QLabel()
                    value.setWordWrap(True)
                    value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                    value.setStyleSheet(f"color: {C.TEXT}; font-size: 10pt;")
                grid.addWidget(value, row, 1)
                self._values[key] = value
                self._names[key] = name
                row += 1
        layout.addLayout(grid)
        layout.addStretch(1)

        player.track_changed.connect(lambda _track: self._invalidate())
        player.track_updated.connect(lambda _track: self._invalidate())
        player.sound_changed.connect(self._invalidate)
        player.state_changed.connect(self._on_state)

    def _location_row(self) -> QWidget:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self._path = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        self._path.setStyleSheet(f"color: {C.TEXT}; font-size: 10pt;")
        column.addWidget(self._path)
        self._reveal = QPushButton("  Show in folder")
        self._reveal.setIcon(icon_pixmap("folder", 16, C.TEXT, 2.0))
        self._reveal.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reveal.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.18);"
            f" border-radius: 6px; padding: 6px 14px; color: {C.TEXT}; font-size: 9pt; font-weight: 600; }}"
            " QPushButton:hover { background: rgba(255,255,255,0.14); border-color: rgba(255,255,255,0.32); }")
        self._reveal.clicked.connect(self._reveal_file)
        column.addWidget(self._reveal, alignment=Qt.AlignmentFlag.AlignLeft)
        return holder

    @property
    def details(self) -> dict | None:
        return dict(self._details) if self._details else None

    def value(self, key: str) -> str:
        """A row's text as shown (without markup), "" for a row that is hidden."""
        return self._plain.get(key, "")

    def _invalidate(self) -> None:
        self._stale = True
        if self.isVisible():
            self.refresh()

    def _on_state(self) -> None:
        # Shuffle switches album levelling to track levelling: the gain row changes.
        if self._details is not None and self.isVisible():
            self._fill_gain()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Play counts and "last played" move on while the tab is hidden; a
        # one-row read is cheaper than tracking them.
        self._stale = True
        self.refresh()

    def _reveal_file(self) -> None:
        if self._details and self._details.get("path"):
            reveal_in_explorer(self._details["path"])

    def refresh(self) -> None:
        self._stale = False
        current = self._player.current
        details = None
        if current is not None and current.get("id") is not None:
            try:
                details = library.track_details(int(current["id"]))
            except Exception:
                details = None
            if details is None:
                details = dict(current)     # not in the library any more: show what the queue knows
        self._details = details
        if details is None:
            self._subtitle.setText("Nothing playing")
            for key in self._values:
                self._set(key, "")
            return
        artist = details.get("artist") or details.get("album_artist") or ""
        album = details.get("album_title") or ""
        self._subtitle.setText("  ·  ".join(part for part in (details.get("title") or "", artist, album) if part))

        label, detail = quality_label(details.get("codec"), details.get("sample_rate"),
                                      details.get("bit_depth"), details.get("bitrate"))
        codec = (details.get("codec") or "").upper()
        self._set("format", f"{label}  ·  {codec}" if codec and codec.lower() != label.lower() else label)
        rate = details.get("sample_rate")
        self._set("sample_rate", f"{rate / 1000:g} kHz" if rate else "")
        depth = details.get("bit_depth")
        self._set("bit_depth", f"{depth}-bit" if depth else "")
        bitrate = details.get("bitrate")
        self._set("bitrate", f"{round(bitrate / 1000):,} kbps" if bitrate else "")
        channels = details.get("channels")
        self._set("channels", _CHANNELS.get(int(channels), f"{int(channels)} channels") if channels else "")
        duration = details.get("duration")
        self._set("duration", fmt_clock(duration) if duration else "")
        size = details.get("size")
        self._set("size", fmt_size(size) if size else "")

        level = details.get("loudness")
        self._set("loudness", _db(level, "LUFS", signed=False) if level is not None else "Not measured yet")
        # Peaks keep their sign: a true peak above 0 dBFS (a hot master's
        # inter-sample overs) is exactly what the limiter is there for.
        peak = details.get("peak")
        self._set("peak", _db(peak, "dBFS") if peak is not None else "")
        album_level = details.get("album_loudness")
        album_peak = details.get("album_peak")
        album_text = ""
        if album_level is not None:
            album_text = _db(album_level, "LUFS", signed=False)
            if album_peak is not None:
                album_text += f"  ·  peak {_db(album_peak, 'dBFS')}"
        self._set("album_loudness", album_text)
        self._fill_gain()

        plays = int(details.get("play_count") or 0)
        self._set("play_count", "Not played yet" if plays == 0 else (f"{plays:,} play" + ("" if plays == 1 else "s")))
        self._set("last_played", _when(details.get("last_played")))
        self._set("added_at", _when(details.get("added_at")))
        self._set("liked_at", _when(details.get("liked_at")) if details.get("liked") else "")

        path = details.get("path") or ""
        self._path.setText(path)
        self._path.setToolTip(path)
        self._set("location", path)
        state = []
        if details.get("missing"):
            state.append("The file is missing: it was moved or deleted since the last scan")
        elif details.get("state") and details.get("state") != "ready":
            state.append(f"Not ready ({details['state']})")
        self._set("state", "  ·  ".join(state))

    def _fill_gain(self) -> None:
        details = self._details
        if not details:
            return
        gain, how = levelling_gain(details, self._player)
        self._set("gain", _db(gain), how)
        state = audio_fx.current()
        total = audio_fx.level_db(state, gain)
        effects = audio_fx.describe(state)
        if audio_fx.is_active(state):
            self._set("effects", effects, f"{_db(total)} in all, with a limiter so nothing clips")
        else:
            self._set("effects", effects)

    def _set(self, key: str, text: str, note: str = "") -> None:
        """Show a row (with an optional dimmer second line), or hide it when empty."""
        widget = self._values.get(key)
        if widget is None:
            return
        visible = bool(text)
        self._plain[key] = f"{text}\n{note}" if visible and note else (text if visible else "")
        if isinstance(widget, QLabel):
            if note:
                widget.setTextFormat(Qt.TextFormat.RichText)
                widget.setText(f"{html_escape(text)}<br><span style='color: rgba(255,255,255,0.58);"
                               f" font-size: 9pt;'>{html_escape(note)}</span>")
            else:
                widget.setTextFormat(Qt.TextFormat.PlainText)
                widget.setText(text)
        widget.setVisible(visible)
        self._names[key].setVisible(visible)
        for caption, keys in self._captions:
            if key in keys:
                caption.setVisible(any(self._plain.get(k) for k in keys))


# --- the bar ----------------------------------------------------------------------------

class NowPlayingBar(QWidget):
    expand_requested = Signal(str)       # "" | "lyrics" | "queue" | "details"
    artist_requested = Signal(str)
    album_requested = Signal(int)

    # The two sides are the same width so the transport stays centred in the
    # window; they give up space (the slider first) on a narrow window.
    _SIDE_WIDE = 364
    _SIDE_NARROW = 318

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._colours = library.parse_palette(None)
        self._shown_second = -1
        self.setFixedHeight(90)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 22, 10)
        layout.setSpacing(16)

        self._left = left = QWidget()
        left.setFixedWidth(self._SIDE_WIDE)
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)
        self._cover = ArtView(62, 62, radius=6)
        self._cover.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cover.mousePressEvent = lambda event: self.expand_requested.emit("")
        left_layout.addWidget(self._cover)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addStretch(1)
        self._title = ElidedLabel()
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 10.5pt; font-weight: 700;")
        self._title.setCursor(Qt.CursorShape.PointingHandCursor)
        self._title.mousePressEvent = lambda event: self._open_album()
        text.addWidget(self._title)
        self._artist = ElidedLabel()
        self._artist.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9.5pt;")
        self._artist.setCursor(Qt.CursorShape.PointingHandCursor)
        self._artist.mousePressEvent = lambda event: self._open_artist()
        text.addWidget(self._artist)
        text.addStretch(1)
        left_layout.addLayout(text, 1)
        self.like = LikeButton(player, size=34, icon_size=19)
        left_layout.addWidget(self.like, 0, Qt.AlignmentFlag.AlignVCenter)
        left_layout.addSpacing(8)
        layout.addWidget(left)

        centre = QWidget()
        centre.setMaximumWidth(660)
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        centre_layout.setSpacing(0)
        transport, self._buttons = _transport(player, 40)
        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addLayout(transport)
        wrap.addStretch(1)
        centre_layout.addLayout(wrap)
        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        self._elapsed = QLabel("0:00")
        self._elapsed.setFixedWidth(46)
        self._elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._elapsed.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 8.5pt;")
        seek_row.addWidget(self._elapsed)
        self._seek = SeekBar()
        self._seek.setFixedHeight(18)
        self._seek.seek_requested.connect(player.seek)
        seek_row.addWidget(self._seek, 1)
        self._total = ClickableLabel("0:00")
        self._total.setFixedWidth(46)
        self._total.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 8.5pt;")
        _time_toggle(self._total, self._refresh_times)
        seek_row.addWidget(self._total)
        centre_layout.addLayout(seek_row)
        layout.addWidget(centre, 1)

        self._right = right = QWidget()
        right.setFixedWidth(self._SIDE_WIDE)
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)
        right_layout.addStretch(1)
        self._lyrics_button = IconButton("lyrics", size=36, icon_size=19, tooltip="Lyrics")
        self._lyrics_button.clicked.connect(lambda: self.expand_requested.emit("lyrics"))
        right_layout.addWidget(self._lyrics_button)
        queue = IconButton("queue", size=36, icon_size=19, tooltip="Up next")
        queue.clicked.connect(lambda: self.expand_requested.emit("queue"))
        right_layout.addWidget(queue)
        right_layout.addSpacing(4)
        right_layout.addWidget(_sound_button(player, self))
        self.sleep = SleepTimerButton(player, size=36, icon_size=19)
        right_layout.addWidget(self.sleep)
        right_layout.addSpacing(2)
        volume_row, _, self._volume = _volume_row(player, 104)
        right_layout.addLayout(volume_row)
        right_layout.addSpacing(4)
        expand = IconButton("fullscreen", size=36, icon_size=17, tooltip="Now Playing")
        expand.clicked.connect(lambda: self.expand_requested.emit(""))
        right_layout.addWidget(expand)
        layout.addWidget(right)

        player.track_changed.connect(self._on_track)
        player.state_changed.connect(self._on_state)
        player.position_changed.connect(self._on_position)
        self._on_track(player.current)
        self._on_state()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        wide = self.width() >= 1180
        side = self._SIDE_WIDE if wide else self._SIDE_NARROW
        if self._left.width() != side:
            self._left.setFixedWidth(side)
            self._right.setFixedWidth(side)
            self._volume.setFixedWidth(104 if wide else 84)
            self._lyrics_button.setVisible(wide)

    def _on_track(self, track) -> None:
        if not track:
            return
        self._title.setText(track.get("title") or "")
        self._artist.setText(track.get("artist") or track.get("album_artist") or "")
        self._cover.set_art((track.get("cover") or "").replace(".jpg", "-sm.jpg") or None,
                            track.get("album_title") or track.get("title") or "")
        self._colours = library.parse_palette(track.get("palette"))
        self._seek.set_accent(self._colours["accent"])
        self._seek.set_duration(float(track.get("duration") or 0))
        self._total.setText(_total_text(0.0, float(track.get("duration") or 0)))
        self._shown_second = -1
        self.update()

    def _on_state(self) -> None:
        _sync_transport(self._buttons, self._player)

    def _on_position(self, position: float, duration: float) -> None:
        if not self.isVisible():
            return                      # caught up in showEvent instead
        if duration:
            self._seek.set_duration(duration)
        self._seek.set_position(position)
        # Labels only when the displayed second changes: position arrives many
        # times a second and a label repaint each time is waste.
        second = int(position)
        if second != self._shown_second:
            self._shown_second = second
            self._elapsed.setText(fmt_clock(position))
            if duration:
                self._total.setText(_total_text(position, duration))

    def _refresh_times(self) -> None:
        self._shown_second = -1
        self._on_position(self._player.position, self._player.duration)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._shown_second = -1
        self._on_position(self._player.position, self._player.duration)

    def _open_album(self) -> None:
        current = self._player.current
        if current and current.get("album_id"):
            self.album_requested.emit(int(current["album_id"]))

    def _open_artist(self) -> None:
        current = self._player.current
        # A friend's artist has no page in this library: their album is where
        # "Playing from" goes.
        if current and not current.get("friend"):
            self.artist_requested.emit(current.get("album_artist_name") or current.get("album_artist")
                                       or current.get("artist") or "")

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = QRectF(self.rect())
        painter.fillRect(rect, QColor(C.BG_ELEV))
        tint = QLinearGradient(0, 0, rect.width() * 0.55, 0)
        dark = QColor(self._colours["dark"])
        dark.setAlpha(235)
        tint.setColorAt(0.0, dark)
        dark.setAlpha(0)
        tint.setColorAt(1.0, dark)
        painter.fillRect(rect, tint)
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1))
        painter.drawLine(0, 0, int(rect.width()), 0)


# --- the full view -------------------------------------------------------------------------

class NowPlayingView(QWidget):
    back_requested = Signal()
    artist_requested = Signal(str)
    album_requested = Signal(int)
    # "Playing from" something that is not an album, an artist or the queue:
    # {"kind": "liked" | "songs" | "search" | ..., "title", "id"}. The window opens it.
    context_requested = Signal(dict)
    # The lyrics screensaver asking the window for the whole screen, and giving
    # it back. See ui/screensaver.py.
    fullscreen_requested = Signal(bool)

    TABS = ("lyrics", "queue", "details")

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._colours = library.parse_palette(None)
        self._blurred = QImage()
        self._blurred_for: str | None = None
        self._background: QPixmap | None = None
        self._background_key: tuple | None = None
        self._shown_second = -1
        self._cover_style = ""
        self._asked_fullscreen = False
        self._was_maximized = False

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 24, 48, 30)
        root.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(0)
        back = IconButton("chevron_down", size=42, icon_size=24, tooltip="Close Now Playing  (Esc)")
        back.clicked.connect(self.back_requested.emit)
        top.addWidget(back, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addSpacing(12)
        self.playing_from = PlayingFrom(player)
        self.playing_from.album_requested.connect(self.album_requested)
        self.playing_from.artist_requested.connect(self.artist_requested)
        self.playing_from.context_requested.connect(self.context_requested)
        self.playing_from.queue_requested.connect(lambda: self.show_tab("queue"))
        top.addWidget(self.playing_from, 1, Qt.AlignmentFlag.AlignVCenter)
        top.addSpacing(16)
        self._tabs = QButtonGroup(self)
        self._tabs.setExclusive(True)
        for index, label in enumerate(("Lyrics", "Up next", "Details")):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setChecked(index == 0)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            self._tabs.addButton(chip, index)
            if index:
                top.addSpacing(8)
            top.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self._tabs.idClicked.connect(lambda index: self._side.setCurrentIndex(index))
        # The row lives in a widget of its own only so the screensaver has
        # something to hide: a bare layout cannot be turned off, and the sleep
        # timer's one-second clock is in here.
        self._top_row = QWidget()
        self._top_row.setLayout(top)
        root.addWidget(self._top_row)

        body = QHBoxLayout()
        body.setSpacing(56)

        left = QVBoxLayout()
        left.setSpacing(6)
        self._cover = CoverView()
        left.addWidget(self._cover, 1)
        self.vinyl = VinylView()
        left.addWidget(self.vinyl, 1)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._title = ElidedLabel()
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 20pt; font-weight: 800;")
        title_row.addWidget(self._title, 1)
        self.like = LikeButton(player, size=40, icon_size=24)
        title_row.addWidget(self.like, 0, Qt.AlignmentFlag.AlignVCenter)
        left.addLayout(title_row)
        self._subtitle = ElidedLabel()
        self._subtitle.setStyleSheet(f"color: rgba(255,255,255,0.78); font-size: 11.5pt;")
        self._subtitle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._subtitle.mousePressEvent = self._open_from_subtitle
        left.addWidget(self._subtitle)
        self._quality = QLabel()
        left.addWidget(self._quality, alignment=Qt.AlignmentFlag.AlignLeft)
        left.addSpacing(10)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        self._elapsed = QLabel("0:00")
        self._elapsed.setStyleSheet("color: rgba(255,255,255,0.72); font-size: 9pt;")
        self._elapsed.setFixedWidth(48)
        self._elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self._elapsed)
        self._seek = SeekBar()
        self._seek.seek_requested.connect(player.seek)
        seek_row.addWidget(self._seek, 1)
        self._total = ClickableLabel("0:00")
        self._total.setStyleSheet("color: rgba(255,255,255,0.72); font-size: 9pt;")
        self._total.setFixedWidth(48)
        _time_toggle(self._total, self._refresh_times)
        seek_row.addWidget(self._total)
        left.addLayout(seek_row)

        transport, self._buttons = _transport(player, 58)
        transport_wrap = QHBoxLayout()
        transport_wrap.addStretch(1)
        transport_wrap.addLayout(transport)
        transport_wrap.addStretch(1)
        left.addLayout(transport_wrap)

        # Volume, Sound and the sleep timer, centred. The time left on a sleep
        # timer comes and goes beside the moon; an equal space on the far side
        # keeps the row from jumping sideways when it does.
        volume_row, _, self._volume = _volume_row(player)
        volume_wrap = QHBoxLayout()
        volume_wrap.setSpacing(4)
        volume_wrap.addStretch(1)
        volume_wrap.addSpacing(42)
        volume_wrap.addLayout(volume_row)
        volume_wrap.addSpacing(6)
        volume_wrap.addWidget(_sound_button(player, self))
        self.sleep = SleepTimerButton(player)
        volume_wrap.addWidget(self.sleep)
        self._sleep_left = QLabel()
        self._sleep_left.setFixedWidth(42)
        self._sleep_left.setStyleSheet(f"color: {C.ACCENT}; font-size: 8.5pt; font-weight: 700;")
        self.sleep.remaining_changed.connect(self._sleep_left.setText)
        self._sleep_left.setText(self.sleep.remaining_text)
        volume_wrap.addWidget(self._sleep_left)
        volume_wrap.addStretch(1)
        left.addLayout(volume_wrap)

        left.addSpacing(8)
        self.next_up = NextUpCard(player)
        left.addWidget(self.next_up)

        self._left_holder = QWidget()
        self._left_holder.setLayout(left)
        self._left_holder.setMaximumWidth(520)
        self._left_holder.setMinimumWidth(340)
        body.addWidget(self._left_holder, 5)

        self._side = QStackedWidget()
        self.lyrics = LyricsView()
        self.lyrics.seek_requested.connect(player.seek)
        self._side.addWidget(self.lyrics)
        self.queue = QueuePanel(player)
        self._side.addWidget(self.queue)
        self.details = DetailsPanel(player)
        self._side.addWidget(self.details)
        body.addWidget(self._side, 6)
        root.addLayout(body, 1)

        player.track_changed.connect(self._on_track)
        player.state_changed.connect(self._on_state)
        player.position_changed.connect(self._on_position)
        # The screensaver covers this page rather than replacing it, so
        # _on_position keeps running and the lyrics keep advancing behind it.
        # Its lyrics view is built here because it is this module that owns the
        # class; screensaver.py importing it back would be a cycle.
        self.screensaver = ScreensaverView(player, LyricsView(),
                                           ready=self._screensaver_ready, parent=self)
        self.screensaver.entered.connect(self._on_screensaver_entered)
        self.screensaver.left.connect(self._on_screensaver_left)
        self.lyrics.lyrics_changed.connect(self.screensaver.lyrics.set_lyrics)
        self.lyrics.status_changed.connect(self.screensaver.lyrics.set_status)
        # F11 starts it without waiting out the idle clock, which is what F11
        # does on a film. A window shortcut rather than a key event: this page
        # hardly ever holds the focus itself.
        self._saver_key = QShortcut(QKeySequence(Qt.Key.Key_F11), self,
                                    activated=self.start_screensaver)
        self._saver_key.setEnabled(False)

        self.set_cover_style(str(settings.get("music_cover_style", "disc")))
        self._on_track(player.current)
        _sync_transport(self._buttons, player)
        self.vinyl.set_playing(player.is_playing, animate=False)
        self.lyrics.set_playing(player.is_playing)

    def show_tab(self, name: str) -> None:
        index = self.TABS.index(name) if name in self.TABS else 0
        self._tabs.button(index).setChecked(True)
        self._side.setCurrentIndex(index)

    @property
    def current_tab(self) -> str:
        return self.TABS[self._side.currentIndex()]

    def start_screensaver(self) -> None:
        """F11, or the idle clock running out.

        The setting is checked here as well as in ScreensaverView._eligible:
        only the idle path went through _eligible, so F11 took over the screen
        even for somebody who had turned the screensaver off.

        There has to be a song to look at — an empty record on a black screen is
        not a screensaver — but it does not have to be playing. The idle clock
        still asks for that (walking away from a paused player is not the same
        as leaving the music on), while F11 is somebody deciding: PAUSED_DIM and
        PAUSED_AFTER exist for exactly this picture.
        """
        if not settings.get("music_screensaver", True):
            return
        if self._player.current and self.isVisible():
            self.show_tab("lyrics")
            self.screensaver.enter()

    def _screensaver_ready(self) -> bool:
        return _on_screen(self) and self.current_tab == "lyrics"

    def _on_screensaver_entered(self) -> None:
        # Hiding these three is what stops everything behind the overlay:
        # VinylView's 33 ms timer, the lyrics' breathing dots and the sleep
        # timer's clock all stop in their own hideEvents.
        self._asked_fullscreen = not self.window().isFullScreen()
        # Remembered for the minimised case in _on_screensaver_left, which has
        # to put the state back itself rather than go through the window.
        self._was_maximized = self.window().isMaximized()
        self._top_row.setVisible(False)
        self._left_holder.setVisible(False)
        self._side.setVisible(False)
        if self._asked_fullscreen:
            self.fullscreen_requested.emit(True)

    def _on_screensaver_left(self) -> None:
        self._top_row.setVisible(True)
        self._left_holder.setVisible(True)
        self._side.setVisible(True)
        self._shown_second = -1
        self._on_position(self._player.position, self._player.duration)
        if self._asked_fullscreen:
            # Only if it was this that asked: a film already full screen, or a
            # window the user put there, must come back exactly as it was.
            self._asked_fullscreen = False
            window = self.window()
            if window is not None and window.isMinimized():
                # Minimising is one of the things that ends the screensaver —
                # through this view's own hideEvent — and the window's handler
                # leaves full screen with showNormal()/showMaximized(), either
                # of which un-minimises. The window somebody just put away came
                # straight back on screen, full size and lit. Drop the
                # full-screen bit in place instead, keeping the minimised bit
                # and whatever the window was before it went full screen.
                state = window.windowState() & ~Qt.WindowState.WindowFullScreen
                if self._was_maximized:
                    state |= Qt.WindowState.WindowMaximized
                window.setWindowState(state)
            else:
                self.fullscreen_requested.emit(False)

    @property
    def cover_style(self) -> str:
        return self._cover_style

    def set_cover_style(self, style: str) -> None:
        """"disc" (the turning record) or "cover" (the flat square), switched at once.

        Also re-read from settings every time the view is shown, which is when
        a change made on the Settings page first matters.
        """
        style = "cover" if style == "cover" else "disc"
        if style == self._cover_style:
            return
        self._cover_style = style
        disc = style == "disc"
        self.vinyl.setVisible(disc)
        self._cover.setVisible(not disc)
        current = self._player.current
        (self.vinyl if disc else self._cover).set_cover(current.get("cover") if current else None)

    def _on_state(self) -> None:
        _sync_transport(self._buttons, self._player)
        self.vinyl.set_playing(self._player.is_playing)
        self.lyrics.set_playing(self._player.is_playing)

    def _on_track(self, track) -> None:
        if not track:
            return
        self._title.setText(track.get("title") or "")
        album = track.get("album_title") or track.get("album") or ""
        artist = track.get("artist") or track.get("album_artist") or ""
        self._subtitle.setText("  ·  ".join(b for b in (artist, album) if b))
        (self.vinyl if self._cover_style == "disc" else self._cover).set_cover(track.get("cover"))
        self._colours = library.parse_palette(track.get("palette"))
        self.vinyl.set_accent(self._colours["accent"])
        duration = float(track.get("duration") or 0)
        self.vinyl.set_progress(0.0, duration)
        self._seek.set_accent(self._colours["accent"])
        self.queue.set_accent(self._colours["accent"])
        self.lyrics.set_accent(self._colours["accent"])
        self._seek.set_duration(duration)
        self._total.setText(_total_text(0.0, duration))
        label, detail = quality_label(track.get("codec"), track.get("sample_rate"),
                                      track.get("bit_depth"), track.get("bitrate"))
        self._quality.setText(f"{label}  ·  {detail}" if detail else label)
        self._quality.setStyleSheet(
            f"background: rgba(255,255,255,0.10); border: 1px solid rgba(255,255,255,0.28);"
            f"border-radius: 4px; padding: 3px 10px; color: {C.TEXT}; font-size: 8.5pt; font-weight: 700;")
        self._quality.setVisible(bool(track.get("codec")))
        self._shown_second = -1
        self._build_backdrop(track.get("cover"))
        self._background = None             # new colours, new cover: compose again
        self.lyrics.set_status("Finding lyrics…")
        self.update()

    def _on_position(self, position: float, duration: float) -> None:
        if not self.isVisible():
            return                      # caught up in showEvent instead
        if duration:
            self._seek.set_duration(duration)
        self._seek.set_position(position)
        self.lyrics.set_position(position)
        second = int(position)
        if second != self._shown_second:
            self._shown_second = second
            self._elapsed.setText(fmt_clock(position))
            if duration:
                self._total.setText(_total_text(position, duration))
            if self._cover_style == "disc":
                self.vinyl.set_progress(position, duration or self._player.duration)

    def _refresh_times(self) -> None:
        self._shown_second = -1
        self._on_position(self._player.position, self._player.duration)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.set_cover_style(str(settings.get("music_cover_style", "disc")))
        self._shown_second = -1
        self._on_position(self._player.position, self._player.duration)
        self.screensaver.set_watching(True)
        self._saver_key.setEnabled(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.screensaver.set_watching(False)
        self._saver_key.setEnabled(False)
        self.screensaver.leave("page hidden")

    def _open_from_subtitle(self, event) -> None:
        current = self._player.current
        if current and current.get("album_id"):
            self.album_requested.emit(int(current["album_id"]))

    def _build_backdrop(self, cover: str | None) -> None:
        """A tiny, heavily blurred copy of the cover to sit behind everything.

        Blurred at 120px and scaled up rather than blurred at full size: the
        result is indistinguishable and it costs a couple of milliseconds.
        """
        if cover == self._blurred_for:
            return
        self._blurred_for = cover
        self._blurred = QImage()
        if not cover:
            return
        try:
            from PIL import Image, ImageEnhance, ImageFilter

            with Image.open(cover) as opened:
                image = opened.convert("RGB")
            image.thumbnail((120, 120))
            image = image.filter(ImageFilter.GaussianBlur(14))
            image = ImageEnhance.Brightness(image).enhance(0.62)
            data = image.tobytes("raw", "RGB")
            self._blurred = QImage(data, image.width, image.height, image.width * 3,
                                   QImage.Format.Format_RGB888).copy()
        except Exception:
            self._blurred = QImage()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._background = None
        if self.screensaver.active:
            self.screensaver.setGeometry(self.rect())

    def _compose_background(self) -> QPixmap:
        """Gradient, blurred cover and shade, drawn once per song and size.

        The children here are transparent, so every seek-bar tick and every
        frame of the lyrics scrolling repaints this widget beneath them. Drawing
        the gradients and scaling the cover each time was measured at ~2 MP a
        second; a cached copy turns each repaint into a plain copy of the part
        that changed.
        """
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(max(1, int(self.width() * ratio)), max(1, int(self.height() * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(0, 0, self.width(), self.height())
        base = QLinearGradient(0, 0, rect.width(), rect.height())
        base.setColorAt(0.0, QColor(self._colours["mid"]))
        base.setColorAt(0.65, QColor(self._colours["dark"]))
        base.setColorAt(1.0, QColor("#050505"))
        painter.fillRect(rect, base)

        if not self._blurred.isNull():
            scale = max(rect.width() / self._blurred.width(), rect.height() / self._blurred.height())
            size = QRectF(0, 0, self._blurred.width() * scale, self._blurred.height() * scale)
            size.moveCenter(rect.center())
            painter.setOpacity(0.42)
            painter.drawImage(size, self._blurred)
            painter.setOpacity(1.0)

        # Keep text legible whatever the cover: darken towards the bottom.
        shade = QLinearGradient(0, 0, 0, rect.height())
        shade.setColorAt(0.0, QColor(0, 0, 0, 40))
        shade.setColorAt(1.0, QColor(0, 0, 0, 150))
        painter.fillRect(rect, shade)
        painter.end()
        return pixmap

    def paintEvent(self, event) -> None:
        if self._background is None or self._background_key != (self.size(), self.devicePixelRatioF()):
            self._background = self._compose_background()
            self._background_key = (self.size(), self.devicePixelRatioF())
        painter = QPainter(self)
        area = QRectF(event.rect())
        ratio = self.devicePixelRatioF()
        painter.drawPixmap(area, self._background,
                           QRectF(area.x() * ratio, area.y() * ratio,
                                  area.width() * ratio, area.height() * ratio))
