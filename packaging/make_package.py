r"""Zip the built app into the file the updater downloads.

    python packaging\build_app.py
    python packaging\build_updater.py
    python packaging\make_package.py

Writes packaging\out\Mistery-<version>-win64.zip: every file from the frozen
bundle, at paths relative to the install folder, so the updater can unpack it
straight over %LOCALAPPDATA%\Programs\Mistery. `Mistery.exe` is at the top of
the zip, not inside a `Mistery\` folder — a wrapper folder would mean the
updater had to guess how many levels to strip, and guessing is how an updater
writes files somewhere nobody expected.

What it checks before it writes anything, and why each one is a hard stop:

  Mistery.exe            without it the zip is not an update, it is a mess
                         somebody's install gets replaced with.
  MisteryUpdate.exe      an install from this zip could never update itself
                         again, and nobody would find out until the release
                         after this one.
  version.txt            the updater compares this line, on disk, without
                         starting Python. If it disagrees with the manifest, an
                         installed copy either re-applies this update forever or
                         refuses the next one.

The zip is not the thing that makes an update safe — the signed manifest is, and
it carries this file's size and SHA-256. This script exists so the release
workflow stays short enough to read.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

sys.path.insert(0, str(HERE))
from check_version import version_in_code  # noqa: E402

REQUIRED = ("Mistery.exe", "MisteryUpdate.exe", "version.txt")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")   # what updater/manifest.py accepts


def build(bundle: Path, out_dir: Path, version: str) -> Path:
    if not bundle.is_dir():
        raise SystemExit(
            f"{bundle} is not there. Run packaging/build_app.py first — it "
            f"writes the frozen bundle this packages up.")

    missing = [name for name in REQUIRED if not (bundle / name).is_file()]
    if missing:
        built_by = {
            "Mistery.exe": "packaging/build_app.py",
            "MisteryUpdate.exe": "packaging/build_updater.py",
            "version.txt": "packaging/build_app.py",
        }
        lines = "\n".join(f"    {name:<18} built by {built_by[name]}" for name in missing)
        raise SystemExit(f"{bundle} is missing:\n{lines}")

    on_disk = (bundle / "version.txt").read_text(encoding="utf-8").strip()
    if on_disk != version:
        raise SystemExit(
            f"version.txt in the bundle says {on_disk!r} but app/__init__.py says "
            f"{version!r}. The bundle is from an older build — rebuild it rather "
            f"than shipping a zip that lies about what is in it.")

    out_dir.mkdir(parents=True, exist_ok=True)
    package = out_dir / f"Mistery-{version}-win64.zip"
    if not SAFE_NAME.match(package.name):
        raise SystemExit(f"{package.name} is not a plain file name the updater will accept")
    package.unlink(missing_ok=True)

    files = sorted(p for p in bundle.rglob("*") if p.is_file())
    raw_bytes = sum(p.stat().st_size for p in files)

    started = time.monotonic()
    # Sorted, and written one at a time, so two builds of the same bundle differ
    # only where the files themselves differ. Deflate at the default level: at
    # level 9 the same 289 MB bundle took about half again as long to compress
    # for a percent or so of size, and this runs on every release.
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(bundle).as_posix())
    took = time.monotonic() - started

    size = package.stat().st_size
    print(f"{package}")
    print(f"  {len(files)} files, {raw_bytes / 1e6:.0f} MB on disk")
    print(f"  {size / 1e6:.0f} MB zipped ({size / raw_bytes:.0%}), {took:.0f} s")
    return package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zip the frozen app for the updater.")
    parser.add_argument("--bundle", default=str(HERE / "out" / "dist" / "Mistery"),
                        help="the folder build_app.py produced (default: %(default)s)")
    parser.add_argument("--out", default=str(HERE / "out"),
                        help="where to write the zip (default: %(default)s)")
    parser.add_argument("--print-name", action="store_true",
                        help="print the file name this would write and stop")
    args = parser.parse_args(argv)

    version = version_in_code()
    if args.print_name:
        print(f"Mistery-{version}-win64.zip")
        return 0
    build(Path(args.bundle), Path(args.out), version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
