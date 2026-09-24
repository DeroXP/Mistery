"""Friends: your library and theirs, PC to PC.

The page for library sharing (app/share/): the switch, the codes that make two
Misterys friends, and the list of friends with whether each can be reached.
Browsing a friend's library starts from here; this page is how a friend gets
into the list in the first place.

Adding a friend takes one code, sent either way. Press "Get my code" and send
it, or paste the one a friend sent: the same 13 groups a movie night invite has,
with a 2 in front (app/share/pairing.py). No account, no sign-in, nothing on a
server in between: the code carries this PC's addresses and the fingerprint of
its certificate, and the two Misterys introduce themselves directly.

Everything that touches the network runs on a thread of its own and comes back
through a signal: a friend's PC that is off takes up to 16 s to not answer (8 s
at each of their two addresses), and STUN, asked for this PC's internet address,
up to 2 s.
"""

from __future__ import annotations

import html
import logging
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from .. import db
from ..config import settings
# The movie night panel's pieces, so a friend code looks like an invite code.
from .party_dialog import (
    _MONO, _STYLE, _AMBER, _Disclosure, _Dot, _Spinner, _box, _button, _text, and_list,
    look_up_network, router_steps, split_code, vpn_name, when_text,
)
from .theme import C

_log = logging.getLogger("share")

CHECK_AGAIN_AFTER = 60.0        # the page shown twice in a minute does not knock on every door again
# A friend who has just used this PC's code starts listening a moment after
# pairing ends, not before: knocking at once found nobody there.
NEW_FRIEND_WAIT_MS = 1500


def _port() -> int:
    try:
        return max(1024, min(65535, int(settings.get("party_port", 42170) or 42170)))
    except (TypeError, ValueError):
        return 42170


def _background_holds() -> bool:
    """Whether the Mistery with no window still has the port (its mutex is held)."""
    from ..share import background

    try:
        return background.running()
    except OSError:
        return False


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def library_words(friend_id: int) -> str:
    """"12 films · 3 shows · 5 albums": what we hold of a friend's library."""
    rows = db.query("SELECT kind, COUNT(*) AS n FROM friend_media WHERE friend_id = ? "
                    "GROUP BY kind", (friend_id,))
    counts = {row["kind"]: int(row["n"]) for row in rows}
    parts = []
    for kind, word in (("movie", "film"), ("show", "show"), ("album", "album")):
        if counts.get(kind):
            parts.append(_plural(counts[kind], word))
    if counts.get("track") and not counts.get("album"):
        parts.append(_plural(counts["track"], "song"))
    return " · ".join(parts)


