r"""The update controls on the Settings screen, built for real and pressed.

    python packaging\test_settings_updates.py

packaging\test_updater.py proves app/updates.py against a real MisteryUpdate.exe;
this proves the screen that calls it — that the checkbox and the Check now
button exist on an installed copy, that they are left out of a source checkout
where there is no updater to talk to, that the checkbox follows updater.json
rather than settings.json, and that a press of Check now comes back and says
something rather than leaving the screen disabled. The reason it is a test at
all: a Settings toggle written against a setting nothing reads is exactly the
trap this wiring replaced.

Qt runs offscreen, so there is no window and nothing to click. Nothing here
touches %APPDATA%\Mistery: MISTERY_DATA_DIR points at a throwaway folder and is
set before app.config is imported, because that import is what creates one.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path(tempfile.mkdtemp(prefix="mistery-settings-data-"))
INSTALL = Path(tempfile.mkdtemp(prefix="mistery-settings-install-"))
os.environ["MISTERY_DATA_DIR"] = str(DATA)               # before the imports below
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                          # noqa: E402

from app import db, updates                                         # noqa: E402
from app.ui.settings_view import SettingsView                       # noqa: E402

failures: list[str] = []


def check(label: str, got, expected) -> None:
    if got == expected:
        print(f"  ok    {label}: {got!r}")
    else:
        failures.append(label)
        print(f"  FAIL  {label}: got {got!r}, expected {expected!r}")


def main() -> int:
    app = QApplication([])
    db.init()
    # install_dir() means "the folder Mistery.exe is in" and this is a test
    # script sitting somewhere else. The one seam; everything below is the
    # shipping code reading and writing a real updater.json.
    updates.install_dir = lambda: INSTALL

    try:
        print("a source checkout, with no MisteryUpdate.exe beside it:")
        view = SettingsView()
        check("the whole updates block is left out", view._auto_update, None)
        view.reload()                       # and reload() does not trip over it
        view.deleteLater()

        print("\nan installed copy:")
        # Not a real exe — running it fails, which is the point further down.
        (INSTALL / "MisteryUpdate.exe").write_bytes(b"MZ not really an updater")
        (INSTALL / "updater.json").write_text('{"auto_update": true}', encoding="utf-8")
        view = SettingsView()
        check("the switch is there", view._auto_update is not None, True)
        check("it is on", view._auto_update.isChecked(), True)
        check("Check now is enabled", view._check_updates.isEnabled(), True)

        (INSTALL / "updater.json").write_text('{"auto_update": false}', encoding="utf-8")
        view.reload()
        check("updater.json off turns the switch off", view._auto_update.isChecked(), False)
        # Off means the updater will not talk to the network even when asked, so
        # a button that asks it to would be a lie. See updater/main.py run().
        check("and Check now goes with it", view._check_updates.isEnabled(), False)

        print("\nwhen the updater cannot be run at all:")
        check("the switch says it could not be set", updates.set_auto_update(True), False)
        view._on_update_switched(False)
        check("the box goes back to what the file says",
              view._auto_update.isChecked(), False)

        (INSTALL / "updater.json").write_text('{"auto_update": true}', encoding="utf-8")
        view.reload()
        view._check_for_updates()
        check("both controls are held while a check runs",
              (view._auto_update.isEnabled(), view._check_updates.isEnabled()),
              (False, False))
        deadline = time.monotonic() + 30
        while view._update_status.text() == "Checking…" and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        check("both come back afterwards",
              (view._auto_update.isEnabled(), view._check_updates.isEnabled()),
              (True, True))
        check("and the screen says what happened", view._update_status.text(),
              "Mistery could not start its updater.")
    finally:
        shutil.rmtree(DATA, ignore_errors=True)
        shutil.rmtree(INSTALL, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
