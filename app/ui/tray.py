"""The system tray icon: Mistery with no window, still playing."""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..config import settings
from ..util import elide

# The sleep timer's lengths, in minutes: the same as the moon button's menu on
# the bar and on Now Playing (SleepTimerButton.MINUTES in now_playing.py), in
# the same words and order, so the tray offers what the window does. The length
# picked last (music_sleep_last) is offered first when it isn't one of these.
SLEEP_PRESETS = (15, 30, 45, 60)


def minutes_label(minutes: float) -> str:
    return f"{minutes:g} minute{'s' if minutes != 1 else ''}"


def sleep_status(player) -> str:
    """"Sleep timer: 23 min left", or "" with no timer set.

    Rounded up, so the last minute reads "1 min left" rather than "0".
    """
    mode = player.sleep_mode
    remaining = player.sleep_remaining
    if mode is None or remaining is None:
        return ""
    left = f"{max(1, math.ceil(remaining / 60))} min left"
    if mode == "track":
        return f"Sleep timer: end of this song, {left}"
    if mode == "queue":
        return f"Sleep timer: end of the queue, {left}"
    return f"Sleep timer: {left}"


class MisteryTray(QObject):
    show_requested = Signal()
    quit_requested = Signal()

    def __init__(self, player, icon: QIcon, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._icon = QSystemTrayIcon(icon, self)
        self._icon.setToolTip("Mistery")
        self._icon.activated.connect(self._on_activated)

        menu = QMenu()
        self._now = QAction("Nothing playing", menu)
        self._now.setEnabled(False)
        menu.addAction(self._now)
        menu.addSeparator()
        self._toggle = menu.addAction("Pause", player.toggle_pause)
        menu.addAction("Next", player.next)
        menu.addAction("Previous", player.previous)
        menu.addSeparator()
        # Falling asleep to music is a tray thing as much as a window thing: the
        # timer is set, shown and turned off from here too. The submenu's title
        # is the status ("Sleep timer: 23 min left") and its choices are built
        # as it opens, both worked out on the spot, so nothing ticks to keep a
        # label up to date that nobody is looking at.
        self._sleep = QMenu("Sleep timer", menu)
        self._sleep.aboutToShow.connect(self.build_sleep_menu)
        menu.addMenu(self._sleep)
        menu.addSeparator()
        menu.addAction("Open Mistery", self.show_requested.emit)
        menu.addAction("Quit Mistery", self.quit_requested.emit)
        self._menu = menu                     # a tray icon doesn't own its menu
        self._icon.setContextMenu(menu)
        menu.aboutToShow.connect(self.refresh_sleep)

        player.track_changed.connect(lambda _t: self.refresh())
        player.state_changed.connect(self.refresh)
        player.queue_changed.connect(self.refresh_sleep)
        player.sleep_timer_changed.connect(self.refresh_sleep)
        self.refresh()

    @property
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    @property
    def visible(self) -> bool:
        return self._icon.isVisible()

    @property
    def sleep_menu(self) -> QMenu:
        return self._sleep

    def set_visible(self, visible: bool) -> None:
        if visible != self._icon.isVisible():
            self._icon.setVisible(visible)

    def refresh(self) -> None:
        track = self._player.current
        if track:
            title = track.get("title") or ""
            artist = track.get("artist") or track.get("album_artist") or ""
            line = f"{title} — {artist}" if artist else title
            self._now.setText(elide(line, 48))
            # Windows caps tray tooltips at 127 characters.
            state = "" if self._player.is_playing else "Paused · "
            self._icon.setToolTip(elide(f"Mistery · {state}{line}", 120))
        else:
            self._now.setText("Nothing playing")
            self._icon.setToolTip("Mistery")
        self._toggle.setText("Pause" if self._player.is_playing else "Play")
        self._toggle.setEnabled(self._player.has_queue)
        self.refresh_sleep()

    def refresh_sleep(self) -> None:
        self._sleep.setTitle(sleep_status(self._player) or "Sleep timer")
        self._sleep.setEnabled(self._player.has_queue)

    def build_sleep_menu(self) -> None:
        """The sleep timer's choices, as the moon button's menu lays them out:
        the lengths (the current one ticked), the two ends, and Turn off."""
        player = self._player
        mode = player.sleep_mode
        last = settings.get("music_sleep_last", 30)
        menu = self._sleep
        menu.clear()                    # the actions are the menu's own, deleted with it
        choices = list(SLEEP_PRESETS)
        if isinstance(last, (int, float)) and not isinstance(last, bool) and last > 0 \
                and last not in choices:
            choices.insert(0, last)
        for minutes in choices:
            action = menu.addAction(minutes_label(minutes),
                                    lambda m=minutes: player.set_sleep_timer(minutes=m))
            action.setCheckable(True)
            action.setChecked(mode == "minutes" and minutes == last)
        menu.addSeparator()
        has_song = player.current is not None
        for label, end_of in (("End of this song", "track"), ("End of queue", "queue")):
            action = menu.addAction(label, lambda e=end_of: player.set_sleep_timer(end_of=e))
            action.setCheckable(True)
            action.setChecked(mode == end_of)
            action.setEnabled(has_song)
        if mode is not None:
            menu.addSeparator()
            menu.addAction("Turn off timer", player.cancel_sleep_timer)

    def notify(self, title: str, message: str) -> None:
        if self._icon.isVisible() and self._icon.supportsMessages():
            self._icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_requested.emit()
