<!--
GitHub shows this file as the repository's front page when it exists, ahead of
the README.md in the root. That is why it is here: the root README is the long
technical one, 950 lines of how the app works and why, and it is the wrong first
thing to hand somebody who just wants to know what this is. Both are kept; this
one links to that one.
-->

# Mistery

A media player for the films, shows and music already on your disk. Windows 11,
dark and cinematic, with **mpv** doing the decoding — full GPU decoding, HDR,
HEVC, and a frame on screen when you press play.

It is a personal project, built for one person's library and shared because it
works. It plays your files. It does not find, stream, buy or download media, and
there is no account, no subscription and nothing to sign in to.

## Install

Download **MisterySetup.exe** from [the latest release][releases] and run it.

- It installs for you only, in `%LOCALAPPDATA%\Programs\Mistery` — no
  administrator prompt, nothing machine-wide.
- The download is about 90 MB and the install is about 425 MB — 179 MB of app
  and updater, 254 MB of mpv and ffmpeg, and an 11 MB uninstaller. The mpv and
  ffmpeg half arrives as a 101 MB download that the installer fetches from their
  own projects while it runs and checks against a pinned SHA-256. They are not
  redistributed here.
- Windows SmartScreen will warn you: the installer is not code-signed, because a
  certificate costs a few hundred a year for a project one person maintains.
  *More info* → *Run anyway*, and compare the SHA-256 on the release page with
  the file you downloaded if you would rather be sure.
- Your library stays where it is. Mistery scans folders you point it at and
  keeps its own database, artwork and settings in `%APPDATA%\Mistery`.

To uninstall: Settings → Apps → Mistery, or `Uninstall.exe` in the install
folder. It offers to keep your library database and artwork, in case you are
only reinstalling.

## Updates

Mistery checks for a new version in the background and installs it while you are
not using it — there is no "please restart" box, and no prompt.

- An hourly scheduled task runs `MisteryUpdate.exe`, which does nothing at all
  unless Mistery has been closed for at least 30 minutes.
- What tells it a new version exists is a small manifest signed with an
  **Ed25519** key that lives on the release machine and in one GitHub secret,
  and nowhere else. The updater carries only the public half, compiled in, and
  refuses anything that does not verify against it — before it downloads
  anything, and again before it unpacks anything.
- It checks, in this order: the signature, that the version really is newer,
  the download's size, and its SHA-256. Any mismatch and the download is
  deleted and nothing changes.
- The whole thing is [updater/](../updater), about 1,900 lines, and the release
  side is [packaging/sign_manifest.py](../packaging/sign_manifest.py).

If you would rather it did not: turn automatic updates off in Settings, which
writes `"auto_update": false` into `updater.json` in the install folder — the
updater never reads your app settings at all, on purpose, so that is where its
own switch lives. Deleting the `MisteryUpdate` task in Task Scheduler works too.

## What it sends over the network

Everything Mistery contacts, and why. There is nothing else in the code — this
list is what `grep` finds, not what we remember writing.

| Where | What for | When |
| --- | --- | --- |
| `api.themoviedb.org`, `image.tmdb.org` | film and show details, posters, backdrops | only with your own TMDB API key, which you paste into Settings |
| `api.tvmaze.com` | episode titles, air dates | matching TV folders |
| `en.wikipedia.org`, `query.wikidata.org` | a paragraph and a few facts about a film or an artist | opening a detail page that has none yet |
| `itunes.apple.com` | album artwork and track details | tidying music metadata |
| `lrclib.net` | lyrics, synced where they exist | playing a track whose lyrics are not stored |
| `discord.com` | "watching *Heat*" in your Discord status | only if you turn Discord presence on; it is off by default, and can show titles or hide them |
| this project's update service | "is there a newer version" | hourly, and it sends nothing but the request |

What never leaves your machine: what you have, what you watched, when, for how
long, your settings, your file paths, your library database. There is no
analytics, no crash reporting, no telemetry of any kind, and no account. The
update check has no identifier in it — the service keeps no logs of who asked
and could not tell you apart if it did.

## Running it from source

Python 3.12 and mpv are all it really needs.

```bash
git clone https://github.com/DeroXP/Mistery.git
cd Mistery
pip install -r requirements.txt
winget install shinchiro.mpv     # ffmpeg too, if you want thumbnails
python main.py
```

`python selfcheck.py` inspects a library and says what is wrong with it;
`python smoketest.py` actually launches the app and plays things, against a copy
of your library in a temporary folder.

## Building the installer yourself

```bash
pip install -r requirements.txt pyinstaller
python packaging/build_app.py          # freeze Mistery.exe
python packaging/build_updater.py      # freeze MisteryUpdate.exe
python packaging/make_package.py       # zip the two for the updater
python packaging/build_setup.py        # packaging/out/setup/MisterySetup.exe
```

Measured on the machine this was written on, for 1.0.0: the frozen bundle is
179 MB in 232 files, the update zip 79 MB and 5 seconds to compress, and
MisterySetup.exe 90 MB with the whole app zipped inside it.

`python packaging/test_release.py` checks that this list, `docs/SETUP.md` and the
workflow all name files that exist — it is there because the workflow once called
a `build_installer.py` that never existed, and looked for the installer one folder
away from where it is written.

A release is a tag: pushing `v1.2.0` runs
[.github/workflows/release.yml](workflows/release.yml), which does all of the
above on a clean Windows runner, signs the manifest with the repository secret,
and uploads the installer, the zip and the manifest to the release page. It
refuses to build if the tag and `app/__init__.py` disagree about the version.
[docs/SETUP.md](../docs/SETUP.md) is the one-time setup behind that.

## What is in here

| | |
| --- | --- |
| [`app/`](../app), [`main.py`](../main.py) | the player itself — UI, library, metadata, music |
| [`packaging/`](../packaging) | freezing, the installer, the signing tools |
| [`updater/`](../updater) | `MisteryUpdate.exe`: check, verify, replace |
| [`server/`](../server) | the download page and the update service |
| [`tools/`](../tools) | database repair, shortcuts, icons |
| [`README.md`](../README.md) | **the long one**: how everything works and why |

## Licence and other people's work

Mistery has no licence file yet, which means the usual default: all rights
reserved, and you are welcome to read it and build it for yourself.

The app uses **PySide6** (Qt for Python, LGPL) and, at run time, **mpv** and
**ffmpeg** (GPL/LGPL). Neither mpv nor ffmpeg is redistributed here: the
installer downloads the official builds from their own projects, with a pinned
URL and a pinned checksum, which keeps their licensing between you and them.
Film and show data comes from TMDB and TVmaze, and this project is not endorsed
by either.

[releases]: https://github.com/DeroXP/Mistery/releases/latest
