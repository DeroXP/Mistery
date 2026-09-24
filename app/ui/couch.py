"""Playing from the couch: a game controller in the window.

A yellow glow sits on whatever the controller is on, and moves to the nearest
thing in the direction pressed (the d-pad or the left stick). A opens or
presses it, B goes back, X is Watch together, Y is Search, LB and RB jump a
row, Start opens the menu on the left. In the player: A pauses, the d-pad and
LB/RB skip, LT/RT skip further, Y turns subtitles on and off, X skips an
intro, B closes it. Along the bottom, the buttons that do something, printed
the way this controller prints them (PlayStation's shapes, or Xbox letters).

Nothing here takes Qt's keyboard focus away from anything, except a text
field the controller opens (so a keyboard can type into it): the glow is its
own idea of "here", and the mouse moving puts it away.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSlider, QAbstractSpinBox, QApplication, QComboBox, QDialog, QLineEdit,
    QScrollArea, QScrollBar, QWidget,
)

import shiboken6

from ..config import settings
from ..gamepad import Gamepad
from .theme import C, ui_font
from .widgets.cards import _BaseCard

_log = logging.getLogger("gamepad")

_ACTIONABLE = (QAbstractButton, QLineEdit, QComboBox, QAbstractSlider, QAbstractSpinBox, _BaseCard)

# What each controller has printed on its face buttons, and their colours.
_FACES = {
    "xbox": {"a": ("A", "#8FCB7E"), "b": ("B", "#E4877A"), "x": ("X", "#7FB0E0"), "y": ("Y", C.ACCENT),
             "lb": ("LB", "#CDC3B6"), "rb": ("RB", "#CDC3B6"), "lt": ("LT", "#CDC3B6"), "rt": ("RT", "#CDC3B6")},
    "playstation": {"a": ("✕", "#8DB8F0"), "b": ("○", "#E4877A"), "x": ("□", "#E7A2C6"), "y": ("△", "#8FCB9E"),
                    "lb": ("L1", "#CDC3B6"), "rb": ("R1", "#CDC3B6"), "lt": ("L2", "#CDC3B6"),
                    "rt": ("R2", "#CDC3B6")},
}
_FACES["generic"] = _FACES["xbox"]

# The player's keys, pressed for it (PlayerView.handle_key).
_PLAYER_KEYS = {
    "a": Qt.Key.Key_Space, "b": Qt.Key.Key_Escape, "left": Qt.Key.Key_Left, "right": Qt.Key.Key_Right,
    "lb": Qt.Key.Key_Left, "rb": Qt.Key.Key_Right, "lt": Qt.Key.Key_J, "rt": Qt.Key.Key_L,
    "up": Qt.Key.Key_Up, "down": Qt.Key.Key_Down, "y": Qt.Key.Key_S, "x": Qt.Key.Key_I,
}
# And a popup menu's, or an open list's.
_POPUP_KEYS = {"up": Qt.Key.Key_Up, "down": Qt.Key.Key_Down, "left": Qt.Key.Key_Left, "right": Qt.Key.Key_Right,
               "a": Qt.Key.Key_Return, "b": Qt.Key.Key_Escape}


def _gap(a1: int, a2: int, b1: int, b2: int) -> int:
    """How far apart two ranges are along one axis; 0 when they overlap."""
    return max(0, max(a1, b1) - min(a2, b2))


def score(current: QRect, candidate: QRect, direction: str) -> float | None:
    """How good a next stop `candidate` is from `current` going `direction`
    (lower is better), or None when it is not that way at all. Nearest along
    the way first, and something in the same row or column well before
    something off to the side."""
    cur, rect = current, candidate
    if direction in ("left", "right"):
        if direction == "right":
            if rect.center().x() <= cur.center().x() + 2 or rect.left() < cur.left() + 2:
                return None
            major = max(0, rect.left() - cur.right())
        else:
            if rect.center().x() >= cur.center().x() - 2 or rect.right() > cur.right() - 2:
                return None
            major = max(0, cur.left() - rect.right())
        minor = _gap(cur.top(), cur.bottom(), rect.top(), rect.bottom())
        off = abs(rect.center().y() - cur.center().y())
    else:
        if direction == "down":
            if rect.center().y() <= cur.center().y() + 2 or rect.top() < cur.top() + 2:
                return None
            major = max(0, rect.top() - cur.bottom())
        else:
            if rect.center().y() >= cur.center().y() - 2 or rect.bottom() > cur.bottom() - 2:
                return None
            major = max(0, cur.top() - rect.bottom())
        minor = _gap(cur.left(), cur.right(), rect.left(), rect.right())
        off = abs(rect.center().x() - cur.center().x())
    return major + 4 * minor + 0.1 * off


class FocusGlow(QWidget):
    """The yellow ring round whatever the controller is on."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._radius = 14.0
        self.hide()

    def around(self, rect: QRect, radius: float) -> None:
        """Round `rect` (in the parent's coordinates), with room for the glow."""
        self._radius = radius
        self.setGeometry(rect.adjusted(-10, -10, 10, 10))
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ring = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for spread, alpha in ((8, 26), (5, 50), (2.5, 90)):
            glow = QColor(C.ACCENT)
            glow.setAlpha(alpha)
            painter.setPen(QPen(glow, 2))
            painter.drawRoundedRect(ring.adjusted(-spread, -spread, spread, spread),
                                    self._radius + spread, self._radius + spread)
        painter.setPen(QPen(QColor(C.ACCENT), 3))
        painter.drawRoundedRect(ring.adjusted(-1, -1, 1, 1), self._radius + 1, self._radius + 1)


