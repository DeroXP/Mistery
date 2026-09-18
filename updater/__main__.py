"""`python -m updater` — the same program MisteryUpdate.exe is, from source.

The frozen exe runs packaging/updater_entry.py, which is three lines that do
what this does. Two entry points for one program because PyInstaller wants a
script file rather than a package, and because running the updater from a
source checkout is how most of its tests drive it.
"""

from .main import main

raise SystemExit(main())
