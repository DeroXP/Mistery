"""The landing page: one HTML document, built here, with no JavaScript at all.

Every number on this page comes from somewhere real. The version, the file name,
the size and the SHA-256 come out of the signed manifest, so the page cannot
advertise a download that the release machine did not sign. The claims in the
feature cards are the measurements from the app's own README - 10.0 dB to 2.2 dB
of album-to-album loudness, 0.2-0.4% of a core while it plays in the tray, the
screensaver's 24 px of drift over six minutes - because a page full of adjectives
tells a stranger nothing about whether the thing is any good.

No JavaScript is not a pose, it is what makes the Content-Security-Policy in
service.py honest: `script-src 'none'` and `default-src 'none'` are easy to mean
when the page has nothing to run. It also means the page works before the first
frame, on a phone on a train, and that there is nothing here that could watch
anybody. Everything dynamic is rendered server-side, once per request, from a
manifest that is already in memory.

The one thing this page must never do is claim something Mistery does not do. It
plays files you already have. It does not find, buy or download media, and
nothing on this page may imply that it does. The one thing it streams is a movie
night: a file the host already has, from their PC straight to the friends they
invited, while they watch it together.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from config import Config
from manifest import Current

# The app's own palette, from app/ui/theme.py, so the page reads as the product:
# near-black #141414, one reserved red #E50914, white as the primary action.
# The restraint is the point - in the app, red marks the current page and watch
# progress and nothing else, and the primary button is white. Same here.


@dataclass(frozen=True)
class Shot:
    """A screenshot of the app, dropped into static/shots by whoever has it running."""

    file_name: str
    caption: str


def human_size(num_bytes: int) -> str:
    """8.4 MB. One decimal, because two is noise and none hides a 40% difference."""
    if num_bytes >= 1024 * 1024 * 1024:
        return f"{num_bytes / 1024 / 1024 / 1024:.1f} GB"
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / 1024 / 1024:.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def _date(released: str) -> str:
    """2026-09-17T18:40:00Z -> 17 September 2026. Left alone if it is not that."""
    months = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")
    try:
        year, month, day = released[:10].split("-")
        return f"{int(day)} {months[int(month) - 1]} {year}"
    except Exception:  # noqa: BLE001 - a malformed date is not worth a 500
        return released


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


# Movie night leads, across the whole row: it is what 1.2.0 adds, and a seventh
# card in the three-column grid would otherwise sit alone on a row of its own.
WIDE_FEATURES = {"Movie night, with friends on their own PCs"}

FEATURES = [
    (
        "Movie night, with friends on their own PCs",
        "Start a movie night on a film or an episode and send the code. Friends who also have "
        "Mistery paste it and watch with you, in step — <b>within 42–53 ms of each other</b> "
        "95% of the time, measured — straight from your PC to theirs, with no server in "
        "between. Anyone can pause or seek, everybody waits for a friend who is buffering, "
        "and your own place in the film is never touched: the party's place is kept apart. "
        "It is encrypted with a certificate made for that evening, and it listens on one "
        "port only while it runs.",
    ),
    (
        "One library, whatever is in it",
        "Films, shows and albums live in the same library, scanned out of folders you "
        "choose. Scene filenames are parsed — "
        "<code>Night.Train.2019.2160p.HDR.x265</code> becomes "
        "<b>Night Train</b>, 2019, with 4K, HDR and HEVC badges — and "
        "<code>S01E04</code>, <code>1x04</code> and <code>Season 1 Episode 5</code> all "
        "group into show, season, episode. Artwork and descriptions come from public "
        "sources; a TMDB key makes them better but is not required. A rescan of an "
        "unchanged library costs one <code>stat()</code> per file.",
    ),
    (
        "Every album at the same volume",
        "Half a music library carries ReplayGain tags and half does not, which is why one "
        "album arrives quiet and the next one shouts. Mistery ignores the tags and measures "
        "the files instead — EBU R128 integrated loudness and true peak, once per song, in "
        "the background. Across the albums this was built against, loudest to quietest went "
        "from <b>10.0 dB apart to 2.2 dB</b>. Nothing is ever pushed past −1 dBFS, so a "
        "quiet master that already touches full scale stays quiet rather than clipping.",
    ),
    (
        "Lyrics, and a screensaver made of them",
        "Synced lyrics come from an <code>.lrc</code> beside the file, the file's own tags, "
        "or LRCLIB — free, keyless, cached, including the misses. Click a line to jump to "
        "it. Leave a song playing and don't touch anything for three minutes and the whole "
        "screen becomes the song: the record turning, the words at the size of a poster, "
        "the waveform underneath, on black. It is built to be left on an OLED — the picture "
        "drifts 24 px over six minutes, it dims as the hours pass, and it does not hold the "
        "display awake, because letting Windows blank the panel is the best protection there is.",
    ),
    (
        "Playlists and categories",
        "Tick as many categories as you like — Anime, Romance, Action — and the page narrows "
        "to them; what you tick is still ticked tomorrow. Four services name genres four "
        "different ways, so they are mapped onto one list and one chip means one thing. "
        "Right-click a song, a film or an episode to add it to a playlist, and the playlists "
        "it is already in are ticked. A playlist is a list of library ids, so renaming a "
        "folder or finishing a download leaves it alone.",
    ),
    (
        "It remembers, and the keyboard reaches it",
        "Every file remembers where you stopped, and Continue Watching on Home previews from "
        "that frame when you hover it. Close Mistery mid-album and the queue, the song, the "
        "position, shuffle and repeat come back next time — loaded and paused, silent until "
        "you press play. Your keyboard's media keys start it from there, and they keep "
        "working with a game in the foreground, because mpv registers a real Windows media "
        "session. Close the window while music plays and it carries on from the tray, at "
        "0.2–0.4% of one core.",
    ),
    (
        "mpv underneath, not a browser",
        "4K HEVC in an MKV with EAC3 5.1 and HDR10 is what a real library looks like, and it "
        "is precisely what a browser-based player cannot open. Mistery embeds mpv — full GPU "
        "decoding, every codec, real HDR tone mapping — and wraps it in a Qt interface. "
        "Hardware-decoded HEVC, AV1 and H.264; dialogue boost for 5.1 tracks mastered for a "
        "cinema; subtitle and audio tracks remembered per show; intros found by listening to "
        "what every episode of a season has in common, so Skip intro works with no database "
        "of timestamps anywhere.",
    ),
]


def render(config: Config, current: Current | None, shots: list[Shot]) -> str:
    title = "Mistery — a player for the films, shows and music you already have"
    description = (
        "A dark, fast Windows library for the video and music files on your own disk. "
        "mpv for playback, one library for films, shows and albums, and music levelled "
        "to one volume."
    )
    canonical = f'<link rel="canonical" href="{_esc(config.site_url)}/">' if config.site_url else ""
    og_url = f'<meta property="og:url" content="{_esc(config.site_url)}/">' if config.site_url else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(description)}">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#141414">
{canonical}
<meta property="og:type" content="website">
<meta property="og:title" content="Mistery">
<meta property="og:description" content="{_esc(description)}">
<meta property="og:image" content="/static/icon.png">
{og_url}
<link rel="icon" href="/static/icon.png" type="image/png">
<link rel="stylesheet" href="/static/site.css">
</head>
<body>
<header class="top">
  <a class="brand" href="/">
    <img src="/static/icon.png" alt="" width="32" height="32">
    <span>Mistery</span>
  </a>
  <nav>
    <a href="{_esc(config.source_url)}">Source</a>
    <a href="{_esc(config.releases_url)}">Releases</a>
  </nav>
</header>

<main>
{_hero(config, current)}
{_requirements()}
{_shots(shots)}
{_features()}
{_updates(config, current)}
</main>

{_footer(config, current)}
</body>
</html>
"""


