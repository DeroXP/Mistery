"""Does the tag being released match the version in the code?

    python packaging\\check_version.py v1.1.0

Exits 0 if `app/__init__.py` says `__version__ = "1.1.0"`, and 1 with a loud
message otherwise. The release workflow runs this as its first real step, before
the ten-minute build, because a tag that disagrees with the code produces an
installer that reports the wrong version, an update manifest that offers a
version nobody can verify, and a Release page that lies.

`app/__init__.py` is read with a regex rather than imported: importing the
package on a machine that has never run Mistery is exactly how you accidentally
create a data folder, and the CI runner has no PySide6 at that point anyway.

With no argument it just prints the version in the code, which is how the
workflow learns what to call its files.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT = REPO_ROOT / "app" / "__init__.py"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
IN_FILE_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)


def version_in_code() -> str:
    text = INIT.read_text(encoding="utf-8")
    match = IN_FILE_RE.search(text)
    if not match:
        raise SystemExit(f"no __version__ = \"...\" line in {INIT}")
    version = match.group(1)
    if not VERSION_RE.match(version):
        raise SystemExit(
            f"{INIT} says __version__ = {version!r}, which is not X.Y.Z. "
            f"Mistery uses plain semver: 1.1.0, not 1.1 and not 1.1.0b2."
        )
    return version


def main(argv: list[str]) -> int:
    version = version_in_code()
    if not argv:
        print(version)
        return 0

    tag = argv[0].strip()
    if not tag.startswith("v"):
        print(f"\nThe tag is {tag!r}. Release tags start with v: v{version}\n",
              file=sys.stderr)
        return 1
    tagged = tag[1:]
    if tagged != version:
        print(
            f"\nThe tag and the code disagree, so nothing will be built.\n"
            f"    tag               {tag}\n"
            f"    app/__init__.py   __version__ = \"{version}\"\n\n"
            f"Fix whichever is wrong. Usually it is the code: bump\n"
            f"__version__ to {tagged}, commit, then move the tag:\n"
            f"    git tag -d {tag} && git push origin :refs/tags/{tag}\n"
            f"    git tag {tag} && git push origin {tag}\n",
            file=sys.stderr,
        )
        return 1

    print(f"{tag} matches app/__init__.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
