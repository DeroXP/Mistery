"""Application shell: top navigation, page stack and background wiring."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

import logging
import time

from .. import db, images, vr
from ..config import icon_path, settings
from ..models import MediaItem, ShowItem
from ..util import reveal_in_explorer
from ..workers import LibraryService
from ..music import library as music_library
from ..music import lyrics as music_lyrics
from ..music.player import MusicPlayer
from .album_view import AlbumView, ArtistView
from .detail_view import DetailView
from .home_view import HomeView
from .library_view import LibraryView
from .music_view import MusicView
from .playlists_view import PlaylistsView
from .now_playing import NowPlayingBar, NowPlayingView
from .player_view import PlayerView
from .settings_view import SettingsView
from .show_view import ShowView
from .theme import STYLESHEET, TOPBAR_H, C
from .tray import MisteryTray
from .widgets.icons import IconButton
from .widgets.transition import HeroTransition

_NAV = [
    ("home", "Home"),
    ("film", "Movies"),
    ("tv", "Shows"),
    ("music", "Music"),
    ("queue", "Playlists"),
    ("search", "Search"),
    ("settings", "Settings"),
]
_NAV_SEARCH = next(i for i, (name, _) in enumerate(_NAV) if name == "search")
_NAV_SETTINGS = next(i for i, (name, _) in enumerate(_NAV) if name == "settings")
_NAV_MUSIC = next(i for i, (name, _) in enumerate(_NAV) if name == "music")
_NAV_PLAYLISTS = next(i for i, (name, _) in enumerate(_NAV) if name == "queue")


class NavButton(QPushButton):
    """A text nav item in the top bar, underlined in red while it's the page."""

    def __init__(self, icon_name: str, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon_name = icon_name

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.isChecked():
            return
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C.ACCENT))
        painter.drawRoundedRect(
            QRectF(12, self.height() - 4.0, self.width() - 24, 2.5), 1.2, 1.2
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Mistery")
        self.resize(1440, 900)
        self.setMinimumSize(960, 620)
        self.setStyleSheet(STYLESHEET)

        icon = icon_path()
        if icon:
            self.setWindowIcon(QIcon(str(icon)))

        self.service = LibraryService(self)
        # (stack index, what that page was showing) — see _page_state.
        self._history: list[tuple[int, object]] = []
        self._suppress_nav = False

        root = QWidget()
        root.setObjectName("RootPane")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(root)

        self.topbar = self._build_topbar()
        layout.addWidget(self.topbar)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.transition = HeroTransition(self)
        self.music = MusicPlayer(self)
        self._in_tray = False
        self._quitting = False
        self._maximized_before_fullscreen = False
        self._last_played: MediaItem | None = None     # this playback's latest title
        # When the song's countdown was last sent to Discord, and from where:
        # (monotonic time, position, playing). See _on_music_position.
        self._presence_anchor: tuple[float, float, bool] | None = None
        self.tray = MisteryTray(self.music, self.windowIcon(), self)
        self.tray.show_requested.connect(self.show_from_tray)
        self.tray.quit_requested.connect(self.quit_app)

        self.home = HomeView()
        self.movies = LibraryView("movies")
        self.shows = LibraryView("shows")
        self.search = LibraryView("search")
        self.detail = DetailView()
        self.show_page = ShowView()
        self.settings_page = SettingsView()
        self.player = PlayerView()
        self.music_page = MusicView(self.music)
        self.playlists_page = PlaylistsView()
        self.album_page = AlbumView(self.music)
        self.artist_page = ArtistView(self.music)
        self.now_playing = NowPlayingView(self.music)

        for page in (self.home, self.movies, self.shows, self.search,
                     self.detail, self.show_page, self.settings_page, self.player,
                     self.music_page, self.playlists_page, self.album_page, self.artist_page,
                     self.now_playing):
            self.stack.addWidget(page)

        # Spotify's defining piece of furniture: the player that follows you.
        self.now_bar = NowPlayingBar(self.music)
        self.now_bar.setVisible(False)
        layout.addWidget(self.now_bar)

        self._wire()
        self._install_shortcuts()

        self.stack.setCurrentWidget(self.home)
        # Last time's queue, paused on the song and the second it was left at.
        # Everything is wired by now, so the bar, the tray menu and the shortcuts
        # pick it up from the signals it sends, before the window is first shown.
        # Nothing plays or starts mpv until Play (restored 22 songs in 0.9 ms,
        # 1110 in 8.7 ms), and Discord is told nothing: see _publish_music_presence.
        self.music.restore_session()
        self.reload_all()

        # Pick up database edits made outside this window (a maintenance script,
        # a second instance) rather than showing a stale library until restart.
        self._data_version = db.data_version()
        self._external_watch = QTimer(self)
        self._external_watch.setInterval(2500)
        self._external_watch.timeout.connect(self._check_external_changes)
        self._external_watch.start()

        QTimer.singleShot(150, self._initial_scan)
        # Artwork that lost a race with a scanner, a backup or the artwork pass
        # itself gets another go. This used to happen once, nine seconds in,
        # which only covered the failures that happened during startup — a cover
        # that failed while you were looking at the page kept its placeholder
        # for the rest of the session. It costs a set lookup when nothing has
        # failed, and each file is only offered a few times.
        self._art_retry = QTimer(self)
        self._art_retry.setInterval(15_000)
        self._art_retry.timeout.connect(self._retry_failed_artwork)
        self._art_retry.start()

        # Albums arrive by download. Check often while something is still
        # arriving, rarely otherwise, so finished songs appear without a rescan.
        self._music_watch = QTimer(self)
        self._music_watch.setSingleShot(True)
        self._music_watch.timeout.connect(self._watch_music)
        self._music_watch.start(30_000)

    def _retry_failed_artwork(self) -> None:
        failed = images.failed_paths()
        if not failed or self._in_tray or self.stack.currentWidget() is self.player:
            return
        logging.getLogger("images").info(
            "retrying %d artwork file(s) that failed to load", len(failed))
        images.clear_failures()
        self.reload_all()

    def _check_external_changes(self) -> None:
        if self.stack.currentWidget() is self.player or self.service.busy:
            return                      # our own writes, or mid-playback
        version = db.data_version()
        if version != self._data_version:
            self._data_version = version
            self.reload_all()

    # --- construction -------------------------------------------------------

    def _build_topbar(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("TopBar")
        panel.setFixedHeight(TOPBAR_H)
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(36, 0, 28, 0)
        layout.setSpacing(4)

        brand = QLabel("MISTERY")
        brand.setObjectName("Brand")
        layout.addWidget(brand)
        layout.addSpacing(22)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for index, (icon_name, text) in enumerate(_NAV):
            button = NavButton(icon_name, text)
            button.setChecked(index == 0)
            self._nav_group.addButton(button, index)
            layout.addWidget(button)
        self._nav_group.idClicked.connect(self._on_nav)

        layout.addStretch(1)

        self._status = QLabel()
        self._status.setObjectName("NavStats")
        self._status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._status)
        layout.addSpacing(14)

        self._rescan = IconButton("refresh", size=36, icon_size=19,
                                  tooltip="Rescan the library  (Ctrl+R)")
        self._rescan.clicked.connect(lambda: self.service.refresh(force=False))
        layout.addWidget(self._rescan)
        return panel

    def _wire(self) -> None:
        self.home.play_requested.connect(lambda item: self.play(item))
        self.home.play_in_vr_requested.connect(self.play_in_vr)
        self.home.item_action.connect(self._on_card_action)
        self.home.open_media.connect(self.open_media)
        self.home.open_show.connect(self.open_show)
        self.home.add_folder_requested.connect(self._add_folder)

        for view in (self.movies, self.shows, self.search):
            view.play_requested.connect(lambda item: self.play(item))
            view.item_action.connect(self._on_card_action)
            view.open_media.connect(self.open_media)
            view.open_show.connect(self.open_show)
            view.add_folder_requested.connect(self._add_folder)

        # Not a bare play(): the rest of the playlist goes with it, so Up Next
        # and autoplay work for a list of films the way they do for a series.
        self.playlists_page.play_requested.connect(
            lambda item: self.play(item, line_up=self.playlists_page.line_up))
        self.playlists_page.item_action.connect(self._on_card_action)
        self.playlists_page.open_media.connect(self.open_media)
        self.playlists_page.open_playlist.connect(self.open_playlist)

        self.show_page.item_action.connect(self._on_card_action)

        self.detail.play_requested.connect(self.play)
        self.detail.play_in_vr_requested.connect(self.play_in_vr)
        self.detail.back_requested.connect(self.go_back)
        self.detail.media_changed.connect(self.reload_all)

        self.show_page.play_requested.connect(self.play)
        self.show_page.open_media.connect(self.open_media)
        self.show_page.back_requested.connect(self.go_back)

        self.settings_page.rescan_requested.connect(
            lambda force: self.service.refresh(force=force)
        )
        self.settings_page.discord_changed.connect(self.player.apply_presence_settings)
        # After the player has (re)started presence: the player only republishes
        # a film, so ticking "Hide titles" mid-song left the song's title on the
        # profile until the track changed, and presence turned on mid-song
        # showed nothing for it.
        self.settings_page.discord_changed.connect(self._publish_music_presence)
        self.settings_page.sound_changed.connect(self.music.apply_sound)
        self.settings_page.music_cover_changed.connect(self._on_music_cover_changed)

        self.player.closed.connect(self._on_player_closed)
        self.player.playback_started.connect(self.transition.reveal)
        self.player.playback_started.connect(self._remember_played)
        self.player.transition_requested.connect(
            lambda pixmap, rect: self.transition.start(pixmap, rect, self._video_rect())
        )
        self.player.fullscreen_requested.connect(self._set_fullscreen)
        self.player.progress_changed.connect(self._on_progress_changed)
        self.player.error.connect(self._on_player_error)

        self.music_page.album_opened.connect(self.open_album)
        self.music_page.artist_opened.connect(self.open_artist)
        # "Added to Road trip." from a song's right-click menu. The music pages
        # have no status bar of their own, and the page itself does not change
        # when a song joins a list that is not on screen.
        self.music_page.status.connect(self._on_status)
        self.album_page.status.connect(self._on_status)
        self.artist_page.status.connect(self._on_status)
        self.album_page.back_requested.connect(self.go_back)
        self.album_page.artist_requested.connect(self.open_artist)
        self.artist_page.back_requested.connect(self.go_back)
        self.artist_page.album_requested.connect(self.open_album)
        self.now_playing.back_requested.connect(self.go_back)
        self.now_playing.album_requested.connect(self.open_album)
        self.now_playing.artist_requested.connect(self.open_artist)
        self.now_bar.expand_requested.connect(self.open_now_playing)
        self.now_bar.album_requested.connect(self.open_album)
        self.now_bar.artist_requested.connect(self.open_artist)
        # "Playing from …" on Now Playing: an album or an artist comes through
        # album_requested / artist_requested above, and the rest (Liked Songs,
        # Songs, a search) as the context itself.
        self.now_playing.context_requested.connect(self.open_context)
        # The lyrics screensaver takes the whole screen for itself, and gives it
        # back when it ends. It asks rather than calling showFullScreen, because
        # leaving full screen has to put a maximised window back as it was
        # (_set_fullscreen) and the film player uses the same route.
        self.now_playing.fullscreen_requested.connect(self._set_fullscreen)

        self.music.track_changed.connect(self._on_music_track)
        self.music.state_changed.connect(self._on_music_state)
        self.music.position_changed.connect(self._on_music_position)
        self.music.queue_changed.connect(self._update_chrome)
        self.music.sleep_timer_fired.connect(self._on_sleep_timer_fired)
        self.music.error.connect(self._on_status)
        self.service.music_changed.connect(self._on_music_library_changed)

        self.service.status.connect(self._on_status)
        self.service.library_changed.connect(self.reload_all)
        self.service.busy_changed.connect(
            lambda busy: self._rescan.setEnabled(not busy)
        )
        self.service.scan_finished.connect(self._on_scan_finished)
        self.service.error.connect(lambda text: self._on_status(text.splitlines()[-1][:120]))

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._on_search_shortcut)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=lambda: self.service.refresh())
        QShortcut(QKeySequence("Alt+Left"), self, activated=self._on_back_shortcut)
        # The music keys are shortcuts rather than keyPressEvent cases because a
        # window only hears the keys its focus widget turns down, and a text
        # button that has just been clicked keeps focus and takes Space as a
        # second click: Space after Play on an album restarted the album from
        # song 1 instead of pausing, and after a top-bar tab it reloaded the
        # page. Arrow keys on a focused button move focus along its group, so
        # Ctrl+Right after a tab click opened the next tab. A shortcut is asked
        # first, and text fields still keep their keys — a QLineEdit claims
        # Space and Ctrl+arrows through ShortcutOverride, so typing a search is
        # unaffected. They're only enabled while there's music to drive and no
        # film on screen (the film player has its own Space); see _update_chrome.
        self._music_shortcuts = [
            QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.music.toggle_pause,
                      autoRepeat=False),
            QShortcut(QKeySequence("Ctrl+Right"), self, activated=self.music.next),
            QShortcut(QKeySequence("Ctrl+Left"), self, activated=self.music.previous),
            # There was no way to change the music volume without the mouse.
            # Ctrl with the up/down arrows, Shift for single steps, Ctrl+M to
            # mute — off while a film is on screen, like the rest of these.
            QShortcut(QKeySequence("Ctrl+Up"), self, activated=lambda: self._nudge_music_volume(5)),
            QShortcut(QKeySequence("Ctrl+Down"), self, activated=lambda: self._nudge_music_volume(-5)),
            QShortcut(QKeySequence("Ctrl+Shift+Up"), self, activated=lambda: self._nudge_music_volume(1)),
            QShortcut(QKeySequence("Ctrl+Shift+Down"), self, activated=lambda: self._nudge_music_volume(-1)),
            QShortcut(QKeySequence("Ctrl+M"), self, activated=self._toggle_music_mute),
            # The media keys too. Qt passes a media key on to Windows unless a
            # shortcut claims it (QTBUG-43343, qwindowskeymapper.cpp), and
            # Windows hands it to the active media session, which is Mistery's
            # own music: handled in keyPressEvent as well, one press with the
            # window focused could pause and play again. With a shortcut Qt
            # keeps it, so it acts once; with nothing to drive (the shortcuts
            # off), it still reaches whatever other player is active.
            QShortcut(QKeySequence(Qt.Key.Key_MediaTogglePlayPause), self,
                      activated=self.music.toggle_pause, autoRepeat=False),
            QShortcut(QKeySequence(Qt.Key.Key_MediaPlay), self, activated=self.music.play, autoRepeat=False),
            QShortcut(QKeySequence(Qt.Key.Key_MediaPause), self, activated=self.music.pause, autoRepeat=False),
            QShortcut(QKeySequence(Qt.Key.Key_MediaNext), self, activated=self.music.next),
            QShortcut(QKeySequence(Qt.Key.Key_MediaPrevious), self, activated=self.music.previous),
        ]
        for shortcut in self._music_shortcuts:
            shortcut.setEnabled(False)

    def _nudge_music_volume(self, step: int) -> None:
        # Reaching for the volume is never a way of asking to stay silent, so
        # a press unmutes — but a press asking for quieter has to be quieter,
        # or Ctrl+Down out of silence is full volume, which at night is the
        # one thing nobody means by it. The bar does the same: it sets the
        # level you dragged to and unmutes on the way.
        if self.music.muted:
            self.music.set_muted(False)
            if step > 0:
                return
        self.music.set_volume(max(0, min(100, int(self.music.volume) + step)))

    def _toggle_music_mute(self) -> None:
        self.music.set_muted(not self.music.muted)

    def _on_back_shortcut(self) -> None:
        # Window-wide shortcuts fire before the player's own key handling, and
        # go_back() only swaps the page: the film played on, audio and progress
        # saves included, with no controls left to stop it. Back from a film is
        # closing it, as Esc and the overlay's arrow do, and closing goes back.
        if self.stack.currentWidget() is self.player:
            self.player.stop_and_close()
        else:
            self.go_back()

    def _on_search_shortcut(self) -> None:
        # The top bar is hidden while a film plays, so Search isn't somewhere to
        # go from there — and a stray Ctrl+F shouldn't end the film either.
        if self.stack.currentWidget() is self.player:
            return
        self._on_nav(_NAV_SEARCH)

    # --- navigation ---------------------------------------------------------

    def _on_nav(self, index: int) -> None:
        # Parallel to _NAV, and it has to stay that way: an entry added to one
        # list and not the other silently shifts every page after it.
        page = [self.home, self.movies, self.shows, self.music_page, self.playlists_page,
                self.search, self.settings_page][index]
        if page is self.search:
            self.search.focus_search()
        elif page is self.playlists_page:
            self.playlists_page.show_playlist(None)
        elif page is self.music_page:
            # Same answer as Playlists above: a nav entry is "take me to that
            # page", not "take me back to where I was in it". Without this,
            # MusicView.reload early-returns into whatever playlist was last
            # opened, and _on_nav has just cleared the history so Back is no
            # help either — the page's own arrow was the only way out.
            self.music_page.show_tab(self.music_page.current_tab)
        self._history.clear()
        self._go(page, push=False)
        self._sync_nav(index)

    def _sync_nav(self, index: int | None) -> None:
        self._suppress_nav = True
        buttons = self._nav_group.buttons()
        self._nav_group.setExclusive(False)
        for i, button in enumerate(buttons):
            button.setChecked(index is not None and i == index)
        self._nav_group.setExclusive(True)
        self._suppress_nav = False

    def _go(self, page: QWidget, push: bool = True) -> None:
        current = self.stack.currentWidget()
        if push and current is not page:
            self._history.append((self.stack.currentIndex(), self._page_state(current)))
        # Shown first, loaded second: a page whose record has gone asks to go
        # back from inside reload(), and that must leave from this page. Loaded
        # while still hidden, the request popped the entry for the page before
        # it, and then this page was shown anyway, stale.
        self.stack.setCurrentWidget(page)
        self._update_chrome()
        if hasattr(page, "reload") and page not in (self.detail, self.show_page):
            page.reload()

    def _page_state(self, page: QWidget):
        """What a page is pointed at, for the pages that are re-pointed rather
        than rebuilt — one album page serves every album."""
        if page is self.album_page:
            return self.album_page.album_id
        if page is self.artist_page:
            return self.artist_page.artist
        if page is self.detail:
            return self.detail._item
        if page is self.show_page:
            return self.show_page._show
        if page is self.playlists_page:
            return self.playlists_page.playlist_id
        return None

    def go_back(self) -> None:
        if not self._history:
            self._go(self.home, push=False)
            self._sync_nav(0)
            return
        index, state = self._history.pop()
        self.stack.setCurrentIndex(index)
        self._update_chrome()
        page = self.stack.currentWidget()
        # History used to hold page indexes only, so album A > artist > album B
        # > Back > Back came back to album B: the page showed whatever it was
        # last pointed at, and Play played that. Re-point it at what it showed.
        if page is self.album_page and state is not None and state != page.album_id:
            page.set_album(state)
        elif page is self.artist_page and state and state != page.artist:
            page.set_artist(state)
        elif page is self.detail and state is not None and state.id != page._item.id:
            page.set_media(state)
        elif page is self.show_page and state is not None and state.id != page._show.id:
            page.set_show(state)
        elif page is self.playlists_page and state != page.playlist_id:
            # One page serves every playlist, as the album page serves every
            # album: point it back at the list this history entry was showing.
            page.show_playlist(state)
            page.reload()
        elif hasattr(page, "reload") and page not in (self.detail, self.show_page):
            page.reload()

    def open_media(self, item: MediaItem) -> None:
        self.detail.set_media(item)
        self._go(self.detail)
        self._sync_nav(None)

    def open_show(self, show: ShowItem) -> None:
        self.show_page.set_show(show)
        self._go(self.show_page)
        self._sync_nav(None)

    def _update_chrome(self) -> None:
        page = self.stack.currentWidget()
        # The film player and the full Now Playing view both take the window.
        immersive = page is self.player or page is self.now_playing
        self.topbar.setVisible(not immersive)
        self.now_bar.setVisible(self.music.has_queue and not immersive)
        # The tray icon is there whenever there's music to control, and always
        # while it's the only way back into the app.
        self.tray.set_visible(self._in_tray or self.music.has_queue)
        music_keys = self.music.has_queue and page is not self.player
        for shortcut in self._music_shortcuts:
            shortcut.setEnabled(music_keys)

    # --- the tray -----------------------------------------------------------

    def go_to_tray(self) -> None:
        """No window, music still playing, and as little else running as possible."""
        if self.stack.currentWidget() is self.player:
            self.player.stop_and_close()        # a film never plays on unseen
        # From here the app may end without _shutdown: a game crashing the
        # machine, the power going. The player saves every 15 s while playing
        # anyway; this makes the session exact as of leaving the window.
        self.music.save_session()
        self._in_tray = True
        self.tray.set_visible(True)
        self.hide()
        self._set_background_mode(True)
        if not settings.get("tray_hint_shown"):
            settings.set("tray_hint_shown", True)
            self.tray.notify("Mistery is still playing",
                             "Music carries on from here. Click the icon to open Mistery, "
                             "or right-click it for playback controls.")

    def show_from_tray(self) -> None:
        was_in_tray = self._in_tray
        self._in_tray = False
        self._set_background_mode(False)
        if self.isMinimized():
            # Un-minimised only. showNormal() also dropped fullscreen and
            # maximised: a film minimised in fullscreen came back in a window
            # while the player still believed it was fullscreen, so the first
            # F did nothing visible and the first Esc closed nothing. A
            # minimised window keeps those flags alongside WindowMinimized.
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        self._update_chrome()
        if was_in_tray:
            current = self.music.current
            if current:
                self._fetch_lyrics(current, show=True)    # skipped while hidden
            QTimer.singleShot(1500, self._watch_music)     # downloads may have finished

    def quit_app(self) -> None:
        """Really quit — the tray's Quit, as opposed to closing the window."""
        self._quitting = True
        self._shutdown()
        QApplication.instance().quit()

    def _set_background_mode(self, enabled: bool) -> None:
        """Everything that doesn't need to run when nobody is looking, stopped.

        Hiding a window stops Qt painting it and nothing else — measured, a
        hidden window with music playing cost the same as a visible one. The
        cost was elsewhere: mpv streaming the playback position to seek bars,
        the library watchers, and the background pipeline, which runs ffmpeg.
        """
        self.music.set_low_power(enabled or self.isMinimized())
        playing_film = self.stack.currentWidget() is self.player
        pause_library = enabled or (playing_film and settings.get("pause_background_during_playback", True))
        self.service.set_paused(pause_library)
        if enabled:
            self._external_watch.stop()
            self._music_watch.stop()
            self._art_retry.stop()
        else:
            self._external_watch.start()
            self._schedule_music_watch()
            self._art_retry.start()

    @property
    def in_tray(self) -> bool:
        return self._in_tray

    # --- music --------------------------------------------------------------

    def open_album(self, album_id: int) -> None:
        # Checked before the page is pointed at it: a page with nothing to show
        # asks to go back from inside set_album, and while it is still hidden
        # that pops the history of the page you are actually on.
        if music_library.album(int(album_id)) is None:
            self._on_status("That album is no longer in the library.")
            return
        self.album_page.set_album(int(album_id))
        self._go(self.album_page)
        self._sync_nav(None)

    def open_playlist(self, playlist_id: int) -> None:
        """One video playlist, from a card or from "Playing from".

        Checked before the page is pointed at it, the way open_album is: a page
        with nothing to show asks to go back from inside its own reload, and
        while it is still hidden that pops the history of the page you are on.
        """
        if db.playlist(int(playlist_id)) is None:
            self._on_status("That playlist is no longer there.")
            return
        self.playlists_page.show_playlist(int(playlist_id))
        self._go(self.playlists_page)
        self._sync_nav(_NAV_PLAYLISTS)

    def open_artist(self, name: str) -> None:
        if not name:
            return
        # Artist pages are keyed by an album's artist, exactly. A song credited
        # to "A, B", or tagged in a different case, has none of its own — and
        # opening one anyway left the previous artist's page on screen (Play
        # played them) with the Back history emptied.
        if not music_library.artist_albums(name):
            self._on_status(f"No albums by {name} in the library.")
            return
        self.artist_page.set_artist(name)
        self._go(self.artist_page)
        self._sync_nav(None)

    def open_now_playing(self, tab: str = "") -> None:
        if not self.music.has_queue:
            return
        if tab:
            self.now_playing.show_tab(tab)
        if self.stack.currentWidget() is not self.now_playing:
            self._go(self.now_playing)
            self._sync_nav(None)

    def open_context(self, context: dict | None = None) -> None:
        """"Playing from …" clicked: the page the queue was started from.

        Takes the context the click carried, or the player's own when the
        signal carries none. An album or artist that has gone since says so in
        the top bar (open_album / open_artist), as it does from anywhere else.
        """
        if not isinstance(context, dict):
            context = self.music.context
        if not context:
            return
        kind, ident = context.get("kind"), context.get("id")
        if kind == "album":
            try:
                self.open_album(int(ident))
            except (TypeError, ValueError):
                pass
        elif kind == "artist":
            self.open_artist(str(ident or context.get("title") or ""))
        elif kind in ("liked", "songs", "search"):
            # The list itself, searched again when the queue came from a search
            # (search results, or Liked Songs narrowed by one: the search is the
            # context's id), and with the search box cleared otherwise so Liked
            # Songs shows every liked song rather than a filtered few.
            search = str(ident or "") if kind in ("search", "liked") else ""
            self.music_page.show_tab("liked" if kind == "liked" else "songs", search)
            self._go(self.music_page)
            self._sync_nav(_NAV_MUSIC)
        elif kind == "playlist":
            # Music playlists only: a film has no "Playing from". The page keeps
            # the list open over its tabs, so show_tab is not the way in.
            try:
                playlist_id = int(ident)
            except (TypeError, ValueError):
                return
            # Checked here as well as in open_playlist: a playlist deleted while
            # its songs played on left "Playing from" pointing at nothing, and
            # the page showed the old title and rows anyway.
            if db.playlist(playlist_id) is None:
                self._on_status("That playlist is no longer there.")
                return
            self.music_page.show_playlist(playlist_id)
            self._go(self.music_page)
            self._sync_nav(_NAV_MUSIC)
        elif kind == "queue":
            self.open_now_playing("queue")

    def _on_music_cover_changed(self) -> None:
        """Settings > Music changed the Now Playing cover style. Now Playing also
        reads it whenever it is shown; this switches it straight away, so the
        view is never built around the style it is about to drop."""
        self.now_playing.set_cover_style(str(settings.get("music_cover_style", "disc")))

    def _on_sleep_timer_fired(self) -> None:
        """The sleep timer is for falling asleep to whatever is on, a film included.

        The music player has already faded out and paused itself. A film is
        another matter: music steps aside for one, but a minutes timer set
        before it keeps counting, and without this the film played on all night
        after the timer ran out. The player puts up a line saying why it paused.
        """
        if self.stack.currentWidget() is self.player:
            self.player.pause_for_sleep()

    def _on_music_track(self, track) -> None:
        self._update_chrome()
        self._publish_music_presence()
        if not track:
            return
        self._fetch_lyrics(track, show=True)
        # Fetch the next song's too, so its lyrics are there the moment it starts.
        upcoming = self.music.upcoming
        if upcoming:
            self._fetch_lyrics(upcoming[0], show=False)

    def _fetch_lyrics(self, track: dict, show: bool) -> None:
        if self._in_tray:
            return          # no network while gaming; fetched when the window returns
        allow_online = bool(settings.get("fetch_lyrics", True))
        track_id = int(track["id"])

        def done(found) -> None:
            current = self.music.current
            if not show or not current or int(current["id"]) != track_id:
                return                  # the song changed while we were looking
            self.now_playing.lyrics.set_lyrics(found)
            if found is not None and not found.available and not allow_online:
                self.now_playing.lyrics.set_status(
                    "No lyrics in this file. Online lyrics are off in Settings.")

        self.service.run_async(lambda: music_lyrics.find(dict(track), allow_online), on_done=done)

    def _on_music_state(self) -> None:
        self._publish_music_presence()

    def _publish_music_presence(self) -> None:
        """Discord shows the song — unless a film is playing, which wins."""
        self._presence_anchor = None
        presence = self.player.presence
        if not presence.enabled or self.stack.currentWidget() is self.player:
            return
        track = self.music.current
        # A queue that has run out is over, not paused: after an album ended
        # with repeat off, the profile said "Paused" on its last song for hours.
        # Nor is last session's queue, brought back at startup, something you
        # are listening to: opening Mistery would otherwise put a paused song
        # on your profile that you never played today.
        if not track or self.music.restored or (
                not self.music.is_playing and (not self.music.has_queue or self.music.is_idle)):
            presence.clear()
            return
        from ..discord_presence import asset_key

        album = track.get("album_title") or track.get("album") or ""
        if settings.get("discord_hide_titles"):
            presence.set_listening("Listening to music", "", "", None, not self.music.is_playing)
            return
        # The song's own length first. The player's duration still belongs to
        # the previous song until mpv reports the new one, which is after this
        # publish: a 12 s song after a 40 s one showed 40 s left, and a 40 s
        # song after a 12 s one ran out of time a third of the way in.
        length = track.get("duration") or self.music.duration or 0
        remaining = max(0.0, length - self.music.position)
        presence.set_listening(track.get("title") or "", track.get("artist") or "", album,
                               remaining, not self.music.is_playing, asset_key(album))
        self._presence_anchor = (time.monotonic(), self.music.position, self.music.is_playing)

    def _on_music_position(self, position: float, _duration: float) -> None:
        """Republish when the song jumps rather than plays on — a seek, or
        Previous back to its start. Discord counts down to the end time it was
        given, so without this a seek left it counting to the old one."""
        anchor = self._presence_anchor
        if anchor is None:
            return
        published_at, published_position, playing = anchor
        expected = published_position + (time.monotonic() - published_at if playing else 0.0)
        if abs(position - expected) > 3.0:
            self._publish_music_presence()

    def _on_music_library_changed(self) -> None:
        # Covers may just have been made (or made again), so a file that failed
        # to load earlier deserves its retries back.
        images.forget_failure_counts()
        current = self.stack.currentWidget()
        for page in (self.music_page, self.album_page, self.artist_page):
            if page is current:
                page.reload()
        self._schedule_music_watch()

    def _watch_music(self) -> None:
        if self._in_tray:
            return
        playing_video = self.stack.currentWidget() is self.player
        if not playing_video and not self.service.busy:
            self.service.refresh_music()
        self._schedule_music_watch()

    def _schedule_music_watch(self) -> None:
        downloading = music_library.incomplete_count() > 0
        self._music_watch.start(20_000 if downloading else 180_000)

    # --- playback -----------------------------------------------------------

    def _clicked_card(self):
        """The card under the pointer, if playback was started by clicking one.

        Cards emit on mouse press, so the pointer is still over the one that
        asked. Reading it here beats threading a widget through four signal
        hops, and when there is nothing under the pointer — the keyboard, the
        hero, a menu — the transition simply cross-fades instead.
        """
        from PySide6.QtGui import QCursor

        from .widgets.cards import _BaseCard

        widget = QApplication.widgetAt(QCursor.pos())
        while widget is not None:
            if isinstance(widget, _BaseCard):
                return widget
            widget = widget.parentWidget()
        return None

    def _video_rect(self) -> QRect:
        """Where the picture will appear, in screen coordinates."""
        surface = self.player.surface
        if not surface.isVisible():
            return QRect()
        return QRect(surface.mapToGlobal(QPoint(0, 0)), surface.size())

    def play(self, item: MediaItem, start_at: float | None = None,
             line_up: tuple[list[int], int] | None = None) -> None:
        fresh = db.get_media(item.id)
        if fresh is not None:
            item = MediaItem.from_row(fresh)
        # Set on every play, not only the playlist ones: a film started from
        # Home after a playlist must not inherit the playlist's line-up.
        self.player.set_line_up(*(line_up or ([], 0)))
        # Background scanning and thumbnailing must not compete with playback.
        if settings.get("pause_background_during_playback", True):
            self.service.set_paused(True)
        # Music steps aside for a film. It isn't resumed afterwards on purpose:
        # an album starting up again at the end of a two-hour film is a surprise.
        if self.music.is_playing:
            self.music.pause()

        card = self._clicked_card()
        if card is not None:
            pixmap, source_rect = card.snapshot()
        else:
            # Started from the hero, a menu or the keyboard: there is no tile to
            # grow, but the gap still wants covering, so this title's own art
            # cross-fades in place instead. One decode, on the way into a file
            # that is about to be opened anyway.
            pixmap = QPixmap(item.wide_art or item.art or "")
            source_rect = QRect()

        self._go(self.player)
        self._sync_nav(None)
        # The player has only just been shown; let the layout settle so the
        # video rect we animate towards is the real one.
        QApplication.processEvents()
        self.transition.start(pixmap, source_rect, self._video_rect())

        # Starting mpv blocks this thread while it waits for the IPC pipe, so
        # give the cover one event loop turn to get on screen first.
        QTimer.singleShot(0, lambda: self._begin_playback(item, start_at))

    def _begin_playback(self, item: MediaItem, start_at: float | None) -> None:
        if not self.player.play(item, start_at):
            # Nothing started, so the player will never emit `closed` — but
            # play() already paused the library for it. Leaving by any other
            # road than the close path kept background work paused for the rest
            # of the session: the next rescan parked on its first step, the
            # Rescan button stayed grey and the download watcher, seeing the
            # service busy, stopped looking for new albums. The reload also
            # drops the card for a file the player has just marked missing.
            reason = self._status.text()        # the player has just put it there
            self._on_player_closed()
            self._on_status(reason)             # not the library totals the reload wrote over it

    def _on_card_action(self, action: str, item) -> None:
        """Right-click menu on a poster/still card."""
        if action == "play":
            if isinstance(item, ShowItem):
                self.open_show(item)
            else:
                self.play(item)
        elif action == "vr":
            self.play_in_vr(item)
        elif action == "details":
            if isinstance(item, ShowItem):
                self.open_show(item)
            else:
                self.open_media(item)
        elif action == "playlist":
            # The card emitted an action and stopped there (cards never touch
            # the database); the second menu is popped here, at the pointer.
            from .widgets.playlist_menu import add_to_playlist_menu

            menu = add_to_playlist_menu(self, "video", [item.id], self._on_playlist_change)
            menu.exec(QCursor.pos())
        elif action == "watched":
            db.set_watched(item.id, not item.watched)
            self.reload_all()
        elif action == "folder":
            reveal_in_explorer(item.path)

    def _on_playlist_change(self, message: str) -> None:
        """A playlist was added to or taken from. db.data_version deliberately
        does not report our own writes, so _check_external_changes will never
        notice: reload the page that is on screen.

        The line goes up after the reload, not before: reload_all ends with
        _on_status(""), which paints the library totals over anything already
        there, so "Added to Films." never survived its own click.
        """
        self.reload_all()
        self._on_status(message)

    def play_in_vr(self, item: MediaItem) -> None:
        """Hand the file to a VR player, or explain what's missing."""
        targets = vr.detect(force=True)
        target = vr.preferred_target(targets)

        if target is None:
            box = QMessageBox(self)
            box.setWindowTitle("Play in VR")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText("Nothing to hand off to yet")
            box.setInformativeText(vr.NOTHING_INSTALLED)
            settings_button = box.addButton("Open Settings", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            if box.clickedButton() is settings_button:
                self._on_nav(_NAV_SETTINGS)     # a bare 4 was Search
            return

        if target.kind == vr.MIRROR:
            # These stream the whole desktop, so the right move is to play here.
            box = QMessageBox(self)
            box.setWindowTitle("Play in VR")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(f"{target.name} puts your desktop in the headset")
            box.setInformativeText(
                f"{target.note}\n\nMistery will play {item.title} fullscreen now — "
                "put the headset on and it will be on the virtual screen."
            )
            play_button = box.addButton("Play fullscreen", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is play_button:
                self.play(item, item.resume_position)
                QTimer.singleShot(700, self._enter_fullscreen_playback)
            return

        ok, message = vr.launch(target, item.path)
        self._on_status(message)
        if not ok:
            QMessageBox.warning(self, "Play in VR", message)

    def _enter_fullscreen_playback(self) -> None:
        if self.stack.currentWidget() is self.player and not self.player.is_fullscreen:
            self.player.toggle_fullscreen()

    def _remember_played(self) -> None:
        item = self.player.current_item
        if item is not None:
            self._last_played = item

    def _on_player_closed(self) -> None:
        self.transition.cancel()
        self.service.set_paused(self._in_tray)
        played, self._last_played = self._last_played, None
        self.go_back()
        # Back on the show, turn to the season the player was last in: Up Next
        # and autoplay carry on into the following season, and the page kept
        # the season you had picked before pressing Play.
        if (played is not None and played.is_episode and self.stack.currentWidget() is self.show_page
                and played.show_id == self.show_page._show.id):
            self.show_page.show_season(played.season)
        self.reload_all()
        self._publish_music_presence()

    def _on_player_error(self, text: str) -> None:
        self._on_status(text[:160])

    def _on_progress_changed(self) -> None:
        # Cheap: only the home rows care while playback is running.
        if self.stack.currentWidget() is not self.player:
            self.home.reload()

    def _set_fullscreen(self, fullscreen: bool) -> None:
        if not fullscreen and (self.isHidden() or self.isMinimized()):
            # Giving the screen back must never mean coming back on screen.
            # Minimising, or closing to the tray, hides Now Playing; its
            # hideEvent ends the screensaver; the screensaver asks for full
            # screen off — and a show*() here put the window straight back up,
            # maximised, half a second after Win+D, and left a close-to-tray
            # visible with _in_tray already True. Same flags, no show: the
            # minimised/hidden bit is kept, so whatever un-hides it next
            # (show_from_tray, the taskbar) gets the size it expected.
            state = self.windowState() & ~Qt.WindowState.WindowFullScreen
            if self._maximized_before_fullscreen:
                state |= Qt.WindowState.WindowMaximized
            self.setWindowState(state)
            return
        if fullscreen:
            # Fullscreen replaces the maximised state rather than adding to it,
            # so it's remembered here: leaving with showNormal() handed a
            # maximised window back smaller, every time.
            if not self.isFullScreen():
                self._maximized_before_fullscreen = self.isMaximized()
            self.showFullScreen()
        elif self._maximized_before_fullscreen:
            self.showMaximized()
        else:
            self.showNormal()
        # The picture is about to be somewhere else; take the cover with it.
        QTimer.singleShot(140, lambda: self.transition.retarget(self._video_rect()))

    # --- library ------------------------------------------------------------

    def _initial_scan(self) -> None:
        if settings.get("scan_on_startup", True):
            self.service.refresh(force=False)

    def _add_folder(self) -> None:
        self.settings_page.add_folder()

    def _on_status(self, text: str) -> None:
        # One line: this now sits in the top bar, not a tall sidebar panel.
        if text:
            self._status.setText(text.replace("\n", " ")[:110])
            return
        stats = db.library_stats()
        bits = [
            f"{stats['movies']} movies",
            f"{stats['shows']} shows",
            f"{stats['files']} files",
        ]
        if stats.get("downloading"):
            count = stats["downloading"]
            bits.append(f"{count} still downloading")
        self._status.setText("   ·   ".join(bits))

    def _on_scan_finished(self, result) -> None:
        self._on_status(result.summary())

    def reload_all(self) -> None:
        self._data_version = db.data_version()
        current = self.stack.currentWidget()
        for page in (self.home, self.movies, self.shows, self.search, self.settings_page,
                     self.music_page, self.playlists_page, self.album_page, self.artist_page):
            if page is current or page is self.home:
                page.reload()
        if current is self.detail and self.detail._item.id:
            self.detail.set_media(self.detail._item)
        elif current is self.show_page and self.show_page._show.id:
            self.show_page.set_show(self.show_page._show)
        self._on_status("")

    # --- events -------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        if self.stack.currentWidget() is self.player:
            if self.player.handle_key(event):
                event.accept()
                return
        elif self._handle_music_key(event):
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self._history:
            self.go_back()
            event.accept()
            return
        super().keyPressEvent(event)

    def _handle_music_key(self, event) -> bool:
        """Anywhere outside the film player, the keyboard drives the music.

        These are the media keys, which no widget claims. Space and Ctrl+arrows
        are shortcuts instead (see _install_shortcuts), because a clicked
        button does claim those.
        """
        media = {
            Qt.Key.Key_MediaTogglePlayPause: self.music.toggle_pause,
            Qt.Key.Key_MediaPlay: self.music.play,
            Qt.Key.Key_MediaPause: self.music.pause,
            Qt.Key.Key_MediaNext: self.music.next,
            Qt.Key.Key_MediaPrevious: self.music.previous,
        }
        action = media.get(event.key())
        if action is None:
            return False
        action()
        return True

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if self.stack.currentWidget() is self.player:
            self.player._sync_overlay()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self.stack.currentWidget() is self.player:
                QTimer.singleShot(60, self.player._sync_overlay)
            # Minimised is nearly the tray: nothing on screen needs the position
            # stream. The library keeps working, though — that's the difference.
            self.music.set_low_power(self._in_tray or self.isMinimized())

    def closeEvent(self, event) -> None:
        # Closing while music plays sends Mistery to the tray instead: that is
        # the whole point of background playback. With nothing playing, close
        # means quit — an invisible app lingering for no reason is a surprise.
        if (not self._quitting and self.music.is_playing and self.tray.available
                and settings.get("close_to_tray", True)):
            event.ignore()
            self.go_to_tray()
            return
        self._shutdown()
        super().closeEvent(event)
        # Hiding to the tray means the app must not quit when its last window
        # closes, so a real close has to say so.
        QApplication.instance().quit()

    def _shutdown(self) -> None:
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        # Where the music is, before anything else is torn down: the film
        # player's shutdown comes first below, and a failure in it would lose
        # the session. music.shutdown() saves again as it stops mpv (1-2 ms
        # each); a queue brought back and never played keeps its saved point.
        self.music.save_session()
        self.tray.set_visible(False)
        # Gone from the screen first: service.shutdown() can wait up to 8 s for a
        # scan stage (an ffprobe, a metadata retry) to finish, and a window that
        # stays up frozen for that long looks like a hang rather than a quit.
        self.hide()
        self.player.shutdown()
        self.music.shutdown()
        self.service.shutdown()
