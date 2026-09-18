r"""Does a signed manifest survive the trip to the three programs that read it?

    python packaging\test_signing.py

The signing key is the one thing in this project that cannot be fixed after the
fact. If the signer and the updater disagree about a single byte of what was
signed, every installed copy of Mistery refuses every update from then on, and
the only way out is asking people to download an installer by hand. So this
makes a throwaway key, signs a real manifest with the real script, and hands the
result to all three verifiers that exist:

    packaging/ed25519.py    the signing machine's own check
    server/signing.py       the web service, verify-only by design
    updater/manifest.py     MisteryUpdate.exe, the one that matters

Then it changes one byte at a time and requires all three to refuse. A verifier
that accepts a tampered manifest is worse than no verifier at all, because it
looks like security.

Nothing here touches %APPDATA%, the repository's own public_key.txt, or any
installed copy of anything: the key, the fake release files and the manifest all
live in a temporary folder that is deleted at the end.

49 checks in 0.6 s, measured. Exit code 0 means every one of them passed.
"""

from __future__ import annotations

import base64
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "server"))

import ed25519                    # noqa: E402  packaging's signing half
import sign_manifest              # noqa: E402
import check_version              # noqa: E402

failures: list[str] = []
timings: dict[str, float] = {}


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if condition else 'FAIL'}  {name}{'  - ' + detail if detail else ''}")
    if not condition:
        failures.append(name)
    return condition


def optional_module(name: str, why: str):
    """Import a verifier, or say plainly that it was not tested rather than pass."""
    try:
        return importlib.import_module(name)
    except Exception as exc:                               # noqa: BLE001
        print(f"  ----  {name} could not be imported ({exc})")
        print(f"        {why}")
        failures.append(f"{name} not importable")
        return None


