"""The one file PyInstaller is pointed at to build MisterySetup.exe.

It exists so that the installer's package is imported as `installer.*` rather
than `packaging.installer.*`. `packaging` is also the name of a real library
that PyInstaller itself depends on, and a build that has two different things
called `packaging` on its path finds out about it halfway through, in a
traceback that does not mention any of this.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from installer.setup_main import guarded_main    # noqa: E402

if __name__ == "__main__":
    raise SystemExit(guarded_main())
