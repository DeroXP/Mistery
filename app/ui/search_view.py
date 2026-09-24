"""Search: one big field for everything, and somewhere to start.

Two letters or more look through your films, shows, episodes and music, and
through what your friends share (search_data.find): the best match on a big
card on the left, the rest grouped beside it, with chips to keep to one kind.
Before you type: what you looked for lately, six moods made from your own
library, and Surprise me, which opens a film you haven't started that fits the
hour.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from ..images import art_pixmap, load_async
from ..util import elide, fmt_clock
from . import search_data as data
from .theme import C, display_family, ui_font
from .widgets.flow import FlowLayout
from .widgets.icons import IconButton, icon_pixmap, paint_icon

GROUP_CAP = 8               # rows a group shows before "Show all"
TOP_W = 420                 # the top result's card


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


class _Warmth(QVariantAnimation):
    """0 to 1 and back on hover (or focus), for a widget that paints with it."""

    def __init__(self, owner: QWidget, ms: int = 180) -> None:
        super().__init__(owner)
        self.value_now = 0.0
        self.setDuration(ms)
        self.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.valueChanged.connect(self._on_value)
        self._owner = owner

    def toward(self, target: float) -> None:
        self.stop()
        self.setStartValue(self.value_now)
        self.setEndValue(target)
        self.start()

    def _on_value(self, value) -> None:
        self.value_now = float(value)
        self._owner.update()


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(inner)
    return area


def _heading(text: str, size: int = 16) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f'color: {C.TEXT}; font-family: "{display_family()}"; font-size: {size}pt; font-weight: 700;')
    return label


# --- the field ----------------------------------------------------------------------------------


class _FieldBox(QWidget):
    """The big field: a rounded box whose edge glows yellow while it has the focus."""

    RING = 6

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(76 + 2 * self.RING)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("SearchField")
        self.edit.setPlaceholderText("Films, shows, episodes, music, and your friends' libraries")
        self.edit.setAccessibleName("Search")
        self.edit.setStyleSheet(
            f"QLineEdit#SearchField {{ background: transparent; border: none; padding: 0; margin: 0;"
            f" font-size: 17pt; font-weight: 500; color: {C.TEXT};"
            f" selection-background-color: {C.ACCENT}; selection-color: {C.ON_ACCENT}; }}")
        self.clear = IconButton("close", size=40, icon_size=18, tooltip="Clear the search", parent=self)
        self.clear.setVisible(False)
        row = QHBoxLayout(self)
        row.setContentsMargins(self.RING + 66, self.RING, self.RING + 18, self.RING)
        row.setSpacing(12)
        row.addWidget(self.edit, 1)
        row.addWidget(self.clear, 0, Qt.AlignmentFlag.AlignVCenter)
        self.edit.textChanged.connect(lambda text: self.clear.setVisible(bool(text)))
        self.clear.clicked.connect(self._clear)
        self._glow = _Warmth(self, 200)
        self.edit.installEventFilter(self)

    def _clear(self) -> None:
        self.edit.clear()
        self.edit.setFocus()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.edit and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self._glow.toward(1.0 if event.type() == QEvent.Type.FocusIn else 0.0)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.edit.setFocus()                 # the whole box is the field, glass and all
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        glow = self._glow.value_now
        ring = self.RING
        box = QRectF(ring, ring, self.width() - 2 * ring, self.height() - 2 * ring)
        if glow > 0:
            halo = QColor(C.ACCENT)
            halo.setAlpha(round(40 * glow))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo)
            painter.drawRoundedRect(box.adjusted(-ring, -ring, ring, ring), 24 + ring, 24 + ring)
        painter.setBrush(QColor("#161311"))
        painter.setPen(QPen(_mix(QColor("#2B2520"), QColor(C.ACCENT), glow), 2))
        painter.drawRoundedRect(box.adjusted(1, 1, -1, -1), 23, 23)
        paint_icon(painter, "search", QRectF(ring + 26, (self.height() - 26) / 2, 26, 26),
                   _mix(QColor(C.TEXT_FAINT), QColor(C.ACCENT), glow), 2.0)


# --- before you type ----------------------------------------------------------------------------


class _MoodTile(QAbstractButton):
    """One mood: its colours, its name, what it means, and how many there are.
    Under the pointer it lifts, with a yellow edge."""

    LIFT = 4

    def __init__(self, mood: data.Mood, parent=None) -> None:
        super().__init__(parent)
        self.mood = mood
        self._count = 0
        self._warm = _Warmth(self, 240)
        self.setFixedHeight(128 + 2 * self.LIFT)
        self.setMinimumWidth(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{mood.name}: {mood.line}")

    def set_count(self, count: int) -> None:
        self._count = count
        self.setToolTip(f"{count} to watch" if count else "Nothing in your library fits this yet")
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._warm.toward(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._warm.toward(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        warm = self._warm.value_now
        card = QRectF(self.rect()).adjusted(3, self.LIFT, -3, -self.LIFT)
        card.translate(0, -self.LIFT * warm)
        path = QPainterPath()
        path.addRoundedRect(card, 22, 22)
        start, end = (QColor(colour) for colour in self.mood.colours)
        fill = QLinearGradient(card.topLeft(), card.bottomRight())
        fill.setColorAt(0.0, start.lighter(100 + round(12 * warm)))
        fill.setColorAt(1.0, end)
        painter.fillPath(path, fill)

        painter.save()
        painter.setClipPath(path)
        paint_icon(painter, self.mood.icon, QRectF(card.right() - 92, card.bottom() - 84, 86, 86),
                   QColor(246, 240, 230, round(44 + 30 * warm)), 1.5)
        painter.restore()

        painter.setPen(QColor(C.TEXT))
        painter.setFont(QFont(display_family(), 19, QFont.Weight.Bold))
        painter.drawText(QRectF(card.left() + 22, card.top() + 18, card.width() - 44, 34),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.mood.name)
        painter.setPen(QColor("#E8E0D4"))
        painter.setFont(ui_font(10))
        painter.drawText(QRectF(card.left() + 22, card.top() + 54, card.width() - 110, 40),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                         self.mood.line)
        words = str(self._count) if self._count else "none yet"
        painter.setFont(ui_font(9, QFont.Weight.DemiBold))
        width = QFontMetrics(painter.font()).horizontalAdvance(words) + 20
        pill = QRectF(card.left() + 22, card.bottom() - 36, width, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 70))
        painter.drawRoundedRect(pill, 11, 11)
        painter.setPen(QColor(246, 240, 230, 220))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, words)

        if warm > 0:
            edge = QColor(C.ACCENT)
            edge.setAlpha(round(255 * warm))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(edge, 2))
            painter.drawRoundedRect(card.adjusted(1, 1, -1, -1), 21, 21)


# --- results ------------------------------------------------------------------------------------


def _thumb_size(hit: data.Hit) -> tuple[int, int]:
    kind = getattr(hit.item, "kind", "") if hit.kind == "friend" else hit.kind
    if kind in ("episode",):
        return 86, 52
    if kind in ("song", "album", "artist", "track"):
        return 52, 52
    return 38, 52                       # a poster: film, show


class _Row(QAbstractButton):
    """One result: its picture, its name and what it is. Under the pointer it
    lifts a little and a round Play comes up on the right, when it plays."""

    open_hit = Signal(object)
    play_hit = Signal(object)

    def __init__(self, hit: data.Hit, parent=None) -> None:
        super().__init__(parent)
        self.hit = hit
        self._warm = _Warmth(self, 200)
        self.setFixedHeight(76)
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{hit.title}, {hit.sub}")
        self.clicked.connect(lambda: self.open_hit.emit(self.hit))
        width, height = _thumb_size(hit)
        radius = 26 if hit.kind == "artist" else 10
        self._pixmap = (art_pixmap(hit.art, hit.title, width, height, radius, self.devicePixelRatioF(),
                                   self._on_art) if hit.art else None)

    def _on_art(self, pixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._warm.toward(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._warm.toward(0.0)
        super().leaveEvent(event)

    def _play_rect(self) -> QRectF:
        return QRectF(self.width() - 16 - 36, (self.height() - 36) / 2, 36, 36)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if (event.button() == Qt.MouseButton.LeftButton and self.hit.playable
                and self._play_rect().contains(event.position())):
            self.setDown(False)
            self.play_hit.emit(self.hit)
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                               | QPainter.RenderHint.TextAntialiasing)
        warm = max(self._warm.value_now, 1.0 if self.hasFocus() else 0.0)
        card = QRectF(0, 2 - 2 * warm, self.width(), 72)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_mix(QColor("#161311"), QColor("#221E1A"), warm))
        painter.drawRoundedRect(card, 18, 18)

        width, height = _thumb_size(self.hit)
        thumb = QRectF(card.left() + 10, card.center().y() - height / 2, width, height)
        if self._pixmap is not None and not self._pixmap.isNull():
            painter.drawPixmap(thumb.toRect(), self._pixmap)
        else:
            shade = QLinearGradient(thumb.topLeft(), thumb.bottomRight())
            shade.setColorAt(0.0, QColor("#452640"))
            shade.setColorAt(1.0, QColor("#12162A"))
            painter.setBrush(shade)
            radius = height / 2 if self.hit.kind == "artist" else 10
            painter.drawRoundedRect(thumb, radius, radius)
        if self.hit.who:
            badge = QRectF(thumb.right() - 14, thumb.bottom() - 14, 20, 20)
            painter.setBrush(QColor(C.ACCENT))
            painter.drawEllipse(badge)
            painter.setPen(QColor(C.ON_ACCENT))
            painter.setFont(ui_font(8, QFont.Weight.Black))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self.hit.who[:1].upper())

        left = thumb.right() + 16
        room = card.width() - left - (64 if self.hit.playable else 16)
        painter.setPen(QColor(C.TEXT))
        painter.setFont(ui_font(10, QFont.Weight.DemiBold))
        title = QFontMetrics(painter.font()).elidedText(self.hit.title, Qt.TextElideMode.ElideRight, int(room))
        painter.drawText(QRectF(left, card.top() + 14, room, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        painter.setPen(QColor(C.TEXT_FAINT))
        painter.setFont(ui_font(9))
        sub = QFontMetrics(painter.font()).elidedText(self.hit.sub, Qt.TextElideMode.ElideRight, int(room))
        painter.drawText(QRectF(left, card.top() + 37, room, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)

        if self.hit.playable and warm > 0:
            button = self._play_rect().translated(0, -2 * warm)
            fill = QColor(C.ACCENT)
            fill.setAlpha(round(255 * warm))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawEllipse(button)
            glyph = QColor(C.ON_ACCENT)
            glyph.setAlpha(round(255 * warm))
            paint_icon(painter, "play", button.adjusted(9, 9, -8, -9), glyph)


class _Group(QWidget):
    """One kind of result: its heading and count, and its rows, two across."""

    open_hit = Signal(object)
    play_hit = Signal(object)

    def __init__(self, group: str, hits: list[data.Hit], parent=None) -> None:
        super().__init__(parent)
        self.group = group
        self.hits = hits
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(_heading(data.GROUP_TITLES[group], 15))
        count = QLabel(str(len(hits)))
        count.setObjectName("SectionCount")
        head.addWidget(count, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        layout.addLayout(head)
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(8)
        # Two equal columns even with one row: alone, a row took both.
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        layout.addLayout(self._grid)
        self.rows: list[_Row] = []
        self._more = QPushButton(f"Show all {len(hits)}")
        self._more.setObjectName("SectionLink")
        self._more.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more.clicked.connect(self._show_all)
        layout.addWidget(self._more, 0, Qt.AlignmentFlag.AlignLeft)
        self._fill(hits[:GROUP_CAP])
        self._more.setVisible(len(hits) > GROUP_CAP)

    def _fill(self, hits: list[data.Hit]) -> None:
        for hit in hits:
            row = _Row(hit)
            row.open_hit.connect(self.open_hit.emit)
            row.play_hit.connect(self.play_hit.emit)
            index = len(self.rows)
            self._grid.addWidget(row, index // 2, index % 2)
            self.rows.append(row)

    def _show_all(self) -> None:
        self._fill(self.hits[len(self.rows):])
        self._more.setVisible(False)


class _Picture(QWidget):
    """The top result's picture, with the card's rounded top corners."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(236)
        self._image = QImage()
        self._path: str | None = None

    def set_path(self, path: str | None) -> None:
        self._path = path
        self._image = QImage()
        if path:
            load_async(path, lambda image, wanted=path: self._on_image(image, wanted))
        self.update()

    def _on_image(self, image: QImage, wanted: str) -> None:
        if wanted == self._path:
            self._image = image
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0, 0, 0, 30), 24, 24)
        painter.setClipPath(path)
        if self._image.isNull():
            shade = QLinearGradient(rect.topLeft(), rect.bottomRight())
            shade.setColorAt(0.0, QColor("#452640"))
            shade.setColorAt(1.0, QColor("#12162A"))
            painter.fillRect(rect, shade)
            return
        scale = max(rect.width() / self._image.width(), rect.height() / self._image.height())
        width, height = self._image.width() * scale, self._image.height() * scale
        painter.drawImage(QRectF((rect.width() - width) / 2, (rect.height() - height) * 0.4, width, height),
                          self._image)
        fade = QLinearGradient(0, rect.height() * 0.55, 0, rect.height())
        fade.setColorAt(0.0, QColor(22, 19, 17, 0))
        fade.setColorAt(1.0, QColor(22, 19, 17, 200))
        painter.fillRect(rect, fade)


