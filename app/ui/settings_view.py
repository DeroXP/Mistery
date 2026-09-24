"""Settings: library folders, TMDB key, playback preferences."""

from __future__ import annotations

import html
import os
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from .. import __version__, db, updates, vr
from ..discord_presence import DiscordPresence
from ..config import find_ffmpeg, find_ffprobe, find_mpv, settings
from ..metadata.tmdb import TmdbClient
from ..music import audio_fx, library as music_library, loudness
from ..player.mpv_process import quality_preset
from ..util import fmt_duration, fmt_size, reveal_in_explorer
from .theme import C
from .widgets.segments import SegmentTray


def _saver_after() -> int:
    """The idle delay, clamped to what the spin box can hold. settings.json is
    a file a person can edit, and ScreensaverView floors it at 10 s anyway."""
    try:
        value = int(settings.get("music_screensaver_after", 180) or 180)
    except (TypeError, ValueError):
        value = 180
    return max(10, min(3600, value))


def _party_port() -> int:
    """The movie night port, clamped to what the spin box can hold: settings.json
    is a file a person can edit."""
    try:
        value = int(settings.get("party_port", 42170) or 42170)
    except (TypeError, ValueError):
        value = 42170
    return max(1024, min(65535, value))


def _forwarded_words(port: int) -> str:
    """The box for a port forwarded by hand, with the number in it: a forward is
    for one port, and the box has to say which."""
    return f"I've forwarded port {port} to this PC on my router"


# The UPnP box's words, which the explanation under the boxes quotes. It said
# "with the box above ticked" when the box above it was UPnP; since "I've
# forwarded port …" went in between, the box above is that one, which asks the
# router nothing. Named, it cannot point at the wrong box again. (The note a
# new port number leaves names the forwarded box the same way.)
_UPNP_BOX = "Ask my router to open the port"