def _hero(config: Config, current: Current | None) -> str:
    if current is not None and current.installer:
        installer = current.installer
        size = human_size(int(installer.get("size", 0)))
        version = _esc(current.version)
        button = f"""
      <a class="download" href="/download">
        <span class="download-main">Download Mistery {version}</span>
        <span class="download-sub">{_esc(installer.get('name', 'MisterySetup.exe'))} &middot; {size} &middot; Windows 11, 64-bit</span>
      </a>"""
        # The first thing that happens to anyone who takes this download is a
        # blue box saying "Windows protected your PC", and the button that
        # looks like the way out is "Don't run". Someone who was not told is
        # someone who does not install it, and the release page says this while
        # this page used to not - the one page they read before downloading.
        # It is also the honest thing: the file really is unsigned, and the way
        # to check it is the SHA-256 further down.
        signing_note = f"""
    <p class="unsigned">The first time you run it, Windows will say
    <b>&ldquo;Windows protected your PC&rdquo;</b> and offer you a
    <b>Don&rsquo;t run</b> button. That is because this installer is not
    code-signed &mdash; a certificate is a few hundred a year for something one
    person maintains. Click <b>More info</b>, then <b>Run anyway</b>.
    If you would rather be sure of what you have first, the
    <a href="#checksum">SHA-256 at the bottom of this page</a> is the one this
    file should have.</p>"""
        note = ""
        if current.body.get("notes"):
            note = f'<p class="whats-new"><span class="pill">New in {version}</span> {_esc(current.body["notes"])}</p>'
    else:
        # No release yet, or the manifest could not be verified. Say so plainly
        # rather than offering a button that 404s - and never fall back to
        # guessing a URL, because a guessed download is the one thing this
        # service must not do.
        button = f"""
      <a class="download download-none" href="{_esc(config.releases_url)}">
        <span class="download-main">No release published yet</span>
        <span class="download-sub">The releases page on GitHub is the place to look</span>
      </a>"""
        signing_note = ""     # nothing to download, nothing to warn about
        note = ""

    return f"""
<section class="hero">
  <div class="hero-text">
    <h1>Your films, your shows and your music, in one dark library.</h1>
    <p class="lede">Mistery is a Windows player for the files that are already on your disk.
    Point it at your folders and it works out what everything is, finds the artwork, and then
    plays it properly — mpv underneath, so 4K HEVC in an MKV with 5.1 audio and HDR just plays.</p>
    {button}{signing_note}
    <p class="honest">It plays files you already have. Mistery does not find, stream, buy or
    download anything, and there is no account to make.</p>
    {note}
  </div>
  <div class="hero-mark" aria-hidden="true">
    <img src="/static/icon.png" alt="" width="220" height="220">
  </div>
</section>
"""


