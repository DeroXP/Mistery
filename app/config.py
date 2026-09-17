"""Application paths, user settings and external-tool discovery."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

APP_NAME = "Mistery"

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts",
    ".webm", ".flv", ".mpg", ".mpeg", ".divx", ".vob", ".ogv", ".rmvb", ".3gp",
}

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".idx"}

AUDIO_EXTS = {
    ".flac", ".mp3", ".m4a", ".aac", ".alac", ".ogg", ".oga", ".opus", ".wav",
    ".aif", ".aiff", ".wma", ".ape", ".wv", ".mka", ".dsf",
}


def data_dir() -> Path:
    """Per-user application data directory (created on first access).

    MISTERY_DATA_DIR points it somewhere else entirely — the library, settings,
    artwork, thumbnails and log. Tests use it so they can run against a copy of
    a real library without writing a single file into the real one.
    """
    override = os.environ.get("MISTERY_DATA_DIR")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("APPDATA")
        root = Path(base) / APP_NAME if base else Path.home() / f".{APP_NAME.lower()}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def virtualized_appdata() -> Path | None:
    """Where %APPDATA% writes are really going, if not where they appear to.

    A process started from inside a packaged (MSIX) desktop app — the Claude
    desktop app, for one — sees a merged view of AppData: existing files are
    shared, but every *new* file is silently redirected into that package's
    private LocalCache. Run Mistery that way and it writes artwork the normal
    Mistery can never see, while recording in the shared library that the
    artwork exists. This is how four album covers went missing. Returns the
    private folder when that redirection is happening, None otherwise.
    """
    if os.environ.get("MISTERY_DATA_DIR") or os.name != "nt":
        return None
    local = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    if not local or not appdata:
        return None
    packages = Path(local) / "Packages"
    if not packages.is_dir():
        return None
    probe_name = f".mistery-virtualization-probe-{os.getpid()}"
    probe = Path(appdata) / APP_NAME / probe_name
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("probe", encoding="utf-8")
    except OSError:
        return None
    try:
        for private in packages.glob(f"*/LocalCache/Roaming/{APP_NAME}"):
            if (private / probe_name).exists():
                return private
        return None
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def art_dir() -> Path:
    d = data_dir() / "art"
    d.mkdir(parents=True, exist_ok=True)
    return d


def thumbs_dir() -> Path:
    d = data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def music_art_dir() -> Path:
    d = data_dir() / "music-art"
    d.mkdir(parents=True, exist_ok=True)
    return d


def assets_dir() -> Path:
    """Bundled assets that ship with the source, not user data."""
    return Path(__file__).resolve().parent.parent / "assets"


def icon_path() -> Path | None:
    icon = assets_dir() / "icon.ico"
    return icon if icon.is_file() else None


def db_path() -> Path:
    return data_dir() / "library.db"


def settings_path() -> Path:
    return data_dir() / "settings.json"


# --- external tools ---------------------------------------------------------

_MPV_CANDIDATES = [
    r"C:\Program Files\MPV Player\mpv.exe",
    r"C:\Program Files\mpv\mpv.exe",
    r"C:\Program Files (x86)\MPV Player\mpv.exe",
]


def _process_is_alive(pid: int) -> bool:
    """Liveness test that never touches the process.

    NOT os.kill(pid, 0): on Windows os.kill only understands CTRL_C_EVENT and
    CTRL_BREAK_EVENT — any other signal calls TerminateProcess, so the "check"
    would kill whatever owns that pid.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)      # genuinely a no-op signal on POSIX
            return True
        except OSError:
            return False

    import ctypes
    import ctypes.wintypes as wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _process_started_at(pid: int) -> float | None:
    """When a process was created, on the time.time() clock; None if unknown."""
    if os.name != "nt":
        return None

    import ctypes
    import ctypes.wintypes as wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime   # 100 ns since 1601
        return ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel32.CloseHandle(handle)


def app_is_running() -> int | None:
    """PID of a live Mistery instance, or None. Used to avoid racing it.

    The lock records the start time too: a pid alone is not enough, because
    Windows recycles pids and a stale lock could point at something unrelated.
    Mistery writes the lock after it has started, so a process with that pid
    created after the lock was written is some other program. Without that
    comparison, a Mistery ended in Task Manager could leave a lock that kept
    Mistery from opening for as long as whatever inherited its pid ran.
    """
    lock = data_dir() / "app.lock"
    if not lock.is_file():
        return None
    try:
        parts = lock.read_text(encoding="utf-8").strip().split(",")
        pid = int(parts[0])
        started = float(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError):
        return None

    if not _process_is_alive(pid):
        return None
    if started is not None and (time.time() - started) < 0:
        return None              # clock moved; treat as stale rather than block
    created = _process_started_at(pid) if started is not None else None
    if created is not None and created > started + 2:
        return None              # the pid has been handed to something else since
    return pid


