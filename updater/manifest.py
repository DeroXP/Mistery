r"""Asking what the current version is, and deciding whether to believe it.

An updater is a remote code execution channel you build on purpose: whatever
the manifest points at is what the machine will run tomorrow. TLS is not enough
on its own, because TLS only says the bytes came from that host — and the host
is a small web service on a platform we do not control, whose whole job is to
repeat a file. So the manifest carries an Ed25519 signature made on the machine
that cuts releases, and this file refuses everything that does not verify
against the public key compiled into the exe.

The envelope, written by packaging/sign_manifest.py:

    {
      "schema": 1,
      "key_id": "3f9c1a2b",                 first 8 hex of SHA-256(public key)
      "signature": "<base64, 64 bytes>",
      "manifest": "<base64 of the body's exact UTF-8 bytes>"
    }

The body travels base64-encoded so that the signed bytes are *exactly* the
bytes: no canonical-JSON rules to agree on, no re-serialising attacker-shaped
input before checking it, and nothing in the body is parsed until the signature
over it has been verified. The signature covers SIGNING_CONTEXT + those bytes,
so a signature this key made for anything else can never be replayed here.

The order below is the point of the whole file, and it is the order the
CONTRACT sets out: signature, then version, then size, then hash — each one
before anything is unpacked, each failure a refusal that changes nothing.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Any

from . import ed25519, keys

# Must match packaging/sign_manifest.py exactly. If either ever changes, every
# updater already installed is the side that cannot be changed, so the number
# goes up and old copies refuse politely instead of guessing.
SCHEMA = 1
SIGNING_CONTEXT = b"mistery-update-manifest/1\n"

# A real manifest is about 700 bytes. A megabyte is a thousand times the room
# it needs, and a hard stop on a redirect that leads somewhere endless.
MAX_MANIFEST_BYTES = 1_000_000

# The frozen app is 174 MB here, measured. 1 GB leaves room for it to grow and
# still refuses a manifest that would fill someone's disk.
MAX_PACKAGE_BYTES = 1_000_000_000

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

USER_AGENT = "MisteryUpdate/1 (+https://github.com/DeroXP/Mistery)"


class Refused(Exception):
    """This is not something we will act on. The message goes in update.log.

    Every refusal ends the same way — nothing downloaded is kept, nothing on
    disk is touched, the exit code is not zero — so there is one exception type
    rather than a family of them.
    """


class Unreachable(Exception):
    """Could not ask. Not a refusal: no answer is not a bad answer.

    Wi-Fi off, DNS down, GitHub having a minute. Logged at a lower key and
    retried on the next hourly run.
    """


# --- fetching ----------------------------------------------------------------


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but only to https.

    The manifest URL is a GitHub "latest release" link, which is two redirects
    away from the storage host, so redirects have to be followed. urllib's own
    handler is happy to follow https -> http; a downgrade would hand the
    connection to anyone on the path. The signature would still catch a forged
    manifest, but there is no reason to let the bytes travel in the clear.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code, f"refusing a redirect to {newurl.split(':')[0]}://",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _HttpsOnlyRedirects())


def fetch(url: str, limit: int, timeout: float = 30.0) -> bytes:
    """Get a small file over https, or raise Unreachable. At most `limit` bytes.

    The cap is read as it streams rather than trusted from Content-Length: a
    server that lies about its length is exactly the server we are guarding
    against, and reading `limit + 1` bytes costs nothing.
    """
    if not url.lower().startswith("https://"):
        raise Refused(f"{url} is not https; refusing to fetch it")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/octet-stream",
        "Cache-Control": "no-cache",
    })
    try:
        with opener().open(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise Unreachable(f"{url} answered {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        raise Unreachable(f"{url}: {exc}") from None
    if len(data) > limit:
        raise Refused(f"{url} is larger than {limit} bytes; not reading further")
    return data


# --- verifying ---------------------------------------------------------------


def verify(raw: bytes, trusted: dict[str, bytes] | None = None) -> dict[str, Any]:
    """The manifest body, or Refused. Nothing else in the updater parses one.

    Nothing from `raw` is believed until the signature over it checks out. The
    envelope itself has to be parsed first — there is no way to find the
    signature otherwise — but only three base64 strings are taken out of it,
    and base64 cannot do anything but decode.

    `trusted` is {key_id: 32 bytes} and defaults to the keys compiled into this
    build. It is a parameter so that packaging/sign_manifest.py can check a
    release against this exact code with the key it just signed with, before
    the release goes anywhere.
    """
    trusted = keys.trusted_keys() if trusted is None else trusted
    if not trusted:
        raise Refused(
            "this build carries no public key, so no update can be verified. "
            "It was built before packaging/make_key.py was run; rebuild it "
            "with the key in updater/keys.py.")

    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(f"the manifest is not JSON: {exc}") from None
    if not isinstance(envelope, dict):
        raise Refused("the manifest is not a JSON object")
    if envelope.get("schema") != SCHEMA:
        raise Refused(
            f"manifest schema is {envelope.get('schema')!r}, and this updater "
            f"speaks {SCHEMA}. A newer Mistery will have to be installed by hand.")

    try:
        body_bytes = base64.b64decode(envelope["manifest"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise Refused(f"malformed manifest envelope: {exc}") from None
    if len(signature) != 64:
        raise Refused(f"the signature is {len(signature)} bytes, not 64")

    signed = SIGNING_CONTEXT + body_bytes
    named = envelope.get("key_id")
    # Try the key the manifest names first so the log can say which one was
    # meant, then the rest: key_id is a hint from an untrusted document, never
    # a decision. A manifest signed by a key we do not have fails either way.
    order = sorted(trusted, key=lambda k: k != named)
    for key_id in order:
        if ed25519.verify(trusted[key_id], signed, signature):
            body = _parse_body(body_bytes)
            body["_key_id"] = key_id
            return body

    raise Refused(
        f"SIGNATURE DOES NOT VERIFY. The manifest says it was signed with key "
        f"{named!r}; this updater trusts {sorted(trusted)}. Either it was "
        f"changed on the way here, or it was signed by a key this build does "
        f"not know. Nothing has been downloaded.")


def _parse_body(body_bytes: bytes) -> dict[str, Any]:
    """The body, checked field by field. Anything odd is a refusal.

    Everything here has already been proved to come from the release key, so
    this is not defence against an attacker — it is defence against a release
    that was built wrong, which is far more likely and just as damaging.
    """
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(f"the signed manifest body is not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise Refused("the signed manifest body is not a JSON object")
    if body.get("schema") != SCHEMA:
        raise Refused(f"manifest body schema is {body.get('schema')!r}, expected {SCHEMA}")

    version = body.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise Refused(f"manifest version {version!r} is not X.Y.Z")

    package = body.get("package")
    if not isinstance(package, dict):
        raise Refused("the manifest has no package to download")

    name = package.get("name")
    if not isinstance(name, str) or not SAFE_NAME_RE.match(name):
        # The name becomes a file name in update\. Anything with a separator,
        # a colon or a dot-dot in it never gets that far.
        raise Refused(f"package name {name!r} is not a plain file name")

    url = package.get("url")
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise Refused(f"package url {url!r} is not https")

    size = package.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_PACKAGE_BYTES:
        raise Refused(f"package size {size!r} is not between 1 and {MAX_PACKAGE_BYTES}")

    digest = package.get("sha256")
    if not isinstance(digest, str) or not HEX64_RE.match(digest.lower()):
        raise Refused(f"package sha256 {digest!r} is not 64 hex characters")
    package["sha256"] = digest.lower()

    notes = body.get("notes")
    body["notes"] = " ".join(str(notes).split())[:300] if notes else ""
    return body


# --- comparing ---------------------------------------------------------------


def parts(version: str) -> tuple[int, int, int] | None:
    """1.2.3 as (1, 2, 3), or None if it is not a plain three-part version."""
    if not isinstance(version, str) or not VERSION_RE.match(version.strip()):
        return None
    a, b, c = version.strip().split(".")
    return int(a), int(b), int(c)


def is_newer(offered: str, installed: str | None) -> bool:
    """Strictly newer, by number, never by string.

    "1.10.0" > "1.9.0" is the whole reason this is not a string comparison. An
    installed version we cannot read means we do not know what is there, and
    replacing an unknown install with a download is not a decision an unattended
    program should make — so that is a no as well.
    """
    new = parts(offered)
    old = parts(installed or "")
    if new is None or old is None:
        return False
    return new > old