def _checked_when(answer: dict) -> str:
    """The "Checked 17 Sep, 20:41." tail for an update line, or nothing.

    The updater stamps every answer it writes, so an answer with no stamp is one
    this session failed to get — and dating that would be a lie.
    """
    try:
        when = float(answer.get("checked", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if when <= 0:
        return ""
    return " Checked " + time.strftime("%d %b, %H:%M", time.localtime(when)) + "."


def _section(title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
    card = QWidget()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(24, 20, 24, 22)
    layout.setSpacing(14)

    heading = QLabel(title)
    heading.setObjectName("SectionTitle")
    layout.addWidget(heading)
    if subtitle:
        note = QLabel(subtitle)
        note.setObjectName("Faint")
        note.setWordWrap(True)
        layout.addWidget(note)
    return card, layout


class SettingsView(QWidget):
    library_changed = Signal()
    rescan_requested = Signal(bool)
    discord_changed = Signal()
    sound_changed = Signal()
    music_cover_changed = Signal()          # the Now Playing cover style
    gamepad_changed = Signal(bool)          # a game controller on or off (ui/couch.py)
    # Answers from work done on a thread of its own (the TMDB key check, the
    # Discord art export), carried back to the UI thread.
    _tmdb_checked = Signal(bool, str)
    _art_exported = Signal(str, int, int, str)
    _update_checked = Signal(object)        # what MisteryUpdate.exe --check-now found
    _update_switched = Signal(bool)         # whether the on/off switch was written

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tmdb_checked.connect(self._on_tmdb_checked)
        self._art_exported.connect(self._on_art_exported)
        self._update_checked.connect(self._on_update_checked)
        self._update_switched.connect(self._on_update_switched)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # The title and a menu of the sections stay put; the sections scroll
        # under them. The menu follows the scrolling (_follow_scroll).
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(52, 30, 52, 14)
        top_layout.setSpacing(14)
        heading = QLabel("Settings")
        heading.setObjectName("PageTitle")
        top_layout.addWidget(heading)
        self._section_tray = SegmentTray(wrap=True)
        self._section_group = QButtonGroup(self)
        self._section_group.setExclusive(True)
        self._section_group.idClicked.connect(self._go_to_section)
        top_layout.addWidget(self._section_tray, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(top)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)
        self._scroll = scroll

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(52, 8, 52, 52)
        layout.setSpacing(22)
        scroll.setWidget(page)

        # Built in the order they always were, then listed for the menu.
        folders = self._build_folders()
        tmdb = self._build_tmdb()
        playback = self._build_playback()
        music = self._build_music()
        sound = self._build_sound()
        thumbnails = self._build_thumbnails()
        discord = self._build_discord()
        self._movie_night = self._build_movie_night()
        vr_card = self._build_vr()
        controller = self._build_controller()
        about = self._build_about()
        self._sections: list[tuple[str, QWidget]] = [
            ("Library", folders), ("Artwork", tmdb), ("Playback", playback), ("Music", music),
            ("Sound", sound), ("Thumbnails", thumbnails), ("Discord", discord),
            ("Movie night", self._movie_night), ("VR", vr_card), ("Controller", controller),
            ("System", about),
        ]
        for index, (name, card) in enumerate(self._sections):
            layout.addWidget(card)
            chip = QPushButton(name)
            chip.setCheckable(True)
            chip.setChecked(index == 0)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            self._section_group.addButton(chip, index)
            self._section_tray.add(chip)
        layout.addStretch(1)
        scroll.verticalScrollBar().valueChanged.connect(self._follow_scroll)

    # --- the section menu -----------------------------------------------------

    def _go_to_section(self, index: int) -> None:
        card = self._sections[index][1]
        bar = self._scroll.verticalScrollBar()
        bar.setValue(max(0, min(bar.maximum(), card.y() - 8)))
        self._section_group.button(index).setChecked(True)

    def _follow_scroll(self, value: int) -> None:
        """Light the section at the top of the view as the page scrolls."""
        bar = self._scroll.verticalScrollBar()
        current = 0
        for index, (_name, card) in enumerate(self._sections):
            if card.y() - 40 <= value:
                current = index
        if value >= bar.maximum() and bar.maximum() > 0:
            current = len(self._sections) - 1       # the last ones cannot reach the top
        button = self._section_group.button(current)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    # --- library folders ----------------------------------------------------

    def _build_folders(self) -> QWidget:
        card, layout = _section(
            "Library folders",
            "Mistery scans these folders for video files. Subfolders are included.",
        )

        self._folders = QListWidget()
        self._folders.setMaximumHeight(140)
        self._folders.setStyleSheet(
            f"QListWidget {{ background: {C.BG_ELEV}; border: 1px solid {C.BORDER};"
            f" border-radius: 8px; padding: 6px; }}"
            f"QListWidget::item {{ padding: 6px 8px; border-radius: 5px; }}"
            f"QListWidget::item:selected {{ background: {C.SURFACE_ACTIVE}; }}"
        )
        layout.addWidget(self._folders)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        add = QPushButton("Add folder…")
        add.clicked.connect(self.add_folder)
        buttons.addWidget(add)

        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_folder)
        buttons.addWidget(remove)

        rescan = QPushButton("Rescan now")
        rescan.clicked.connect(lambda: self.rescan_requested.emit(False))
        buttons.addWidget(rescan)

        deep = QPushButton("Force full rescan")
        deep.setToolTip("Re-read every file even if nothing looks changed")
        deep.clicked.connect(lambda: self.rescan_requested.emit(True))
        buttons.addWidget(deep)

        self._retry = QPushButton("Retry failed")
        self._retry.setToolTip(
            "Queue anything that previously failed to read, or failed to get "
            "artwork or thumbnails"
        )
        self._retry.clicked.connect(self._retry_failed)
        buttons.addWidget(self._retry)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._folders_note = QLabel()
        self._folders_note.setObjectName("Faint")
        self._folders_note.setWordWrap(True)
        layout.addWidget(self._folders_note)
        return card

    def _retry_failed(self) -> None:
        db.retry_failed()
        self.rescan_requested.emit(False)
        self._folders_note.setText("Queued. Watch the top bar for progress.")

    def add_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose a library folder")
        if not chosen:
            return
        folders = list(settings.get("library_folders", []))
        if chosen not in folders:
            folders.append(chosen)
            settings.set("library_folders", folders)
            self._reload_folders()
            self.rescan_requested.emit(False)

    def _remove_folder(self) -> None:
        item = self._folders.currentItem()
        if item is None:
            return
        # The path itself, not the row's text: a folder that has gone missing
        # is listed with "(missing)" after it, so comparing the text never
        # matched the one folder most worth removing. It stayed in settings
        # for good, and every attempt started a pointless rescan.
        chosen = item.data(Qt.ItemDataRole.UserRole) or item.text()
        before = list(settings.get("library_folders", []))
        folders = [f for f in before if f != chosen]
        if folders == before:
            return
        settings.set("library_folders", folders)
        self._reload_folders()
        self.rescan_requested.emit(False)

    def _reload_folders(self) -> None:
        self._folders.clear()
        for folder in settings.get("library_folders", []):
            entry = QListWidgetItem(folder)
            entry.setData(Qt.ItemDataRole.UserRole, folder)
            if not Path(folder).is_dir():
                entry.setText(f"{folder}   (missing)")
                entry.setForeground(Qt.GlobalColor.red)
            self._folders.addItem(entry)

    # --- tmdb ---------------------------------------------------------------

    def _build_tmdb(self) -> QWidget:
        card, layout = _section(
            "Artwork and metadata",
            "With a free TMDB API key you get real posters, backdrops, plots, genres "
            "and ratings. Without one, Mistery generates artwork from the video itself. "
            "Get a key at themoviedb.org → Settings → API.",
        )

        row = QHBoxLayout()
        row.setSpacing(9)
        self._tmdb_key = QLineEdit(settings.get("tmdb_api_key", ""))
        self._tmdb_key.setPlaceholderText("TMDB API key (v3 auth)")
        self._tmdb_key.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self._tmdb_key, 1)

        show = QPushButton("Show")
        show.setCheckable(True)
        show.setFixedWidth(70)
        show.toggled.connect(
            lambda on: self._tmdb_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        row.addWidget(show)

        save = QPushButton("Save and test")
        save.setObjectName("Primary")
        save.clicked.connect(self._save_tmdb_key)
        row.addWidget(save)
        self._tmdb_save = save
        layout.addLayout(row)

        self._tmdb_status = QLabel()
        self._tmdb_status.setObjectName("Faint")
        self._tmdb_status.setWordWrap(True)
        layout.addWidget(self._tmdb_status)

        refetch = QPushButton("Re-fetch titles and artwork for the whole library")
        refetch.setToolTip(
            "Looks everything up again through TMDB, TVmaze and Wikipedia. "
            "Use this if a lookup failed while you were offline."
        )
        refetch.clicked.connect(self._refetch_metadata)
        layout.addWidget(refetch, alignment=Qt.AlignmentFlag.AlignLeft)
        return card

    def _save_tmdb_key(self) -> None:
        key = self._tmdb_key.text().strip()
        settings.set("tmdb_api_key", key)
        if not key:
            self._tmdb_status.setText("Key cleared — artwork will be generated with ffmpeg.")
            return
        # The check is a request to TMDB. Made right here, on the UI thread, it
        # froze the whole window for the full timeout (12 s measured) on a
        # network that connects but never answers, and "Checking…" was never
        # even painted. The button stays off until the answer is in, so two
        # checks can't finish out of order.
        self._tmdb_status.setText("Checking…")
        self._tmdb_save.setEnabled(False)

        def check() -> None:
            ok, message = TmdbClient(key).verify_key()
            try:
                self._tmdb_checked.emit(ok, message)
            except RuntimeError:
                pass                    # the window is already gone

        threading.Thread(target=check, name="tmdb-verify", daemon=True).start()

    def _on_tmdb_checked(self, ok: bool, message: str) -> None:
        self._tmdb_save.setEnabled(True)
        colour = C.SUCCESS if ok else C.DANGER
        # Escaped: a connection error quotes objects like "<urllib3...>", which
        # the label took for markup and dropped.
        self._tmdb_status.setText(
            f"<span style='color:{colour}'>{html.escape(message)}</span>")
        if ok:
            self._refetch_metadata()

    def _refetch_metadata(self) -> None:
        db.execute("UPDATE media SET meta_state = 'pending'")
        db.execute("UPDATE shows SET meta_state = 'pending'")
        self.rescan_requested.emit(False)

    # --- playback -----------------------------------------------------------

    def _build_playback(self) -> QWidget:
        card, layout = _section("Playback")

        self._dialogue = QCheckBox("Dialogue boost by default (compresses loud action)")
        self._dialogue.setChecked(bool(settings.get("dialogue_boost")))
        self._dialogue.toggled.connect(lambda v: settings.set("dialogue_boost", v))
        layout.addWidget(self._dialogue)

        self._tone = QCheckBox("Tone-map HDR to SDR (turn off if your display is HDR)")
        self._tone.setChecked(bool(settings.get("hdr_tone_mapping", True)))
        self._tone.toggled.connect(self._on_tone_toggled)
        layout.addWidget(self._tone)

        self._autoplay = QCheckBox("Auto-play the next episode (with a cancellable countdown)")
        self._autoplay.setChecked(bool(settings.get("autoplay_next", True)))
        self._autoplay.toggled.connect(lambda v: settings.set("autoplay_next", v))
        layout.addWidget(self._autoplay)

        self._auto_skip = QCheckBox(
            "Auto-skip intros (learned automatically, or set from the TV menu while playing)"
        )
        self._auto_skip.setChecked(bool(settings.get("auto_skip_intro", True)))
        self._auto_skip.toggled.connect(lambda v: settings.set("auto_skip_intro", v))
        layout.addWidget(self._auto_skip)

        self._detect = QCheckBox(
            "Learn intro and credits positions by matching audio across each season"
        )
        self._detect.setChecked(bool(settings.get("detect_intros", True)))
        self._detect.toggled.connect(lambda v: settings.set("detect_intros", v))
        layout.addWidget(self._detect)

        self._subs = QCheckBox("Turn subtitles on by default")
        self._subs.setChecked(bool(settings.get("subs_on_by_default")))
        self._subs.toggled.connect(lambda v: settings.set("subs_on_by_default", v))
        layout.addWidget(self._subs)

        self._pause_bg = QCheckBox("Pause library scanning and thumbnails while playing")
        self._pause_bg.setChecked(bool(settings.get("pause_background_during_playback", True)))
        self._pause_bg.toggled.connect(
            lambda v: settings.set("pause_background_during_playback", v)
        )
        layout.addWidget(self._pause_bg)

        quality_row = QHBoxLayout()
        quality_row.setSpacing(10)
        quality_row.addWidget(QLabel("Video quality"))
        self._quality = QComboBox()
        self._quality.addItem("Data saver — cheapest scaling, lowest GPU load", "fast")
        self._quality.addItem("Standard — good scaling (recommended)", "balanced")
        self._quality.addItem("Best — best scaling, heaviest GPU load", "high")
        index = self._quality.findData(
            quality_preset(settings.get("video_quality", "balanced"))
        )
        self._quality.setCurrentIndex(index if index >= 0 else 1)
        self._quality.currentIndexChanged.connect(self._on_quality_changed)
        quality_row.addWidget(self._quality, 1)
        quality_row.addStretch(1)
        layout.addLayout(quality_row)

        hw_row = QHBoxLayout()
        hw_row.setSpacing(10)
        hw_row.addWidget(QLabel("Hardware decoding"))
        self._hwdec = QComboBox()
        self._hwdec.addItems(["auto-safe", "auto", "d3d11va", "dxva2", "vulkan", "no"])
        self._hwdec.setCurrentText(settings.get("hwdec", "auto-safe"))
        self._hwdec.currentTextChanged.connect(self._on_hwdec_changed)
        hw_row.addWidget(self._hwdec)

        hw_row.addSpacing(20)
        hw_row.addWidget(QLabel("Skip step"))
        self._seek_step = QSpinBox()
        self._seek_step.setRange(5, 60)
        self._seek_step.setSuffix(" s")
        self._seek_step.setValue(int(settings.get("seek_step", 10)))
        self._seek_step.valueChanged.connect(lambda v: settings.set("seek_step", v))
        hw_row.addWidget(self._seek_step)
        hw_row.addStretch(1)
        layout.addLayout(hw_row)

        self._playback_note = QLabel()
        self._playback_note.setObjectName("Faint")
        self._playback_note.setWordWrap(True)
        layout.addWidget(self._playback_note)
        return card

    def _build_music(self) -> QWidget:
        card, layout = _section(
            "Music",
            "Albums in your library folders are found automatically, and tracks that "
            "are still downloading appear the moment they finish. Lyrics come from a "
            ".lrc file next to the song or from the file's own tags first.",
        )
        self._lyrics_online = QCheckBox(
            "Find synced lyrics online (LRCLIB) when a song has none of its own"
        )
        self._lyrics_online.setChecked(bool(settings.get("fetch_lyrics", True)))
        self._lyrics_online.toggled.connect(lambda v: settings.set("fetch_lyrics", v))
        layout.addWidget(self._lyrics_online)

        # The screensaver takes over the whole screen, is on by default and
        # arrives three minutes after you last touched anything, so it needs an
        # off switch somewhere a person can find. Until this row existed the
        # only way to stop it was to edit settings.json.
        self._screensaver = QCheckBox(
            "Let Now Playing take over the screen while a song plays"
        )
        self._screensaver.setChecked(bool(settings.get("music_screensaver", True)))
        self._screensaver.toggled.connect(self._on_screensaver_toggled)
        layout.addWidget(self._screensaver)

        saver_row = QHBoxLayout()
        saver_row.setSpacing(10)
        saver_after_caption = QLabel("After")
        saver_after_caption.setMinimumWidth(150)
        saver_row.addWidget(saver_after_caption)
        self._screensaver_after = QSpinBox()
        self._screensaver_after.setRange(10, 3600)
        self._screensaver_after.setSingleStep(30)
        self._screensaver_after.setSuffix(" s of stillness")
        self._screensaver_after.setValue(_saver_after())
        self._screensaver_after.valueChanged.connect(
            lambda v: settings.set("music_screensaver_after", v))
        self._screensaver_after.setEnabled(self._screensaver.isChecked())
        saver_row.addWidget(self._screensaver_after)
        saver_row.addStretch(1)
        layout.addLayout(saver_row)
        saver_note = QLabel(
            "The record, the words and the song's own shape on black, drifting and "
            "dimming as the hours pass. F11 starts it at once, and it ends when you "
            "actually move the mouse. With this off nothing takes over the screen, and "
            "songs are not measured in the background for the waveform either."
        )
        saver_note.setObjectName("Faint")
        saver_note.setWordWrap(True)
        layout.addWidget(saver_note)

        self._close_to_tray = QCheckBox(
            "Keep playing in the system tray when I close the window"
        )
        self._close_to_tray.setChecked(bool(settings.get("close_to_tray", True)))
        self._close_to_tray.toggled.connect(lambda v: settings.set("close_to_tray", v))
        layout.addWidget(self._close_to_tray)
        tray_note = QLabel(
            "Only while music is playing — with nothing on, closing quits as usual. In the "
            "tray Mistery stops everything else (library scanning, artwork, lyrics lookups) "
            "and your keyboard's media keys keep working inside games."
        )
        tray_note.setObjectName("Faint")
        tray_note.setWordWrap(True)
        layout.addWidget(tray_note)
        note = QLabel(
            "Only the song playing and the one after it are looked up — the title, "
            "artist, album and length are sent, nothing else — and every answer is "
            "kept, so each song is asked about once."
        )
        note.setObjectName("Faint")
        note.setWordWrap(True)
        layout.addWidget(note)

        self._resume = QCheckBox("Resume music where I left off")
        self._resume.setChecked(bool(settings.get("music_resume", True)))
        self._resume.toggled.connect(lambda v: settings.set("music_resume", v))
        layout.addWidget(self._resume)
        resume_note = QLabel(
            "The next time Mistery starts, the queue is back on the song you were "
            "listening to, paused at the same second. Nothing plays until you press Play."
        )
        resume_note.setObjectName("Faint")
        resume_note.setWordWrap(True)
        layout.addWidget(resume_note)

        # Built like the Sound card's rows below, spacing included (they take the
        # card's), so the two cards' comboboxes sit in one column.
        cover_row = QHBoxLayout()
        cover_caption = QLabel("Now Playing cover")
        cover_caption.setMinimumWidth(150)
        cover_row.addWidget(cover_caption)
        self._cover_style = QComboBox()
        self._cover_style.addItem("Spinning record", "disc")
        self._cover_style.addItem("Album cover", "cover")
        self._cover_style.setMinimumWidth(190)
        self._select_cover_style()
        self._cover_style.currentIndexChanged.connect(self._on_cover_style)
        cover_row.addWidget(self._cover_style)
        cover_row.addStretch(1)
        layout.addLayout(cover_row)
        return card

    def _on_screensaver_toggled(self, on: bool) -> None:
        settings.set("music_screensaver", bool(on))
        self._screensaver_after.setEnabled(bool(on))

    def _select_cover_style(self) -> None:
        index = self._cover_style.findData(str(settings.get("music_cover_style", "disc")))
        self._cover_style.blockSignals(True)
        self._cover_style.setCurrentIndex(max(0, index))
        self._cover_style.blockSignals(False)

    def _on_cover_style(self) -> None:
        settings.set("music_cover_style", self._cover_style.currentData())
        self.music_cover_changed.emit()

    def _reload_music(self) -> None:
        """Catch up with the choices Now Playing can also make (its cover style)."""
        self._select_cover_style()
        self._resume.blockSignals(True)
        self._resume.setChecked(bool(settings.get("music_resume", True)))
        self._resume.blockSignals(False)
        self._screensaver.blockSignals(True)
        self._screensaver.setChecked(bool(settings.get("music_screensaver", True)))
        self._screensaver.blockSignals(False)
        self._screensaver_after.blockSignals(True)
        self._screensaver_after.setValue(_saver_after())
        self._screensaver_after.blockSignals(False)
        self._screensaver_after.setEnabled(self._screensaver.isChecked())

    # --- sound --------------------------------------------------------------

    def _build_sound(self) -> QWidget:
        card, layout = _section(
            "Sound",
            "Downloaded albums disagree about loudness by more than ten decibels — "
            "some carry ReplayGain tags asking to be played quietly, some carry "
            "nothing at all. Mistery measures each song once and plays every album "
            "at the same level. The same controls sit behind the Sound button next "
            "to the volume, where you can hear each change as you make it.",
        )
        self._sound_rows: dict[str, QComboBox] = {}

        def row(key: str, label: str, options: list[tuple[str, str]], note: str = "") -> None:
            line = QHBoxLayout()
            caption = QLabel(label)
            caption.setMinimumWidth(150)
            line.addWidget(caption)
            box = QComboBox()
            for value, text in options:
                box.addItem(text, value)
            current = str(settings.get(key, options[0][0]))
            box.setCurrentIndex(max(0, box.findData(current)))
            box.currentIndexChanged.connect(lambda _index, k=key, b=box: self._on_sound(k, b))
            box.setMinimumWidth(190)
            line.addWidget(box)
            line.addStretch(1)
            layout.addLayout(line)
            self._sound_rows[key] = box
            if note:
                hint = QLabel(note)
                hint.setObjectName("Faint")
                hint.setWordWrap(True)
                layout.addWidget(hint)

        row("music_normalize", "Match volume",
            [(key, f"{loudness.TARGET_LABELS[key]}"
              + (f"  ({loudness.TARGETS[key]:g} LUFS)" if loudness.TARGETS[key] else ""))
             for key in ("off", "quiet", "normal", "loud")],
            "Loud is what streaming services use for their loudest setting.")
        row("music_boost", "Extra loudness",
            [(key, audio_fx.BOOST_LABELS[key]) for key in ("off", "low", "high")],
            "Added on top, with a limiter. These records are mastered with no "
            "headroom, so pushing past +3 dB trades a little of their punch for volume.")
        row("music_bass", "Bass",
            [(key, audio_fx.BASS_LABELS[key]) for key in ("off", "warm", "deep", "massive")])
        row("music_spatial", "3D audio",
            [(key, audio_fx.SPATIAL_LABELS[key]) for key in ("off", "subtle", "normal", "wide")],
            "For headphones: the stereo picture is widened and one channel is put "
            "a few milliseconds behind the other, which the ear reads as space in "
            "front of you. Nothing is subtracted, so nothing goes missing from the "
            "mix — but on speakers, where both ears hear both channels, the two "
            "arrivals meet in the air and it will sound hollow.")

        self._clarity = QCheckBox("Clarity — a 3 dB lift at the top end")
        self._clarity.setChecked(bool(settings.get("music_clarity", False)))
        self._clarity.toggled.connect(self._on_clarity)
        layout.addWidget(self._clarity)

        self._loudness_note = QLabel()
        self._loudness_note.setObjectName("Faint")
        self._loudness_note.setWordWrap(True)
        layout.addWidget(self._loudness_note)
        self._reload_loudness_note()
        return card

    def _on_sound(self, key: str, box: QComboBox) -> None:
        settings.set(key, box.currentData())
        self.sound_changed.emit()

    def _on_clarity(self, value: bool) -> None:
        settings.set("music_clarity", value)
        self.sound_changed.emit()

    def _reload_sound(self) -> None:
        """Catch the page up with changes made from the Sound button."""
        for key, box in self._sound_rows.items():
            box.blockSignals(True)
            box.setCurrentIndex(max(0, box.findData(str(settings.get(key, "off")))))
            box.blockSignals(False)
        self._clarity.blockSignals(True)
        self._clarity.setChecked(bool(settings.get("music_clarity", False)))
        self._clarity.blockSignals(False)
        self._reload_loudness_note()

    def _reload_loudness_note(self) -> None:
        done, total = music_library.loudness_progress()
        if not total:
            text = "No music found yet."
        elif done >= total:
            text = f"All {total} songs measured."
        else:
            text = (f"{done} of {total} songs measured — the rest are done in the "
                    f"background, about half a second each. Until a song is measured "
                    f"its ReplayGain tag is used, or it plays as it is.")
        self._loudness_note.setText(text)

    def _on_quality_changed(self) -> None:
        settings.set("video_quality", self._quality.currentData())
        self._playback_note.setText(
            "Saved. The quality button in the player changes this mid-film too."
        )

    def _on_tone_toggled(self, value: bool) -> None:
        settings.set("hdr_tone_mapping", value)
        self._playback_note.setText("Takes effect the next time playback starts.")

    def _on_hwdec_changed(self, value: str) -> None:
        settings.set("hwdec", value)
        self._playback_note.setText("Takes effect the next time playback starts.")

    # --- thumbnails ---------------------------------------------------------

    def _build_thumbnails(self) -> QWidget:
        card, layout = _section(
            "Seek-bar previews",
            "Frames are sampled in a single hardware-accelerated ffmpeg pass and stored "
            "as a sprite sheet. A two-hour 4K file takes about ten seconds, in the "
            "background and at low priority.",
        )

        self._thumbs_on = QCheckBox("Generate preview thumbnails")
        self._thumbs_on.setChecked(bool(settings.get("generate_thumbs", True)))
        self._thumbs_on.toggled.connect(lambda v: settings.set("generate_thumbs", v))
        layout.addWidget(self._thumbs_on)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Frames per file"))
        self._thumb_count = QSpinBox()
        self._thumb_count.setRange(20, 400)
        self._thumb_count.setSingleStep(10)
        self._thumb_count.setValue(int(settings.get("thumb_count", 120)))
        self._thumb_count.valueChanged.connect(lambda v: settings.set("thumb_count", v))
        row.addWidget(self._thumb_count)
        row.addStretch(1)
        layout.addLayout(row)

        rebuild = QPushButton("Rebuild all preview thumbnails")
        rebuild.clicked.connect(self._rebuild_thumbs)
        layout.addWidget(rebuild, alignment=Qt.AlignmentFlag.AlignLeft)
        return card

    def _rebuild_thumbs(self) -> None:
        db.execute("UPDATE media SET thumbs_state = 'pending'")
        self.rescan_requested.emit(False)

    # --- discord ------------------------------------------------------------

    def _build_discord(self) -> QWidget:
        card, layout = _section(
            "Discord Rich Presence",
            "Shows what you're watching on your Discord profile. Discord needs "
            "an application of its own to put this under, which takes about a "
            "minute to make: discord.com/developers/applications → New "
            "Application → copy the Application ID into the box below. The "
            "application's name is what friends see above the title, so name "
            "it whatever you want that line to say. Nothing else about it "
            "matters, and Mistery never signs in to it — it only tells the "
            "Discord app already running on this PC what to show.",
        )

        self._discord_on = QCheckBox("Show what I'm watching on Discord")
        self._discord_on.setChecked(bool(settings.get("discord_presence")))
        self._discord_on.toggled.connect(self._on_discord_toggled)
        layout.addWidget(self._discord_on)

        self._discord_private = QCheckBox(
            "Hide titles — just show that something is playing"
        )
        self._discord_private.setChecked(bool(settings.get("discord_hide_titles")))
        self._discord_private.toggled.connect(self._on_discord_private)
        layout.addWidget(self._discord_private)

        row = QHBoxLayout()
        row.setSpacing(9)
        self._discord_id = QLineEdit(settings.get("discord_client_id", ""))
        self._discord_id.setPlaceholderText("Discord Application ID (18-19 digits)")
        row.addWidget(self._discord_id, 1)

        test = QPushButton("Save and test")
        test.setObjectName("Primary")
        test.clicked.connect(self._test_discord)
        row.addWidget(test)
        layout.addLayout(row)

        art_row = QHBoxLayout()
        art_row.setSpacing(9)
        export = QPushButton("Export posters for Discord")
        export.setCursor(Qt.CursorShape.PointingHandCursor)
        export.clicked.connect(self._export_discord_art)
        art_row.addWidget(export)
        self._discord_export = export
        art_row.addStretch(1)
        layout.addLayout(art_row)

        self._discord_art_note = QLabel(
            "Most titles need nothing here. Where a film or show was matched "
            "online, its poster already has a public web address and Discord "
            "fetches the picture itself, so it shows up with no upload at all.\n"
            "\n"
            "Two kinds of title still need one: anything Mistery could not "
            "match online, and anything whose artwork it made from the film's "
            "own frames — that picture exists only on this PC, so Discord has "
            "no way to see it. This button builds one 1024x576 image per show "
            "and film, the 16:9 shape Discord wants, each already named the "
            "way Mistery will ask for it. Drop the ones you want into Rich "
            "Presence → Art Assets on your application. Anything you skip "
            "falls back to the Mistery icon, which is itself an image named "
            "'mistery' you can upload there."
        )
        self._discord_art_note.setObjectName("Faint")
        self._discord_art_note.setWordWrap(True)
        layout.addWidget(self._discord_art_note)

        self._discord_status = QLabel()
        self._discord_status.setObjectName("Faint")
        self._discord_status.setWordWrap(True)
        layout.addWidget(self._discord_status)
        return card

    def _export_discord_art(self) -> None:
        """Copy every poster out under the asset name the player will ask for.

        The images are built on a thread of their own. Each is a decode, a blur
        and a PNG write, about 75 ms a title; done here on the UI thread that
        froze the window for over half a minute on a 500-title library.
        """
        from ..config import data_dir

        target = data_dir() / "discord-art"
        self._discord_export.setEnabled(False)
        self._discord_status.setText("Asking Discord what is already uploaded…")
        threading.Thread(
            target=self._plan_and_write_discord_art, args=(target,),
            name="discord-art", daemon=True,
        ).start()

    def _plan_and_write_discord_art(self, target: Path) -> None:
        """Ask Discord what it has, decide what it still needs, then write it.

        Both halves off the UI thread: the list is a request to discord.com (up
        to 6 s when the network is slow), and the plan reads every film, show
        and album. See app/discord_art.py for what is chosen and why.
        """
        from .. import discord_art
        from ..discord_presence import list_assets

        known = list_assets(str(settings.get("discord_client_id", "") or "").strip())
        self._art_plan = discord_art.plan(known)
        self._write_discord_art(target, self._art_plan.wanted)

    def _write_discord_art(self, target: Path, wanted: dict[str, tuple[str, str]]) -> None:
        """The export's file work, off the UI thread. Reports through _art_exported."""
        from ..discord_presence import compose_wide_art

        written, error = 0, ""
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            error = f"Could not create {target}: {exc}"
        else:
            # Clear the folder first, or a renamed title leaves art behind under
            # its old key and gets uploaded by mistake. list() materialises the
            # names before anything is deleted — deleting straight off the
            # generator skips entries. Scoped to images in Mistery's own export
            # folder.
            for stale in list(target.glob("*")):
                if stale.is_file() and stale.suffix.lower() in {
                    ".png", ".jpg", ".jpeg", ".webp"
                }:
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            written = sum(
                compose_wide_art(poster, str(target / f"{key}.png"), backdrop)
                for key, (poster, backdrop) in wanted.items()
            )
        try:
            self._art_exported.emit(str(target), written, len(wanted) - written, error)
        except RuntimeError:
            pass                        # the window is already gone

    def _on_art_exported(self, target: str, written: int, skipped: int, error: str) -> None:
        from ..discord_presence import ART_SIZE

        self._discord_export.setEnabled(True)
        if error:
            self._discord_status.setText(error)
            return
        from .. import discord_art

        plan = getattr(self, "_art_plan", None)
        why = discord_art.describe(plan) if plan is not None else ""
        if written == 0 and not skipped:
            # Nothing to upload is the good outcome now, not a failure: say so
            # plainly, and don't open an empty folder at the owner.
            self._discord_status.setText(
                "Nothing needs uploading — Discord already has everything, or fetches it "
                "from the web." + (f" {why}" if why else ""))
            return
        reveal_in_explorer(target)
        what = ""
        if plan is not None:
            what = f" ({plan.films} film or show poster(s), {plan.albums} album cover(s))"
        note = (f"Wrote {written} image(s){what} at {ART_SIZE[0]}x{ART_SIZE[1]} "
                f"to {target}.")
        if skipped:
            note += f" {skipped} could not be read."
        self._discord_status.setText(
            note + "  Upload them under Rich Presence → Art Assets, keeping the file "
            "names exactly as they are." + (f" {why}" if why else ""))

    def _on_discord_toggled(self, value: bool) -> None:
        settings.set("discord_presence", value)
        self.discord_changed.emit()
        self._reload_discord()

    def _on_discord_private(self, value: bool) -> None:
        settings.set("discord_hide_titles", value)
        self.discord_changed.emit()

    def _test_discord(self) -> None:
        client_id = self._discord_id.text().strip()
        settings.set("discord_client_id", client_id)
        if not client_id:
            self._discord_status.setText(
                f"<span style='color:{C.TEXT_DIM}'>No application ID set.</span>")
            self.discord_changed.emit()
            return

        self._discord_status.setText("Connecting to Discord…")
        QApplication.processEvents()

        presence = DiscordPresence(client_id)
        presence.start()
        presence.set_watching("Mistery", "Testing Rich Presence", 60)
        deadline = time.time() + 6
        while time.time() < deadline and not presence.connected:
            QApplication.processEvents()
            time.sleep(0.1)
        connected = presence.connected
        reason = presence._reject_reason
        presence.clear()
        presence.stop()

        if connected:
            self._discord_status.setText(
                f"<span style='color:{C.SUCCESS}'>Connected — your profile will "
                "show what you're watching.</span>")
        elif reason:
            self._discord_status.setText(
                f"<span style='color:{C.DANGER}'>Discord rejected it: {reason}. "
                "Check the Application ID.</span>")
        else:
            self._discord_status.setText(
                f"<span style='color:{C.DANGER}'>Could not reach Discord. Is the "
                "desktop app running?</span>")
        self.discord_changed.emit()

    def _reload_discord(self) -> None:
        self._discord_id.setText(settings.get("discord_client_id", ""))
        if not settings.get("discord_presence"):
            self._discord_status.setText("")
        elif not settings.get("discord_client_id"):
            self._discord_status.setText(
                f"<span style='color:{C.TEXT_DIM}'>Add an Application ID above, "
                "then press Save and test.</span>")

    # --- movie night --------------------------------------------------------

    def _build_movie_night(self) -> QWidget:
        from ..party import people      # the name rules only: nothing here touches the network

        card, layout = _section(
            "Movie night",
            "Watch a film or episode from your library with friends who have Mistery, in "
            "sync. Start one from a film's page or from the player; friends join with the "
            "code it gives you, and each of you keeps your own place in it.",
        )

        name_row = QHBoxLayout()
        name_caption = QLabel("Your name")
        name_caption.setMinimumWidth(150)
        name_row.addWidget(name_caption)
        self._party_name = QLineEdit()
        self._party_name.setMaxLength(people.NAME_MAX)
        self._party_name.setMinimumWidth(260)
        # The name friends see when this is empty: the Windows user name, as
        # people.display_name() falls back to it.
        self._party_name.setPlaceholderText(
            people.clean_name(os.environ.get("USERNAME", "")) or people.FALLBACK_NAME)
        self._party_name.editingFinished.connect(self._on_party_name)
        name_row.addWidget(self._party_name)
        name_row.addStretch(1)
        layout.addLayout(name_row)

        port_row = QHBoxLayout()
        port_caption = QLabel("Port")
        port_caption.setMinimumWidth(150)
        port_row.addWidget(port_caption)
        self._party_port = QSpinBox()
        # Below 1024 are the ports Windows services and routers keep for
        # themselves; none of them is a sensible place for this.
        self._party_port.setRange(1024, 65535)
        self._party_port.setMinimumWidth(110)
        # Saved once the number is typed, not at every digit: typing 42170 again
        # passed through 4217 on the way, and that unticked "I've forwarded".
        self._party_port.setKeyboardTracking(False)
        self._party_port.valueChanged.connect(self._on_party_port)
        port_row.addWidget(self._party_port)
        port_row.addStretch(1)
        layout.addLayout(port_row)

        self._party_upnp = QCheckBox(f"{_UPNP_BOX} while a movie night or sharing is on (UPnP)")
        self._party_upnp.toggled.connect(self._on_party_upnp)
        layout.addWidget(self._party_upnp)

        # The owner's word that the port is forwarded by hand, and only their
        # word: with UPnP silent (as on the router of the PC this was built
        # on) nothing here can see a forward. Ticked, the host panel says so
        # in one line instead of walking through the router's page every
        # evening. It names the port because a forward is for one number.
        self._party_forwarded = QCheckBox()
        self._party_forwarded.toggled.connect(self._on_party_forwarded)
        layout.addWidget(self._party_forwarded)

        quality_row = QHBoxLayout()
        quality_caption = QLabel("Friends start with")
        quality_caption.setMinimumWidth(150)
        quality_row.addWidget(quality_caption)
        self._party_quality = QComboBox()
        # "auto" sends the file as it is: on the PC this was written on the
        # upload measured 909 Mbit/s and the heaviest film needs 11.7 Mbit/s a
        # friend (transcode.per_friend_mbps). A smaller picture is each
        # friend's own choice, made per request.
        self._party_quality.addItem("The file as it is (recommended)", "auto")
        self._party_quality.addItem("1080p, made on the fly", "1080p")
        self._party_quality.addItem("720p, made on the fly", "720p")
        self._party_quality.setMinimumWidth(260)
        self._party_quality.currentIndexChanged.connect(self._on_party_quality)
        quality_row.addWidget(self._party_quality)
        quality_row.addStretch(1)
        layout.addLayout(quality_row)

        # What the last change did; hidden until there is something to say,
        # or its empty line sat as a gap in the middle of the card.
        self._party_note = QLabel()
        self._party_note.setObjectName("Faint")
        self._party_note.setWordWrap(True)
        self._party_note.setVisible(False)
        layout.addWidget(self._party_note)

        explain = QLabel(
            "Friends can each switch to 1080p or 720p in their player, if the file is too much "
            "for their connection or their PC; this PC makes those as they watch.\n\n"
            "What opening a port means: while a movie night runs, Mistery listens on that one "
            f"port, and with “{_UPNP_BOX}” ticked asks your router to forward it to this PC, "
            "so friends outside your home can reach it. For that evening anyone on the internet "
            "could knock on it, so Mistery answers only someone with that movie night's code. "
            "Everything goes over an encrypted connection, and the film being watched is the only "
            "thing a movie night serves: never your library, never any other file. The port "
            "closes when the movie night ends, and if Mistery crashes mid-film it gives the router "
            "the port back the next time it starts.\n\n"
            "With library sharing on (the Friends page), the port stays open between movie nights "
            "too, for the friends you've added and nobody else: their Mistery has to show the "
            "certificate written down when you added each other. A movie night then uses this PC's "
            "lasting certificate rather than one made for the night.\n\n"
            "Mistery asks a public STUN server — Cloudflare's, then Google's — for your internet "
            "address when your router won't say; that is all it sends."
        )
        explain.setObjectName("Faint")
        explain.setWordWrap(True)
        layout.addWidget(explain)
        self._reload_movie_night()
        return card

    def _reload_movie_night(self) -> None:
        """Back in step with settings.json, saving nothing on the way."""
        boxes = (self._party_name, self._party_port, self._party_upnp, self._party_forwarded,
                 self._party_quality)
        for box in boxes:
            box.blockSignals(True)
        self._party_name.setText(str(settings.get("party_name", "") or ""))
        self._party_port.setValue(_party_port())
        self._party_upnp.setChecked(bool(settings.get("party_upnp", True)))
        self._party_forwarded.setText(_forwarded_words(_party_port()))
        self._party_forwarded.setChecked(bool(settings.get("party_forwarded", False)))
        # "original" is what "auto" means now, so it shows as that choice.
        wanted = str(settings.get("party_quality", "auto") or "auto")
        index = self._party_quality.findData("auto" if wanted == "original" else wanted)
        self._party_quality.setCurrentIndex(max(0, index))
        for box in boxes:
            box.blockSignals(False)

    def _on_party_name(self) -> None:
        from ..party import people

        # Kept exactly as friends will see it: clean_name is what the room
        # applies to every name, so the box shows the result, not the typing.
        name = people.clean_name(self._party_name.text())
        if name != self._party_name.text():
            self._party_name.setText(name)
        if name != str(settings.get("party_name", "") or ""):
            settings.set("party_name", name)
            self._say_party(f"Friends will see you as {name or people.display_name()}.")

    def _on_party_port(self, value: int) -> None:
        settings.set("party_port", int(value))
        self._party_forwarded.setText(_forwarded_words(value))
        note = (f"Saved. The next movie night listens on port {value}. A forward made by hand on "
                "the router has to use the same number.")
        if settings.get("party_forwarded", False):
            # The forward the box spoke for is for the old number: left ticked,
            # the panel would keep saying the port is forwarded when it is not.
            settings.set("party_forwarded", False)
            self._party_forwarded.blockSignals(True)
            self._party_forwarded.setChecked(False)
            self._party_forwarded.blockSignals(False)
            # The box by its words, as the explanation names the UPnP one: this
            # note shows under "Friends start with", two rows below that box,
            # so "the box below" it once said pointed at no box at all.
            note += f" So “{_forwarded_words(value)}” is unticked until you have."
        self._say_party(note)

    def _on_party_upnp(self, on: bool) -> None:
        settings.set("party_upnp", bool(on))
        self._say_party(
            "Saved. The next movie night asks your router to open the port." if on else
            "Saved. Mistery won't ask your router: friends at your place can still join, and "
            "friends elsewhere once you forward the port yourself.")

    def _on_party_forwarded(self, on: bool) -> None:
        settings.set("party_forwarded", bool(on))
        self._say_party(
            "Saved. The movie night panel says the port is forwarded in one line, with the router "
            "steps behind Show how." if on else
            "Saved. The movie night panel shows the router steps again.")

    def _on_party_quality(self) -> None:
        settings.set("party_quality", self._party_quality.currentData())
        self._say_party("Saved. It applies from the next movie night, or the next "
                        "episode of this one.")

    def _say_party(self, text: str) -> None:
        self._party_note.setText(text)
        self._party_note.setVisible(bool(text))

    def show_movie_night(self) -> None:
        """Scroll to Movie night (the host's panel sends a port problem here)."""
        self.reload()
        self._scroll.verticalScrollBar().setValue(max(0, self._movie_night.y() - 20))

    # --- vr -----------------------------------------------------------------

    def _build_controller(self) -> QWidget:
        card, layout = _section(
            "Game controller",
            "An Xbox or PlayStation controller works Mistery from the couch: a yellow glow on "
            "whatever it is on, and what its buttons do along the bottom. It is only read while "
            "Mistery's window is in front, so a game in another window keeps it to itself.",
        )
        self._gamepad_on = QCheckBox("Use a game controller")
        self._gamepad_on.setChecked(bool(settings.get("gamepad", True)))
        self._gamepad_on.toggled.connect(self._on_gamepad_toggled)
        layout.addWidget(self._gamepad_on)
        self._gamepad_bigger = QCheckBox("Bigger text and buttons, for across the room (after a restart)")
        self._gamepad_bigger.setChecked(bool(settings.get("couch_bigger", False)))
        self._gamepad_bigger.toggled.connect(lambda on: settings.set("couch_bigger", bool(on)))
        layout.addWidget(self._gamepad_bigger)
        self._gamepad_state = QLabel("No controller connected.")
        self._gamepad_state.setObjectName("Faint")
        layout.addWidget(self._gamepad_state)
        legend = QLabel(
            "<b>A</b> or <b>✕</b> opens · <b>B</b> or <b>○</b> goes back · <b>X</b> or <b>□</b> Watch together · "
            "<b>Y</b> or <b>△</b> Search · <b>LB RB</b> or <b>L1 R1</b> the rows · <b>Start</b> or "
            "<b>Options</b> the menu<br>In the player: <b>A</b> pause · <b>LB RB</b> back and on "
            f"{int(settings.get('seek_step', 10))} s · <b>LT RT</b> further · <b>Y</b> subtitles · "
            "<b>X</b> skip the intro · up and down, the volume · <b>B</b> close")
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setWordWrap(True)
        legend.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9.5pt;")
        layout.addWidget(legend)
        return card

    def _on_gamepad_toggled(self, on: bool) -> None:
        settings.set("gamepad", bool(on))
        self.gamepad_changed.emit(bool(on))

    def set_gamepad_state(self, words: str) -> None:
        """What is connected, from the window (couch.py)."""
        self._gamepad_state.setText(words)

    def _build_vr(self) -> QWidget:
        card, layout = _section(
            "VR / headset",
            "\"Play in VR\" on a film's page hands the file to a VR video player. "
            "Desktop-streaming apps (Quest Link, Virtual Desktop, Steam Link) work "
            "differently — they put your monitor in the headset, so Mistery just "
            "plays fullscreen instead.",
        )

        self._vr_found = QLabel()
        self._vr_found.setWordWrap(True)
        self._vr_found.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._vr_found)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Use"))
        self._vr_target = QComboBox()
        self._vr_target.currentIndexChanged.connect(self._on_vr_target_changed)
        row.addWidget(self._vr_target, 1)

        rescan = QPushButton("Re-detect")
        rescan.clicked.connect(lambda: self._reload_vr(force=True))
        row.addWidget(rescan)
        row.addStretch(1)
        layout.addLayout(row)

        custom_row = QHBoxLayout()
        custom_row.setSpacing(9)
        self._vr_custom = QLineEdit(settings.get("vr_custom_path", ""))
        self._vr_custom.setPlaceholderText("Optional: path to any VR player .exe")
        self._vr_custom.editingFinished.connect(self._on_vr_custom_changed)
        custom_row.addWidget(self._vr_custom, 1)

        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_vr_player)
        custom_row.addWidget(browse)
        layout.addLayout(custom_row)

        self._vr_args = QLineEdit(settings.get("vr_custom_args", "{file}"))
        self._vr_args.setPlaceholderText("Arguments for the custom player, {file} = the video")
        self._vr_args.editingFinished.connect(
            lambda: settings.set("vr_custom_args", self._vr_args.text().strip() or "{file}")
        )
        layout.addWidget(self._vr_args)
        return card

    def _on_vr_target_changed(self) -> None:
        data = self._vr_target.currentData()
        if data is not None:
            settings.set("vr_target", data)

    def _on_vr_custom_changed(self) -> None:
        settings.set("vr_custom_path", self._vr_custom.text().strip())
        self._reload_vr(force=True)

    def _browse_vr_player(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a VR video player", "", "Programs (*.exe);;All files (*)"
        )
        if chosen:
            self._vr_custom.setText(chosen)
            self._on_vr_custom_changed()

    def _reload_vr(self, force: bool = False) -> None:
        targets = vr.detect(force=force)
        runtime = vr.openxr_runtime()

        lines = []
        if runtime:
            lines.append(
                f"<span style='color:{C.SUCCESS}'>&#10003;</span> OpenXR runtime: "
                f"<b>{runtime}</b>"
            )
        if targets:
            for target in sorted(targets, key=lambda t: t.rank):
                kind = "dedicated player" if target.kind == vr.PLAYER else "desktop streaming"
                suffix = ""
                if target.rank == vr.RANK_UNVERIFIED_PLAYER:
                    suffix = (f" <span style='color:{C.TEXT_FAINT}'>"
                              "— select manually to try</span>")
                lines.append(
                    f"<span style='color:{C.SUCCESS}'>&#10003;</span> "
                    f"<b>{target.name}</b> <span style='color:{C.TEXT_FAINT}'>"
                    f"({kind})</span>{suffix}"
                )
        if lines:
            self._vr_found.setText("<div style='line-height:175%'>"
                                   + "<br>".join(lines) + "</div>")
        else:
            self._vr_found.setText(
                f"<span style='color:{C.TEXT_DIM}'>Nothing detected yet. Install "
                "Meta Quest Link, Virtual Desktop or Steam Link for desktop streaming, "
                "or DeoVR / Skybox VR / Whirligig for a dedicated VR player — then "
                "press Re-detect.</span>"
            )

        current = str(settings.get("vr_target", "auto"))
        self._vr_target.blockSignals(True)
        self._vr_target.clear()
        self._vr_target.addItem("Automatic (best available)", "auto")
        for target in targets:
            self._vr_target.addItem(target.name, target.key)
        index = self._vr_target.findData(current)
        self._vr_target.setCurrentIndex(index if index >= 0 else 0)
        self._vr_target.blockSignals(False)

    # --- about --------------------------------------------------------------

    def _build_about(self) -> QWidget:
        card, layout = _section("System")
        self._about = QLabel()
        self._about.setTextFormat(Qt.TextFormat.RichText)
        self._about.setWordWrap(True)
        self._about.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._about)
        self._build_updates(layout)
        return card

    def _build_updates(self, layout: QVBoxLayout) -> None:
        """The update switch and Check now — on installed copies only.

        Run from source there is no MisteryUpdate.exe beside main.py to talk to,
        so nothing is drawn: a switch that writes a setting no program reads is
        worse than no switch. app/updates.py is the only thing here that knows
        the updater exists, and updater.json in the install folder is the one
        place the answer is kept — not settings.json, because the updater has to
        work on a PC where Mistery has never been opened.
        """
        self._auto_update = None
        if updates.updater_exe() is None:
            return

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self._auto_update = QCheckBox("Keep Mistery up to date")
        self._auto_update.toggled.connect(self._on_auto_update)
        self._check_updates = QPushButton("Check now")
        self._check_updates.clicked.connect(self._check_for_updates)
        row.addWidget(self._auto_update)
        row.addStretch(1)
        row.addWidget(self._check_updates)
        holder = QWidget()
        holder.setLayout(row)
        layout.addWidget(holder)

        self._update_status = QLabel()
        self._update_status.setObjectName("Faint")
        self._update_status.setWordWrap(True)
        layout.addWidget(self._update_status)
        self._reload_updates()

    def _reload_updates(self) -> None:
        """Put the switch and the line under it back in step with updater.json."""
        if self._auto_update is None:
            return
        on = updates.auto_update_enabled()
        self._auto_update.blockSignals(True)
        self._auto_update.setChecked(on)
        self._auto_update.blockSignals(False)
        self._auto_update.setEnabled(True)
        # Off means off: the updater refuses --check-now as well, rather than
        # reaching out because a button was pressed. So the button goes too.
        self._check_updates.setEnabled(on)
        if not on:
            self._update_status.setText(
                "Mistery will not contact the update server while this is off. "
                "Updates are installed only when Mistery has been closed for "
                "half an hour, and never ask for an administrator.")
            return
        answer = updates.last_check()
        if not answer:
            self._update_status.setText(
                "Checked once an hour in the background. An update installs "
                "itself only after Mistery has been closed for half an hour.")
            return
        self._update_status.setText(updates.describe(answer) + _checked_when(answer))

    def _on_auto_update(self, on: bool) -> None:
        """Tell MisteryUpdate.exe, on a thread: it is a process start (0.2 s
        measured, but a first run past an antivirus scan is not), and the
        settings screen freezing on a checkbox is the sort of thing people
        remember. The controls stay off until it answers."""
        self._auto_update.setEnabled(False)
        self._check_updates.setEnabled(False)
        self._update_status.setText("Turning updates on…" if on else "Turning updates off…")

        def switch() -> None:
            told = updates.set_auto_update(on)
            try:
                self._update_switched.emit(told)
            except RuntimeError:
                pass                    # the window is already gone

        threading.Thread(target=switch, name="update-switch", daemon=True).start()

    def _on_update_switched(self, told: bool) -> None:
        # _reload_updates re-reads updater.json, so the box shows what is in the
        # file rather than what was clicked — if the write failed it goes back.
        self._reload_updates()
        if not told:
            self._update_status.setText(
                f"<span style='color:{C.DANGER}'>Could not reach MisteryUpdate.exe, "
                "so the setting was not changed.</span>")

    def _check_for_updates(self) -> None:
        # The switch goes off too while this runs: turning updates off half way
        # through a check would leave the answer contradicting the checkbox.
        self._auto_update.setEnabled(False)
        self._check_updates.setEnabled(False)
        self._update_status.setText("Checking…")

        def ask() -> None:
            answer = updates.check_now()
            try:
                self._update_checked.emit(answer)
            except RuntimeError:
                pass                    # the window is already gone

        threading.Thread(target=ask, name="update-check", daemon=True).start()

    def _on_update_checked(self, answer: dict) -> None:
        self._auto_update.setEnabled(True)
        self._check_updates.setEnabled(True)
        self._update_status.setText(updates.describe(answer) + _checked_when(answer))

    # --- lifecycle ----------------------------------------------------------

    def _reload_playback_toggles(self) -> None:
        """Re-read the settings the player can change behind this page's back.

        Autoplay has a toggle in the control bar, dialogue boost has a button
        and the B key, and auto-skip is in the TV menu — so these checkboxes go
        stale unless they are read again on the way in.
        """
        for box, key, default in (
            (self._autoplay, "autoplay_next", True),
            (self._dialogue, "dialogue_boost", False),
            (self._auto_skip, "auto_skip_intro", True),
        ):
            box.blockSignals(True)
            box.setChecked(bool(settings.get(key, default)))
            box.blockSignals(False)

        # Quality also has a control in the player's own chrome.
        index = self._quality.findData(
            quality_preset(settings.get("video_quality", "balanced"))
        )
        if index >= 0:
            self._quality.blockSignals(True)
            self._quality.setCurrentIndex(index)
            self._quality.blockSignals(False)

    def reload(self) -> None:
        self._reload_folders()
        self._reload_vr()
        self._reload_discord()
        self._reload_movie_night()
        self._reload_playback_toggles()
        self._reload_sound()
        self._reload_music()
        self._reload_updates()

        failed = db.failed_count()
        self._retry.setEnabled(bool(failed))
        self._folders_note.setText(
            f"{failed} file(s) failed to process — press Retry failed."
            if failed else ""
        )
        self._tmdb_key.setText(settings.get("tmdb_api_key", ""))

        if settings.get("tmdb_api_key"):
            self._tmdb_status.setText("A key is saved. Artwork comes from TMDB.")
        else:
            self._tmdb_status.setText(
                "No key set — artwork is generated from video frames."
            )

        stats = db.library_stats()
        def _tool(name: str, path: str | None) -> str:
            colour = C.SUCCESS if path else C.DANGER
            return f"{name}: <span style='color:{colour}'>{path or 'not found'}</span>"

        incomplete = ""
        if stats.get("downloading"):
            incomplete = (
                f"<br><span style='color:{C.TEXT_DIM}'>{stats['downloading']} file(s) "
                "look like downloads in progress — they are re-checked on every scan "
                "and appear once readable.</span>"
            )
        self._about.setText(
            f"<div style='line-height:170%'>"
            f"<b>Mistery {__version__}</b><br>"
            f"{stats['movies']} movies · {stats['shows']} shows · "
            f"{stats['files']} files · {fmt_size(stats['bytes'])} · "
            f"{fmt_duration(stats['seconds'])} of runtime{incomplete}<br>"
            f"{_tool('mpv', find_mpv())}<br>"
            f"{_tool('ffmpeg', find_ffmpeg())}<br>"
            f"{_tool('ffprobe', find_ffprobe())}"
            f"</div>"
        )
