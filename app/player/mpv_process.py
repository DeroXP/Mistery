"""Drive mpv.exe as a child process over its JSON IPC socket.

The mpv build shipped by winget is a single statically linked executable with no
libmpv DLL, so instead of binding the C API we embed the player window with
``--wid`` and talk to it over a Windows named pipe.

I/O detail worth knowing: a named pipe opened with plain ``open()`` gets a
*synchronous* handle, and Windows serialises operations on such a handle. A
blocking read in one thread would therefore stall writes from another and
deadlock. So all traffic goes through a single pump thread that polls with
``PeekNamedPipe`` and never issues a blocking read.

Writes can still block, and that is the second thing worth knowing. The pipe
holds about 4 KB that mpv has not read yet; past that, our write waits for mpv to
read. mpv's IPC thread, meanwhile, writes each reply and event before it reads
the next command — and waits if we are not reading. A pump that writes a long
burst without reading therefore ends with both sides waiting on each other for
good. mpv plays on regardless, but nothing it says reaches the app again: queuing
92 songs (18 KB) left the music bar frozen on the first song, showing it paused,
with every button ignored.

So the pump keeps the bytes of commands mpv has not answered under that 4 KB,
and reads between every write. Every byte in the pipe's inbound buffer belongs
to a command whose reply has not been read, so a write that fits the window
cannot block, however much mpv has to say. Counting commands instead of bytes
was the first attempt, and it still wedged with 600-byte commands.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ..config import find_mpv, settings

_IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0
_log_pipe = logging.getLogger("mpv")

# Flow control for the IPC pipe (see the module docstring). mpv's inbound pipe
# buffer is 4096 bytes — measured, with mpv's IPC thread held busy — so the bytes
# of unanswered commands stay just under it.
_WINDOW_BYTES = 4000
# How long mpv may take to answer before it is worth a line in the log. Never a
# reason to send more: a slow mpv is not helped by being handed more work.
_SLOW_REPLY = 2.0
_NEWLINE = b"\n"

# Properties we keep an eye on for the whole session.
OBSERVED_PROPERTIES = [
    "time-pos", "duration", "pause", "volume", "mute", "speed",
    "sub-visibility", "chapter", "chapter-list", "track-list",
    "eof-reached", "core-idle", "idle-active", "paused-for-cache",
    "demuxer-cache-time", "media-title", "path", "aid", "sid", "vid",
    "video-params/w", "video-params/h", "estimated-vf-fps", "seeking",
]


class MpvUnavailable(RuntimeError):
    """mpv.exe could not be found or refused to start."""


# Scaling quality is pure GPU cost, and it is the one playback knob worth
# exposing: mpv's expensive filters are wasted effort when downscaling 4K into a
# window. Every preset sets the same keys, so switching between them at runtime
# can never leave a filter behind from the previous one — which is what makes
# these safe to apply live instead of only at startup.
QUALITY_PRESETS: dict[str, dict[str, str]] = {
    "fast": {
        "scale": "bilinear", "dscale": "bilinear", "cscale": "bilinear",
        "dither": "no", "correct-downscaling": "no",
        "linear-downscaling": "no", "sigmoid-upscaling": "no", "deband": "no",
    },
    "balanced": {
        "scale": "spline36", "dscale": "mitchell", "cscale": "spline36",
        "dither": "fruit", "correct-downscaling": "yes",
        "linear-downscaling": "no", "sigmoid-upscaling": "yes", "deband": "no",
    },
    "high": {
        "scale": "ewa_lanczossharp", "dscale": "mitchell", "cscale": "ewa_lanczossoft",
        "dither": "fruit", "correct-downscaling": "yes",
        "linear-downscaling": "yes", "sigmoid-upscaling": "yes", "deband": "yes",
    },
}

QUALITY_LABELS = {
    "fast": "Data saver",
    "balanced": "Standard",
    "high": "Best",
}

# Peak detection is a compute pass and contrast recovery another, so only the
# upper presets pay for them.
_HDR_PEAK = {"fast": "no", "balanced": "auto", "high": "yes"}
_HDR_CONTRAST = {"fast": "0", "balanced": "0.30", "high": "0.30"}


def quality_preset(name: str) -> str:
    """Normalise a stored setting to a preset that exists."""
    key = str(name or "").lower()
    return key if key in QUALITY_PRESETS else "balanced"


_OPTION_NAMES: set[str] | None = None
_OPTION_RE = None


def supported_options(mpv_path: str) -> set[str]:
    """Every option name the installed mpv accepts.

    mpv renames and drops options between releases (0.41 removed
    ``--tone-mapping-mode``, for instance) and it aborts on the first unknown
    one, so we check our command line against the binary instead of assuming.
    """
    global _OPTION_NAMES
    if _OPTION_NAMES is not None:
        return _OPTION_NAMES
    names: set[str] = set()
    try:
        completed = subprocess.run(
            [mpv_path, "--list-options"], capture_output=True, timeout=20,
            creationflags=_NO_WINDOW,
        )
        for raw in completed.stdout.decode("utf-8", "replace").splitlines():
            stripped = raw.strip()
            if stripped.startswith("--"):
                name = stripped[2:].split()[0].split("=")[0]
                if name and "removed" not in stripped[-40:]:
                    names.add(name)
    except (OSError, subprocess.SubprocessError):
        pass
    _OPTION_NAMES = names
    return names


def filter_supported(args: list[str], mpv_path: str) -> tuple[list[str], list[str]]:
    """Split a command line into (accepted, dropped) by option name."""
    known = supported_options(mpv_path)
    if not known:
        return args, []          # couldn't introspect; trust the caller
    accepted, dropped = [], []
    for arg in args:
        if not arg.startswith("--"):
            accepted.append(arg)
            continue
        name = arg[2:].split("=", 1)[0]
        if name in known or (name.startswith("no-") and name[3:] in known):
            accepted.append(arg)
        else:
            dropped.append(arg)
    return accepted, dropped


if _IS_WINDOWS:
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PeekNamedPipe = _kernel32.PeekNamedPipe
    _PeekNamedPipe.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD,
    ]
    _PeekNamedPipe.restype = wintypes.BOOL

    def _bytes_available(fd: int) -> int:
        """How many bytes can be read right now without blocking."""
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return -1
        available = wintypes.DWORD(0)
        ok = _PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
        if not ok:
            return -1
        return available.value
    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _JOB = None

    def _die_with_us(process: subprocess.Popen) -> None:
        """Make Windows end mpv if Mistery ends — however it ends.

        A normal quit already stops mpv; this is for a crash or End Task. With
        music playing from the tray, an orphaned mpv would keep playing an album
        with no window left to stop it. A job object with KILL_ON_JOB_CLOSE is
        closed by the OS when this process exits, taking its members with it.
        """
        global _JOB
        try:
            if _JOB is None:
                job = _kernel32.CreateJobObjectW(None, None)
                if not job:
                    return
                limits = _ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
                if not _kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits),
                                                         ctypes.sizeof(limits)):
                    return
                _JOB = job
            _kernel32.AssignProcessToJobObject(_JOB, int(process._handle))
        except Exception:
            pass                    # best effort: never let this stop playback
else:  # pragma: no cover - the app targets Windows, this keeps imports working
    import select

    def _bytes_available(fd: int) -> int:
        readable, _, _ = select.select([fd], [], [], 0)
        return 65536 if readable else 0

    def _die_with_us(process) -> None:
        pass


class MpvProcess(QObject):
    """A running mpv instance rendered into a Qt widget's native window."""

    # The ceiling this mpv runs with: --volume-max on the command line, and
    # the clamp in set_volume. mpv clamps a higher number silently, which
    # would leave a caller's send-guard believing a level mpv never took —
    # so set_volume hands back what it really sent. AudioMpv lowers both to
    # 100, the ceiling music runs with.
    volume_max = 150

    property_changed = Signal(str, object)
    file_loaded = Signal()
    end_file = Signal(str)          # reason: eof / stop / quit / error / redirect
    playback_restart = Signal()
    log_message = Signal(str)
    failed = Signal(str)
    exited = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: subprocess.Popen | None = None
        self._pipe: int | None = None            # raw file descriptor
        self._pipe_file = None                   # keeps the handle alive
        self._pump: threading.Thread | None = None
        self._outbox: queue.Queue[bytes] = queue.Queue()
        self._running = threading.Event()
        self._request_id = 0
        self._request_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._properties: dict[str, Any] = {}
        self._observe_ids: dict[int, str] = {}
        self._ipc_path = ""
        self._stderr_lines: deque[str] = deque(maxlen=60)
        # Longest the pump waits for mpv to speak before looking again. Commands
        # never wait for this; only reading does. Raised for background playback.
        self.max_idle_wait = 0.02

    def _drain_stderr(self) -> None:
        """Keep mpv's stderr pipe empty; a full pipe would stall the process."""
        stream = self._process.stderr if self._process else None
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    self._stderr_lines.append(text)
                    self.log_message.emit(f"[mpv] {text}")
        except (OSError, ValueError):
            pass

    # --- lifecycle ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return bool(self._process and self._process.poll() is None and self._running.is_set())

    def start(self, window_id: int | None, extra_args: list[str] | None = None) -> None:
        """Launch mpv as a child of the native window `window_id`.

        Passing ``None`` gives mpv its own top-level window, which is only
        useful for testing the IPC layer in isolation.
        """
        if self.is_running:
            return

        mpv_path = find_mpv()
        if not mpv_path:
            raise MpvUnavailable(
                "mpv.exe was not found. Install it with:  winget install shinchiro.mpv"
            )

        token = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self._ipc_path = rf"\\.\pipe\mistery-{token}" if _IS_WINDOWS else f"/tmp/mistery-{token}"

        wanted = [*self._base_arguments(window_id), *(extra_args or [])]
        accepted, dropped = filter_supported(wanted, mpv_path)
        if dropped:
            self.log_message.emit(f"[mistery] mpv does not support: {' '.join(dropped)}")
        command = [mpv_path, *accepted]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            raise MpvUnavailable(f"could not start mpv: {exc}") from exc
        _die_with_us(self._process)

        self._stderr_lines.clear()
        threading.Thread(target=self._drain_stderr, name="mpv-stderr", daemon=True).start()

        if not self._connect(timeout=15.0):
            time.sleep(0.2)          # give the drain thread a moment to catch up
            detail = " ".join(self._stderr_lines).strip()
            self.terminate()
            raise MpvUnavailable(
                "mpv started but never opened its IPC pipe. " + detail[:400]
            )

        self._running.set()
        self._pump = threading.Thread(target=self._pump_loop, name="mpv-ipc", daemon=True)
        self._pump.start()

        for name in self.observed_properties():
            self.observe_property(name)
        self.command("request_log_messages", "warn")

    def observed_properties(self) -> list[str]:
        """What to watch for the whole session. Subclasses swap in their own."""
        return OBSERVED_PROPERTIES

    def _base_arguments(self, window_id: int | None) -> list[str]:
        tone_map = settings.get("hdr_tone_mapping", True)
        args = ([f"--wid={window_id}"] if window_id else []) + [
            f"--input-ipc-server={self._ipc_path}",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",          # hold the last frame so we can show our own end screen
            "--no-config",              # our options must win over any stray mpv.conf
            "--load-scripts=no",
            "--really-quiet",           # keep stderr to genuine problems
            "--msg-level=all=warn",
            "--osc=no",                 # we draw the controls ourselves
            "--osd-bar=no",
            "--no-input-default-bindings",
            "--input-vo-keyboard=no",
            "--no-window-dragging",
            "--cursor-autohide=no",     # the overlay owns cursor visibility
            "--input-cursor=no",        # ...and every pointer event, so mpv
                                        # must not react to the mouse at all
            "--ytdl=no",
            "--save-position-on-quit=no",
            "--vo=gpu-next",
            f"--hwdec={settings.get('hwdec', 'auto-safe')}",
            # Subtitles and extra audio sitting next to the file get picked up.
            "--sub-auto=fuzzy",
            "--audio-file-auto=fuzzy",
            f"--alang={settings.get('preferred_audio_lang', 'eng')}",
            f"--slang={settings.get('preferred_sub_lang', 'eng')}",
            f"--sub-visibility={'yes' if settings.get('subs_on_by_default') else 'no'}",
            f"--volume={int(settings.get('volume', 80))}",
            f"--volume-max={self.volume_max}",
            "--audio-client-name=Mistery",
            "--title=Mistery",
            "--sub-font-size=42",
            "--sub-border-size=2.4",
            "--sub-shadow-offset=1",
        ]
        quality = quality_preset(settings.get("video_quality", "balanced"))
        args += [f"--{key}={value}"
                 for key, value in QUALITY_PRESETS[quality].items()]

        if tone_map:
            # Map HDR10 down to SDR without the washed-out grey look. Contrast
            # recovery puts back the local detail that tone mapping flattens.
            args += [
                "--target-colorspace-hint=no",
                "--tone-mapping=bt.2390",
                f"--hdr-compute-peak={_HDR_PEAK[quality]}",
                "--gamut-mapping-mode=perceptual",
                f"--hdr-contrast-recovery={_HDR_CONTRAST[quality]}",
            ]
        else:
            # The display handles HDR itself; hand the signal through untouched.
            args += ["--target-colorspace-hint=yes"]
        return args

    def _connect(self, timeout: float) -> bool:
        """Wait for mpv to create the pipe, then open it for duplex traffic."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process and self._process.poll() is not None:
                return False
            try:
                handle = open(self._ipc_path, "r+b", buffering=0)
            except OSError:
                time.sleep(0.05)
                continue
            self._pipe_file = handle
            self._pipe = handle.fileno()
            return True
        return False

    def terminate(self, timeout: float = 3.0) -> None:
        """Ask mpv to quit, then make sure it actually did."""
        process = self._process

        if process and process.poll() is None:
            # Queue `quit` while the pump is still alive so it actually gets
            # written, and only then tear the transport down.
            try:
                self._write_raw({"command": ["quit"]})
            except Exception:
                pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass

        self._running.clear()
        self._process = None

        if self._pipe_file is not None:
            try:
                self._pipe_file.close()
            except Exception:
                pass
        self._pipe_file = None
        self._pipe = None

        pump, self._pump = self._pump, None
        if pump and pump.is_alive() and pump is not threading.current_thread():
            pump.join(timeout=1.5)

        # Nothing will ever answer outstanding requests now.
        with self._request_lock:
            for event, slot in self._pending.values():
                slot.append({"error": "mpv exited"})
                event.set()
            self._pending.clear()

    # --- IPC plumbing -------------------------------------------------------

    def _write_raw(self, payload: dict) -> None:
        if self._pipe is None:
            raise OSError("mpv IPC pipe is not open")
        # Raw UTF-8 rather than \u escapes: a path in Cyrillic or Japanese is
        # half the bytes, which matters against a 4 KB window.
        self._outbox.put(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")

    def _next_request_id(self) -> int:
        with self._request_lock:
            self._request_id += 1
            return self._request_id

    def command(self, *args: Any) -> None:
        """Fire a command without waiting for the reply."""
        if not self.is_running:
            return
        try:
            self._write_raw({"command": list(args), "request_id": 0})
        except OSError:
            pass

    def command_sync(self, *args: Any, timeout: float = 5.0) -> dict:
        """Send a command and wait for mpv's reply."""
        if not self.is_running:
            return {"error": "mpv is not running"}
        request_id = self._next_request_id()
        event = threading.Event()
        slot: list = []
        with self._request_lock:
            self._pending[request_id] = (event, slot)
        try:
            self._write_raw({"command": list(args), "request_id": request_id})
        except OSError as exc:
            with self._request_lock:
                self._pending.pop(request_id, None)
            return {"error": str(exc)}
        if not event.wait(timeout):
            with self._request_lock:
                self._pending.pop(request_id, None)
            return {"error": "timed out"}
        return slot[0] if slot else {"error": "no reply"}

    def set_property(self, name: str, value: Any) -> None:
        self.command("set_property", name, value)

    def get_property(self, name: str, default: Any = None) -> Any:
        """Cached value from the observer, falling back to a round trip."""
        if name in self._properties:
            return self._properties[name]
        reply = self.command_sync("get_property", name)
        if reply.get("error") == "success":
            return reply.get("data", default)
        return default

    def cached(self, name: str, default: Any = None) -> Any:
        return self._properties.get(name, default)

    def observe_property(self, name: str) -> None:
        observe_id = self._next_request_id()
        self._observe_ids[observe_id] = name
        self.command("observe_property", observe_id, name)

    def unobserve_property(self, name: str) -> None:
        """Stop mpv sending changes for `name` (its last value stays cached)."""
        for observe_id, observed in list(self._observe_ids.items()):
            if observed == name:
                self.command("unobserve_property", observe_id)
                del self._observe_ids[observe_id]

    def is_observing(self, name: str) -> bool:
        return name in self._observe_ids.values()

    def _pump_loop(self) -> None:
        """Single-threaded reader/writer; see the module docstring for why.

        Waiting is done on the outbox rather than with a sleep, so a command
        still goes out the instant it is queued — only *reading* waits, and that
        wait stretches while mpv has nothing to say. A fixed 4 ms sleep meant up
        to 250 wake-ups a second from an idle music player sitting in the tray.

        Commands go out in a byte window: the sizes of commands written but not
        yet answered are kept in order (mpv answers in order), and the next one
        is written only if it still fits under _WINDOW_BYTES — or if nothing is
        outstanding at all, so even an oversized command is never stuck.
        """
        buffer = b""
        fd = self._pipe
        idle_rounds = 0
        waiting: bytes | None = None
        outstanding: deque[int] = deque()      # sizes of unanswered commands
        outstanding_bytes = 0
        last_heard = time.monotonic()
        reported_slow = False

        def answered(count: int) -> None:
            nonlocal outstanding_bytes
            for _ in range(min(count, len(outstanding))):
                outstanding_bytes -= outstanding.popleft()

        try:
            while self._running.is_set() and fd is not None:
                did_work = False

                while True:
                    if waiting is None:
                        try:
                            waiting = self._outbox.get_nowait()
                        except queue.Empty:
                            break
                    if outstanding and outstanding_bytes + len(waiting) > _WINDOW_BYTES:
                        break                       # wait for replies to make room
                    payload, waiting = waiting, None
                    if not self._write_all(fd, payload):
                        self._running.clear()
                        break
                    if not outstanding:
                        last_heard = time.monotonic()
                    outstanding.append(len(payload))
                    outstanding_bytes += len(payload)
                    did_work = True
                    buffer, replies, messages, alive = self._read_available(fd, buffer)
                    if not alive:
                        self._running.clear()
                        break
                    if replies:
                        answered(replies)
                        last_heard = time.monotonic()
                if not self._running.is_set():
                    break

                buffer, replies, messages, alive = self._read_available(fd, buffer)
                if not alive:
                    break
                if messages:
                    # Events count as work too: a seek bar or subtitle switch
                    # should not wait out a stretched poll interval.
                    did_work = True
                if replies:
                    answered(replies)
                    last_heard = time.monotonic()
                    reported_slow = False
                stalled = time.monotonic() - last_heard if outstanding else 0.0
                if stalled > _SLOW_REPLY and not reported_slow:
                    _log_pipe.info("mpv is taking %.0f s to answer %d command(s)",
                                   stalled, len(outstanding))
                    reported_slow = True

                if self._process and self._process.poll() is not None:
                    break
                if did_work:
                    idle_rounds = 0
                elif outstanding and (waiting is not None or not self._outbox.empty()):
                    # Something is waiting for room. Replies normally take
                    # microseconds, so poll fast — but back off if mpv is
                    # genuinely stuck, instead of spinning at a kilohertz.
                    time.sleep(0.0005 if stalled < 0.01 else min(self.max_idle_wait, stalled / 4))
                else:
                    idle_rounds += 1
                    wait = min(self.max_idle_wait, 0.002 * idle_rounds)
                    if waiting is None:
                        try:
                            waiting = self._outbox.get(timeout=wait)
                        except queue.Empty:
                            pass
                    else:
                        time.sleep(wait)
        finally:
            self._running.clear()
            # mpv is gone or the pipe broke: nobody will ever answer a caller
            # blocked in command_sync, so tell them now rather than at timeout.
            with self._request_lock:
                for event, slot in self._pending.values():
                    slot.append({"error": "mpv exited"})
                    event.set()
                self._pending.clear()
        code = self._process.poll() if self._process else 0
        self.exited.emit(code if code is not None else 0)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> bool:
        """Write every byte, or report that the pipe is gone.

        os.write may return having written only part of a buffer, and half a JSON
        line would corrupt every message that follows it.
        """
        view = memoryview(payload)
        try:
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    return False
                view = view[written:]
        except OSError:
            return False
        return True

    def _read_available(self, fd: int, buffer: bytes) -> tuple[bytes, int, int, bool]:
        """Read and dispatch whatever mpv has already sent, without blocking.

        Returns (the leftover partial line, replies seen, messages seen, whether
        the pipe is still alive).
        """
        replies = messages = 0
        while True:
            available = _bytes_available(fd)
            if available < 0:
                return buffer, replies, messages, False
            if not available:
                return buffer, replies, messages, True
            try:
                chunk = os.read(fd, min(available, 1 << 16))
            except OSError:
                return buffer, replies, messages, False
            if not chunk:
                return buffer, replies, messages, False
            buffer += chunk
            while _NEWLINE in buffer:
                line, _, buffer = buffer.partition(_NEWLINE)
                line = line.strip()
                if line:
                    messages += 1
                    if self._dispatch(line):
                        replies += 1

    def _dispatch(self, line: bytes) -> bool:
        """Route one message from mpv. True when it was the reply to a command."""
        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return False
        if not isinstance(message, dict):
            return False

        # Every reply carries "error" (mpv answers "success" when all is well);
        # events never do. request_id is echoed back when one was sent.
        if "event" not in message and ("request_id" in message or "error" in message):
            request_id = message.get("request_id")
            with self._request_lock:
                waiting = self._pending.pop(request_id, None)
            if waiting:
                event, slot = waiting
                slot.append(message)
                event.set()
            return True

        event_name = message.get("event")
        if event_name == "property-change":
            name = message.get("name") or self._observe_ids.get(message.get("id"), "")
            if name:
                value = message.get("data")
                self._properties[name] = value
                self.property_changed.emit(name, value)
        elif event_name == "file-loaded":
            self.file_loaded.emit()
        elif event_name == "end-file":
            self.end_file.emit(str(message.get("reason", "unknown")))
        elif event_name == "playback-restart":
            self.playback_restart.emit()
        elif event_name == "log-message":
            text = f"[{message.get('prefix')}] {(message.get('text') or '').strip()}"
            self.log_message.emit(text)
        elif event_name == "shutdown":
            self._running.clear()
        return False

    # --- playback helpers ---------------------------------------------------

    def load(self, path: str | Path, start_at: float = 0.0,
             options: dict[str, str] | None = None) -> None:
        """Open a file, replacing whatever is playing.

        `options` are mpv options for this file only: mpv applies them while it
        opens the file (so an audio track is chosen before any sound, not
        switched a moment later) and puts the previous values back when the
        file ends.
        """
        pairs = dict(options or {})
        if start_at and start_at > 1:
            pairs["start"] = f"+{start_at:.3f}"
        if pairs:
            # %n% gives each value by its byte length, so nothing inside one (a
            # comma, say) can be read as the start of the next option.
            text = ",".join(f"{key}=%{len(str(value).encode('utf-8'))}%{value}"
                            for key, value in pairs.items())
            self.command("loadfile", str(path), "replace", 0, text)
        else:
            self.command("loadfile", str(path), "replace")
        # --keep-open holds the last frame by setting pause=yes, and `pause` is
        # global: loading the next file does not clear it. Without this the next
        # episode arrives already paused and just sits there.
        self.set_property("pause", False)

    def play(self) -> None:
        self.set_property("pause", False)

    def pause(self) -> None:
        self.set_property("pause", True)

    def toggle_pause(self) -> None:
        self.command("cycle", "pause")

    def stop(self) -> None:
        self.command("stop")

    def seek(self, seconds: float, mode: str = "relative") -> None:
        self.command("seek", seconds, mode)

    def seek_absolute(self, position: float) -> None:
        self.command("seek", position, "absolute", "exact")

    def set_volume(self, value: float) -> float:
        """mpv's volume, clamped to this process's ceiling. Returns the number
        actually sent: a caller that remembers what it sent (the music fade,
        player._apply_volume) needs the clamped one, not the one it asked for.
        Takes a float because that fade is a fraction of the level."""
        value = max(0.0, min(float(self.volume_max), float(value)))
        self.set_property("volume", value)
        return value

    def set_speed(self, value: float) -> None:
        self.set_property("speed", max(0.25, min(4.0, float(value))))

    def set_audio_track(self, track_id: int | str) -> None:
        self.set_property("aid", track_id)

    def set_subtitle_track(self, track_id: int | str) -> None:
        if track_id in ("no", None, -1):
            self.set_property("sid", "no")
        else:
            self.set_property("sid", track_id)
            self.set_property("sub-visibility", True)

    def add_subtitle_file(self, path: str) -> None:
        self.command("sub-add", path, "select")

    def set_audio_filters(self, chain: str) -> None:
        self.set_property("af", chain or "")

    def set_quality(self, name: str) -> str:
        """Switch scaling quality on the running player. Returns the preset used.

        Every option is checked against this mpv build first — they get renamed
        and dropped between releases, and an unknown property here would just
        fail silently in the IPC reply.
        """
        preset = quality_preset(name)
        known = supported_options(find_mpv() or "")
        for key, value in QUALITY_PRESETS[preset].items():
            if not known or key in known:
                self.set_property(key, value)
        if settings.get("hdr_tone_mapping", True):
            for key, table in (("hdr-compute-peak", _HDR_PEAK),
                               ("hdr-contrast-recovery", _HDR_CONTRAST)):
                if not known or key in known:
                    self.set_property(key, table[preset])
        return preset

    def video_size(self) -> str:
        """Something like '3840x2160' for the file on screen, or ''."""
        width = self.cached("video-params/w")
        height = self.cached("video-params/h")
        if not width or not height:
            return ""
        return f"{int(width)}x{int(height)}"

    def next_chapter(self) -> None:
        self.command("add", "chapter", 1)

    def previous_chapter(self) -> None:
        self.command("add", "chapter", -1)

    def screenshot(self, path: str) -> None:
        self.command("screenshot-to-file", path, "video")

    def tracks(self, kind: str) -> list[dict]:
        track_list = self._properties.get("track-list") or []
        if not isinstance(track_list, list):
            return []
        return [t for t in track_list if isinstance(t, dict) and t.get("type") == kind]

    def chapters(self) -> list[dict]:
        chapter_list = self._properties.get("chapter-list") or []
        return chapter_list if isinstance(chapter_list, list) else []
