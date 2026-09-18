"""MisterySetup — the installer, the uninstaller and the pieces they share.

Kept as its own package under packaging/ so that the only thing frozen into
MisterySetup.exe is this folder: no app code, no PySide6, no Qt. The entry point
is packaging/setup_entry.py, which puts packaging/ on the path and calls
installer.setup_main.main().
"""
