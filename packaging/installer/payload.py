"""The frozen Mistery, carried inside MisterySetup.exe and unpacked out of it.

MisterySetup.exe is a PyInstaller onefile exe with a zip glued onto the end and
a 56-byte trailer saying where that zip starts. PyInstaller's own --add-data
would have worked, but its bootloader unpacks everything it carries into %TEMP%
before the installer's first line runs: the app bundle is 174 MB, so that would
be 174 MB written to the temp folder and then written again to the install
folder, on a machine that has not agreed to spend 350 MB yet. Reading the zip
straight out of the exe writes each file exactly once.

Appending after the archive is safe: PyInstaller's bootloader scans backwards
for its own cookie rather than assuming it is the last thing in the file, which
is what makes code-signing a onefile exe possible. Checked here first with a
50 MB dummy payload before anything was built on top of it.

The trailer, at the very end of the exe, little-endian:

    8s   b"MISTPAY1"     so a setup exe with no payload can say so plainly
    Q    offset          where the zip starts
    Q    length          how long it is
    32s  sha256          of those bytes
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import sys
import threading
import zipfile
from pathlib import Path
from typing import Callable

from .setup_common import Cancelled, InstallError, is_frozen

MAGIC = b"MISTPAY1"
TRAILER = struct.Struct("<8sQQ32s")
TRAILER_SIZE = TRAILER.size                      # 56

Progress = Callable[[str, int, int], None]


class _Slice(io.RawIOBase):
    """A read-only window onto part of a file, so zipfile sees only the zip.

    zipfile finds the end-of-central-directory by searching backwards from what
    it thinks is the end of the file. Handing it the whole exe would have it
    searching past the trailer and guessing; handing it a window that ends
    exactly where the zip ends means it never has to guess.
    """

    def __init__(self, handle, start: int, length: int) -> None:
        self._handle = handle
        self._start = start
        self._length = length
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._position + offset
        else:
            target = self._length + offset
        self._position = max(0, min(self._length, target))
        return self._position

    def readinto(self, buffer) -> int:
        left = self._length - self._position
        if left <= 0:
            return 0
        want = min(len(buffer), left)
        self._handle.seek(self._start + self._position)
        chunk = self._handle.read(want)
        buffer[:len(chunk)] = chunk
        self._position += len(chunk)
        return len(chunk)


def setup_exe() -> Path:
    """MisterySetup.exe itself.

    sys.executable in a PyInstaller onefile build is the exe the person ran, not
    the unpacked copy in %TEMP%, which is the whole reason this works.
    """
    return Path(sys.executable).resolve()


def _read_trailer(path: Path) -> tuple[int, int, bytes] | None:
    try:
        with open(path, "rb") as handle:
            handle.seek(-TRAILER_SIZE, os.SEEK_END)
            magic, offset, length, digest = TRAILER.unpack(handle.read(TRAILER_SIZE))
    except (OSError, struct.error):
        return None
    if magic != MAGIC:
        return None
    return offset, length, digest


def loose_payload_dir() -> Path | None:
    """The build folder to install from when running the installer unfrozen.

    `python -m packaging.installer.setup_main --payload packaging/out/dist/Mistery`
    is how the installer gets tested without rebuilding the exe every time; the
    environment variable is the same thing for the test harness.

    None inside MisterySetup.exe, always. A shipped installer that reads a
    folder name out of the environment and installs whatever is in it never
    reaches the SHA-256 check on its own payload, which is the only thing it
    knows about its own contents. Measured before this guard: with
    MISTERY_SETUP_PAYLOAD pointed at a folder holding a 41-byte text file named
    Mistery.exe, the real 82 MB MisterySetup.exe said "installed Mistery 9.9.9"
    in 2.0 s, wrote the Add/Remove Programs entry, and would have pointed the
    hourly update task at that folder. Nobody crosses a security boundary doing
    that — setting your own environment and running your own code are the same
    privilege — but it is a test seam in a file other people download, and it
    turns "run the installer" into "install whatever this variable names". The
    seam belongs in the source tree, where the tests live.
    """
    if is_frozen():
        return None
    value = os.environ.get("MISTERY_SETUP_PAYLOAD", "").strip()
    return Path(value) if value else None


def write_uninstaller(destination: Path) -> int:
    """Put a copy of this exe in the install folder, without the payload.

    Uninstall.exe is MisterySetup.exe under another name — it already knows
    every folder, key and task name involved — but it has no use for the 78 MB
    of zipped Mistery glued to the end, and leaving that behind would make the
    install 78 MB bigger than the thing it installed. Cutting the file off at
    the offset in the trailer leaves exactly the PyInstaller onefile exe that
    was there before the payload was appended: 11 MB, and it runs.

    Returns the bytes written.
    """
    exe = setup_exe()
    trailer = _read_trailer(exe)
    cut = trailer[0] if trailer is not None else exe.stat().st_size
    with open(exe, "rb") as source, open(destination, "wb") as out:
        left = cut
        while left:
            chunk = source.read(min(8 << 20, left))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
    return cut - left


def version() -> str | None:
    """The version the payload will install, read out of its version.txt."""
    folder = loose_payload_dir()
    if folder is not None:
        try:
            return (folder / "version.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return None
    trailer = _read_trailer(setup_exe())
    if trailer is None:
        return None
    offset, length, _digest = trailer
    try:
        with open(setup_exe(), "rb") as handle:
            with zipfile.ZipFile(io.BufferedReader(_Slice(handle, offset, length))) as bundle:
                return bundle.read("version.txt").decode("utf-8").strip()
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def unpacked_size() -> int:
    """How much the app will take on disk once written out."""
    folder = loose_payload_dir()
    if folder is not None:
        return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    trailer = _read_trailer(setup_exe())
    if trailer is None:
        return 0
    offset, length, _digest = trailer
    try:
        with open(setup_exe(), "rb") as handle:
            with zipfile.ZipFile(io.BufferedReader(_Slice(handle, offset, length))) as bundle:
                return sum(info.file_size for info in bundle.infolist())
    except (OSError, ValueError, zipfile.BadZipFile):
        return 0


def _safe_relative(name: str) -> Path:
    """The path a zip entry is allowed to become, or an error.

    This zip is built by packaging/build_setup.py from our own build folder, so
    in practice nothing here ever fires. It is checked anyway because the cost
    is four comparisons and the thing being prevented is an installer writing
    into Startup.
    """
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/") or ":" in cleaned.split("/")[0]:
        raise InstallError(f"the payload contains an absolute path: {name}")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise InstallError(f"the payload contains a path that escapes: {name}")
    if not parts:
        raise InstallError(f"the payload contains an empty name: {name!r}")
    return Path(*parts)


def extract(destination: Path, report: Progress,
            cancel: threading.Event | None = None) -> int:
    """Write the app into `destination`. Returns the bytes written.

    Copies from the loose build folder when running from source, so the
    installer can be tested without being frozen first. Inside
    MisterySetup.exe there is no such folder — see loose_payload_dir — so the
    only way through here is the zip at the end of the exe, and the only way
    past the hash check below is to match it.
    """
    folder = loose_payload_dir()
    if folder is not None:
        return _copy_tree(folder, destination, report, cancel)

    exe = setup_exe()
    trailer = _read_trailer(exe)
    if trailer is None:
        raise InstallError(
            "This MisterySetup.exe has no Mistery inside it. It was built "
            "without a payload — rebuild it with packaging/build_setup.py.")
    offset, length, expected = trailer

    with open(exe, "rb") as handle:
        digest = hashlib.sha256()
        handle.seek(offset)
        left = length
        while left:
            chunk = handle.read(min(8 << 20, left))
            if not chunk:
                break
            left -= len(chunk)
            digest.update(chunk)
            report("Checking the installer", length - left, length)
        if left or digest.digest() != expected:
            raise InstallError(
                "MisterySetup.exe is damaged — the copy of Mistery inside it "
                "does not match its own checksum. Download the installer again.")

        written = 0
        with zipfile.ZipFile(io.BufferedReader(_Slice(handle, offset, length))) as bundle:
            entries = [info for info in bundle.infolist() if not info.is_dir()]
            total = sum(info.file_size for info in entries) or 1
            for info in entries:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                target = destination / _safe_relative(info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, open(target, "wb") as out:
                    while True:
                        chunk = source.read(4 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        written += len(chunk)
                        report("Copying Mistery", written, total)
    return written


def _copy_tree(source: Path, destination: Path, report: Progress,
               cancel: threading.Event | None) -> int:
    files = [p for p in sorted(source.rglob("*")) if p.is_file()]
    total = sum(p.stat().st_size for p in files) or 1
    written = 0
    for path in files:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "rb") as handle, open(target, "wb") as out:
            while True:
                chunk = handle.read(4 << 20)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                report("Copying Mistery", written, total)
    return written


def append_to(exe: Path, folder: Path) -> tuple[int, int]:
    """Glue `folder` onto the end of `exe` as the payload. Build-time only.

    Returns (zip bytes, uncompressed bytes). Deflated at the default level:
    the payload is mostly Qt DLLs, which are already about as compressible as
    they are going to get, and build_setup.py prints both numbers so the trade
    can be looked at rather than argued about.
    """
    files = [p for p in sorted(folder.rglob("*")) if p.is_file()]
    if not files:
        raise InstallError(f"nothing to append: {folder} is empty")

    buffer = io.BytesIO()
    raw = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, str(path.relative_to(folder)).replace("\\", "/"))
            raw += path.stat().st_size
    blob = buffer.getvalue()

    with open(exe, "ab") as handle:
        offset = handle.tell()
        handle.write(blob)
        handle.write(TRAILER.pack(MAGIC, offset, len(blob),
                                  hashlib.sha256(blob).digest()))
    return len(blob), raw