def main() -> int:
    server_signing = optional_module(
        "signing", "The web service verifies every manifest before it serves it; "
                   "without this file that check is untested.")
    updater_manifest = optional_module(
        "updater.manifest", "This is the verifier that decides what runs on "
                            "someone else's PC. A release must not be cut "
                            "without it passing.")
    updater_keys = optional_module("updater.keys", "The updater's trusted key list.")

    print("\nthe three copies of the wire format agree")
    if server_signing:
        check("server/signing.py SCHEMA", server_signing.SCHEMA == sign_manifest.SCHEMA,
              f"{server_signing.SCHEMA} == {sign_manifest.SCHEMA}")
        check("server/signing.py context",
              server_signing.MANIFEST_CONTEXT == sign_manifest.MANIFEST_CONTEXT,
              repr(server_signing.MANIFEST_CONTEXT))
    if updater_manifest:
        check("updater/manifest.py SCHEMA",
              updater_manifest.SCHEMA == sign_manifest.SCHEMA,
              f"{updater_manifest.SCHEMA} == {sign_manifest.SCHEMA}")
        check("updater/manifest.py context",
              updater_manifest.SIGNING_CONTEXT == sign_manifest.MANIFEST_CONTEXT,
              repr(updater_manifest.SIGNING_CONTEXT))

    print("\nthe arithmetic, against RFC 8032's own test vectors")
    started = time.perf_counter()
    ed25519.selftest()
    timings["RFC 8032 vectors (3)"] = (time.perf_counter() - started) * 1000
    check("packaging/ed25519.py selftest", True, f"{timings['RFC 8032 vectors (3)']:.0f} ms")

    # A signature made here, checked by each verifier's own low-level code. This
    # is the check that catches a copy of the maths going subtly wrong.
    seed = ed25519.generate_private_key()
    public = ed25519.public_key(seed)
    message = b"mistery cross-implementation check"
    signature = ed25519.sign(seed, message)
    if server_signing:
        check("server verify_bytes accepts our signature",
              server_signing.verify_bytes(public, message, signature))
        check("server verify_bytes refuses a flipped bit",
              not server_signing.verify_bytes(public, message + b"x", signature))
    updater_ed25519 = optional_module("updater.ed25519", "The updater's own arithmetic.")
    if updater_ed25519:
        check("updater verify accepts our signature",
              updater_ed25519.verify(public, message, signature))
        check("updater verify refuses a flipped bit",
              not updater_ed25519.verify(public, message + b"x", signature))
        # And the other direction: what the updater's copy signs, ours accepts.
        check("we accept what the updater's copy signs",
              ed25519.verify(public, message, updater_ed25519.sign(seed, message)))

    work = Path(tempfile.mkdtemp(prefix="mistery-signing-test-"))
    try:
        return run_end_to_end(work, server_signing, updater_manifest, updater_keys)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_end_to_end(work: Path, server_signing, updater_manifest, updater_keys) -> int:
    print("\nmake_key.py, for real")
    key_file = work / "test-signing.key"
    started = time.perf_counter()
    made = subprocess.run(
        [sys.executable, str(HERE / "make_key.py"), "--out", str(key_file),
         "--no-write-public"],
        capture_output=True, text=True)
    timings["make_key.py"] = (time.perf_counter() - started) * 1000
    check("make_key.py exits 0", made.returncode == 0, made.stderr.strip()[:200])
    check("the key file is one line", key_file.is_file()
          and key_file.read_bytes().count(b"\n") == 1)
    check("no CR in the key file", b"\r" not in key_file.read_bytes(),
          "gh secret set sends this file verbatim")
    private_key = base64.b64decode(key_file.read_text(encoding="ascii").strip(), validate=True)
    check("the key is 32 bytes", len(private_key) == 32, f"{len(private_key)} bytes")
    public_key = ed25519.public_key(private_key)
    check("make_key printed the public key it wrote",
          base64.b64encode(public_key).decode("ascii") in made.stdout)
    check("make_key did not print the private key",
          base64.b64encode(private_key).decode("ascii") not in made.stdout,
          "the private key must never reach a build log")

    refused = subprocess.run(
        [sys.executable, str(HERE / "make_key.py"), "--out",
         str(REPO_ROOT / "packaging" / "should-never-exist.key")],
        capture_output=True, text=True)
    check("make_key refuses to write a key inside the repository",
          refused.returncode != 0
          and not (REPO_ROOT / "packaging" / "should-never-exist.key").exists(),
          refused.stderr.strip().splitlines()[1] if refused.stderr.strip() else "")

    print("\nsign_manifest.py, on real files")
    # Two files that stand in for a release: the sizes are nonsense, the hashing
    # is not — sign_manifest reads them a megabyte at a time like the real ones.
    version = check_version.version_in_code()
    package = work / f"Mistery-{version}-win64.zip"
    package.write_bytes(b"not really a zip, but it hashes the same way" * 1000)
    installer = work / "MisterySetup.exe"
    installer.write_bytes(b"not really an installer" * 500)
    manifest_path = work / "mistery-update.json"

    # Point the script at a public_key.txt in the temp folder. The repository's
    # own must not be created or touched by a test.
    real_public_key_file = sign_manifest.PUBLIC_KEY_FILE
    sign_manifest.PUBLIC_KEY_FILE = work / "public_key.txt"
    sign_manifest.PUBLIC_KEY_FILE.write_text(
        base64.b64encode(public_key).decode("ascii") + "\n", encoding="ascii", newline="\n")
    try:
        base_url = "https://github.com/DeroXP/Mistery/releases/download/v" + version
        started = time.perf_counter()
        code = sign_manifest.main([
            "--package", str(package), "--installer", str(installer),
            "--base-url", base_url, "--version", version,
            "--notes", "  a note   with   untidy spacing  ",
            "--out", str(manifest_path), "--key-file", str(key_file),
        ])
        timings["sign a manifest"] = (time.perf_counter() - started) * 1000
        check("sign_manifest exits 0", code == 0)
        check("the manifest was written", manifest_path.is_file())
        raw = manifest_path.read_bytes()
        timings["manifest size (bytes)"] = len(raw)

        envelope = json.loads(raw.decode("utf-8"))
        check("envelope has the four fields the verifiers read",
              set(envelope) == {"schema", "key_id", "signature", "manifest"},
              ", ".join(sorted(envelope)))
        body = json.loads(base64.b64decode(envelope["manifest"]).decode("utf-8"))
        check("the body carries the version from app/__init__.py",
              body["version"] == version, version)
        check("notes are squashed to one line", body["notes"] == "a note with untidy spacing",
              repr(body["notes"]))
        check("the package url is the release asset",
              body["package"]["url"] == f"{base_url}/{package.name}")
        check("the sha256 is the file's own",
              body["package"]["sha256"] == _sha256(package))

        print("\nall three verifiers accept it")
        check("packaging/sign_manifest.py", bool(
            sign_manifest.verify_envelope(raw, public_key)))
        if server_signing:
            started = time.perf_counter()
            server_body = server_signing.verify_manifest(raw, public_key)
            timings["server verify"] = (time.perf_counter() - started) * 1000
            check("server/signing.py", server_body == body,
                  f"{timings['server verify']:.1f} ms")
        if updater_manifest and updater_keys:
            # The updater trusts what was compiled into it. Point that at the
            # throwaway key for the length of this test.
            updater_keys.UPDATE_PUBLIC_KEY = base64.b64encode(public_key).decode("ascii")
            started = time.perf_counter()
            updater_body = updater_manifest.verify(raw)
            timings["updater verify"] = (time.perf_counter() - started) * 1000
            check("updater/manifest.py", updater_body["version"] == version,
                  f"{timings['updater verify']:.1f} ms")
            check("updater names the key it verified with",
                  updater_body.get("_key_id") == sign_manifest.key_id(public_key),
                  str(updater_body.get("_key_id")))

        print("\nand all three refuse a manifest that was changed")
        for name, mutated in _tampered(raw, envelope, body, public_key):
            refusals = [_refuses_packaging(mutated, public_key)]
            if server_signing:
                refusals.append(_refuses(server_signing.verify_manifest,
                                         (mutated, public_key), server_signing.BadManifest))
            if updater_manifest:
                refusals.append(_refuses(updater_manifest.verify, (mutated,),
                                         updater_manifest.Refused))
            check(name, all(refusals),
                  "" if all(refusals) else f"accepted by {refusals.count(True)} of "
                                           f"{len(refusals)} verifiers")

        print("\nthe wrong key is refused, the right one is not")
        other_public = ed25519.public_key(ed25519.generate_private_key())
        check("another key's holder cannot verify this manifest",
              _refuses_packaging(raw, other_public))
        if server_signing:
            check("server refuses it with another key",
                  _refuses(server_signing.verify_manifest, (raw, other_public),
                           server_signing.BadManifest))

        print("\nsign_manifest refuses to sign what it should not")
        check("a key that is not the shipped public key",
              sign_manifest.main([
                  "--package", str(package), "--installer", str(installer),
                  "--base-url", base_url, "--out", str(work / "no.json"),
                  "--key-file", str(_other_key_file(work)),
              ]) == 1)
        check("a --version that disagrees with app/__init__.py",
              sign_manifest.main([
                  "--package", str(package), "--installer", str(installer),
                  "--base-url", base_url, "--version", "9.9.9",
                  "--out", str(work / "no.json"), "--key-file", str(key_file),
              ]) == 1)
        check("an http base URL",
              sign_manifest.main([
                  "--package", str(package), "--installer", str(installer),
                  "--base-url", "http://example.invalid/x",
                  "--out", str(work / "no.json"), "--key-file", str(key_file),
              ]) == 1)
        check("nothing was written when it refused",
              not (work / "no.json").exists())
    finally:
        sign_manifest.PUBLIC_KEY_FILE = real_public_key_file

    print("\ncheck_version.py, the guard on the tag")
    check("the right tag passes", check_version.main([f"v{version}"]) == 0)
    check("a different version fails", check_version.main(["v9.9.9"]) == 1)
    check("a tag with no v fails", check_version.main([version]) == 1)
    check("nonsense fails", check_version.main(["release-2"]) == 1)

    print("\nmeasured")
    for name, value in timings.items():
        print(f"  {name:<28} {value:>8.1f}" + ("" if "bytes" in name else " ms"))

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + "; ".join(failures))
        return 1
    print("all checks passed")
    return 0


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _other_key_file(work: Path) -> Path:
    path = work / "someone-elses.key"
    path.write_text(
        base64.b64encode(ed25519.generate_private_key()).decode("ascii") + "\n",
        encoding="ascii", newline="\n")
    return path


