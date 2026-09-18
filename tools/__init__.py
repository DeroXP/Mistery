"""Scripts that ship with Mistery: the library repair and the icon maker.

A package, not a loose folder, so that `from tools import repair_db` works the
same from source and inside the frozen app — where there is no tools\\ folder on
disk to load a file from. Each script still runs on its own:

    python tools/repair_db.py
"""
