"""The script PyInstaller freezes into MisteryUpdate.exe.

PyInstaller wants a script, not a package, and a package's own __main__.py run
as a script cannot use relative imports. So this file exists to be that script,
and does nothing but hand over to the real one — `python -m updater` from a
source checkout runs the same code through updater/__main__.py.
"""

import sys

from updater.main import main

if __name__ == "__main__":
    sys.exit(main())
