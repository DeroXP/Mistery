"""Ed25519 in plain Python — the signing half, for the machine that cuts releases.

There are three copies of this arithmetic in the repository and each one has a
reason to exist:

    packaging/ed25519.py   this file. Signs. Runs on the release machine and in
                           the GitHub Actions runner, never on a user's PC.
    server/signing.py      verify only, deliberately — a web service that cannot
                           sign is a web service that cannot push code to anyone.
    updater/ed25519.py     verify only, vendored into MisteryUpdate.exe, which
                           ships frozen and alone.

They are separate so that no one of them can be quietly broken into doing more
than its job, and packaging/test_signing.py signs with this file and verifies
with the other two on every run, so they cannot drift apart without a failing
test saying so.

Why not `cryptography`: measured, it is 11 MB on disk (9.5 MB of it one
`_rust.pyd`) plus about 1 MB of cffi, to do 32 bytes of arithmetic. The updater
downloads on someone else's connection, so that weight is real; and having the
signer depend on a library the verifier cannot have is how a signer and a
verifier end up disagreeing.

The arithmetic is RFC 8032's reference implementation, appendix A. Only the
names and the comments are ours. `selftest()` checks it against RFC 8032's own
test vectors, and make_key.py and sign_manifest.py both run it before they touch
a real key.

Not constant-time, deliberately. That would matter for signing on a machine
someone else can measure, and this never signs on one: signing happens once per
release, inside a GitHub Actions runner, from a key held in a repository secret.
Verification is timing-safe by construction — the public key and the signature
are both public.

Measured on Python 3.12.10, this machine: keypair 1.0 ms, sign 2.0 ms,
verify 2.6 ms. A manifest is under a kilobyte, so the hashing costs nothing
next to the two scalar multiplications.
"""

from __future__ import annotations

import hashlib
import secrets

# The curve. _P is the field, _L the order of the base point.
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493

_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _sha512_int(data: bytes) -> int:
    return int.from_bytes(_sha512(data), "little")


# Points are (X, Y, Z, T) in extended coordinates: x = X/Z, y = Y/Z, xy = T/Z.
# Keeping the projective Z avoids a modular inverse on every addition.


def _point_add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _P
    B = (a[1] + a[0]) * (b[1] + b[0]) % _P
    C = 2 * a[3] * b[3] * _D % _P
    D = 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _point_mul(scalar: int, point):
    result = (0, 1, 1, 0)  # the neutral element
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(a, b) -> bool:
    # Same point iff the cross-multiplied affine coordinates agree.
    if (a[0] * b[2] - b[0] * a[2]) % _P != 0:
        return False
    return (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y: int, sign: int):
    """The x that goes with this y, or None if the point is not on the curve."""
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


def _compress(point) -> bytes:
    zinv = pow(point[2], _P - 2, _P)
    x = point[0] * zinv % _P
    y = point[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data: bytes):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _expand(seed: bytes):
    """RFC 8032's clamped scalar and the prefix used for deterministic nonces."""
    if len(seed) != 32:
        raise ValueError("an Ed25519 private key is exactly 32 bytes")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8       # clear the low 3 bits: kill the cofactor
    a |= 1 << 254             # set bit 254: fixed-length scalar, no timing leak
    return a, h[32:]


# --- the four things callers actually want -----------------------------------


def generate_private_key() -> bytes:
    """32 random bytes from the OS. That is the whole private key."""
    return secrets.token_bytes(32)


def public_key(private_key: bytes) -> bytes:
    a, _ = _expand(private_key)
    return _compress(_point_mul(a, _G))


def sign(private_key: bytes, message: bytes) -> bytes:
    a, prefix = _expand(private_key)
    A = _compress(_point_mul(a, _G))
    # The nonce is derived from the key and the message, never from randomness:
    # a repeated nonce hands over the private key, and this way it cannot happen
    # even on a machine with a broken RNG.
    r = _sha512_int(prefix + message) % _L
    R = _compress(_point_mul(r, _G))
    h = _sha512_int(R + A + message) % _L
    s = (r + h * a) % _L
    return R + int.to_bytes(s, 32, "little")


def verify(public_key_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """True only if this exact key signed these exact bytes. Never raises."""
    try:
        if len(public_key_bytes) != 32 or len(signature) != 64:
            return False
        A = _decompress(public_key_bytes)
        if A is None:
            return False
        R = _decompress(signature[:32])
        if R is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            # RFC 8032 step 3: reject a non-canonical S rather than reduce it.
            # Reducing would let a second, different signature verify for the
            # same message, which breaks "the manifest I checked is the manifest
            # I stored".
            return False
        h = _sha512_int(signature[:32] + public_key_bytes + message) % _L
        return _point_equal(_point_mul(s, _G), _point_add(R, _point_mul(h, A)))
    except Exception:
        # A malformed key or signature is a "no", not a crash.
        return False


# --- self-test ---------------------------------------------------------------

# RFC 8032 section 7.1, test vectors 1, 2 and 3: (seed, public, message, sig).
_RFC_8032_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


def selftest() -> None:
    """Check this file against RFC 8032. Raises AssertionError if it is wrong.

    Cheap insurance — 3 vectors, 14 ms measured — and both make_key.py and
    sign_manifest.py run it before they touch a real key. A subtly wrong
    implementation would produce signatures the updater cannot verify, and the
    first machine to notice would be someone else's.
    """
    for seed_hex, pub_hex, msg_hex, sig_hex in _RFC_8032_VECTORS:
        seed = bytes.fromhex(seed_hex)
        message = bytes.fromhex(msg_hex)
        expected_sig = bytes.fromhex(sig_hex)
        assert public_key(seed).hex() == pub_hex, "public key mismatch"
        assert sign(seed, message) == expected_sig, "signature mismatch"
        assert verify(bytes.fromhex(pub_hex), message, expected_sig)
        # A single flipped bit anywhere must fail.
        assert not verify(bytes.fromhex(pub_hex), message + b"x", expected_sig)
        bad = bytearray(expected_sig)
        bad[0] ^= 1
        assert not verify(bytes.fromhex(pub_hex), message, bytes(bad))


if __name__ == "__main__":
    import time

    selftest()
    seed = generate_private_key()
    pub = public_key(seed)
    body = b'{"version": "1.1.0"}' * 45          # about the size of a real manifest

    started = time.perf_counter()
    for _ in range(20):
        signature = sign(seed, body)
    sign_ms = (time.perf_counter() - started) / 20 * 1000

    started = time.perf_counter()
    for _ in range(20):
        ok = verify(pub, body, signature)
    verify_ms = (time.perf_counter() - started) / 20 * 1000

    print(f"RFC 8032 vectors pass. sign {sign_ms:.1f} ms, "
          f"verify {verify_ms:.1f} ms, ok={ok}")