class HintBar(QWidget):
    """Along the bottom: the buttons that do something here, as printed."""

    HEIGHT = 44

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._hints: list[tuple[str, str, str]] = []        # (glyph, colour, words)
        self._kind = "xbox"
        self.hide()

    def show_hints(self, kind: str, hints: list[tuple[str, str]], bottom_gap: int = 0) -> None:
        faces = _FACES.get(kind, _FACES["xbox"])
        faces = dict(faces, rows=(f"{faces['lb'][0]} {faces['rb'][0]}", "#CDC3B6"))     # both bumpers
        self._hints = [(faces[button][0] if button in faces else button, faces.get(button, ("", "#CDC3B6"))[1], words)
                       for button, words in hints]
        font = ui_font(10, QFont.Weight.DemiBold)
        metrics = QFontMetrics(font)
        width = 28 + sum(metrics.horizontalAdvance(words) + metrics.horizontalAdvance(glyph) + 44
                         for glyph, _colour, words in self._hints)
        parent = self.parentWidget()
        self.setFixedSize(min(width, parent.width() - 40), self.HEIGHT)
        self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 18 - bottom_gap)
        self.show()
        self.raise_()
        self.update()

    def hints(self) -> list[tuple[str, str, str]]:
        return list(self._hints)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor("#2B2520"), 1))
        painter.setBrush(QColor(22, 19, 17, 240))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        font = ui_font(10, QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        x = 16.0
        for glyph, colour, words in self._hints:
            chip_w = max(26.0, metrics.horizontalAdvance(glyph) + 14.0)
            chip = QRectF(x, (self.height() - 26) / 2, chip_w, 26)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawRoundedRect(chip, 13, 13)
            painter.setPen(QColor(C.ON_ACCENT))
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, glyph)
            x += chip_w + 8
            painter.setPen(QColor(C.TEXT_DIM))
            text_w = metrics.horizontalAdvance(words)
            painter.drawText(QRectF(x, 0, text_w + 2, self.height()), Qt.AlignmentFlag.AlignVCenter, words)
            x += text_w + 22


