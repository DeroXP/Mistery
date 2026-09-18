"""Check the paths that change when Mistery is frozen.

    python packaging/test_frozen_paths.py

Four things the source tree cannot show on its own:

  - a PC with no mpv on PATH has to find the one the installer fetched into
    runtime\\ beside the exe, and a PC that has its own mpv has to keep it;
  - assets\\ moves inside the PyInstaller payload when frozen;
  - the library repair is imported as tools.repair_db now, not loaded from a
    file path that only exists in a source checkout;
  - main() swaps in a staged MisteryUpdate.exe before it does anything else.
    Nothing in a source run exercises that — the file it looks for is only ever
    there on an installed copy the hour after an update — so it is checked by
    reading main.py rather than by running it.

Nothing here needs a frozen build: sys.frozen and sys.executable are what the
app reads, so setting them is a faithful stand-in. The real exe is checked by
packaging/smoke_frozen.py.

It writes nothing outside a temporary folder of its own — MISTERY_DATA_DIR is
set before app.config is imported, because importing it creates a settings file.
"""

from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA = tempfile.mkdtemp(prefix="mistery-frozen-paths-")
os.environ["MISTERY_DATA_DIR"] = _DATA           # before the import below

from app import config                                            # noqa: E402

failures: list[str] = []


def check(name: str, got, expected) -> None:
    if got == expected:
        print(f"  ok    {name}: {got}")
    else:
        failures.append(f"{name}: got {got!r}, expected {expected!r}")
        print(f"  FAIL  {name}: got {got!r}, expected {expected!r}")


def stub(path: Path) -> Path:
    """An empty file where an executable would be. shutil.which only asks
    whether the file is there and readable, which is all these tests need."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def as_frozen(install: Path):
    """Pretend we are Mistery.exe in `install`, the way PyInstaller sets it up."""
    class _Frozen:
        def __enter__(self):
            sys.frozen = True                                     # type: ignore[attr-defined]
            self._executable = sys.executable
            sys.executable = str(install / "Mistery.exe")
            sys._MEIPASS = str(install / "_internal")             # type: ignore[attr-defined]

        def __exit__(self, *_):
            del sys.frozen                                        # type: ignore[attr-defined]
            del sys._MEIPASS                                      # type: ignore[attr-defined]
            sys.executable = self._executable
    return _Frozen()


def check_updater_swap() -> None:
    """main.py must define _swap_in_new_updater and call it first in main().

    This is the only way MisteryUpdate.exe ever gets replaced: it cannot
    overwrite itself while it is the running process, so an update leaves the
    new one as MisteryUpdate.exe.new and Mistery finishes the job at its next
    start. Drop the call and the updater is frozen at the version the installer
    put down, for the life of that install — the one component nobody can ever
    fix remotely. It went missing once already, which is why this is a test.

    main.py is read rather than imported: importing it pulls in PySide6 and
    app.config, and app.config writes a settings.json on import.
    """
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body
                 if isinstance(node, ast.FunctionDef)}
    check("main.py defines _swap_in_new_updater",
          "_swap_in_new_updater" in functions, True)
    called_first = False
    if "main" in functions:
        body = [statement for statement in functions["main"].body
                if not (isinstance(statement, ast.Expr)
                        and isinstance(statement.value, ast.Constant))]
        first = body[0] if body else None
        called_first = (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Call)
                        and getattr(first.value.func, "id", "") == "_swap_in_new_updater")
    # Before the single-instance check, not merely somewhere in main(): a second
    # Mistery started while the first is open exits a few lines further down.
    check("main() calls it before anything else", called_first, True)
    if "_swap_in_new_updater" not in functions:
        return

    # And then run the shipping function for real, on files in a folder of our
    # own. Windows renames the running exe out of the way rather than deleting
    # it, so all three files are checked.
    namespace: dict = {"sys": sys, "Path": Path}
    exec(compile(ast.Module(body=[functions["_swap_in_new_updater"]], type_ignores=[]),
                 "main.py", "exec"), namespace)
    folder = Path(tempfile.mkdtemp(prefix="mistery-swap-"))
    try:
        (folder / "MisteryUpdate.exe").write_bytes(b"the updater that ran")
        (folder / "MisteryUpdate.exe.old").write_bytes(b"one from an update ago")
        namespace["_swap_in_new_updater"](folder)
        check("nothing staged, nothing touched",
              (folder / "MisteryUpdate.exe").read_bytes(), b"the updater that ran")

        (folder / "MisteryUpdate.exe.new").write_bytes(b"the new updater")
        namespace["_swap_in_new_updater"](folder)
        check("the staged updater is now MisteryUpdate.exe",
              (folder / "MisteryUpdate.exe").read_bytes(), b"the new updater")
        check("MisteryUpdate.exe.new is gone",
              (folder / "MisteryUpdate.exe.new").exists(), False)
        check("the one it replaced is kept as .old",
              (folder / "MisteryUpdate.exe.old").read_bytes(), b"the updater that ran")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="mistery-frozen-paths-"))
    install = sandbox / "Programs" / "Mistery"
    own_mpv = sandbox / "elsewhere"
    path_before = os.environ.get("PATH", "")
    try:
        print("from source:")
        check("install_dir", config.install_dir(), ROOT)
        check("assets_dir", config.assets_dir(), ROOT / "assets")
        check("icon.ico is there", config.icon_path() is not None, True)

        print("\nfrozen, pretending Mistery.exe lives in", install)
        with as_frozen(install):
            check("install_dir", config.install_dir(), install)
            check("runtime_dir", config.runtime_dir(), install / "runtime")
            check("assets_dir", config.assets_dir(), install / "_internal" / "assets")

            # The machine the installer just ran on: nothing on PATH at all, and
            # nothing fetched yet. (find_mpv also guesses at Program Files, and
            # this machine happens to have one there, so ffmpeg is the honest
            # test of "found nothing" — it has no guesses.)
            os.environ["PATH"] = ""
            check("no ffmpeg anywhere", config.find_ffmpeg(), None)
            for tool in ("mpv", "ffmpeg", "ffprobe"):
                stub(install / "runtime" / f"{tool}.exe")
            check("bundled mpv", config.find_mpv(),
                  str(install / "runtime" / "mpv.exe"))
            check("bundled ffmpeg", config.find_ffmpeg(),
                  str(install / "runtime" / "ffmpeg.exe"))
            check("bundled ffprobe", config.find_ffprobe(),
                  str(install / "runtime" / "ffprobe.exe"))

            # Someone who already has mpv keeps theirs, bundled copy or not.
            # (PATHEXT is upper case, so what comes back is mpv.EXE.)
            stub(own_mpv / "mpv.exe")
            os.environ["PATH"] = str(own_mpv)
            check("PATH wins", (config.find_mpv() or "").lower(),
                  str(own_mpv / "mpv.exe").lower())
    finally:
        os.environ["PATH"] = path_before
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\nthe repair tool:")
    try:
        from tools import repair_db

        check("tools.repair_db imports", callable(repair_db.repair), True)
        # main._repair_and_restart used to load the file by hand and register it
        # under a name of its own; the dataclasses in it resolve through
        # sys.modules[cls.__module__], so the name has to be a real one.
        check("its dataclasses can find their module",
              repair_db.Salvage.__module__ in sys.modules, True)
    except Exception as exc:
        failures.append(f"tools.repair_db: {exc}")
        print(f"  FAIL  tools.repair_db: {exc}")

    print("\nswapping in a staged updater:")
    check_updater_swap()

    shutil.rmtree(_DATA, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
