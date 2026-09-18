"""Build MisterySetup.exe — one file, no Python needed on the other machine.

    python packaging/build_setup.py

Two steps. PyInstaller freezes packaging/setup_entry.py into a onefile,
windowed exe that carries tkinter and nothing else — no PySide6, no numpy, no
Pillow, none of the app's dependencies, because the installer imports none of
them. Then the frozen Mistery build folder is zipped and glued onto the end of
that exe, with a 56-byte trailer saying where it starts (payload.py explains the
format and why it is not --add-data).

Onefile here, onedir for the app, and the reason is the opposite of the usual
one: a onefile exe unpacks its payload into %TEMP% on every start, which is why
the app must not be one — but the installer runs once, and being a single file
you can put on a USB stick is the entire job.

Needs only PyInstaller. It does not import the app, so it does not need Qt.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
NAME = "MisterySetup"

# Where the freeze step leaves the app. build_app.py writes the second one and
# build_updater.py puts MisteryUpdate.exe beside it in the same folder, so one
# --app covers both; the first is here because that is where the plan said the
# folder would be, and checking two paths is cheaper than the afternoon spent
# finding out which one a future build script picked.
PAYLOAD_CANDIDATES = (
    PACKAGING / "out" / "app",
    PACKAGING / "out" / "dist" / "Mistery",
)

# The installer needs tkinter and the standard library. Everything else that
# happens to be importable in the build environment is Qt and friends riding
# along for nothing, so the app's dependencies are named here and left out.
#
# Only third-party packages. An earlier version of this list also excluded
# `email`, on the grounds that an installer sends no mail — and urllib.request
# imports it through http.client, so the built exe died on its first import with
# a traceback that a --windowed build turns into a message box and a hung
# process. The standard library stays whole.
EXCLUDED = ("PySide6", "shiboken6", "PIL", "numpy", "requests", "mutagen",
            "cryptography", "certifi", "urllib3", "charset_normalizer",
            "test", "pydoc_data")


def installer_path(out_root: Path = PACKAGING / "out") -> Path:
    """Where build() leaves MisterySetup.exe: <out>\\setup\\MisterySetup.exe.

    The `setup\\` folder is PyInstaller's --distpath, kept apart from the spec
    file and the work folder that land in <out> beside it.

    It is a function rather than a line inside build() because the release
    workflow names this path in six places, and it named the wrong one:
    packaging/out/MisterySetup.exe, no setup\\ segment, which nothing noticed
    until the size check twenty minutes into a build. packaging/test_release.py
    compares the workflow against what this returns, in about a second.
    """
    return out_root / "setup" / f"{NAME}.exe"


def find_payload(explicit: Path | None) -> Path:
    if explicit is not None:
        folder = explicit.resolve()
        if not folder.is_dir():
            raise SystemExit(f"--app {folder} is not a folder")
        return folder
    for candidate in PAYLOAD_CANDIDATES:
        if (candidate / "Mistery.exe").is_file():
            return candidate
    raise SystemExit(
        "No frozen Mistery to put inside the installer. Run\n"
        "    python packaging/build_app.py\n"
        "first, or point this at a build folder with --app.")


def folder_size(folder: Path) -> tuple[int, int]:
    total = files = 0
    for path in folder.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            files += 1
    return total, files


def check_it_compiles() -> None:
    """Refuse to build if any installer module has a syntax error.

    PyInstaller does not fail on one: it records "cannot parse" in its warn file,
    leaves the module out, and hands back an exe that dies on its first import.
    Built --windowed, that death is a message box nobody clicks and a process
    that never exits — twenty minutes were spent on exactly that, so the check
    is here instead.
    """
    import compileall

    if not compileall.compile_dir(str(PACKAGING / "installer"), quiet=1,
                                  force=True, legacy=False):
        raise SystemExit("installer\\ does not compile — fix that first")
    if not compileall.compile_file(str(PACKAGING / "setup_entry.py"), quiet=1,
                                   force=True, legacy=False):
        raise SystemExit("setup_entry.py does not compile")


def build(out_root: Path, payload_dir: Path, keep_work: bool = False) -> Path:
    check_it_compiles()
    sys.path.insert(0, str(PACKAGING))
    from installer import payload as payload_module   # noqa: PLC0415

    exe = installer_path(out_root)
    dist = exe.parent
    work = out_root / "setup-build"
    out_root.mkdir(parents=True, exist_ok=True)
    icon = ROOT / "assets" / "icon.ico"

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--windowed",               # a window, not a console flashing behind one
        "--name", NAME,
        "--icon", str(icon),
        # The window's own title-bar icon. 270 KB, unpacked to %TEMP% with the
        # rest of the bootloader's payload, which is why this is the one thing
        # --add-data is used for and the 174 MB app is not.
        "--add-data", f"{icon}{os.pathsep}.",
        "--paths", str(PACKAGING),
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(out_root),
        "--log-level", "WARN",
    ]
    for name in EXCLUDED:
        command += ["--exclude-module", name]
    command.append(str(PACKAGING / "setup_entry.py"))

    print(f"freezing the installer with {sys.executable}")
    started = time.monotonic()
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller failed ({result.returncode})")
    froze_in = time.monotonic() - started

    if not exe.is_file():
        raise SystemExit(f"build finished but {exe} is not there")
    bare = exe.stat().st_size

    if not (payload_dir / "MisteryUpdate.exe").is_file():
        # Not fatal — the installer notices and skips the task — but an installer
        # shipped like this makes copies of Mistery that can never update
        # themselves, and nobody finds out until the release after this one.
        print(f"\n  WARNING: there is no MisteryUpdate.exe in {payload_dir}.\n"
              "  Anything installed from this MisterySetup.exe will not update\n"
              "  itself. Run packaging/build_updater.py and build again.\n")

    payload_bytes, files = folder_size(payload_dir)
    print(f"appending {payload_dir} ({payload_bytes / 1e6:.0f} MB, {files} files)")
    started = time.monotonic()
    zipped, raw = payload_module.append_to(exe, payload_dir)
    zipped_in = time.monotonic() - started

    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)

    final = exe.stat().st_size
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    version = (payload_dir / "version.txt").read_text(encoding="utf-8").strip() \
        if (payload_dir / "version.txt").is_file() else "?"

    print(f"\n{exe}")
    print(f"  installs         Mistery {version}")
    print(f"  installer alone  {bare / 1e6:.1f} MB   (frozen in {froze_in:.0f} s)")
    print(f"  payload          {zipped / 1e6:.1f} MB zipped from "
          f"{raw / 1e6:.0f} MB   ({zipped_in:.0f} s, "
          f"{100 - 100 * zipped / raw:.0f}% smaller)")
    print(f"  MisterySetup.exe {final / 1e6:.1f} MB")
    print(f"  sha256           {digest}")
    return exe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", type=Path, default=None,
                        help="the frozen Mistery folder to put inside "
                             "(default packaging/out/app or "
                             "packaging/out/dist/Mistery)")
    parser.add_argument("--out", type=Path, default=PACKAGING / "out",
                        help="where to build (default packaging/out)")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args(argv)
    build(args.out.resolve(), find_payload(args.app), keep_work=args.keep_work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
