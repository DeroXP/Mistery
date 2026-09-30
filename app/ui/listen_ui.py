"""Listening parties' screens: the panel, the line on Now Playing, Home's card.

- ListenPanel. Before a party: what is about to happen, Start, or join with a
  code. While one is on: the code to send (the host), who is in and who is the
  DJ, a search of the host's music to add songs from, and what everyone did.
- PartyLine. On Now Playing while a party is on: who you are listening with,
  and the latest thing somebody did. A click opens the panel.
- ListeningCard. On Home, when a friend is listening to your music: who, what,
  and Join them.

They all read one ListeningSession (app/party/listen_session.py) and never
touch the network themselves.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..util import fmt_clock
from .party_dialog import (
    _MONO, _MovieNightDialog, _Spinner, _box, _button, _text, split_code,
)
from .theme import C
from .widgets.flow import FlowLayout
from .widgets.icons import paint_icon

_CAPTION = f"color: {C.TEXT_FAINT}; font-size: 8.5pt; font-weight: 700; letter-spacing: 1.2px;"
MAX_RESULTS = 8
ACTIVITY_SHOWN = 6


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(_CAPTION)
    return label


def song_line(song: dict) -> str:
    """"Blue in Green · Miles Davis · 5:37"."""
    bits = [str(song.get("title") or "Untitled")]
    if song.get("artist"):
        bits.append(str(song["artist"]))
    if song.get("duration"):
        bits.append(fmt_clock(float(song["duration"])))
    return "  ·  ".join(bits)


def people_words(session) -> list[tuple[str, str]]:
    """(name, note) for everyone in the party: "you", "DJ", "host"."""
    dj, me = session.dj_id, session.me_id
    shown = []
    for person in session.people:
        notes = []
        if person.get("id") == me:
            notes.append("you")
        if person.get("id") == dj:
            notes.append("DJ")
        elif person.get("host"):
            notes.append("host")
        if person.get("buffering"):
            notes.append("loading…")
        shown.append((str(person.get("name") or "Friend"), ", ".join(notes)))
    return shown


class _ResultRow(QWidget):
    """One song of the host's music: its words, Play next, Add."""

    add = Signal(int, bool)             # song id, next

    def __init__(self, song: dict, parent=None) -> None:
        super().__init__(parent)
        self.song = song
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(10)
        words = QLabel()
        words.setStyleSheet(f"color: {C.TEXT}; font-size: 9.8pt;")
        words.setTextFormat(Qt.TextFormat.PlainText)
        words.setText(song_line(song))
        words.setMinimumWidth(80)
        row.addWidget(words, 1)
        up_next = _button("Play next", "Chip")
        up_next.clicked.connect(lambda: self.add.emit(int(song["id"]), True))
        row.addWidget(up_next)
        queue = _button("Add", "Chip")
        queue.clicked.connect(lambda: self.add.emit(int(song["id"]), False))
        row.addWidget(queue)


