"""The phone remote's page: an empty frame for remote.js to fill in.

Everything on it comes from the owner's PC through the relay (relay.py), asked
for and signed by the page's script with the key from the link's #fragment,
which this service never sees. The page itself carries nothing about anybody:
the PC's id in the address is all it knows, and that id cannot ask a PC for
anything without the key.

One script and one stylesheet, both files under /static, so the policy below
can say script-src 'self' and nothing else may run here.
"""

from __future__ import annotations

import html

CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' blob: data:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0B0A09">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex">
<title>Mistery remote</title>
<link rel="icon" href="/static/icon.png">
<link rel="apple-touch-icon" href="/static/icon.png">
<link rel="stylesheet" href="/static/remote.css">
</head>
"""


def render(rid: str) -> str:
    """The app, for one PC. remote.js reads the id off the body and the key off
    the address (#), keeps the key on this phone, and takes it out of the
    address bar."""
    return _HEAD + f"""<body data-remote="{html.escape(rid)}">
<div id="app">
  <header class="top">
    <div class="brand"><img src="/static/icon.png" alt=""><span>Mistery</span></div>
    <div class="pc"><span id="pc-dot" class="dot"></span><span id="pc-name">Connecting…</span></div>
  </header>
  <main id="view" aria-live="polite"></main>
  <div id="mini" class="mini" hidden></div>
  <nav class="tabs" id="tabs">
    <button data-tab="music" class="on">Music</button>
    <button data-tab="films">Films</button>
    <button data-tab="shows">Shows</button>
    <button data-tab="lists">Playlists</button>
    <button data-tab="remote">On my PC</button>
  </nav>
</div>
<div id="sheet" class="sheet" hidden></div>
<div id="screen" class="screen" hidden></div>
<audio id="audio" preload="auto"></audio>
<script src="/static/remote.js" defer></script>
</body>
</html>
"""


def render_help() -> str:
    """/remote itself: what it is, and that the link comes from the PC."""
    return _HEAD + """<body class="help">
<main class="helpbox">
  <img src="/static/icon.png" alt="" class="helpicon">
  <h1>Mistery on your phone</h1>
  <p>Play your Mistery library on your phone, wherever you are, or use your phone
  as a remote for what your PC is playing.</p>
  <p>On your PC, open Mistery, go to <b>Settings</b>, then <b>Phone remote</b>, switch it
  on and choose <b>Link a phone</b>. Scan the code, or open the link it gives you on
  your phone. Keep that link to yourself: it is the key to your library.</p>
  <p class="faint">Your PC has to be on with Mistery running. Songs and films come from
  your PC through this site, which keeps nothing.</p>
</main>
</body>
</html>
"""