def _requirements() -> str:
    # "About 300 MB" was a guess made before there was an installer to measure.
    # The install is 459.7 MB, which Windows counts in 1024s and reports as
    # 438 MB in Add/Remove Programs: 194 MB of frozen app and updater (the
    # release build of 1.2.0 says so: "84.6 MB zipped from 194 MB"; the app
    # alone is 185.0 MB, and movie night's cryptography package is most of
    # what 1.2.0 added), 254.4 MB of mpv and ffmpeg unpacked from a 101.0 MB
    # download, and the 11.3 MB uninstaller, measured on 2026-09-17. The tool
    # halves are the constants in packaging/installer/tool_downloads.py, which
    # are themselves measured. Say the number a stranger will see on their own
    # machine, and round it up rather than down.
    return """
<section class="specs">
  <div>
    <h3>Windows 11, 64-bit</h3>
    <p>Built and tested on Windows 11. It installs for you only — no admin prompt, nothing
    machine-wide, and updates need no prompt either.</p>
  </div>
  <div>
    <h3>About 440 MB on disk</h3>
    <p>185 MB of app, and 250 MB of mpv and ffmpeg, which the installer fetches from their
    own projects during setup — a 100 MB download — rather than shipping copies of them.</p>
  </div>
  <div>
    <h3>Your own media files</h3>
    <p>Films, shows and albums in folders you choose. Nothing is uploaded, and your library
    stays exactly where it is.</p>
  </div>
</section>
"""


