r"""Build the update manifest for a release and sign it with Ed25519.

The manifest is the only thing that tells an installed Mistery to go and run new
code, so it is the one file in this project that must not be forgeable. HTTPS is
not enough on its own: whoever controls the web server would otherwise control
every machine Mistery is installed on. So the manifest is signed with a key that
exists in two places only — a file the owner keeps, and the GitHub repository
secret MISTERY_SIGNING_KEY — and the updater carries nothing but the public half.

Build and sign (this is what .github/workflows/release.yml runs):

    python packaging\sign_manifest.py ^
        --package   packaging\out\Mistery-1.1.0-win64.zip ^
        --installer packaging\out\setup\MisterySetup.exe ^
        --base-url  https://github.com/DeroXP/Mistery/releases/download/v1.1.0 ^
        --notes     "Playlists, categories and the lyrics screensaver." ^
        --out       packaging\out\mistery-update.json

Check one, from anywhere, with only the public key:

    python packaging\sign_manifest.py --verify packaging\out\mistery-update.json

The file that ships is an envelope:

    {
      "schema": 1,
      "key_id": "3f9c1a2b",            first 8 hex of SHA-256(public key)
      "signature": "<base64, 64 bytes>",
      "manifest": "<base64 of the body's exact UTF-8 bytes>"
    }

The body travels base64-encoded on purpose. Signing "the JSON" would mean every
program in the chain agreeing on key order, spacing and unicode escaping
forever, and it would mean parsing attacker-shaped input before deciding whether
to trust it. This way the signed bytes are exactly the bytes, and a verifier can
check the signature *before* it parses anything.

MANIFEST_CONTEXT is prepended to the signed bytes so that a signature this key
made for some other purpose can never be replayed as an update manifest.

The body:

    {
      "schema": 1,
      "version": "1.1.0",
      "released": "2026-09-17T19:40:00Z",
      "notes": "one line, shown to the user",
      "package":   {"name": "...zip", "url": "https://...", "size": 1, "sha256": "..."},
      "installer": {"name": "MisterySetup.exe", "url": "https://...", "size": 1, "sha256": "..."}
    }

`package` is what the updater downloads and unpacks over an existing install.
`installer` is what the download button on the landing page points at.

The private key is read from an environment variable (MISTERY_SIGNING_KEY) or
from --key-file. It is never printed, never written anywhere, and never passed
on a command line, where it would sit in the shell history and in the process
list for anyone on the machine to read.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
PUBLIC_KEY_FILE = HERE / "public_key.txt"

sys.path.insert(0, str(HERE))
import ed25519  # noqa: E402  (the signing half; see its docstring)
from check_version import version_in_code  # noqa: E402

# These three have to match server/signing.py and updater/manifest verification
# byte for byte. They are written out here rather than imported so that signing
# a release does not depend on the web service's source being present — and
# packaging/test_signing.py asserts the copies are equal, so they cannot drift.
SCHEMA = 1
MANIFEST_CONTEXT = b"mistery-update-manifest/1\n"
MANIFEST_NAME = "mistery-update.json"

# What the updater will accept on the other end (updater/manifest.py). Checking
# it here means a bad release stops in the workflow, where somebody is watching,
# instead of being refused quietly on other people's PCs an hour later.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")   # it becomes a file name in update\
MAX_PACKAGE_BYTES = 1_000_000_000                    # the updater's ceiling


class Problem(Exception):
    """Stop the release. Printed as one plain message, without a traceback."""


# --- reading the things being released ---------------------------------------


def describe(path: Path, base_url: str | None, explicit_url: str | None) -> dict:
    """Name, size, SHA-256 and download URL for one release asset."""
    if not path.is_file():
        raise Problem(f"not there: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        # A megabyte at a time: the installer is small, but the update zip is
        # hundreds of megabytes and there is no reason to hold it in memory.
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if size == 0:
        raise Problem(f"empty file: {path}")
    if not SAFE_NAME.match(path.name):
        raise Problem(
            f"{path.name} is not a plain file name. The updater turns this into "
            f"a file name inside its own update folder and refuses anything "
            f"with a separator, a colon or a space in it."
        )
    if size > MAX_PACKAGE_BYTES:
        raise Problem(
            f"{path.name} is {size / 1e9:.2f} GB, over the {MAX_PACKAGE_BYTES / 1e9:.1f} GB "
            f"ceiling updater/manifest.py enforces. It would download and then be "
            f"thrown away on every machine."
        )

    if explicit_url:
        url = explicit_url
    elif base_url:
        url = base_url.rstrip("/") + "/" + path.name
    else:
        raise Problem(f"no URL for {path.name}: pass --base-url or an explicit URL")
    if not url.startswith("https://"):
        raise Problem(f"{url} is not https — the updater and the server both refuse it")
    return {"name": path.name, "url": url, "size": size, "sha256": digest.hexdigest()}


def read_private_key(key_env: str, key_file: str | None) -> bytes:
    """32 bytes, from a file or an environment variable. Base64 or hex."""
    if key_file:
        raw = Path(os.path.expandvars(key_file)).expanduser().read_text(encoding="utf-8")
        where = key_file
    else:
        raw = os.environ.get(key_env, "")
        where = f"${key_env}"
        if not raw.strip():
            raise Problem(
                f"no signing key in {where}. In GitHub Actions that means the "
                f"repository secret MISTERY_SIGNING_KEY is missing or empty — "
                f"see docs/SETUP.md, step 4."
            )
    text = raw.strip()
    try:
        if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
            key = bytes.fromhex(text)
        else:
            key = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise Problem(f"the key in {where} is not base64 or hex: {exc}") from None
    if len(key) != 32:
        raise Problem(
            f"the key in {where} decodes to {len(key)} bytes; an Ed25519 private "
            f"key is exactly 32. Did the secret get truncated, or is this a PEM "
            f"file? packaging/make_key.py writes the format this expects."
        )
    return key


def trusted_public_key() -> bytes | None:
    """The public key this repository ships, if it has been generated yet."""
    if not PUBLIC_KEY_FILE.is_file():
        return None
    text = PUBLIC_KEY_FILE.read_text(encoding="utf-8").strip()
    try:
        key = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        raise Problem(f"{PUBLIC_KEY_FILE} is not valid base64") from None
    if len(key) != 32:
        raise Problem(f"{PUBLIC_KEY_FILE} holds {len(key)} bytes, not 32")
    return key


def key_id(public_key: bytes) -> str:
    """Eight characters that name a key, so a human can tell two keys apart.

    A label, not a security property: nothing decides what to trust from this
    string. The same eight characters appear in updater/keys.py, in the server's
    /api/health, and in the envelope, so one glance says whether three programs
    mean the same key.
    """
    return hashlib.sha256(public_key).hexdigest()[:8]


# --- the envelope ------------------------------------------------------------


def build_envelope(body: dict, private_key: bytes) -> bytes:
    body_bytes = json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8")
    signature = ed25519.sign(private_key, MANIFEST_CONTEXT + body_bytes)
    envelope = {
        "schema": SCHEMA,
        "key_id": key_id(ed25519.public_key(private_key)),
        "signature": base64.b64encode(signature).decode("ascii"),
        "manifest": base64.b64encode(body_bytes).decode("ascii"),
    }
    return json.dumps(envelope, indent=2).encode("utf-8") + b"\n"


def verify_envelope(envelope_bytes: bytes, public_key: bytes) -> dict:
    """Return the manifest body, or raise. The order the other verifiers use.

    Nothing is trusted until the signature checks out: the body is decoded from
    base64 (which cannot execute anything), verified, and only then parsed.
    """
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    if envelope.get("schema") != SCHEMA:
        raise Problem(f"envelope schema {envelope.get('schema')!r}, expected {SCHEMA}")
    try:
        body_bytes = base64.b64decode(envelope["manifest"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise Problem(f"malformed envelope: {exc}") from None
    if not ed25519.verify(public_key, MANIFEST_CONTEXT + body_bytes, signature):
        raise Problem(
            "SIGNATURE DOES NOT VERIFY. Either this manifest was signed with a "
            "different key, or it was changed after signing. Do not publish it."
        )
    body = json.loads(body_bytes.decode("utf-8"))
    if body.get("schema") != SCHEMA:
        raise Problem(f"body schema {body.get('schema')!r}, expected {SCHEMA}")
    return body


def cross_check(envelope_bytes: bytes, public_key: bytes, body: dict) -> list[str]:
    """Hand the manifest to the other two verifiers in this repository.

    This script's own check uses the file that just signed, so it cannot catch
    the failure that actually matters: the signer and a verifier disagreeing
    about what was signed. server/signing.py and updater/manifest.py are
    separate implementations, both written verify-only, and between them they are
    every program that will ever read this document. Two more milliseconds each.

    A module that will not import is reported and not fatal — this is a second
    opinion. A module that imports and *refuses the manifest* is fatal, because
    that is a release nobody could install.
    """
    lines: list[str] = []

    sys.path.insert(0, str(REPO_ROOT / "server"))
    try:
        import signing  # noqa: PLC0415  (optional, and only wanted here)
    except ImportError as exc:
        lines.append(f"  ! could not cross-check against server/signing.py: {exc}")
    else:
        if signing.SCHEMA != SCHEMA or signing.MANIFEST_CONTEXT != MANIFEST_CONTEXT:
            raise Problem(
                f"server/signing.py reads a different shape: schema "
                f"{signing.SCHEMA!r} against {SCHEMA!r}, context "
                f"{signing.MANIFEST_CONTEXT!r} against {MANIFEST_CONTEXT!r}. One "
                f"side was changed without the other; this release would be one "
                f"the website rejects."
            )
        try:
            theirs = signing.verify_manifest(envelope_bytes, public_key)
        except signing.BadManifest as exc:
            raise Problem(f"server/signing.py refuses this manifest: {exc}") from None
        if theirs != body:
            raise Problem("server/signing.py read a different body out of this manifest")
        lines.append("  server/signing.py accepts it — the website will serve it")

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from updater import keys as updater_keys  # noqa: PLC0415
        from updater import manifest as updater_manifest  # noqa: PLC0415
    except ImportError as exc:
        lines.append(f"  ! could not cross-check against updater/manifest.py: {exc}")
        return lines

    trusted = updater_keys.trusted_keys()
    if not trusted:
        # Before make_key.py has been run there is nothing to check against, and
        # saying so is more useful than a check that passes by doing nothing.
        lines.append("  ? updater/keys.py carries no key yet, so the updater's own "
                     "check could not run")
        return lines
    try:
        theirs = updater_manifest.verify(envelope_bytes)
    except updater_manifest.Refused as exc:
        raise Problem(
            f"the updater in this build refuses this manifest: {exc}\n"
            f"That is every installed copy of Mistery refusing this release. "
            f"Usually it means updater/keys.py and packaging/public_key.txt hold "
            f"different keys."
        ) from None
    if theirs.get("version") != body["version"]:
        raise Problem("updater/manifest.py read a different version out of this manifest")
    lines.append(f"  updater/manifest.py accepts it with key {theirs.get('_key_id')} — "
                 f"installed copies will take it")
    return lines


# --- the two commands --------------------------------------------------------


def megabytes(count: int) -> str:
    return f"{count / 1024 / 1024:.1f} MB"


def do_sign(args: argparse.Namespace) -> int:
    ed25519.selftest()      # 14 ms, and it rules out a broken build of the maths

    version = version_in_code()
    if args.version and args.version.lstrip("v") != version:
        raise Problem(
            f"--version {args.version} but app/__init__.py says {version}. The "
            f"code is the one source of truth for the version; fix whichever is "
            f"wrong rather than signing a manifest that disagrees with the exe "
            f"inside it."
        )

    private_key = read_private_key(args.key_env, args.key_file)
    public_key = ed25519.public_key(private_key)

    trusted = trusted_public_key()
    if trusted is None:
        raise Problem(
            f"{PUBLIC_KEY_FILE} does not exist, so there is no way to tell "
            f"whether this key is the one installed copies of Mistery trust. Run "
            f"packaging/make_key.py once and commit public_key.txt "
            f"(docs/SETUP.md, step 3)."
        )
    if trusted != public_key:
        raise Problem(
            f"this signing key ({key_id(public_key)}) is not the key this "
            f"repository ships ({key_id(trusted)}). Signing with it would produce "
            f"an update every installed copy correctly refuses. Either the "
            f"repository secret holds the wrong key, or public_key.txt was "
            f"changed without shipping an updater that carries the new key."
        )

    notes = args.notes or ""
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8")
    # One line: the updater shows it in a notification, and Windows truncates
    # anything past about three lines anyway.
    notes = " ".join(notes.split())[:300]

    body = {
        "schema": SCHEMA,
        "version": version,
        "released": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": notes,
        "package": describe(Path(args.package), args.base_url, args.package_url),
        "installer": describe(Path(args.installer), args.base_url, args.installer_url),
    }

    out = Path(args.out)
    if out.is_dir():
        out = out / MANIFEST_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_envelope(body, private_key))

    # Read it back off the disk and check it with the public key alone. A
    # release that fails here is one nobody could install.
    envelope_bytes = out.read_bytes()
    try:
        checked = verify_envelope(envelope_bytes, trusted)
        if checked != body:
            raise Problem("the manifest read back does not match what was signed")
        second_opinions = cross_check(envelope_bytes, trusted, body)
    except Problem:
        # Leave nothing behind that a later step could pick up and publish. A
        # manifest that failed its own checks must not exist on disk.
        out.unlink(missing_ok=True)
        raise

    print(f"signed {out} with key {key_id(public_key)}")
    print(f"  version    {body['version']}   released {body['released']}")
    for part in ("package", "installer"):
        item = body[part]
        print(f"  {part:<10} {item['name']}  {megabytes(item['size'])}")
        print(f"             sha256 {item['sha256']}")
        print(f"             {item['url']}")
    if notes:
        print(f"  notes      {notes}")
    print(f"  manifest   {out.stat().st_size} bytes, signature verifies")
    for line in second_opinions:
        print(line)
    return 0


def do_verify(args: argparse.Namespace) -> int:
    ed25519.selftest()
    if args.public_key:
        text = args.public_key
        if Path(text).is_file():
            text = Path(text).read_text(encoding="utf-8")
        try:
            public_key = base64.b64decode(text.strip(), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise Problem(f"--public-key is not base64: {exc}") from None
        if len(public_key) != 32:
            raise Problem(f"--public-key decodes to {len(public_key)} bytes, not 32")
    else:
        public_key = trusted_public_key()
        if public_key is None:
            raise Problem(f"no --public-key given and {PUBLIC_KEY_FILE} does not exist")

    path = Path(args.verify)
    try:
        envelope_bytes = path.read_bytes()
    except OSError as exc:
        raise Problem(f"{path}: {exc}") from None
    body = verify_envelope(envelope_bytes, public_key)

    print(f"{path}: signature verifies against key {key_id(public_key)}")
    print(f"  version    {body['version']}   released {body.get('released', '?')}")
    for part in ("package", "installer"):
        item = body.get(part) or {}
        print(f"  {part:<10} {item.get('name', '?')}  "
              f"{megabytes(int(item.get('size', 0)))}")
        print(f"             sha256 {item.get('sha256', '?')}")
        print(f"             {item.get('url', '?')}")
    if body.get("notes"):
        print(f"  notes      {body['notes']}")
    for line in cross_check(envelope_bytes, public_key, body):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and sign Mistery's update manifest.")
    parser.add_argument("--verify", metavar="MANIFEST",
                        help="check an existing manifest instead of making one")
    parser.add_argument("--public-key", metavar="B64_OR_FILE",
                        help="key to check against (default: packaging/public_key.txt)")

    parser.add_argument("--version", help="X.Y.Z — must match app/__init__.py")
    parser.add_argument("--package", help="the update zip the updater downloads")
    parser.add_argument("--installer", help="MisterySetup.exe")
    parser.add_argument("--base-url", help="where the release assets will live")
    parser.add_argument("--package-url", help="full URL for the zip, instead of --base-url")
    parser.add_argument("--installer-url", help="full URL for the installer")
    parser.add_argument("--notes", default="", help="one line, shown to the user")
    parser.add_argument("--notes-file", help="read that line from a file instead")
    parser.add_argument("--out", default=str(HERE / "out" / MANIFEST_NAME),
                        help="default: %(default)s")
    parser.add_argument("--key-env", default="MISTERY_SIGNING_KEY",
                        help="environment variable holding the private key "
                             "(default: %(default)s)")
    parser.add_argument("--key-file", help="read the private key from this file instead")

    args = parser.parse_args(argv)
    try:
        if args.verify:
            return do_verify(args)
        missing = [name for name in ("package", "installer") if not getattr(args, name)]
        if missing:
            parser.error("signing needs " + " and ".join("--" + name for name in missing))
        return do_sign(args)
    except Problem as exc:
        print(f"\nsign_manifest: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