def ago(stamp: float | None, now: float | None = None) -> str:
    """"just now", "5 minutes ago", "3 hours ago", "yesterday", "on 12 Sep"."""
    if not stamp:
        return "never"
    now = now if now is not None else time.time()
    seconds = max(0.0, now - float(stamp))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 6 * 3600:
        hours = int(seconds // 3600)
        return "an hour ago" if hours == 1 else f"{hours} hours ago"
    return when_text(stamp, now)


def reach_view(port: int, lan: str | None, wan: str | None, vpn: str | None,
               listening: bool, error: str | None, forwarded: bool,
               router_forward: bool) -> tuple[str, str, str, bool]:
    """(dot colour, title, text, show the router steps) for a friend code.

    What the code can do, in the movie night panel's words: a code is an
    invite that makes a friend instead of a movie night, and gets through (or
    not) for exactly the same reasons.
    """
    if not listening:
        return (_AMBER, "Mistery can't listen for your friend yet",
                html.escape(error or "The port is busy.") + " The code works as soon as it can.",
                False)
    if not wan:
        if vpn:
            text = (f"{html.escape(vpn_name(vpn))} is on, so Mistery can't find your internet "
                    "address and this code works at your place only. Pause it (or let Mistery "
                    "bypass it), then press Make a new code.")
        else:
            text = ("Mistery couldn't find your internet address, so this code works at your "
                    "place only. Check that you're online, then press Make a new code.")
        return _AMBER, "Friends at your place can use it", text, False
    if router_forward:
        return (C.SUCCESS, "Friends anywhere can use it",
                f"Your router forwards port {port} to this PC while sharing is on.", False)
    if forwarded:
        return (C.INFO, f"You've forwarded port {port} to this PC",
                "One code for friends at your place and elsewhere.", False)
    here = f"this PC ({html.escape(lan)})" if lan else "this PC"
    return (_AMBER, "Friends at your place can use it now",
            f"For friends elsewhere, forward TCP port {port} to {here} in your router's "
            "settings, as below. The same port as movie night: one forward does both.", True)


class FriendRow(QFrame):
    """One friend: who, whether they can be reached, and what they share."""

    check = Signal(int)
    browse = Signal(int)
    pause = Signal(int, bool)           # friend id, paused
    remove = Signal(int)

    def __init__(self, friend, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("FriendRow")
        self.friend_id = int(friend["id"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.dot = _Dot()
        top.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.name = QLabel()
        self.name.setStyleSheet(f"color: {C.TEXT}; font-size: 11.5pt; font-weight: 700;")
        top.addWidget(self.name)
        self.state = QLabel()
        self.state.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9.5pt;")
        top.addWidget(self.state, 1)
        self.spinner = _Spinner(14)
        self.spinner.setVisible(False)
        top.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        # First and white: looking at what they have is what this row is for.
        self.browse_button = _button("Browse", "Primary")
        self.browse_button.setToolTip("Their films, shows and music, even while their PC is off.")
        self.browse_button.clicked.connect(lambda: self.browse.emit(self.friend_id))
        top.addWidget(self.browse_button)
        self.check_button = _button("Check again", "Chip")
        self.check_button.setToolTip("See whether their PC answers, and fetch what's new in "
                                     "their library.")
        self.check_button.clicked.connect(lambda: self.check.emit(self.friend_id))
        top.addWidget(self.check_button)
        self.pause_button = _button("Pause", "Chip")
        self.pause_button.clicked.connect(
            lambda: self.pause.emit(self.friend_id, self.pause_button.text() == "Pause"))
        top.addWidget(self.pause_button)
        self.remove_button = _button("Remove", "Chip")
        self.remove_button.clicked.connect(lambda: self.remove.emit(self.friend_id))
        top.addWidget(self.remove_button)
        layout.addLayout(top)

        self.library = _text("", 9.8, C.TEXT_DIM, rich=False)
        layout.addWidget(self.library)
        self.detail = _text("", 9.2, C.TEXT_FAINT, rich=False)
        self.detail.setVisible(False)
        layout.addWidget(self.detail)
        self._state = "unknown"
        self.update_from(friend)

    def update_from(self, friend) -> None:
        """The row, from the friend's row in the library."""
        self._friend = friend
        self.name.setText(friend["name"])
        paused = not friend["sharing"]
        self.pause_button.setText("Resume" if paused else "Pause")
        self.pause_button.setToolTip(
            "Let them see your library again." if paused else
            "Stop sharing your library with them for now. They stay a friend, and you can "
            "still see theirs.")
        held = library_words(self.friend_id)
        when = friend["catalog_at"]
        if held:
            self.library.setText(f"{held}" + (f", as of {ago(when)}" if when else ""))
        else:
            self.library.setText("Their library shows here once their PC has answered.")
        self._show_state()

    def set_state(self, state: str, text: str = "") -> None:
        """checking | online | offline | closed | trouble, and a sentence for the last three."""
        self._state = state
        self.detail.setText(text)
        self.detail.setVisible(bool(text) and state != "online")
        self._show_state()

    def _show_state(self) -> None:
        friend = self._friend
        checking = self._state == "checking"
        self.spinner.setVisible(checking)
        self.check_button.setEnabled(not checking)
        paused = " · you've paused sharing with them" if not friend["sharing"] else ""
        seen = friend["last_seen"]
        if checking:
            self.dot.set_color(C.TEXT_FAINT)
            self.state.setText("Checking…" + paused)
        elif self._state == "online":
            self.dot.set_color(C.SUCCESS)
            self.state.setText("Online" + paused)
        elif self._state == "closed":
            self.dot.set_color(_AMBER)
            self.state.setText("Online, not sharing with you right now" + paused)
        elif self._state in ("offline", "trouble"):
            self.dot.set_color(C.TEXT_FAINT)
            self.state.setText(("Not reachable" if self._state == "offline" else "Didn't work")
                               + (f" · last seen {ago(seen)}" if seen else "") + paused)
        else:
            self.dot.set_color(C.TEXT_FAINT)
            self.state.setText((f"Last seen {ago(seen)}" if seen else "") + paused)


_PAGE_STYLE = _STYLE + f"""
#FriendRow {{
    background: {C.BG};
    border: 1px solid {C.BORDER};
    border-radius: 10px;
}}
"""


class FriendsView(QWidget):
    """The Friends page."""

    friends_changed = Signal()          # added, removed or paused: the rest of the app may care
    open_friend = Signal(int)           # their library, please (app/ui/friend_library_view.py)
    # Answers from threads, back on Qt's thread.
    _code_ready = Signal(object)        # dict, see _make_code
    _added = Signal(object)             # a friend id, or the sentence saying why not
    _paired = Signal(int)               # somebody used this PC's code (the listener's thread)
    _checked = Signal(int, object)      # friend id, dict
    _switched = Signal(object)          # the sharer's state after the switch moved

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(_PAGE_STYLE)
        self._code_ready.connect(self._on_code_ready)
        self._added.connect(self._on_added)
        self._paired.connect(self._on_paired)
        self._checked.connect(self._on_checked)
        self._switched.connect(self._on_switched)
        self._rows: dict[int, FriendRow] = {}
        self._checked_at: dict[int, float] = {}
        self._checking: set[int] = set()
        self._code = ""
        self._link = ""
        self._watching = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)
        self._scroll = scroll
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(52, 34, 52, 52)
        layout.setSpacing(22)
        scroll.setWidget(page)

        heading = QLabel("Friends")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        intro = _text("Share your library with friends who have Mistery, and watch theirs. "
                      "Films, shows and music stream straight from one PC to the other and are "
                      "never copied, and there's no account to make.", 10.5, C.TEXT_DIM, rich=False)
        layout.addWidget(intro)

        layout.addWidget(self._build_night())
        layout.addWidget(self._build_friends())
        layout.addWidget(self._build_add())
        layout.addWidget(self._build_sharing())
        layout.addStretch(1)
        # The night card follows the movie night while this page is on screen.
        self._night_timer = QTimer(self)
        self._night_timer.setInterval(2000)
        self._night_timer.timeout.connect(self._say_night)
        # Filled when first shown (showEvent), not here: the window makes this
        # page at launch, and reload() pulls in sharing's certificate code.

    # --- building -------------------------------------------------------------

    def _build_night(self) -> QWidget:
        """A friend's movie night on this PC's film (app/share/nights.py), while
        it is on: who started it, of what, who is watching, and End it. Only
        here and on quitting does this PC's owner hear of it at all."""
        card, layout = _card("A movie night on this PC")
        self.night_text = _text("", 10.0, C.TEXT_DIM, rich=False)
        layout.addWidget(self.night_text)
        row = QHBoxLayout()
        self.night_end = _button("End it", "Ghost")
        self.night_end.clicked.connect(self._end_night)
        row.addWidget(self.night_end)
        row.addStretch(1)
        layout.addLayout(row)
        card.setVisible(False)
        self.night_card = card
        return card

    def _build_friends(self) -> QWidget:
        card, layout = _card("Your friends")
        self._empty = _text("No friends yet. Get your code below and send it to a friend, or "
                            "paste the code a friend sent you.", 10.0, C.TEXT_DIM, rich=False)
        layout.addWidget(self._empty)
        self._list = QVBoxLayout()
        self._list.setSpacing(10)
        layout.addLayout(self._list)
        return card

    def _build_add(self) -> QWidget:
        card, layout = _card("Add a friend", "One code, sent either way: yours to them, or "
                             "theirs to you. It adds one friend, then it's used up.")

        # --- yours ---------------------------------------------------------------
        mine = QLabel("SEND THEM YOUR CODE")
        mine.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 8.5pt; font-weight: 700;"
                           " letter-spacing: 1.2px;")
        layout.addWidget(mine)
        # One row, hidden whole while a code shows: its empty wait line still
        # took a line of height, a gap above the code box.
        self.get_row = QWidget()
        get_row = QHBoxLayout(self.get_row)
        get_row.setContentsMargins(0, 0, 0, 0)
        get_row.setSpacing(12)
        self.get_code = _button("Get my code", "Primary")
        self.get_code.clicked.connect(lambda: self._make_code(fresh=False))
        get_row.addWidget(self.get_code)
        self.code_spinner = _Spinner()
        self.code_spinner.setVisible(False)
        get_row.addWidget(self.code_spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        self.code_wait = _text("", 9.5, C.TEXT_DIM, rich=False)
        get_row.addWidget(self.code_wait, 1)
        layout.addWidget(self.get_row)

        self.code_box, code = _box("CodeBox", margins=(22, 18, 22, 18), spacing=12)
        self.code_label = QLabel()
        self.code_label.setStyleSheet(f"color: {C.TEXT}; {_MONO} font-size: 14pt; "
                                      "font-weight: 600; letter-spacing: 1px;")
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setCursor(Qt.CursorShape.IBeamCursor)
        code.addWidget(self.code_label)
        copy_row = QHBoxLayout()
        copy_row.setSpacing(10)
        self.copy_code = _button("Copy code", "Primary")
        self.copy_code.clicked.connect(lambda: self._copy(self._code, "Copied"))
        copy_row.addWidget(self.copy_code)
        self.copy_link = _button("Copy link", "Ghost")
        self.copy_link.setToolTip("The same code as a link your friend can click: it opens Mistery "
                                  "with the code filled in. The code stays after the #, so the "
                                  "website never sees it.")
        self.copy_link.clicked.connect(lambda: self._copy(self._link, "Link copied"))
        copy_row.addWidget(self.copy_link)
        self.copied = QLabel()
        self.copied.setStyleSheet(f"color: {C.SUCCESS}; font-weight: 600;")
        self.copied.setVisible(False)
        copy_row.addSpacing(6)
        copy_row.addWidget(self.copied)
        copy_row.addStretch(1)
        self.new_code = _button("Make a new code", "Chip")
        self.new_code.setToolTip("The code you have now stops working.")
        self.new_code.clicked.connect(lambda: self._make_code(fresh=True))
        copy_row.addWidget(self.new_code)
        self.cancel_code = _button("Cancel it", "Chip")
        self.cancel_code.setToolTip("Nobody can use the code any more.")
        self.cancel_code.clicked.connect(self._cancel_code)
        copy_row.addWidget(self.cancel_code)
        code.addLayout(copy_row)
        self.code_hint = _text("Send it to your friend however you like. In their Mistery they "
                               "open <b>Friends</b> and paste it under <b>Add theirs</b>. It "
                               "works once, for a day.", 9.5, C.TEXT_FAINT)
        code.addWidget(self.code_hint)

        reach = QHBoxLayout()
        reach.setSpacing(10)
        self.reach_dot = _Dot()
        reach.addWidget(self.reach_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.reach_title = QLabel()
        self.reach_title.setStyleSheet(f"color: {C.TEXT}; font-size: 10.5pt; font-weight: 700;")
        reach.addWidget(self.reach_title, 1)
        code.addLayout(reach)
        self.reach_text = _text("", 9.5, C.TEXT_DIM)
        code.addWidget(self.reach_text)
        self.steps = _Disclosure("How to forward the port")
        code.addWidget(self.steps)
        layout.addWidget(self.code_box)

        # --- theirs --------------------------------------------------------------
        layout.addSpacing(6)
        theirs = QLabel("ADD THEIRS")
        theirs.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 8.5pt; font-weight: 700;"
                             " letter-spacing: 1.2px;")
        layout.addWidget(theirs)
        paste_row = QHBoxLayout()
        paste_row.setSpacing(10)
        self.their_code = QLineEdit()
        self.their_code.setObjectName("CodeInput")
        self.their_code.setPlaceholderText("Paste the code or link your friend sent")
        self.their_code.returnPressed.connect(self._add_theirs)
        paste_row.addWidget(self.their_code, 1)
        self.add_button = _button("Add friend", "Primary")
        self.add_button.clicked.connect(self._add_theirs)
        paste_row.addWidget(self.add_button)
        layout.addLayout(paste_row)
        add_status = QHBoxLayout()
        add_status.setSpacing(10)
        self.add_spinner = _Spinner()
        self.add_spinner.setVisible(False)
        add_status.addWidget(self.add_spinner, 0, Qt.AlignmentFlag.AlignTop)
        self.add_note = _text("", 9.8, C.TEXT_DIM, rich=False)
        self.add_note.setVisible(False)
        add_status.addWidget(self.add_note, 1)
        layout.addLayout(add_status)
        return card

    def _build_sharing(self) -> QWidget:
        card, layout = _card("Sharing")
        self.share_box = QCheckBox("Share my library with my friends")
        self.share_box.toggled.connect(self._on_share_toggled)
        layout.addWidget(self.share_box)
        self.music_box = QCheckBox("Include my music")
        self.music_box.toggled.connect(lambda on: self._set("sharing_music", on))
        layout.addWidget(self.music_box)
        self.awake_box = QCheckBox("Keep this PC awake while a friend is watching")
        self.awake_box.setToolTip("Only while something is actually streaming to a friend. "
                                  "The screen is left to go dark as usual.")
        self.awake_box.toggled.connect(lambda on: self._set("sharing_keep_awake", on))
        layout.addWidget(self.awake_box)
        self.background_box = QCheckBox("Keep sharing while Mistery is closed")
        self.background_box.setToolTip(
            "Mistery goes on serving your friends with no window, from when you sign in to "
            "Windows. It steps aside whenever Mistery itself is open, and you'll find it in Task "
            "Manager's Startup list.")
        self.background_box.toggled.connect(self._on_background_toggled)
        layout.addWidget(self.background_box)
        self.share_state = _text("", 9.8, C.TEXT_DIM, rich=False)
        layout.addWidget(self.share_state)
        explain = _text(
            "Only friends you've added can connect: each of your Misterys checks the other's "
            "certificate, the one written down when you added each other, before anything is "
            "said. Friends see titles, artwork and lengths, and stream what they pick; they never "
            "see where your files are, and can't reach anything else on your PC. Sharing uses the "
            "same port as movie night, so while it's on, that port stays open.",
            9.2, C.TEXT_FAINT, rich=False)
        layout.addWidget(explain)
        return card

    # --- keeping it current ---------------------------------------------------------

    def reload(self) -> None:
        """Back in step with the library and settings.json, saving nothing."""
        for box, key, default in ((self.share_box, "sharing_enabled", False),
                                  (self.music_box, "sharing_music", True),
                                  (self.awake_box, "sharing_keep_awake", True),
                                  (self.background_box, "sharing_background", True)):
            box.blockSignals(True)
            box.setChecked(bool(settings.get(key, default)))
            box.blockSignals(False)
        self._reload_friends()
        self._reload_code()
        self._say_sharing()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._watch_pairing()
        self.reload()
        self.check_all()
        self._say_night()
        self._night_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._night_timer.stop()
        super().hideEvent(event)

    # --- a friend's movie night on this PC --------------------------------------------

    @staticmethod
    def _night():
        from .. import share

        nights = getattr(share, "nights", None)     # never imported: never held one
        return nights.current() if nights is not None else None

    def _say_night(self) -> None:
        night = self._night()
        self.night_card.setVisible(night is not None)
        if night is None:
            return
        asked = db.friend(night.asked_by)
        who = asked["name"] if asked is not None else "A friend"
        watching = [str(p.get("name") or "a friend") for p in list(night.hub.people)]
        now = (f"{and_list(watching)} {'is' if len(watching) == 1 else 'are'} watching." if watching
               else "Nobody is in it right now, and it ends by itself in a couple of minutes.")
        self.night_text.setText(f"{who} started a movie night of {night.title} from your "
                                f"library, on this PC, for your friends. {now}")

    def _end_night(self) -> None:
        night = self._night()
        if night is None:
            self._say_night()
            return
        watching = [str(p.get("name") or "a friend") for p in list(night.hub.people)]
        if watching:
            answer = QMessageBox.question(
                self, "End the movie night",
                f"End the movie night of {night.title} for {and_list(watching)}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        from ..party import people
        from ..share import nights

        reason = f"{people.display_name()} ended the movie night on their PC."
        # Off Qt's thread: ending waits for every guest to be told (up to 3 s).
        threading.Thread(target=lambda: nights.end_now(reason), name="share-night-end",
                         daemon=True).start()
        self.night_end.setEnabled(False)
        QTimer.singleShot(600, self, self._after_night_end)

    def _after_night_end(self) -> None:
        self.night_end.setEnabled(True)
        self._say_night()

    def _reload_friends(self) -> None:
        friends = db.friends()
        known = {int(friend["id"]) for friend in friends}
        for friend_id in [f for f in self._rows if f not in known]:
            row = self._rows.pop(friend_id)
            row.setParent(None)
            row.deleteLater()
        for index, friend in enumerate(friends):
            friend_id = int(friend["id"])
            row = self._rows.get(friend_id)
            if row is None:
                row = FriendRow(friend)
                row.check.connect(lambda fid: self.check(fid, force=True))
                row.browse.connect(self.open_friend.emit)
                row.pause.connect(self._pause)
                row.remove.connect(self._remove)
                self._rows[friend_id] = row
            else:
                row.update_from(friend)
            self._list.insertWidget(index, row)
        self._empty.setVisible(not friends)

    def _reload_code(self) -> None:
        """The code box: shown while a code is waiting to be used."""
        from ..share import pairing

        waiting = pairing.pending_secret() is not None and bool(self._code)
        self.code_box.setVisible(waiting)
        self.get_row.setVisible(not waiting)
        if not waiting and pairing.pending_secret() is None:
            self._code = self._link = ""

    def _say_sharing(self) -> None:
        from ..share import sharer as share_sharer

        lender = share_sharer.current()
        on = bool(settings.get("sharing_enabled"))
        friends = len(self._rows)
        if not on:
            text = ("Sharing is off. Friends can't see your library, and you can still see theirs."
                    if friends else "Sharing switches itself on when you add your first friend.")
        elif lender is not None and lender.error and not lender.running and _background_holds():
            # It lets go of the port within a second of Mistery opening, unless a
            # friend's movie night is on this PC: then it serves friends until
            # that ends, and this window takes over (MainWindow._start_sharing).
            text = ("Sharing is on. A friend's movie night is still running on this PC in the "
                    "background, and sharing moves into this window when it ends.")
        elif lender is not None and lender.error and not lender.running:
            text = f"Sharing is on, but Mistery can't listen for friends: {lender.error}"
        elif friends:
            text = (f"Sharing is on. Friends reach this PC on port {_port()}"
                    + (", and a Mistery keeps serving them while this window is closed."
                       if settings.get("sharing_background", True) else "."))
        else:
            text = "Sharing is on. Add a friend to share with."
        self.share_state.setText(text)

    # --- your code ------------------------------------------------------------------

    def _make_code(self, fresh: bool) -> None:
        """Make (or bring back) this PC's code, on a thread: the internet address
        comes from STUN, and the listener has to be up before the code is any use."""
        self.get_code.setEnabled(False)
        self.new_code.setEnabled(False)
        # The spinner and its line are in the row the code box replaces: with a
        # code showing, the button itself says it is working.
        self.new_code.setText("Making…")
        self.code_spinner.setVisible(True)
        self.code_wait.setText("Finding your internet address…")
        self._watch_pairing()

        def make() -> None:
            try:
                from ..party import stun
                from ..share import pairing, sharer as share_sharer

                facts = look_up_network()
                lan = facts.lan_ip
                wan = stun.public_ip(lan, timeout=2.0) if lan else None
                port = _port()
                code = pairing.offer(port, lan, wan, fresh=fresh)
                lender = share_sharer.sharer()
                lender.note_addresses(lan, wan)
                lender.friends_changed()        # a code waiting is reason enough to listen
                self._code_ready.emit({
                    "code": code, "link": pairing.link(code), "port": port, "lan": lan,
                    "wan": wan, "facts": facts, "listening": lender.running,
                    "error": lender.error, "router_forward": lender.mapping is not None})
            except Exception as problem:            # noqa: BLE001 - said on the page
                _log.exception("share: making a friend code failed")
                self._code_ready.emit({"problem": str(problem)})
            finally:
                db.close_thread_connection()

        threading.Thread(target=make, name="share-code", daemon=True).start()

    def _on_code_ready(self, made: dict) -> None:
        self.get_code.setEnabled(True)
        self.new_code.setEnabled(True)
        self.new_code.setText("Make a new code")
        self.code_spinner.setVisible(False)
        self.code_wait.setText("")
        if "problem" in made:
            # In the row with Get my code, shown for it even if a code was up:
            # what that code can still do is no longer certain.
            self.code_box.setVisible(False)
            self.get_row.setVisible(True)
            self.code_wait.setText("Mistery couldn't make a code: " + made["problem"])
            return
        self._code, self._link = made["code"], made["link"]
        self.code_label.setText(split_code(self._code))
        self.copied.setVisible(False)
        facts = made["facts"]
        colour, title, text, steps = reach_view(
            made["port"], made["lan"], made["wan"], facts.vpn, made["listening"], made["error"],
            bool(settings.get("party_forwarded", False)), made["router_forward"])
        self.reach_dot.set_color(colour)
        self.reach_title.setText(title)
        self.reach_text.setText(text)
        self.steps.setVisible(steps)
        if steps:
            self.steps.body.setText(router_steps(made["port"], made["lan"], facts.router, facts))
        self._reload_code()
        self._say_sharing()

    def _cancel_code(self) -> None:
        from ..share import pairing, sharer as share_sharer

        pairing.cancel()
        self._code = self._link = ""
        self._reload_code()
        # Nothing to listen for any more, unless there are friends to serve.
        threading.Thread(target=lambda: _quietly(share_sharer.sharer().friends_changed),
                         name="share-switch", daemon=True).start()

    def _copy(self, text: str, said: str) -> None:
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.copied.setText("✓ " + said)
        self.copied.setVisible(True)

    def _watch_pairing(self) -> None:
        """Hear about somebody using this PC's code, whichever page is showing."""
        if self._watching:
            return
        from ..share import sharer as share_sharer

        share_sharer.sharer().on_paired.append(self._paired.emit)
        self._watching = True

    def _on_paired(self, friend_id: int) -> None:
        friend = db.friend(friend_id)
        self._code = self._link = ""
        self._reload_code()
        self._reload_friends()
        self._say_sharing()
        self._set_share_box(bool(settings.get("sharing_enabled")))
        if friend is not None:
            self._note(f"{friend['name']} used your code. You're friends now.", C.SUCCESS)
            self._rows[friend_id].set_state("checking")
            QTimer.singleShot(NEW_FRIEND_WAIT_MS, self, lambda: self.check(friend_id, force=True))
        self.friends_changed.emit()

    # --- their code -----------------------------------------------------------------

    def _add_theirs(self) -> None:
        from ..share import pairing

        text = self.their_code.text().strip()
        if not text:
            self._note("Paste the code your friend sent first.", C.TEXT_DIM)
            return
        try:
            code = pairing.read_code(text)
        except pairing.PairError as problem:
            self._note(str(problem), C.DANGER)
            return
        self.add_button.setEnabled(False)
        self.add_spinner.setVisible(True)
        self._note("Reaching your friend's PC…", C.TEXT_DIM, spinner=True)

        def add() -> None:
            try:
                from ..party import stun
                from ..share import sharer as share_sharer

                lender = share_sharer.sharer()
                lan = lender.lan_ip or look_up_network().lan_ip
                wan = lender.wan_ip or (stun.public_ip(lan, timeout=2.0) if lan else None)
                friend_id = pairing.request(code, port=_port(), lan_ip=lan, wan_ip=wan)
                lender.note_addresses(lan, wan)
                lender.friends_changed()        # a friend to serve: the listener starts
                self._added.emit(friend_id)
            except pairing.PairError as problem:
                self._added.emit(str(problem))
            except Exception:                       # noqa: BLE001 - said on the page
                _log.exception("share: adding a friend failed")
                self._added.emit("Something went wrong adding your friend. Try the code again.")
            finally:
                db.close_thread_connection()

        threading.Thread(target=add, name="share-add", daemon=True).start()

    def _on_added(self, result) -> None:
        self.add_button.setEnabled(True)
        self.add_spinner.setVisible(False)
        if isinstance(result, str):
            self._note(result, C.DANGER)
            return
        friend = db.friend(int(result))
        self.their_code.clear()
        self._reload_friends()
        self._set_share_box(bool(settings.get("sharing_enabled")))
        self._say_sharing()
        if friend is not None:
            self._note(f"You and {friend['name']} are friends now.", C.SUCCESS)
            self.check(int(result), force=True)
        self.friends_changed.emit()

    def prefill(self, code: str) -> None:
        """A friend's code from a clicked link (MainWindow.open_link): in the
        box, waiting for Add friend, never added by itself."""
        self.their_code.setText(code)
        self._note("This code came from a link. If it's from a friend of yours, press Add friend.",
                   C.TEXT_DIM)
        self.add_button.setFocus()
        self._scroll.ensureWidgetVisible(self.add_button)

    def _note(self, text: str, colour: str, spinner: bool = False) -> None:
        self.add_spinner.setVisible(spinner)
        self.add_note.setStyleSheet(f"color: {colour}; font-size: 9.8pt;"
                                    + (" font-weight: 600;" if colour == C.SUCCESS else ""))
        self.add_note.setText(text)
        self.add_note.setVisible(bool(text))

    # --- friends ----------------------------------------------------------------------

    def check_all(self) -> None:
        for friend_id in list(self._rows):
            self.check(friend_id)

    def check(self, friend_id: int, force: bool = False) -> None:
        """Knock on a friend's door, on a thread, and take what's new in their library."""
        if friend_id in self._checking:
            return
        if not force and time.monotonic() - self._checked_at.get(friend_id, -1e9) < CHECK_AGAIN_AFTER:
            return
        row = self._rows.get(friend_id)
        if row is None:
            return
        self._checking.add(friend_id)
        row.set_state("checking")

        def run() -> None:
            from ..share import client

            result: dict = {"state": "trouble", "text": ""}
            try:
                friend = db.friend(friend_id)
                if friend is None:
                    result = {"state": "gone"}
                    return
                with client.Channel(friend) as channel:
                    if channel.hello().get("sharing") is False:
                        result = {"state": "closed",
                                  "text": "They've paused sharing with you, or switched sharing "
                                          "off. Their library stays as you last saw it."}
                    else:
                        result = {"state": "online", "changed": channel.refresh()}
            except client.Unreachable as problem:
                # The client words it for a friend (client.unreachable_words).
                result = {"state": "offline", "text": str(problem)}
            except client.ShareError as problem:
                result = {"state": "trouble", "text": str(problem)}
            except Exception:                       # noqa: BLE001 - one friend, not the page
                _log.exception("share: checking on a friend failed")
                result = {"state": "trouble", "text": "Something went wrong talking to their "
                                                      "Mistery."}
            finally:
                db.close_thread_connection()
                self._checked.emit(friend_id, result)

        threading.Thread(target=run, name="share-check", daemon=True).start()

    def _on_checked(self, friend_id: int, result: dict) -> None:
        self._checking.discard(friend_id)
        self._checked_at[friend_id] = time.monotonic()
        row = self._rows.get(friend_id)
        friend = db.friend(friend_id)
        if row is None or friend is None:
            return
        row.update_from(friend)
        row.set_state(result.get("state", "trouble"), result.get("text", ""))

    def _pause(self, friend_id: int, paused: bool) -> None:
        from ..share import sharer as share_sharer

        db.update_friend(friend_id, sharing=0 if paused else 1)
        friend = db.friend(friend_id)
        row = self._rows.get(friend_id)
        if row is not None and friend is not None:
            row.update_from(friend)
        threading.Thread(target=lambda: _quietly(share_sharer.sharer().friends_changed),
                         name="share-switch", daemon=True).start()
        self.friends_changed.emit()

    def _remove(self, friend_id: int) -> None:
        from ..share import sharer as share_sharer

        friend = db.friend(friend_id)
        if friend is None:
            return
        name = friend["name"]
        answer = QMessageBox.question(
            self, f"Remove {name}?",
            f"{name} won't be able to see your library any more, and their library goes from "
            "yours.\n\nTo be friends again, one of you sends the other a new code.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        db.remove_friend(friend_id)
        from ..share import art

        art.forget(friend_id)               # our copies of their pictures go too
        self._checked_at.pop(friend_id, None)
        self._reload_friends()
        self._say_sharing()
        threading.Thread(target=lambda: _quietly(share_sharer.sharer().friends_changed),
                         name="share-switch", daemon=True).start()
        self.friends_changed.emit()

    # --- the switch -------------------------------------------------------------------

    def _on_share_toggled(self, on: bool) -> None:
        from ..share import sharer as share_sharer

        settings.set("sharing_enabled", bool(on))
        self.share_state.setText("Starting…" if on else "Stopping…")

        def switch() -> None:
            lender = share_sharer.sharer()
            _quietly(lender.friends_changed)
            self._switched.emit(lender.error)

        threading.Thread(target=switch, name="share-switch", daemon=True).start()

    def _on_switched(self, _error) -> None:
        self._say_sharing()

    def _set_share_box(self, on: bool) -> None:
        self.share_box.blockSignals(True)
        self.share_box.setChecked(on)
        self.share_box.blockSignals(False)

    @staticmethod
    def _set(key: str, on: bool) -> None:
        settings.set(key, bool(on))

    def _on_background_toggled(self, on: bool) -> None:
        """The sign-in entry follows at once, and unticked, a Mistery already
        serving in the background is asked to leave (the updater's own knock)."""
        settings.set("sharing_background", bool(on))

        def follow() -> None:
            from ..share import background

            background.sync()
            if not on:
                background.ask_to_stop()

        threading.Thread(target=lambda: _quietly(follow), name="share-background",
                         daemon=True).start()
        self._say_sharing()


def _card(title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
    """A settings-style card: a title, a line under it, and room for the rest."""
    card = QWidget()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(24, 20, 24, 22)
    layout.setSpacing(14)
    heading = QLabel(title)
    heading.setObjectName("SectionTitle")
    layout.addWidget(heading)
    if subtitle:
        note = _text(subtitle, 9.5, C.TEXT_FAINT, rich=False)
        layout.addWidget(note)
    return card, layout


def _quietly(work) -> None:
    """Sharing switched on or off, on a thread: a failure is logged, never raised."""
    try:
        work()
    except Exception:                               # noqa: BLE001
        _log.exception("share: starting or stopping sharing failed")
    finally:
        db.close_thread_connection()
