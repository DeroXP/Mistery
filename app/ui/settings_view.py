"""Settings: library folders, TMDB key, playback preferences."""

from __future__ import annotations

import html
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from .. import db, vr
from ..discord_presence import DiscordPresence
from ..config import find_ffmpeg, find_ffprobe, find_mpv, settings
from ..metadata.tmdb import TmdbClient
from ..music import audio_fx, library as music_library, loudness
from ..player.mpv_process import quality_preset
from ..util import fmt_duration, fmt_size, reveal_in_explorer
from .theme import C


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
    # Answers from work done on a thread of its own (the TMDB key check, the
    # Discord art export), carried back to the UI thread.
    _tmdb_checked = Signal(bool, str)
    _art_exported = Signal(str, int, int, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tmdb_checked.connect(self._on_tmdb_checked)
        self._art_exported.connect(self._on_art_exported)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(52, 34, 52, 52)
        layout.setSpacing(22)
        scroll.setWidget(page)

        heading = QLabel("Settings")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)

        layout.addWidget(self._build_folders())
        layout.addWidget(self._build_tmdb())
        layout.addWidget(self._build_playback())
        layout.addWidget(self._build_music())
        layout.addWidget(self._build_sound())
        layout.addWidget(self._build_thumbnails())
        layout.addWidget(self._build_discord())
        layout.addWidget(self._build_vr())
        layout.addWidget(self._build_about())
        layout.addStretch(1)

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
            "an application of its own to attribute this to, which takes about "
            "a minute to make: discord.com/developers/applications → New "
            "Application → copy the Application ID. Optionally upload an image "
            "named 'mistery' under Rich Presence → Art Assets for the icon.",
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
            "Discord shows art you have uploaded to your own application, so it "
            "can only use a picture you have given it. This builds one 1024x576 "
            "image per show and film — the 16:9 shape Discord requires — already "
            "named the way Mistery will ask for it. Drop the whole folder into "
            "Rich Presence → Art Assets and each title gets its own image. "
            "Anything you skip falls back to the Mistery icon."
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
        from ..discord_presence import asset_key

        # One image per show and per film — an episode uses its show's art.
        wanted: dict[str, tuple[str, str]] = {}
        for row in list(db.all_shows()) + list(db.movies()):
            columns = row.keys()
            backdrop = (row["backdrop"] if "backdrop" in columns else "") or ""
            poster = row["poster"] or backdrop
            if poster:
                wanted.setdefault(asset_key(row["title"]), (poster, backdrop))
        wanted.pop("", None)

        target = data_dir() / "discord-art"
        self._discord_export.setEnabled(False)
        self._discord_status.setText(f"Building {len(wanted)} image(s)…")
        threading.Thread(
            target=self._write_discord_art, args=(target, wanted),
            name="discord-art", daemon=True,
        ).start()

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
        reveal_in_explorer(target)
        note = (f"Wrote {written} image(s) at {ART_SIZE[0]}x{ART_SIZE[1]} "
                f"to {target}.")
        if skipped:
            note += f" {skipped} could not be read."
        self._discord_status.setText(
            note + "  Upload them under Rich Presence → Art Assets, keeping the "
            "file names exactly as they are."
        )

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

    # --- vr -----------------------------------------------------------------

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
        return card

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
        self._reload_playback_toggles()
        self._reload_sound()
        self._reload_music()

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
            f"{stats['movies']} movies · {stats['shows']} shows · "
            f"{stats['files']} files · {fmt_size(stats['bytes'])} · "
            f"{fmt_duration(stats['seconds'])} of runtime{incomplete}<br>"
            f"{_tool('mpv', find_mpv())}<br>"
            f"{_tool('ffmpeg', find_ffmpeg())}<br>"
            f"{_tool('ffprobe', find_ffprobe())}"
            f"</div>"
        )