class _TopCard(QWidget):
    """The best match, big: its picture, what it is, its name, and its buttons."""

    open_hit = Signal(object)
    play_hit = Signal(object)

    KIND_WORDS = {"film": "Film", "show": "Show", "episode": "Episode", "song": "Song", "album": "Album",
                  "artist": "Artist"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.hit: data.Hit | None = None
        self.setFixedWidth(TOP_W)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.picture = _Picture()
        layout.addWidget(self.picture)
        body = QVBoxLayout()
        body.setContentsMargins(22, 16, 22, 22)
        body.setSpacing(6)
        self.kind = QLabel()
        self.kind.setTextFormat(Qt.TextFormat.RichText)
        body.addWidget(self.kind)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet(
            f'color: {C.TEXT}; font-family: "{display_family()}"; font-size: 24pt; font-weight: 700;')
        body.addWidget(self.title)
        self.sub = QLabel()
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11pt;")
        body.addWidget(self.sub)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 10, 0, 0)
        buttons.setSpacing(10)
        self.primary = QPushButton()
        self.primary.setObjectName("Primary")
        self.primary.setFixedHeight(46)
        self.primary.setStyleSheet("padding: 0 22px 0 16px; border-radius: 22px; font-size: 11.5pt;")
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.clicked.connect(self._on_primary)
        buttons.addWidget(self.primary)
        self.secondary = QPushButton()
        self.secondary.setObjectName("Ghost")
        self.secondary.setFixedHeight(46)
        self.secondary.setStyleSheet("padding: 0 20px; border-radius: 22px; font-size: 11pt;")
        self.secondary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.secondary.clicked.connect(lambda: self.hit and self.open_hit.emit(self.hit))
        buttons.addWidget(self.secondary)
        buttons.addStretch(1)
        body.addLayout(buttons)
        layout.addLayout(body)
        self._primary_plays = True

    def set_hit(self, hit: data.Hit) -> None:
        self.hit = hit
        self.picture.set_path(hit.wide_art or hit.art)
        word = self.KIND_WORDS.get(hit.kind, "")
        if hit.kind == "friend":
            word = {"movie": "Film", "show": "Show", "album": "Album"}.get(getattr(hit.item, "kind", ""), "")
            word += f" · {hit.who}'s"
        self.kind.setText(f'<span style="color: {C.ACCENT}; font-size: 9pt; font-weight: 700; letter-spacing: 1.4px;">'
                          f"{word.upper()}</span>")
        self.title.setText(elide(hit.title, 80))
        self.sub.setText(hit.sub)
        ratio = self.devicePixelRatioF()
        resume = getattr(hit.item, "resume_position", 0) if hit.kind in ("film", "episode") else 0
        if hit.playable:
            self._primary_plays = True
            self.primary.setText(f"Resume · {fmt_clock(resume)}" if resume else
                                 ("Play album" if hit.kind == "album" else "Play"))
            self.primary.setIcon(QIcon(icon_pixmap("play", 18, C.PLAY_FG, ratio)))
            self.secondary.setText({"song": "Open album", "album": "Open album", "friend": "Open"}.get(hit.kind, "More info"))
            self.secondary.setVisible(True)
        else:
            self._primary_plays = False
            self.primary.setText({"show": "Open show", "artist": "Open artist"}.get(hit.kind, "Open"))
            self.primary.setIcon(QIcon())
            self.secondary.setVisible(False)

    def _on_primary(self) -> None:
        if self.hit is None:
            return
        (self.play_hit if self._primary_plays else self.open_hit).emit(self.hit)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#241F1B"), 1))
        painter.setBrush(QColor("#161311"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 24, 24)


# --- the page -----------------------------------------------------------------------------------


class SearchView(QWidget):
    open_media = Signal(object)             # a film or an episode: its page
    open_show = Signal(object)
    play_requested = Signal(object)         # a film or an episode: play it
    open_album = Signal(int)
    open_artist = Signal(str)
    play_music = Signal(list, int, object)  # songs, the one to start at, where they came from
    friend_open_requested = Signal(object)
    friend_play_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._filter = "all"
        self._mood: int | None = None
        self._hits: list[data.Hit] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(46, 24, 46, 0)
        outer.setSpacing(12)

        self._box = _FieldBox()
        self._search = self._box.edit
        outer.addWidget(self._box)

        chips = QHBoxLayout()
        chips.setContentsMargins(6, 0, 6, 0)
        chips.setSpacing(8)
        self._chips = QButtonGroup(self)
        self._chips.setExclusive(True)
        for index, (key, label) in enumerate(data.CHIPS):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setChecked(key == "all")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setProperty("group", key)
            self._chips.addButton(chip, index)
            chips.addWidget(chip)
        self._chips.idClicked.connect(self._on_chip)
        chips.addStretch(1)
        self._back = QPushButton("Back to moods")
        self._back.setObjectName("SectionLink")
        self._back.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back.clicked.connect(self._leave_mood)
        self._back.setVisible(False)
        chips.addWidget(self._back)
        self._count = QLabel()
        self._count.setObjectName("SectionAside")
        chips.addWidget(self._count)
        outer.addLayout(chips)

        self._pages = QStackedWidget()
        self._start = self._build_start()
        self._results_inner = QWidget()
        self._results = _scroll(self._results_inner)
        self._nothing = self._build_nothing()
        for page in (self._start, self._results, self._nothing):
            self._pages.addWidget(page)
        outer.addWidget(self._pages, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(160)
        self._debounce.timeout.connect(self._run)
        self._search.textChanged.connect(self._on_text)
        self._search.returnPressed.connect(self._on_enter)
        # Nothing is read from the library until the page is shown (reload):
        # the window builds every page at start-up.
        self._count.setText("Press / anywhere to search")
        self._pages.setCurrentWidget(self._start)

    # --- the parts ----------------------------------------------------------------------------

    def _build_start(self) -> QScrollArea:
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(6, 14, 6, 32)
        layout.setSpacing(26)

        self._recent_box = QWidget()
        recent = QVBoxLayout(self._recent_box)
        recent.setContentsMargins(0, 0, 0, 0)
        recent.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(_heading("Recent"))
        head.addStretch(1)
        forget = QPushButton("Clear")
        forget.setObjectName("SectionLink")
        forget.setCursor(Qt.CursorShape.PointingHandCursor)
        forget.clicked.connect(self._forget_recent)
        head.addWidget(forget)
        recent.addLayout(head)
        holder = QWidget()
        self._recent_flow = FlowLayout(holder, margin=0, h_spacing=10, v_spacing=10)
        recent.addWidget(holder)
        layout.addWidget(self._recent_box)

        moods = QVBoxLayout()
        moods.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(_heading("Browse by mood"))
        note = QLabel("Made from your own genres, lengths and what you finish")
        note.setObjectName("SectionAside")
        head.addWidget(note, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch(1)
        moods.addLayout(head)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self._tiles: list[_MoodTile] = []
        for index, mood in enumerate(data.MOODS):
            tile = _MoodTile(mood)
            tile.clicked.connect(lambda _checked=False, which=index: self.show_mood(which))
            grid.addWidget(tile, index // 3, index % 3)
            self._tiles.append(tile)
        moods.addLayout(grid)
        layout.addLayout(moods)

        surprise = QHBoxLayout()
        surprise.setSpacing(16)
        self._surprise = QPushButton("Surprise me")
        self._surprise.setObjectName("Primary")
        self._surprise.setIcon(QIcon(icon_pixmap("shuffle", 20, C.PLAY_FG, self.devicePixelRatioF())))
        self._surprise.setIconSize(QSize(20, 20))
        self._surprise.setFixedHeight(50)
        self._surprise.setStyleSheet("padding: 0 24px 0 18px; border-radius: 24px; font-size: 12pt;")
        self._surprise.setCursor(Qt.CursorShape.PointingHandCursor)
        self._surprise.clicked.connect(self._on_surprise)
        surprise.addWidget(self._surprise)
        self._surprise_note = QLabel("Opens a film you haven't started, one that fits tonight")
        self._surprise_note.setObjectName("SectionAside")
        surprise.addWidget(self._surprise_note)
        surprise.addStretch(1)
        layout.addLayout(surprise)
        layout.addStretch(1)
        return _scroll(inner)

    def _build_nothing(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 14, 6, 0)
        self._nothing_words = QLabel()
        self._nothing_words.setWordWrap(True)
        self._nothing_words.setStyleSheet(
            f"background: #161311; border-radius: 24px; padding: 36px 40px; color: {C.TEXT_DIM}; font-size: 12pt;")
        layout.addWidget(self._nothing_words)
        to_moods = QPushButton("Browse by mood instead")
        to_moods.setObjectName("SectionLink")
        to_moods.setCursor(Qt.CursorShape.PointingHandCursor)
        to_moods.clicked.connect(lambda: self._search.clear())
        layout.addWidget(to_moods, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return panel

    # --- what the window asks of it -----------------------------------------------------------

    def focus_search(self) -> None:
        self._search.setFocus()
        self._search.selectAll()

    def reload(self) -> None:
        """The library changed, or the page is being shown: redo what is on it."""
        if self._mood is not None:
            self.show_mood(self._mood)
        elif len(self.term()) >= data.MIN_LETTERS:
            self._run()
        else:
            self._show_start()

    def term(self) -> str:
        return self._search.text().strip()

    def hits(self) -> list[data.Hit]:
        """What the page is showing now: the results, with the chip's filter."""
        return [hit for hit in self._hits if self._filter in ("all", hit.group)]

    # --- typing -------------------------------------------------------------------------------

    def _on_text(self, _text: str) -> None:
        if self._mood is not None and self.term():
            self._mood = None
            self._back.setVisible(False)
        self._debounce.start()

    def _on_enter(self) -> None:
        """Results come as you type, so Enter keeps the search (Recent) and
        takes the keys to the results: Tab and Enter from the top result on."""
        self._debounce.stop()
        self._run()
        data.remember(self.term())
        if self._pages.currentWidget() is self._results and getattr(self, "top_card", None) is not None:
            self.top_card.primary.setFocus(Qt.FocusReason.TabFocusReason)

    def _run(self) -> None:
        if self._mood is not None:
            return
        if len(self.term()) < data.MIN_LETTERS:
            self._hits = []
            self._show_start("Type two letters or more" if self.term() else "")
            return
        self._hits = data.find(self.term())
        self._show_results()

    def _on_chip(self, index: int) -> None:
        self._filter = data.CHIPS[index][0]
        if self._hits:
            self._show_results()

    # --- moods and surprises ------------------------------------------------------------------

    def show_mood(self, index: int) -> None:
        mood = data.MOODS[index]
        self._mood = index
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._box.clear.setVisible(False)
        self._hits = mood.pick()
        self._back.setVisible(True)
        self._show_results()

    def _leave_mood(self) -> None:
        self._mood = None
        self._back.setVisible(False)
        self._hits = []
        self._show_start()

    def _on_surprise(self) -> None:
        film = data.surprise()
        if film is None:
            self._surprise_note.setText("Every film here has been started: nothing new to pick")
            return
        self.open_media.emit(film)

    def _forget_recent(self) -> None:
        data.forget_recent()
        self._fill_recent()

    def _fill_recent(self) -> None:
        self._recent_flow.clear()
        terms = data.recent()
        ratio = self.devicePixelRatioF()
        for term in terms:
            chip = QPushButton(term)
            chip.setObjectName("Recent")
            chip.setIcon(QIcon(icon_pixmap("clock", 16, C.TEXT_FAINT, ratio)))
            chip.setIconSize(QSize(16, 16))
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFixedHeight(36)
            # Under half the height: past half, Qt draws the corners square.
            chip.setStyleSheet(
                f"QPushButton#Recent {{ background: #221E1A; border: none; border-radius: 17px; padding: 0 16px 0 12px;"
                f" color: {C.TEXT}; font-size: 10.5pt; }} QPushButton#Recent:hover {{ background: {C.SURFACE_ACTIVE}; }}")
            chip.clicked.connect(lambda _checked=False, text=term: self._search.setText(text))
            self._recent_flow.addWidget(chip)
        self._recent_box.setVisible(bool(terms))

    # --- showing ------------------------------------------------------------------------------

    def _show_start(self, note: str = "") -> None:
        self._fill_recent()
        for tile in self._tiles:
            try:
                tile.set_count(len(tile.mood.pick()))
            except Exception:                   # noqa: BLE001 - a tile without its count
                tile.set_count(0)
        self._count.setText(note or "Press / anywhere to search")
        self._set_chips_enabled(None)
        self._pages.setCurrentWidget(self._start)

    def _set_chips_enabled(self, groups: set[str] | None) -> None:
        for chip in self._chips.buttons():
            key = chip.property("group")
            chip.setEnabled(groups is None or key == "all" or key in groups)

    def _show_results(self) -> None:
        present = {hit.group for hit in self._hits}
        self._set_chips_enabled(present)
        if self._filter != "all" and self._filter not in present:
            self._filter = "all"
            self._chips.button(0).setChecked(True)
        shown = self.hits()
        noun = "result" if len(shown) == 1 else "results"
        if self._mood is not None:
            mood = data.MOODS[self._mood]
            self._count.setText(f"{mood.name}: {len(shown)} to watch")
        else:
            self._count.setText(f"{len(shown)} {noun} for “{self.term()}”")
        if not shown:
            if self._mood is not None:
                self._nothing_words.setText(f"Nothing in your library is {data.MOODS[self._mood].name.lower()} yet: "
                                            "its categories come from your films' and shows' genres.")
            else:
                self._nothing_words.setText(f"Nothing called “{self.term()}”, here or in your friends' libraries. "
                                            "Try fewer letters, or another spelling.")
            self._pages.setCurrentWidget(self._nothing)
            return

        old = self._results.takeWidget()
        if old is not None:
            old.deleteLater()
        inner = QWidget()
        row = QHBoxLayout(inner)
        row.setContentsMargins(6, 14, 6, 32)
        row.setSpacing(28)
        top = data.top_result(shown)
        self.top_card = _TopCard()
        self.top_card.set_hit(top)
        self.top_card.open_hit.connect(self._open)
        self.top_card.play_hit.connect(self._play)
        row.addWidget(self.top_card, 0, Qt.AlignmentFlag.AlignTop)
        groups = QVBoxLayout()
        groups.setSpacing(24)
        self.groups: list[_Group] = []
        for key in data.GROUPS:
            members = [hit for hit in shown if hit.group == key]
            if not members:
                continue
            group = _Group(key, members)
            group.open_hit.connect(self._open)
            group.play_hit.connect(self._play)
            groups.addWidget(group)
            self.groups.append(group)
        groups.addStretch(1)
        row.addLayout(groups, 1)
        self._results_inner = inner
        self._results.setWidget(inner)
        self._pages.setCurrentWidget(self._results)

    # --- acting on a result -------------------------------------------------------------------

    def _open(self, hit: data.Hit) -> None:
        data.remember(self.term())
        if hit.kind in ("film", "episode"):
            self.open_media.emit(hit.item)
        elif hit.kind == "show":
            self.open_show.emit(hit.item)
        elif hit.kind == "album":
            self.open_album.emit(int(hit.item))
        elif hit.kind == "song":
            if hit.item["album_id"] is not None:
                self.open_album.emit(int(hit.item["album_id"]))
        elif hit.kind == "artist":
            self.open_artist.emit(str(hit.item))
        elif hit.kind == "friend":
            self.friend_open_requested.emit(hit.item)

    def _play(self, hit: data.Hit) -> None:
        data.remember(self.term())
        if hit.kind in ("film", "episode"):
            self.play_requested.emit(hit.item)
        elif hit.kind == "song":
            songs = [other.item for other in self.hits() if other.kind == "song"]
            start = next((index for index, song in enumerate(songs) if song["id"] == hit.item["id"]), 0)
            self.play_music.emit(songs, start, {"kind": "search", "title": self.term(), "id": None})
        elif hit.kind == "album":
            from ..music import library as music_library

            songs = music_library.tracks("t.album_id = ?", (int(hit.item),))
            if songs:
                self.play_music.emit(list(songs), 0, {"kind": "album", "title": hit.title, "id": int(hit.item)})
        elif hit.kind == "friend":
            self.friend_play_requested.emit(hit.item)