class CouchNav(QObject):
    """The controller in the window (see the module's docstring)."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self.pad = Gamepad(self)
        self.pad.pressed.connect(self._on_press)
        self.pad.connected.connect(self._on_connected)
        self.pad.disconnected.connect(self._on_disconnected)
        self.pad.set_enabled(bool(settings.get("gamepad", True)))
        self._lit: QWidget | None = None
        self._opened_sidebar = False
        self.active = False                             # the glow and the hints are showing
        self.glow = FocusGlow(window)
        self._other_glow: FocusGlow | None = None   # in a dialog, which may be deleted when it closes
        self.hint_bar = HintBar(window)
        self._cursor_at = QCursor.pos()
        app = QApplication.instance()
        app.installEventFilter(self)
        app.applicationStateChanged.connect(
            lambda state: self.pad.set_active(state == Qt.ApplicationState.ApplicationActive))
        self.pad.set_active(app.applicationState() == Qt.ApplicationState.ApplicationActive)
        # The glow follows its widget while pages scroll and grow.
        self._follow = QTimer(self)
        self._follow.setInterval(120)
        self._follow.timeout.connect(self._place_glow)

    @property
    def lit(self) -> QWidget | None:
        return self._lit if self._lit is not None and self._alive(self._lit) else None

    # --- the controller coming and going -------------------------------------------------

    def _on_connected(self, kind: str, name: str) -> None:
        words = {"playstation": "PlayStation controller", "xbox": "Xbox controller"}.get(kind, name)
        self._window._on_status(f"{words} connected: press any button to use it")
        page = getattr(self._window, "settings_page", None)
        if page is not None and hasattr(page, "set_gamepad_state"):
            page.set_gamepad_state(f"Connected: {words} ({name}).")
        header = getattr(getattr(self._window, "home", None), "header", None)
        if header is not None and hasattr(header, "set_controller"):
            header.set_controller(kind)

    def _on_disconnected(self) -> None:
        self._window._on_status("Controller disconnected")
        page = getattr(self._window, "settings_page", None)
        if page is not None and hasattr(page, "set_gamepad_state"):
            page.set_gamepad_state("No controller connected.")
        header = getattr(getattr(self._window, "home", None), "header", None)
        if header is not None and hasattr(header, "set_controller"):
            header.set_controller("")
        self.leave()

    # --- couch mode on and off ---------------------------------------------------------------

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        # The mouse puts the glow away: it is the pointer's turn. Moved for
        # real, that is: a page scrolling under a pointer at rest can bring a
        # mouse move with it, and the glow went out as it scrolled.
        if self.active:
            kind = event.type()
            if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
                self.leave()
            elif kind == QEvent.Type.MouseMove and (QCursor.pos() - self._cursor_at).manhattanLength() > 6:
                self.leave()
        return False

    def leave(self) -> None:
        if not self.active:
            return
        self.active = False
        self._unlight()
        self.glow.hide()
        if self._other_glow is not None and shiboken6.isValid(self._other_glow):
            self._other_glow.hide()
        self.hint_bar.hide()
        self._follow.stop()
        if self._opened_sidebar:
            self._window.sidebar.close_now()
            self._opened_sidebar = False

    def _enter(self) -> None:
        if self.active:
            return
        self.active = True
        self._follow.start()

    # --- where things are ----------------------------------------------------------------

    def _root(self) -> QWidget:
        modal = QApplication.activeModalWidget()
        if modal is not None:
            return modal
        active = QApplication.activeWindow()
        return active if active is not None else self._window

    @staticmethod
    def _alive(widget: QWidget) -> bool:
        try:
            return widget.isVisible() and widget.isEnabled()
        except RuntimeError:                    # deleted underneath us
            return False

    def _candidates(self, root: QWidget) -> list[QWidget]:
        preview = getattr(getattr(self._window, "home", None), "hover_preview", None)
        out = []
        for widget in root.findChildren(QWidget):
            if not isinstance(widget, _ACTIONABLE) or isinstance(widget, QScrollBar):
                continue
            if not widget.isVisible() or not widget.isEnabled() or widget.width() < 8 or widget.height() < 8:
                continue
            if widget.window() is not root.window():
                continue
            if preview is not None and preview.isAncestorOf(widget):
                continue                        # the preview's own buttons are the pointer's
            if isinstance(widget, QAbstractButton) and isinstance(widget.parentWidget(), QAbstractSpinBox):
                continue
            out.append(widget)
        return out

    def rect_of(self, widget: QWidget, root: QWidget) -> QRect:
        """Where `widget` is, in `root`: a card by its picture, not its words."""
        if isinstance(widget, _BaseCard):
            return widget.art_rect_in(root)
        return QRect(widget.mapTo(root, QPoint(0, 0)), widget.size())

    def _start_widget(self, root: QWidget, candidates: list[QWidget]) -> QWidget | None:
        """Where the glow starts on a page: its main button, else its first card,
        else what is nearest the top left, all in view and off the left menu."""
        sidebar = getattr(self._window, "sidebar", None)
        view = root.rect()
        seen = [w for w in candidates if view.intersects(self.rect_of(w, root))
                and not (sidebar is not None and sidebar.isAncestorOf(w))]
        for test in (lambda w: isinstance(w, QAbstractButton) and w.objectName() == "Primary",
                     lambda w: isinstance(w, _BaseCard), lambda w: True):
            matches = [w for w in seen if test(w)]
            if matches:
                return min(matches, key=lambda w: (self.rect_of(w, root).top() // 40, self.rect_of(w, root).left()))
        return candidates[0] if candidates else None

    # --- the presses ---------------------------------------------------------------------

    def _on_press(self, button: str) -> None:
        self._cursor_at = QCursor.pos()
        popup = QApplication.activePopupWidget()
        if popup is not None:
            self._press_key(popup, _POPUP_KEYS.get(button))
            return
        window = self._window
        if QApplication.activeModalWidget() is None and window.stack.currentWidget() is window.player:
            self._player(button)
            return
        was_active = self.active
        self._enter()
        root = self._root()
        if button in ("up", "down", "left", "right"):
            self.move(button)
        elif button in ("lb", "rb"):
            self.jump_row(-1 if button == "lb" else 1)
        elif button == "a":
            if not was_active or self.lit is None:
                self.move(None)                 # the first press only shows where you are
            else:
                self.activate(self.lit)
        elif button == "b":
            self.back(root)
        elif button == "y":
            window._on_search_shortcut()
            QTimer.singleShot(0, lambda: self.move(None))
        elif button == "x":
            self.together()
        elif button == "start":
            window.sidebar.open_now()
            self._opened_sidebar = True
            current = window._nav_group.checkedButton()
            if current is not None:
                self.light(current)
        self._show_hints()

    def _press_key(self, target: QWidget, key) -> None:
        if key is None or target is None:
            return
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(target, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier))

    def _player(self, button: str) -> None:
        self.hint_bar.hide()                    # under the film's own window there, unseen
        key = _PLAYER_KEYS.get(button)
        if key is None:
            if button == "start":
                self._window.player.overlay.wake()
            return
        self._window.player.handle_key(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))

    # --- moving ----------------------------------------------------------------------------

    def move(self, direction: str | None) -> None:
        """To the nearest thing that way; with no direction, or nothing lit,
        to where the page starts."""
        root = self._root()
        candidates = self._candidates(root)
        current = self.lit
        if current is not None and current.window() is not root.window():
            current = None
        if direction is None or current is None:
            target = current if (direction is None and current is not None) else self._start_widget(root, candidates)
        else:
            here = self.rect_of(current, root)
            scored = [(score(here, self.rect_of(w, root), direction), w) for w in candidates if w is not current]
            scored = [(value, w) for value, w in scored if value is not None]
            target = min(scored, key=lambda pair: pair[0])[1] if scored else None
        if target is not None:
            self.light(target)

    def jump_row(self, step: int) -> None:
        """To the start of the row above or below."""
        root = self._root()
        current = self.lit
        if current is None:
            self.move(None)
            return
        here = self.rect_of(current, root)
        # The page's rows, not the menu on the left, which is always left-most:
        # unless the glow is in the menu already.
        sidebar = self._window.sidebar
        in_menu = sidebar.isAncestorOf(current)
        rects = [(self.rect_of(w, root), w) for w in self._candidates(root)
                 if w is not current and sidebar.isAncestorOf(w) == in_menu]
        if step > 0:
            ahead = [(r, w) for r, w in rects if r.top() > here.bottom() - 4]
            if not ahead:
                return
            row_y = min(r.center().y() for r, _w in ahead)
        else:
            ahead = [(r, w) for r, w in rects if r.bottom() < here.top() + 4]
            if not ahead:
                return
            row_y = max(r.center().y() for r, _w in ahead)
        row = [(r, w) for r, w in ahead if abs(r.center().y() - row_y) <= max(24, r.height() // 3)]
        self.light(min(row, key=lambda pair: pair[0].left())[1])

    def light(self, widget: QWidget) -> None:
        if not self._alive(widget):
            return                              # gone with a page that was rebuilt
        if widget is self._lit and self.glow.isVisible():
            self._place_glow()
            return
        self._unlight()
        self._lit = widget
        sidebar = self._window.sidebar
        if sidebar.isAncestorOf(widget):
            sidebar.open_now()
            self._opened_sidebar = True
        elif self._opened_sidebar:
            sidebar.close_now()
            self._opened_sidebar = False
        self._reveal(widget)
        if isinstance(widget, _BaseCard):
            widget.set_lit(True)
        self._place_glow()

    def _unlight(self) -> None:
        old, self._lit = self._lit, None
        if old is not None and isinstance(old, _BaseCard):
            try:
                old.set_lit(False)
            except RuntimeError:
                pass

    @staticmethod
    def _reveal(widget: QWidget) -> None:
        """Scroll every scroll area it is in until it shows, innermost first."""
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea) and parent.widget() is not None \
                    and parent.widget().isAncestorOf(widget):
                parent.ensureWidgetVisible(widget, 40, 70)
            parent = parent.parentWidget()

    def _place_glow(self) -> None:
        widget = self.lit
        other = self._other_glow if self._other_glow is not None and shiboken6.isValid(self._other_glow) else None
        if not self.active or widget is None:
            self.glow.hide()
            if other is not None:
                other.hide()
            return
        top = widget.window()
        if top is self._window:
            glow = self.glow
            if other is not None:
                other.hide()
        else:
            # A dialog's own glow, never the window's: a message box is deleted
            # when it closes, and the window's glow would have gone with it.
            if other is None or other.parentWidget() is not top:
                other = self._other_glow = FocusGlow(top)
            glow = other
            self.glow.hide()
        rect = self.rect_of(widget, top)
        radius = 18.0 if isinstance(widget, _BaseCard) else min(22.0, rect.height() / 2)
        glow.around(rect, radius)

    # --- doing ---------------------------------------------------------------------------

    def activate(self, widget: QWidget) -> None:
        if isinstance(widget, _BaseCard):
            widget.clicked.emit(widget.item)
        elif isinstance(widget, QAbstractButton):
            widget.animateClick()
        elif isinstance(widget, QComboBox):
            widget.showPopup()
        elif isinstance(widget, QLineEdit):
            widget.setFocus(Qt.FocusReason.OtherFocusReason)
            widget.selectAll()
        else:
            widget.setFocus(Qt.FocusReason.OtherFocusReason)
        # A page that changed underneath: the glow finds its feet on the next press.
        QTimer.singleShot(250, self._place_glow)

    def back(self, root: QWidget) -> None:
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit) and self.lit is focus:
            focus.clearFocus()                  # out of the field, still on it
            return
        if isinstance(root, QDialog) and root is not self._window:
            root.reject()
            return
        if self._opened_sidebar:
            self._window.sidebar.close_now()
            self._opened_sidebar = False
            self.move(None)
            return
        self._window._on_back_shortcut()
        QTimer.singleShot(250, lambda: self.move(None))

    def together(self) -> None:
        """X: Watch together, with what the glow is on (or the page's own)."""
        widget = self.lit
        window = self._window
        from ..models import MediaItem

        if isinstance(widget, _BaseCard) and isinstance(widget.item, MediaItem):
            widget.action_requested.emit("party", widget.item)
            return
        page = window.stack.currentWidget()
        button = getattr(page, "_party", None) or getattr(getattr(page, "hero", None), "_together", None)
        if isinstance(button, QAbstractButton) and button.isVisible():
            button.animateClick()

    # --- the hints -----------------------------------------------------------------------

    def _show_hints(self) -> None:
        if not self.active:
            return
        kind = self.pad.kind or "xbox"
        window = self._window
        if isinstance(self._root(), QDialog):
            hints = [("a", "Choose"), ("b", "Close")]
        else:
            hints = [("a", "Select"), ("b", "Back"), ("x", "Watch together"), ("y", "Search"), ("rows", "Rows")]
        if self.hint_bar.parentWidget() is not window:
            self.hint_bar.setParent(window)
        bar = getattr(window, "now_bar", None)
        gap = bar.height() if bar is not None and bar.isVisible() else 0
        self.hint_bar.show_hints(kind, hints, gap)
