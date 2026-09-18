r"""MisteryUpdate — the program that replaces Mistery.

It runs from an hourly scheduled task and almost always does nothing. It moves
a file only when Mistery is closed and has been closed for half an hour, and it
trusts a download only when the manifest describing it carries an Ed25519
signature made by a key that lives on the release machine and nowhere else.

    keys.py       the public key this build trusts, and where to ask
    manifest.py   fetching over https, and the signature check
    download.py   the package, its size and its SHA-256
    apply.py      unpacking defensively, and rename-then-replace
    liveness.py   is Mistery running, and how long since it was
    paths.py      the install folder, the data folder, update.log
    main.py       the order all of that happens in, and the exit codes

The app's end of it is app/updates.py: the Settings screen's switch and its
"Check now" button, which run this program (`--enable`, `--disable`,
`--check-now`) rather than importing it, because it ships as an exe beside
Mistery.exe and not as a package the app can import. It is also the reason the
off switch lives in updater.json instead of settings.json — see paths.py.

Nothing here imports `app.*`. Importing app.config constructs Settings(), and
that writes settings.json when it is missing — an updater that creates a data
folder on a PC where Mistery has never been opened is a bug you hear about from
somebody else, whose library then looks empty. The two things the updater needs
from the app (where the data folder is, how app.lock is read) are copied, and
each copy says so where it sits.
"""
