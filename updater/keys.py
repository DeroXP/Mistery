r"""The public key an update must be signed with, and where to go asking.

This file is public on purpose. A public key is not a secret, and having it
compiled into MisteryUpdate.exe is the whole security story: the exe decides at
build time what it will trust, and after that no server, no DNS answer, no
proxy and no compromised web host can change its mind. The private half lives
on the machine that cuts releases and in the GitHub secret MISTERY_SIGNING_KEY,
and nowhere else — see packaging/make_key.py, which generates the pair and
refuses to write the private key anywhere inside this repository.

To set it up once:

    python packaging\make_key.py --out %USERPROFILE%\Documents\mistery-signing.key

It writes packaging\public_key.txt and prints the same line. Paste that line
into UPDATE_PUBLIC_KEY below. packaging\build_updater.py refuses to build an
exe whose key disagrees with public_key.txt, so the two cannot drift apart
without somebody noticing at build time.

An updater built with no key here trusts nothing and says so in update.log on
every run. That is deliberate: empty means "not set up yet", never "accept
anything". The alternative — a build that falls back to trusting whatever it is
handed — is how you ship a remote code execution channel by accident.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

# The key releases are signed with. Base64, 44 characters, 32 bytes decoded.
# Empty until `python packaging\make_key.py` has been run once.
UPDATE_PUBLIC_KEY = "O29eY0C3uBU+SZ48GYMOzjSmEaRq3wUfjqv1Nb+3uhs="

# Older keys, kept only while rotating. Sign the release with the new key, ship
# an updater carrying both, and drop the old one a release or two later once
# the copies still running the previous updater have moved on. A key listed
# here can still install updates, so an entry that is no longer needed is one
# more key that can push code to people: take it out.
RETIRED_PUBLIC_KEYS: list[str] = []

# Where the signed manifest lives when updater.json says nothing.
#
# The GitHub release asset rather than the website: it exists the moment a
# release is cut, needs no domain to have been bought or pointed anywhere, and
# keeps working if the site is down or moves. This is what every installed copy
# uses today — the installer writes updater.json with the off switch in it and
# no manifest_url, so this wins. A manifest_url put there by hand (or by a
# future installer) takes precedence; see paths.manifest_url(). The bytes are
# signed either way, so where they came from decides availability and nothing
# else. The site serves the same file, byte for byte, at /api/update.
DEFAULT_MANIFEST_URL = (
    "https://github.com/DeroXP/Mistery/releases/latest/download/mistery-update.json"
)


def key_id(public_key: bytes) -> str:
    """A short name for a key: the first 8 hex of its SHA-256.

    The same eight characters packaging/sign_manifest.py stamps into the
    manifest envelope and the server prints in /api/health, so a human can tell
    at a glance whether three programs are talking about the same key.
    """
    return hashlib.sha256(public_key).hexdigest()[:8]


def trusted_keys() -> dict[str, bytes]:
    """{key_id: 32 raw bytes} for every key this build accepts.

    Anything that is not 32 bytes of base64 is dropped rather than raised on:
    a typo in this file must not stop the updater from running and writing a
    line about it, and a key it cannot read is a key it will not trust anyway.
    """
    keys: dict[str, bytes] = {}
    for encoded in [UPDATE_PUBLIC_KEY, *RETIRED_PUBLIC_KEYS]:
        raw = _decode(encoded)
        if raw is not None:
            keys[key_id(raw)] = raw
    return keys


def _decode(encoded: str) -> bytes | None:
    text = (encoded or "").strip()
    if not text:
        return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return None
    return raw if len(raw) == 32 else None