class ListenPanel(_MovieNightDialog):
    """The listening party's one window: start one, or everything about the one that's on."""

    join_code_requested = Signal()          # "Join with a code": the window's Join box

    def __init__(self, session, player, parent=None) -> None:
        super().__init__(session, "Listening party", parent)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._player = player
        self._search_n = 0
        self.eyebrow.setText("LISTENING PARTY")
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(3500)
        self._copied_timer.timeout.connect(lambda: self.copied.setVisible(False))
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._search)

        # --- before: what is about to happen -----------------------------------
        self.start_box, start = _box("NoteBox", spacing=8)
        self.start_title = _text("", 10.5, C.TEXT, bold=True, rich=False)
        start.addWidget(self.start_title)
        self.start_text = _text("", 9.6, C.TEXT_DIM)
        start.addWidget(self.start_text)
        self.body.addWidget(self.start_box)

        # --- starting or joining ---------------------------------------------------
        self.progress_row = QWidget()
        progress = QHBoxLayout(self.progress_row)
        progress.setContentsMargins(0, 4, 0, 4)
        progress.setSpacing(12)
        self.spinner = _Spinner()
        progress.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignTop)
        self.progress_text = _text("", 10.5, C.TEXT, rich=False)
        progress.addWidget(self.progress_text, 1)
        self.body.addWidget(self.progress_row)

        # --- on: the code (the host) ---------------------------------------------
        self.code_box, code = _box("CodeBox", margins=(22, 18, 22, 18), spacing=12)
        code.addWidget(_caption("INVITE CODE"))
        self.code_label = QLabel()
        self.code_label.setStyleSheet(f"color: {C.TEXT}; {_MONO} font-size: 14pt; "
                                      "font-weight: 600; letter-spacing: 1px;")
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        code.addWidget(self.code_label)
        copy_row = QHBoxLayout()
        copy_row.setSpacing(10)
        self.copy_code = _button("Copy code", "Primary")
        self.copy_code.clicked.connect(lambda: self._copy(self.session.invite_code, "Copied"))
        copy_row.addWidget(self.copy_code)
        self.copy_link = _button("Copy link", "Ghost")
        self.copy_link.setToolTip("The same code as a link friends can click: it opens Mistery's "
                                  "Join box with the code filled in.")
        self.copy_link.clicked.connect(lambda: self._copy(self.session.invite_link, "Link copied"))
        copy_row.addWidget(self.copy_link)
        self.copied = QLabel()
        self.copied.setStyleSheet(f"color: {C.SUCCESS}; font-weight: 600;")
        self.copied.setVisible(False)
        copy_row.addWidget(self.copied)
        copy_row.addStretch(1)
        code.addLayout(copy_row)
        self.code_hint = _text("Friends paste it into <b>Join</b> (the Movie night button), or "
                               "join from their Friends page with one click.", 9.4, C.TEXT_FAINT)
        code.addWidget(self.code_hint)
        self.port_line = _text("", 9.4, C.TEXT_FAINT, rich=False)
        code.addWidget(self.port_line)
        self.body.addWidget(self.code_box)
        self.body.addSpacing(14)

        # --- on: who is in ----------------------------------------------------------
        self.people_panel = QWidget()
        people = QVBoxLayout(self.people_panel)
        people.setContentsMargins(0, 0, 0, 14)
        people.setSpacing(8)
        people.addWidget(_caption("IN THE PARTY"))
        self.people_row = QWidget()
        self.people_flow = FlowLayout(self.people_row, h_spacing=8, v_spacing=8)
        people.addWidget(self.people_row)
        self.dj_line = _text("", 9.4, C.TEXT_FAINT, rich=False)
        people.addWidget(self.dj_line)
        self.body.addWidget(self.people_panel)

        # --- on: adding a song --------------------------------------------------------
        self.add_panel = QWidget()
        add = QVBoxLayout(self.add_panel)
        add.setContentsMargins(0, 0, 0, 14)
        add.setSpacing(8)
        self.add_caption = _caption("ADD A SONG")
        add.addWidget(self.add_caption)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search the music")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(lambda _text: self._search_timer.start())
        self.search_box.returnPressed.connect(self._search)
        add.addWidget(self.search_box)
        self.results = QVBoxLayout()
        self.results.setSpacing(2)
        add.addLayout(self.results)
        self.results_note = _text("", 9.2, C.TEXT_FAINT, rich=False)
        self.results_note.setVisible(False)
        add.addWidget(self.results_note)
        self.body.addWidget(self.add_panel)

        # --- on: what everyone did ------------------------------------------------------
        self.activity_panel = QWidget()
        activity = QVBoxLayout(self.activity_panel)
        activity.setContentsMargins(0, 0, 0, 8)
        activity.setSpacing(4)
        activity.addWidget(_caption("WHAT'S HAPPENING"))
        self.activity = _text("", 9.6, C.TEXT_DIM, rich=False)
        activity.addWidget(self.activity)
        self.body.addWidget(self.activity_panel)

        self.body.addStretch(1)
        footer = self.footer
        self.join_code = _button("Join with a code", "Ghost")
        self.join_code.setAutoDefault(False)
        self.join_code.clicked.connect(self._join_with_code)
        footer.addWidget(self.join_code)
        footer.addStretch(1)
        self.close_button = _button("Close", "Ghost")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.hide)
        footer.addWidget(self.close_button)
        self.end_button = _button("End the party", "Danger")
        self.end_button.setAutoDefault(False)
        self.end_button.clicked.connect(self._end)
        footer.addWidget(self.end_button)
        self.start_button = _button("Start listening party", "Primary")
        self.start_button.clicked.connect(self._start)
        footer.addWidget(self.start_button)

        session.changed.connect(self.refresh)
        session.message.connect(lambda _text: self._refresh_activity())
        session.error.connect(self._on_error)
        session.found.connect(self._on_found)
        self.refresh()

    # --- what is shown ---------------------------------------------------------------

    def open_fresh(self) -> None:
        self.say_notice("")
        self.search_box.clear()
        self._clear_results()
        self.refresh()

    def refresh(self) -> None:
        session = self.session
        role, phase = session.role, session.phase
        on = phase == "on"
        busy = phase in ("starting", "joining")
        self.start_box.setVisible(role is None)
        self.progress_row.setVisible(busy)
        self.code_box.setVisible(on and role == "host")
        self.people_panel.setVisible(on)
        self.add_panel.setVisible(on)
        self.activity_panel.setVisible(on)
        self.join_code.setVisible(role is None)
        self.start_button.setVisible(role is None)
        self.end_button.setVisible(role is not None)
        self.end_button.setText("End the party" if role == "host" else "Leave")
        if role is None:
            self.headline.setText("Listen together")
            self.subline.setText("Friends hear what you play, in step with you, each on their own "
                                 "PC. They can add songs from your music and vote to skip.")
            current = self._player.current
            mine = current is not None and not current.get("friend") and not current.get("party")
            if mine:
                after = sum(1 for t in self._player.upcoming if not t.get("friend") and not t.get("party"))
                self.start_title.setText(f"Starting with {current.get('title') or 'this song'}")
                self.start_text.setText(
                    (f"and the {after} song{'s' if after != 1 else ''} after it in your queue. "
                     if after else "") + "You're the DJ: you play, pause and skip, and what you "
                    "put on next is what everyone hears.")
            else:
                self.start_title.setText("Play something of yours first")
                self.start_text.setText("A listening party plays your queue: start an album, a "
                                        "playlist or a song, then come back here.")
            self.start_button.setEnabled(mine)
        elif busy:
            self.headline.setText("Joining…" if role == "guest" else "Starting the party…")
            self.subline.setText("")
            self.progress_text.setText(session.status or "One moment…")
        else:
            self.headline.setText(session.title)
            self.subline.setText(session.listeners_text() + ".")
            if role == "host":
                self.code_label.setText(split_code(session.invite_code))
                status = session.port_status
                self.port_line.setText(str(status.text) if status is not None
                                       and status.state not in ("upnp",) else "")
                self.port_line.setVisible(bool(self.port_line.text()))
            host = session.host_name if role == "guest" else "your"
            self.add_caption.setText("ADD A SONG FROM " + (f"{host.upper()}'S MUSIC" if role == "guest"
                                                           else "YOUR MUSIC"))
            self._refresh_people()
            self._refresh_activity()
        self.fit()
        # Again once the new words have been laid out: a label's height reaches
        # the boxes around it through posted events, and measured at once the
        # last line of What's happening was cut off by the buttons.
        QTimer.singleShot(0, self._fit_if_shown)

    def _fit_if_shown(self) -> None:
        if self.isVisible():
            self.fit()

    def _refresh_people(self) -> None:
        while self.people_flow.count():
            item = self.people_flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        for name, note in people_words(self.session):
            chip = QLabel(f"{name}" + (f"  ·  {note}" if note else ""))
            chip.setObjectName("Person")
            self.people_flow.addWidget(chip)
        session = self.session
        if session.is_dj and session.role == "guest":
            self.dj_line.setText("You're the DJ: what you play, everyone hears. Anyone can add songs "
                                 "and vote to skip.")
        elif session.is_dj:
            self.dj_line.setText("You're the DJ. Friends can add songs and vote to skip: half of "
                                 "the party skips a song.")
        else:
            dj = session.dj_name or "The host"
            self.dj_line.setText(f"{dj[:1].upper()}{dj[1:]} is the DJ. You can add songs and vote to "
                                 "skip (Next); pausing pauses it for you alone.")

    def _refresh_activity(self) -> None:
        lines = self.session.activity[-ACTIVITY_SHOWN:]
        self.activity.setText("\n".join(reversed(lines)) if lines else "Nothing yet.")
        if self.isVisible():
            self.fit()
            QTimer.singleShot(0, self._fit_if_shown)

    # --- doing -------------------------------------------------------------------------

    def _start(self) -> None:
        self.say_notice("")
        self.session.start()
        self.refresh()

    def _end(self) -> None:
        session = self.session
        if session.role == "host":
            others = [name for name, note in people_words(session) if "you" not in note]
            if others:
                from PySide6.QtWidgets import QMessageBox

                answer = QMessageBox.question(
                    self, "End the listening party",
                    f"End the listening party for {', '.join(others)}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return
        session.leave()
        self.hide()

    def _join_with_code(self) -> None:
        self.hide()
        self.join_code_requested.emit()

    def _copy(self, text: str, said: str) -> None:
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.copied.setText(said)
        self.copied.setVisible(True)
        self._copied_timer.start()

    def _on_error(self, text: str) -> None:
        self.say_notice(text)
        self.refresh()

    # --- searching the host's music -------------------------------------------------------

    def _search(self) -> None:
        self._search_timer.stop()
        text = self.search_box.text().strip()
        if not text:
            self._clear_results()
            self.fit()
            return
        self._search_n = self.session.search(text)

    def _clear_results(self) -> None:
        while self.results.count():
            item = self.results.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.results_note.setVisible(False)

    def _on_found(self, n: int, songs: list) -> None:
        if n != self._search_n:
            return                          # an older search's answer
        self._clear_results()
        for song in songs[:MAX_RESULTS]:
            row = _ResultRow(song)
            row.add.connect(self._add)
            self.results.addWidget(row)
        if not songs:
            self.results_note.setText("Nothing by that name.")
            self.results_note.setVisible(True)
        elif len(songs) > MAX_RESULTS:
            self.results_note.setText(f"And {len(songs) - MAX_RESULTS} more: type a little more to "
                                      "find the one you want.")
            self.results_note.setVisible(True)
        self.fit()

    def _add(self, song_id: int, up_next: bool) -> None:
        self.session.add_song(song_id, next=up_next)


# --- Now Playing's line -----------------------------------------------------------------

class PartyLine(QWidget):
    """"Listening with Sam and Jo · Sam added Blue in Green": while a party is on."""

    clicked = Signal()

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._session = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Open the listening party")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(40, 2, 12, 2)
        layout.setSpacing(10)
        self.who = QLabel()
        self.who.setStyleSheet(f"color: {C.TEXT}; font-size: 9.8pt; font-weight: 700;")
        layout.addWidget(self.who)
        self.latest = QLabel()
        self.latest.setStyleSheet("color: rgba(255,255,255,0.66); font-size: 9.4pt;")
        layout.addWidget(self.latest, 1)
        self.setVisible(False)
        player.state_changed.connect(self._follow)
        self._follow()                  # made while a party is on already

    def _follow(self) -> None:
        session = self._player.party
        if session is not self._session:
            if self._session is not None:
                for signal, slot in ((self._session.changed, self.refresh),
                                     (self._session.message, self._on_message)):
                    try:
                        signal.disconnect(slot)
                    except (RuntimeError, TypeError):
                        pass
            self._session = session
            if session is not None:
                session.changed.connect(self.refresh)
                session.message.connect(self._on_message)
                self.latest.setText("")
        self.refresh()

    def refresh(self) -> None:
        session = self._session
        on = session is not None and session.on
        self.setVisible(on)
        if not on:
            return
        who = session.listeners_text()
        if not session.is_dj and session.dj_name:
            who += f"  ·  {session.dj_name} is the DJ"
        if session.tuned_out:
            who += "  ·  paused for you"
        self.who.setText(who)

    def _on_message(self, text: str) -> None:
        self.latest.setText(text)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        glyph = QRectF(12, (self.height() - 18) / 2, 18, 18)
        paint_icon(painter, "people", glyph, QColor(C.ACCENT))


# --- Home's card --------------------------------------------------------------------------

class ListeningCard(QFrame):
    """"Sam is listening to your music: Blue in Green · Miles Davis  [Join them]"."""

    join_requested = Signal(int)            # friend id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ListeningCard")
        # The window's Primary is taller than this card leaves room for, and a
        # radius past half a button's height draws square corners: this one's own.
        self.setStyleSheet(f"""
            QFrame#ListeningCard {{ background: {C.SURFACE}; border: 1px solid {C.BORDER_STRONG};
                                     border-radius: 18px; }}
            QPushButton#Primary {{ padding: 10px 24px; font-size: 10.5pt; border-radius: 19px; }}""")
        self._friend_id: int | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(66, 16, 18, 16)
        layout.setSpacing(16)
        words = QVBoxLayout()
        words.setSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet(f"color: {C.TEXT}; font-size: 11pt; font-weight: 700;")
        words.addWidget(self.title)
        self.song = QLabel()
        self.song.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9.8pt;")
        words.addWidget(self.song)
        layout.addLayout(words, 1)
        self.join = _button("Join them", "Primary")
        self.join.setToolTip("Hear what they hear, in step with them: a listening party, with "
                             "them as the DJ.")
        self.join.clicked.connect(lambda: self._friend_id is not None
                                  and self.join_requested.emit(self._friend_id))
        layout.addWidget(self.join, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setVisible(False)

    def set_listening(self, items: list) -> None:
        """The friends listening to your music now (share/presence.py), newest first."""
        playing = [item for item in items if item.playing] or list(items)
        if not playing:
            self._friend_id = None
            self.setVisible(False)
            return
        first = playing[0]
        self._friend_id = first.friend_id
        others = len(playing) - 1
        who = first.name + (f" and {others} other{'s' if others > 1 else ''}" if others else "")
        verb = "are" if others else "is"
        self.title.setText(f"{who} {verb} listening to your music")
        self.song.setText(first.title + (f"  ·  {first.artist}" if first.artist else "")
                          + ("" if first.playing else "  ·  paused"))
        self.join.setText(f"Join {first.name}")
        self.setVisible(True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(super().sizeHint().width(), 76)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        disc = QRectF(18, (self.height() - 36) / 2, 36, 36)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 210, 63, 36))
        painter.drawEllipse(disc)
        paint_icon(painter, "people", disc.adjusted(8, 8, -8, -8), QColor(C.ACCENT))