def _shots(shots: list[Shot]) -> str:
    """The screenshots section, or nothing at all.

    Nothing at all is deliberate. There is no mock-up here, no illustration
    dressed as a screenshot: either these are pictures of the real app or the
    section does not exist. /api/health says how many were found, so the owner
    can see from the outside whether the deploy picked them up.
    """
    if not shots:
        return ""
    figures = "\n".join(
        f"""    <figure>
      <img src="/static/shots/{_esc(shot.file_name)}" alt="{_esc(shot.caption)}" loading="lazy">
      <figcaption>{_esc(shot.caption)}</figcaption>
    </figure>"""
        for shot in shots
    )
    return f"""
<section class="shots">
  <h2>What it looks like</h2>
  <div class="shot-grid">
{figures}
  </div>
</section>
"""


def _features() -> str:
    cards = "\n".join(
        f"""  <article{' class="wide"' if heading in WIDE_FEATURES else ''}>
    <h3>{_esc(heading)}</h3>
    <p>{body}</p>
  </article>"""
        for heading, body in FEATURES
    )
    return f"""
<section class="features">
  <h2>What makes it worth installing</h2>
{cards}
</section>
"""


def _updates(config: Config, current: Current | None) -> str:
    checksum = ""
    if current is not None and current.installer.get("sha256"):
        checksum = f"""
  <p class="checksum" id="checksum">Check what you downloaded before you run it:<br>
  <code>Get-FileHash .\\{_esc(current.installer.get('name', 'MisterySetup.exe'))} -Algorithm SHA256</code><br>
  should print <code class="hash">{_esc(current.installer['sha256'])}</code></p>"""
    return f"""
<section class="updates">
  <h2>How updates work</h2>
  <p>Mistery keeps itself up to date and gets out of the way while it does: it only replaces
  anything when the app has been closed for half an hour, and because it installs for you
  rather than for the machine, there is no prompt.</p>
  <p>Every update is described by a small file signed with an Ed25519 key that lives on the
  release machine and nowhere else — not on this server, not in the source. Mistery carries
  only the matching public key, and checks the signature, the version, the size and the
  SHA-256 of the download before a single file is unpacked. Anything that does not match is
  deleted and logged. This site cannot sign an update, so at its very worst it can serve you
  an old release or nothing at all — it cannot hand your PC new code.</p>
  <p>The feed itself is public: <a href="/api/update">/api/update</a> is the signed file that
  describes the current release — the same bytes an installed Mistery checks, which it fetches
  straight from the GitHub release so that updates keep working even when this site does not.
  <a href="{_esc(config.source_url)}">The source</a> is where you can read what it does with
  them.</p>{checksum}
</section>
"""


def _footer(config: Config, current: Current | None) -> str:
    if current is not None:
        released = _date(str(current.body.get("released", "")))
        line = f"Version {_esc(current.version)}"
        if released:
            line += f", published {_esc(released)}"
    else:
        line = "No published release yet"
    return f"""
<footer>
  <p>{line}. <a href="{_esc(config.releases_url)}">All releases</a> &middot;
  <a href="{_esc(config.source_url)}">Source</a> &middot;
  <a href="/api/update">Update feed</a></p>
  <p class="quiet">No cookies, no analytics, no accounts. This page sets nothing, stores
  nothing and loads nothing from anywhere else.</p>
</footer>
"""


def render_error(status: int, heading: str, detail: str) -> str:
    """The same shell, three lines of content. Used for 404 and 429.

    A default framework error page on a product's front door looks broken, and
    it also leaks which framework is underneath.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{status} &middot; Mistery</title>
<meta name="color-scheme" content="dark">
<link rel="icon" href="/static/icon.png" type="image/png">
<link rel="stylesheet" href="/static/site.css">
</head>
<body>
<header class="top">
  <a class="brand" href="/"><img src="/static/icon.png" alt="" width="32" height="32"><span>Mistery</span></a>
</header>
<main>
  <section class="hero">
    <div class="hero-text">
      <h1>{_esc(heading)}</h1>
      <p class="lede">{_esc(detail)}</p>
      <p><a href="/">Back to the front page</a></p>
    </div>
  </section>
</main>
</body>
</html>
"""
