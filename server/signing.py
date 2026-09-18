"""Checking the signature on an update manifest. This file cannot make one.

That is the point, and it is worth being blunt about: there is no `sign()` here,
no key generation, and nothing that reads a private key. The signing half lives
in `packaging/sign_manifest.py`, which runs on the release machine. If someone
takes this server completely — the container, the environment variables, the
lot — they still cannot produce a manifest that any installed copy of Mistery
will accept. The worst they can do is serve an old manifest that was signed
properly, or serve nothing. That blast radius is the whole reason the manifest
is signed at all; TLS only proves you are talking to this server, and this
server is the thing we are assuming is compromised.

The envelope is what `packaging/sign_manifest.py` writes:

    {
      "schema": 1,
      "key_id": "3f9c1a2b",            first 8 hex of SHA-256(public key)
      "signature": "<base64, 64 bytes>",
      "manifest": "<base64 of the body's exact UTF-8 bytes>"
    }

The body travels base64-encoded so that the bytes that were signed are exactly
the bytes that are checked. The alternative — signing "the JSON" — means every
program in the chain has to agree on key order, spacing and unicode escaping
forever, and it means parsing attacker-shaped input *before* deciding whether to
trust it. Here the order is: decode base64 (which cannot do anything), verify,
then parse.

`MANIFEST_CONTEXT` is prepended to the signed bytes so a signature this key made
for anything else can never be replayed as an update manifest.

Why Ed25519 by hand instead of `cryptography`: the same file has to be verifiable
inside MisteryUpdate.exe, which is frozen with PyInstaller and where
`cryptography` costs about 11 MB of download for one 64-byte check. This is the
verify half of RFC 8032's reference implementation, standard library only.
`test_service.py` checks it against RFC 8032's own test vectors and against
`cryptography`'s Ed25519 over 100 random key/message pairs — both directions,
zero mismatches. One verify measured 1.9 ms on this machine, and it happens once
per manifest fetch (once per 5 minutes by default), not once per request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any

# Must match packaging/sign_manifest.py exactly. If either side ever changes,
# every installed updater in the field is the side that cannot be changed, so
# the number goes up and old copies refuse politely instead of guessing.
SCHEMA = 1
MANIFEST_CONTEXT = b"mistery-update-manifest/1\n"

# A real manifest is about 900 bytes. Anything a thousand times that is a
# redirect into something hostile, not a release.
MAX_MANIFEST_BYTES = 1_000_000

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


# ---------------------------------------------------------------------------
# Ed25519 verification - RFC 8032, verify half only
# ---------------------------------------------------------------------------

_P = 2**255 - 19                                              # the field prime
_L = 2**252 + 27742317777372353535851937790883648493          # the group order
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _sha512_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


# Points are (X, Y, Z, T) in extended coordinates: x = X/Z, y = Y/Z, xy = T/Z.
# Carrying Z avoids a modular inverse on every single addition, which is what
# keeps a scalar multiplication at milliseconds instead of most of a second.

def _point_add(a: tuple, b: tuple) -> tuple:
    ta = (a[1] - a[0]) * (b[1] - b[0]) % _P
    tb = (a[1] + a[0]) * (b[1] + b[0]) % _P
    tc = 2 * a[3] * b[3] * _D % _P
    td = 2 * a[2] * b[2] % _P
    e, f, g, h = tb - ta, td - tc, td + tc, tb + ta
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(scalar: int, point: tuple) -> tuple:
    result = (0, 1, 1, 0)                                     # neutral element
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(a: tuple, b: tuple) -> bool:
    # Projective coordinates, so compare cross-multiplied; two encodings of the
    # same point have different X, Y and Z.
    if (a[0] * b[2] - b[0] * a[2]) % _P != 0:
        return False
    return (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    """The x that belongs with this y, or None if the point is not on the curve."""
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_G_Y = 4 * pow(5, _P - 2, _P) % _P
_G_X = _recover_x(_G_Y, 0)
_G = (_G_X, _G_Y, 1, _G_X * _G_Y % _P)


def _decompress(data: bytes) -> tuple | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def verify_bytes(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only if this exact key signed these exact bytes. Never raises.

    Every kind of failure - wrong length, a point that is not on the curve, a
    non-canonical S, a signature over different bytes - is the same answer, and
    the caller's response to all of them is the same: do not serve this.
    """
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        point_a = _decompress(public_key)
        if point_a is None:
            return False
        point_r = _decompress(signature[:32])
        if point_r is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            # RFC 8032 step 3 says reject, not reduce. Reducing would let one
            # signature be re-encoded a second way that still verifies, and
            # "the manifest I checked is the manifest I cached" would stop
            # being true.
            return False
        h = _sha512_int(signature[:32] + public_key + message) % _L
        return _point_equal(_point_mul(s, _G), _point_add(point_r, _point_mul(h, point_a)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


class BadManifest(Exception):
    """This document is not a manifest that key signed. Never serve it."""


def parse_public_key(text: str) -> bytes:
    """32 bytes from whatever the owner pasted into Railway.

    Accepts base64 (what packaging/make_key.py prints) or hex (what
    updater/keys.py holds), because those are the two places the same key is
    written down and asking a human to remember which is which is how a deploy
    fails at 1am.
    """
    text = text.strip().strip('"').strip("'")
    if not text:
        raise ValueError("empty")
    try:
        if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
            key = bytes.fromhex(text)
        else:
            key = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"not base64 or hex ({exc})") from None
    if len(key) != 32:
        raise ValueError(
            f"decodes to {len(key)} bytes; an Ed25519 public key is exactly 32. "
            f"Did it get truncated on the way into the variable?"
        )
    return key


def key_id(public_key: bytes) -> str:
    """The same short name packaging/sign_manifest.py stamps into the envelope."""
    return hashlib.sha256(public_key).hexdigest()[:8]


def verify_manifest(raw: bytes, public_key: bytes) -> dict[str, Any]:
    """Return the manifest body, or raise BadManifest. Nothing else gets in.

    Order matters and is deliberate: shape, signature, then fields. No URL, size
    or hash in this document is looked at, let alone acted on, before the
    signature over it has been checked.
    """
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BadManifest(f"manifest is {len(raw)} bytes, over the {MAX_MANIFEST_BYTES} cap")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadManifest(f"not JSON: {exc}") from None
    if not isinstance(envelope, dict):
        raise BadManifest("envelope is not a JSON object")
    if envelope.get("schema") != SCHEMA:
        raise BadManifest(f"envelope schema {envelope.get('schema')!r}, expected {SCHEMA}")
    try:
        body_bytes = base64.b64decode(envelope["manifest"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise BadManifest(f"malformed envelope: {exc}") from None

    if not verify_bytes(public_key, MANIFEST_CONTEXT + body_bytes, signature):
        raise BadManifest(
            f"signature does not verify against key {key_id(public_key)}. Either "
            f"this manifest was signed with a different key, or it was changed "
            f"after signing."
        )

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadManifest(f"signed body is not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise BadManifest("signed body is not a JSON object")
    _check_body(body)
    return body


def _check_body(body: dict[str, Any]) -> None:
    """Refuse a manifest that is signed but nonsense.

    A good signature only proves the release machine produced it. It does not
    prove the release machine got it right, and this server renders the version
    and size straight onto a public page - so the shape is checked here, once,
    instead of every place that reads a field.
    """
    if body.get("schema") != SCHEMA:
        raise BadManifest(f"body schema {body.get('schema')!r}, expected {SCHEMA}")
    version = body.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise BadManifest(f"version {version!r} is not X.Y.Z")
    for name in ("package", "installer"):
        asset = body.get(name)
        if not isinstance(asset, dict):
            raise BadManifest(f"{name} is missing")
        url = asset.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            # /download redirects a browser at this URL, so an http or
            # javascript: URL here would be an open redirect with a signature
            # on it. The updater refuses non-https too; so does this.
            raise BadManifest(f"{name} url is not https: {url!r}")
        size = asset.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise BadManifest(f"{name} size is not a positive integer: {size!r}")
        digest = asset.get("sha256")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdefABCDEF" for c in digest)):
            raise BadManifest(f"{name} sha256 is not 64 hex characters")
        file_name = asset.get("name")
        if not isinstance(file_name, str) or not file_name.strip():
            raise BadManifest(f"{name} has no file name")
    notes = body.get("notes", "")
    if not isinstance(notes, str):
        raise BadManifest("notes is not a string")
    released = body.get("released", "")
    if not isinstance(released, str):
        raise BadManifest("released is not a string")