def _refuses_packaging(raw: bytes, public_key: bytes) -> bool:
    try:
        sign_manifest.verify_envelope(raw, public_key)
    except Exception:                                      # noqa: BLE001
        return True
    return False


def _refuses(function, args: tuple, expected: type[Exception]) -> bool:
    """True if calling this verifier refused. An unexpected exception counts.

    A verifier that crashes on a hostile document has still refused it — what
    must never happen is returning a body.
    """
    try:
        function(*args)
    except expected:
        return True
    except Exception:                                      # noqa: BLE001
        return True
    return False


def _tampered(raw: bytes, envelope: dict, body: dict, public_key: bytes):
    """One realistic attack per entry: (what it is, the bytes that carry it).

    These are the shapes a compromised web host or a middlebox would try. Each
    one has to be refused by every verifier — not logged, not repaired, refused.
    """
    def rewrap(changes: dict) -> bytes:
        return json.dumps({**envelope, **changes}, indent=2).encode("utf-8")

    # The point of the whole exercise: a new body under the old signature.
    forged_body = dict(body)
    forged_body["package"] = dict(body["package"])
    forged_body["package"]["url"] = "https://example.invalid/not-mistery.zip"
    yield ("a different download URL under the same signature",
           rewrap({"manifest": base64.b64encode(
               json.dumps(forged_body, indent=2).encode("utf-8")).decode("ascii")}))

    forged_hash = dict(body)
    forged_hash["package"] = dict(body["package"])
    forged_hash["package"]["sha256"] = "0" * 64
    yield ("a different sha256 under the same signature",
           rewrap({"manifest": base64.b64encode(
               json.dumps(forged_hash, indent=2).encode("utf-8")).decode("ascii")}))

    higher = dict(body, version="99.0.0")
    yield ("a higher version number under the same signature",
           rewrap({"manifest": base64.b64encode(
               json.dumps(higher, indent=2).encode("utf-8")).decode("ascii")}))

    flipped = bytearray(base64.b64decode(envelope["signature"]))
    flipped[0] ^= 1
    yield ("one flipped bit in the signature",
           rewrap({"signature": base64.b64encode(bytes(flipped)).decode("ascii")}))

    # RFC 8032 step 3: S must be below the group order. Adding L to it produces a
    # second encoding that naive implementations accept, which would break "the
    # manifest I checked is the manifest I stored".
    signature = base64.b64decode(envelope["signature"])
    s_value = int.from_bytes(signature[32:], "little")
    mauled = signature[:32] + ((s_value + ed25519._L) % (1 << 256)).to_bytes(32, "little")
    yield ("a non-canonical S (signature + the group order)",
           rewrap({"signature": base64.b64encode(mauled).decode("ascii")}))

    yield ("a truncated signature",
           rewrap({"signature": base64.b64encode(signature[:32]).decode("ascii")}))
    yield ("no signature at all",
           json.dumps({k: v for k, v in envelope.items() if k != "signature"},
                      indent=2).encode("utf-8"))
    yield ("a schema this side does not speak", rewrap({"schema": 99}))
    yield ("the body replaced with something that is not JSON",
           rewrap({"manifest": base64.b64encode(b"nonsense").decode("ascii")}))
    yield ("an empty document", b"")
    yield ("a document that is not JSON", b"<!doctype html><title>404</title>")


if __name__ == "__main__":
    raise SystemExit(main())