def subprocess_flags(low_priority: bool = False) -> int:
    """Creation flags for helper processes.

    Background ffmpeg work runs below normal priority so it never competes with
    playback or whatever else is in the foreground.
    """
    import subprocess

    flags = 0
    if os.name == "nt":
        flags |= subprocess.CREATE_NO_WINDOW
        if low_priority:
            flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
    return flags


def _find_tool(name: str, extra: list[str] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra or []:
        if Path(candidate).is_file():
            return candidate
    return None


def find_mpv() -> str | None:
    return _find_tool("mpv", _MPV_CANDIDATES)


def find_ffmpeg() -> str | None:
    return _find_tool("ffmpeg")


def find_ffprobe() -> str | None:
    return _find_tool("ffprobe")


# --- settings ---------------------------------------------------------------

DEFAULTS: dict = {
    "library_folders": [str(Path.home() / "Music")],
    "tmdb_api_key": "",
    "tmdb_language": "en-US",
    # playback
    "volume": 80,
    "dialogue_boost": False,
    "hdr_tone_mapping": True,
    "hwdec": "auto-safe",
    "autoplay_next": True,
    "auto_skip_intro": True,     # jump the intro automatically once it's marked
    "seek_step": 10,
    "preferred_audio_lang": "eng",
    "preferred_sub_lang": "eng",
    "subs_on_by_default": False,
    # when saved progress began recording subtitles as on or off (time.time(),
    # 0 until a player is first built); see player_view._subtitle_state_since
    "subtitle_state_since": 0,
    # video quality: 'fast' | 'balanced' | 'high' (GPU cost of scaling/HDR)
    "video_quality": "balanced",
    # Discord Rich Presence (needs an application ID from the developer portal)
    "discord_presence": False,
    "discord_client_id": "",
    "discord_hide_titles": False,   # show "Watching something" instead of names
    # VR hand-off: which installed player to use, or a custom executable
    "vr_target": "auto",
    "vr_custom_path": "",
    "vr_custom_args": "{file}",
    # music
    "music_volume": 70,
    "music_shuffle": False,
    "music_repeat": "off",          # off | all | one
    "fetch_lyrics": True,           # synced lyrics from LRCLIB when a track has none
    "close_to_tray": True,          # closing the window while music plays keeps it playing
    "tray_hint_shown": False,
    # the queue, song and position come back paused at the next start
    # (music-session.json in the data folder; see MusicPlayer.restore_session)
    "music_resume": True,
    "music_audio_device": "auto",   # an mpv audio-device name; auto follows Windows' default
    "music_cover_style": "disc",    # disc (spinning record) | cover (flat square)
    "music_sleep_last": 30,         # the last sleep timer length chosen, in minutes
    "music_show_remaining": False,  # the time after the seek bar counts down (-2:31) instead of the length
    # sound. Downloaded albums disagree about loudness by more than 10 dB, so
    # levelling is on by default, at the level streaming services use. The +3 dB
    # on top is what stops the loudest album in a library getting quieter when
    # levelling is switched on. See music/loudness.py and music/audio_fx.py.
    "music_normalize": "loud",      # off | quiet | normal | loud  (target LUFS)
    "music_boost": "low",           # off | low (+3 dB) | high (+6 dB)
    "music_bass": "off",            # off | warm | deep | massive
    "music_clarity": False,         # a 3 dB lift at the top end
    "music_spatial": "off",         # off | subtle | normal | wide  (headphones)
    "music_muted": False,           # mpv's own mute, so a mute keeps the level it was at
    # The lyrics tab takes over the screen while a song plays and nobody has
    # touched anything. Built for OLED panels: see ui/screensaver.py.
    "music_screensaver": True,
    "music_screensaver_after": 180,     # seconds of stillness before it starts
    # library behaviour
    # Categories chosen on the Movies and Shows pages, kept between sessions so
    # a narrowed library is still narrowed tomorrow.
    "movie_categories": [],
    "show_categories": [],
    "generate_thumbs": True,
    "thumb_count": 120,
    "detect_intros": True,       # learn intro/credits by matching audio across a season
    "pause_background_during_playback": True,
    # set to app.parser.PARSER_VERSION once a library has been re-parsed
    "parser_version": 0,
    "watched_threshold": 0.92,   # fraction of runtime after which it counts as watched
    "resume_min_seconds": 30,    # don't offer resume for the first N seconds
    "scan_on_startup": True,
    # Updates. The updater is a separate program that only runs when Mistery
    # has been closed for half an hour; this is the switch that stops it.
    "auto_update": True,
}


class Settings:
    """Small JSON-backed settings store, safe to touch from worker threads.

    It never writes defaults over a settings.json it could not read. That used
    to be the first thing that happened after a bad start: the file cut short or
    zero-filled by a power cut (it is rewritten on every volume tick, and not
    flushed), or held shut for a moment by a backup or antivirus scan, loaded as
    nothing, and the next volume change saved the defaults over it. The TMDB key,
    the Discord ID, every Sound setting and close-to-tray went with it.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._values: dict = dict(DEFAULTS)
        self._changed: set[str] = set()     # set in this session
        self._unread = False                # settings.json is there but could not be read
        self._unsaved = False               # the last save did not reach the disk
        self.load()
        atexit.register(self._save_if_unsaved)

    def load(self) -> None:
        path = settings_path()
        if not path.is_file():
            self.save()
            return
        stored, problem, raw = self._read(path)
        if problem == "damaged":
            # Kept for whoever wants to dig into it, then replaced by the copy
            # taken at the last start that read it.
            aside = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                aside.write_bytes(raw)
            except OSError:
                pass
        if stored is None:
            self._unread = problem == "unreadable"
            stored, _, _ = self._read(self._backup_path(), attempts=1)
        else:
            self._keep_backup(raw)
        if stored:
            self._apply(stored, keep_changed=False)

    def _apply(self, stored: dict, keep_changed: bool) -> None:
        with self._lock:
            for key, value in stored.items():
                if keep_changed and key in self._changed:
                    continue
                # A null in the file means the value was lost, not chosen —
                # a settings key written back before it had a default, say.
                # Falling back keeps one of those from silently switching a
                # feature off for good.
                if key in DEFAULTS and (value is not None or DEFAULTS[key] is None):
                    self._values[key] = value

    @staticmethod
    def _backup_path() -> Path:
        return settings_path().with_name(settings_path().name + ".bak")

    def _keep_backup(self, raw: bytes) -> None:
        """A copy of settings that just read cleanly, for a start that can't.
        Flushed to disk, unlike ordinary saves, since it only happens at startup,
        and left alone when nothing changed so a good copy is never at risk."""
        backup = self._backup_path()
        try:
            if backup.read_bytes() == raw:
                return
        except OSError:
            pass
        self._write(backup, raw.decode("utf-8"), durable=True)

    @staticmethod
    def _read(path: Path, attempts: int = 5) -> tuple[dict | None, str, bytes]:
        """The settings in a file, or why not: "unreadable" when the file could
        not be opened (held shut by another program, usually for a moment),
        "damaged" when what it holds is not settings."""
        raw = b""
        for attempt in range(attempts):
            try:
                raw = path.read_bytes()
                break
            except OSError:
                if attempt == attempts - 1:
                    return None, "unreadable", raw
                time.sleep(0.05 * (attempt + 1))
        try:
            stored = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None, "damaged", raw
        if not isinstance(stored, dict):
            return None, "damaged", raw
        return stored, "", raw

    @staticmethod
    def _write(path: Path, text: str, durable: bool = False) -> bool:
        """Write through a temp file of this save's own, then swap it in.

        Every save used to share settings.json.tmp, so two at once (a worker
        recording parser_version while the volume moved) wrote over each other
        and the torn result was swapped in: 4649 unreadable files in 30 bursts of
        400 changes. And a swap refused because another program had the file open
        for a moment was dropped without a word.
        """
        try:
            fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        except OSError:
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                if durable:
                    handle.flush()
                    os.fsync(handle.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    return True
                except PermissionError:
                    time.sleep(0.02 * (attempt + 1))
            return False
        except OSError:
            return False
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def save(self) -> None:
        with self._save_lock:
            path = settings_path()
            if self._unread:
                # Read what the user has before writing anything: this session's
                # own changes go on top of it, never defaults in its place.
                stored, problem, _ = self._read(path, attempts=1)
                if problem == "unreadable" and path.is_file():
                    self._unsaved = True
                    return
                if stored:
                    self._apply(stored, keep_changed=True)
                self._unread = False
            with self._lock:
                snapshot = dict(self._values)
            self._unsaved = not self._write(path, json.dumps(snapshot, indent=2))

    def _save_if_unsaved(self) -> None:
        if self._unsaved:
            self.save()

    def get(self, key: str, default=None):
        with self._lock:
            value = self._values.get(key, DEFAULTS.get(key, default))
        if value is None and DEFAULTS.get(key) is not None:
            return DEFAULTS[key]
        return value

    def set(self, key: str, value) -> None:
        with self._lock:
            self._values[key] = value
            self._changed.add(key)
        self.save()

    def update(self, **kwargs) -> None:
        with self._lock:
            self._values.update(kwargs)
            self._changed.update(kwargs)
        self.save()

    def library_folders(self) -> list[Path]:
        folders = []
        for raw in self.get("library_folders", []):
            path = Path(raw)
            if path.is_dir():
                folders.append(path)
        return folders


settings = Settings()
