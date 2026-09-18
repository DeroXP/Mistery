r"""Everything the updater claims, proved against a real frozen exe.

    "…\build_env\Scripts\python.exe" packaging\test_updater.py

It builds two real MisteryUpdate.exe files with PyInstaller (an old one and a
new one, carrying a throwaway signing key made for this run), puts up a real
HTTPS server on localhost with a self-signed certificate, signs a real manifest
with packaging/sign_manifest.py, and then applies a real update to a real
install folder — one it makes under dist\testroot-<pid>, never the one anybody
uses. Then it proves each refusal: a tampered manifest, a wrong hash, a wrong
size, an older version, a zip that tries to escape the folder, Mistery running,
and Mistery having quit five minutes ago.

Three things it needs beyond the standard library, all of them in the build
environment this project already has: PyInstaller, and `cryptography` for the
self-signed certificate and for checking the hand-written Ed25519 against a
second implementation.

Nothing here touches %APPDATA%\Mistery or the real install. The updater under
test is pointed at a throwaway install with MISTERY_INSTALL_DIR, at a throwaway
data folder with MISTERY_DATA_DIR, and at a mutex name nothing else uses with
MISTERY_TEST_MUTEX_NAME — needed because the mutex Mistery holds is per Windows
user, and on the machine this was written on the real Mistery is usually open.
The one scheduled task it registers is called MisteryUpdateTest-<pid> and is
deleted before the script returns, whatever happens.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import datetime as dt
import functools
import hashlib
import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import ed25519                              # noqa: E402  packaging/ed25519.py

OLD_VERSION = "1.0.0"
NEW_VERSION = "1.1.0"
SIGNING_CONTEXT = b"mistery-update-manifest/1\n"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


# --- the throwaway world -----------------------------------------------------


def build_source_copy(src: Path, public_key_b64: str, version: str) -> None:
    """A copy of just enough of the repository to build an updater from.

    A copy rather than the repository itself because the key has to be compiled
    into the exe, and editing updater/keys.py in place would leave a test key in
    the tree somebody later builds a release from.
    """
    src.mkdir(parents=True, exist_ok=True)
    for folder in ("updater",):
        shutil.copytree(ROOT / folder, src / folder, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    (src / "packaging").mkdir(exist_ok=True)
    # Every packaging script, because they import each other (sign_manifest.py
    # wants check_version.py) and because copying the lot is one line that does
    # not have to be kept up to date as the release tooling grows.
    for script in HERE.glob("*.py"):
        shutil.copy2(script, src / "packaging" / script.name)
    # server/signing.py is the third implementation of the manifest format, and
    # sign_manifest.py cross-checks against it when it is there. Worth having.
    if (ROOT / "server" / "signing.py").is_file():
        (src / "server").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "server" / "signing.py", src / "server" / "signing.py")
    (src / "app").mkdir(exist_ok=True)
    (src / "app" / "__init__.py").write_text(
        f'"""Mistery."""\n\n__version__ = "{version}"\n', encoding="utf-8")
    (src / "assets").mkdir(exist_ok=True)
    if (ROOT / "assets" / "icon.ico").is_file():
        shutil.copy2(ROOT / "assets" / "icon.ico", src / "assets" / "icon.ico")

    keys_file = src / "updater" / "keys.py"
    text = keys_file.read_text(encoding="utf-8")
    text = text.replace('UPDATE_PUBLIC_KEY = ""',
                        f'UPDATE_PUBLIC_KEY = "{public_key_b64}"')
    keys_file.write_text(text, encoding="utf-8")
    (src / "packaging" / "public_key.txt").write_text(public_key_b64 + "\n",
                                                      encoding="utf-8")


def build_updater(src: Path, out: Path) -> Path:
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(src / "packaging" / "build_updater.py"), "--out", str(out)],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, result.stderr)
        raise SystemExit("could not build the updater")
    exe = out / "dist" / "Mistery" / "MisteryUpdate.exe"
    print(f"  built {exe.name}  {exe.stat().st_size / 1e6:.1f} MB  "
          f"in {time.monotonic() - started:.0f} s")
    return exe


def make_install(install: Path, updater_exe: Path) -> None:
    """A believable install folder at 1.0.0: an app, an updater, a payload."""
    install.mkdir(parents=True, exist_ok=True)
    (install / "_internal").mkdir(exist_ok=True)
    (install / "runtime").mkdir(exist_ok=True)

    real_app = ROOT / "packaging" / "out" / "dist" / "Mistery" / "Mistery.exe"
    if real_app.is_file():
        # The frozen app if this machine has built one: a 7.7 MB exe is a more
        # honest thing to rename-and-replace than a text file.
        shutil.copy2(real_app, install / "Mistery.exe")
    else:
        (install / "Mistery.exe").write_bytes(b"MZ" + b"old app\n" * 1000)
    shutil.copy2(updater_exe, install / "MisteryUpdate.exe")
    (install / "version.txt").write_text(OLD_VERSION + "\n", encoding="utf-8")
    (install / "_internal" / "marker.txt").write_text("payload 1.0.0\n", encoding="utf-8")
    (install / "_internal" / "keep-me.txt").write_text("not in the new build\n",
                                                       encoding="utf-8")
    # mpv is fetched by the installer and is never in an update: 120 MB of it
    # would be in every download for no reason. This file proves it stays.
    (install / "runtime" / "mpv.exe").write_bytes(b"pretend mpv\n")


def make_payload(payload: Path, new_updater: Path, install: Path) -> None:
    """What the new release ships: the app, the updater, version.txt, _internal."""
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "_internal").mkdir(exist_ok=True)
    app_bytes = (install / "Mistery.exe").read_bytes() + b"\n-- 1.1.0 --\n"
    (payload / "Mistery.exe").write_bytes(app_bytes)
    shutil.copy2(new_updater, payload / "MisteryUpdate.exe")
    (payload / "version.txt").write_text(NEW_VERSION + "\n", encoding="utf-8")
    (payload / "_internal" / "marker.txt").write_text("payload 1.1.0\n", encoding="utf-8")
    (payload / "_internal" / "added.txt").write_text("new in 1.1.0\n", encoding="utf-8")


def zip_tree(tree: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(tree).as_posix())
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(folder: Path) -> dict[str, tuple[int, float]]:
    """Every file under a folder as (size, mtime) — for proving nothing moved."""
    out = {}
    if folder.is_dir():
        for path in folder.rglob("*"):
            if path.is_file():
                stat = path.stat()
                out[str(path.relative_to(folder))] = (stat.st_size, stat.st_mtime)
    return out


def swap_in_from_main():
    """main.py's own _swap_in_new_updater, compiled out of the shipping file.

    Compiled out rather than imported because importing main.py imports PySide6
    and app.config, and app.config builds Settings() on import — which writes a
    settings.json. Creating a data folder is the one thing these tests must
    never do (updater/paths.py's docstring says why). So the shipping function
    is what runs here: if it is ever dropped from main.py the staged updater
    stays staged for ever, and this raises instead of quietly testing a copy.
    """
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_swap_in_new_updater":
            namespace: dict = {"sys": sys, "Path": Path}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"),
                 namespace)
            return namespace["_swap_in_new_updater"]
    raise AssertionError("main.py no longer defines _swap_in_new_updater — "
                        "MisteryUpdate.exe can never update itself without it")


def main_calls_swap_first() -> bool:
    """Whether main() calls it, and calls it before anything else.

    Having the function in main.py is not enough: it has to run before the
    single-instance check, or a second Mistery started while the first is open
    exits without ever getting there.
    """
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            body = [statement for statement in node.body
                    if not (isinstance(statement, ast.Expr)
                            and isinstance(statement.value, ast.Constant))]
            first = body[0] if body else None
            return (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Call)
                    and getattr(first.value.func, "id", "") == "_swap_in_new_updater")
    return False


# --- signing -----------------------------------------------------------------


def sign_envelope(body: dict, private_key: bytes) -> bytes:
    """The same envelope packaging/sign_manifest.py writes, for bodies it would
    (rightly) refuse to produce — a wrong hash, a wrong size, an old version."""
    body_bytes = json.dumps(body, indent=2, sort_keys=False).encode("utf-8")
    signature = ed25519.sign(private_key, SIGNING_CONTEXT + body_bytes)
    envelope = {
        "schema": 1,
        "key_id": hashlib.sha256(ed25519.public_key(private_key)).hexdigest()[:8],
        "signature": base64.b64encode(signature).decode("ascii"),
        "manifest": base64.b64encode(body_bytes).decode("ascii"),
    }
    return json.dumps(envelope, indent=2).encode("utf-8") + b"\n"


def body_of(manifest_bytes: bytes) -> dict:
    envelope = json.loads(manifest_bytes.decode("utf-8"))
    return json.loads(base64.b64decode(envelope["manifest"]).decode("utf-8"))


# --- the arithmetic everything else rests on ---------------------------------


def test_ed25519() -> None:
    """The vendored copy, checked three ways.

    It is the one piece of security-critical code in this project that is not a
    call into something someone else maintains, so: it must be the same code as
    packaging/ed25519.py (a copy that has drifted is worse than no copy), it
    must agree with that copy on random keys, and it must agree with
    `cryptography`'s Ed25519, which is OpenSSL's.
    """
    import ast
    import secrets

    from updater import ed25519 as vendored

    def shape(path: Path) -> str:
        """The file as a syntax tree, minus docstrings and the __main__ block.

        Not a byte comparison: the two files carry different docstrings by
        design, and a comment improved in one of them is not drift. A changed
        constant or a dropped check is, and that shows up here.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in [tree, *ast.walk(tree)]:
            inner = getattr(node, "body", None)
            if isinstance(inner, list) and inner and isinstance(inner[0], ast.Expr) \
                    and isinstance(getattr(inner[0], "value", None), ast.Constant) \
                    and isinstance(inner[0].value.value, str):
                inner.pop(0)
        body = [node for node in tree.body
                if not (isinstance(node, ast.If)
                        and "__main__" in ast.dump(node.test))]
        return ast.dump(ast.Module(body=body, type_ignores=[]))

    check(shape(ROOT / "updater" / "ed25519.py") == shape(HERE / "ed25519.py"),
          "updater/ed25519.py is the same code as packaging/ed25519.py")

    vendored.selftest()
    ed25519.selftest()
    check(True, "both copies pass the RFC 8032 test vectors")

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)

    agreed = True
    for _ in range(25):
        seed = secrets.token_bytes(32)
        message = secrets.token_bytes(secrets.randbelow(2000) + 1)
        theirs = Ed25519PrivateKey.from_private_bytes(seed)
        their_public = theirs.public_key().public_bytes_raw()
        their_signature = theirs.sign(message)
        agreed &= ed25519.public_key(seed) == their_public
        agreed &= ed25519.sign(seed, message) == their_signature
        agreed &= vendored.verify(their_public, message, their_signature)
        # And the other way: OpenSSL has to accept what we produce.
        try:
            Ed25519PublicKey.from_public_bytes(their_public).verify(
                ed25519.sign(seed, message), message)
        except Exception:
            agreed = False
        mauled = bytearray(their_signature)
        mauled[-1] ^= 0x80
        agreed &= not vendored.verify(their_public, message, bytes(mauled))
    check(agreed, "25 random keys: the same public keys, the same signatures, "
                  "and each verifies the other's (cross-checked with cryptography)")

    started = time.perf_counter()
    seed = secrets.token_bytes(32)
    public, message = ed25519.public_key(seed), b'{"version":"1.1.0"}' * 30
    signature = ed25519.sign(seed, message)
    for _ in range(20):
        vendored.verify(public, message, signature)
    print(f"  verify costs {(time.perf_counter() - started) / 20 * 1000:.1f} ms "
          f"per manifest")


# --- an HTTPS server on localhost --------------------------------------------


def make_certificate(folder: Path) -> tuple[Path, Path]:
    """A self-signed certificate for localhost, valid for a day.

    The updater insists on https and checks the certificate, with no way to
    turn that off — an updater with a "skip TLS" switch is an updater whose
    switch somebody will find. So the test brings its own certificate authority
    instead, and points the child processes at it with SSL_CERT_FILE, which is
    what OpenSSL reads through ssl.create_default_context().
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_file = folder / "test-cert.pem"
    key_file = folder / "test-key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return cert_file, key_file


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the manifest and the zip, says nothing, and keeps a tally.

    The tally is how "--check-now with updates switched off made no request at
    all" is proved: the updater's own log says what it decided, not whether it
    reached out before deciding it.
    """

    served: list[str] = []

    def log_message(self, *args) -> None:       # the test prints its own lines
        QuietHandler.served.append(self.requestline)


def serve(folder: Path, cert: Path, key: Path) -> tuple[http.server.HTTPServer, int]:
    handler = functools.partial(QuietHandler, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# --- running the updater -----------------------------------------------------


class Updater:
    def __init__(self, install: Path, data: Path, cert: Path, mutex: str) -> None:
        self.install = install
        self.data = data
        self.environment = dict(os.environ)
        self.environment.update({
            "MISTERY_INSTALL_DIR": str(install),
            "MISTERY_DATA_DIR": str(data),
            "MISTERY_TEST_MUTEX_NAME": mutex,
            "SSL_CERT_FILE": str(cert),
        })

    def run(self, *args: str, timeout: int = 180) -> int:
        exe = self.install / "MisteryUpdate.exe"
        finished = subprocess.run([str(exe), *args], env=self.environment,
                                  capture_output=True, text=True, timeout=timeout)
        return finished.returncode

    def last_log(self, lines: int = 1) -> str:
        path = self.install / "update.log"
        if not path.is_file():
            return ""
        return "".join(path.read_text(encoding="utf-8").splitlines(True)[-lines:]).strip()


def main() -> int:
    if os.name != "nt":
        print("This is a Windows updater; the test only means anything on Windows.")
        return 1

    pid = os.getpid()
    root = ROOT.parent / f"testroot-{pid}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    print(f"working in {root}")

    mutex_name = f"Local\\MisteryUpdateTest-{pid}"
    task_name = f"MisteryUpdateTest-{pid}"
    server = None
    task_registered = False
    try:
        heading("Ed25519")
        test_ed25519()

        # --- the key, the exes, the install ---------------------------------
        heading("Building")
        private_key = ed25519.generate_private_key()
        public_b64 = base64.b64encode(ed25519.public_key(private_key)).decode()
        key_file = root / "test-signing.key"
        key_file.write_text(base64.b64encode(private_key).decode() + "\n", encoding="ascii")

        old_src, new_src = root / "src-old", root / "src-new"
        build_source_copy(old_src, public_b64, OLD_VERSION)
        build_source_copy(new_src, public_b64, NEW_VERSION)
        old_exe = build_updater(old_src, root / "build-old")
        new_exe = build_updater(new_src, root / "build-new")
        check(sha256(old_exe) != sha256(new_exe), "the two updaters are different files")

        install = root / "install"
        data = root / "appdata"
        data.mkdir()
        make_install(install, old_exe)
        pristine = root / "install-pristine"
        shutil.copytree(install, pristine)
        install_size = sum(p.stat().st_size for p in install.rglob("*") if p.is_file())
        print(f"  install folder   {install_size / 1e6:.1f} MB")

        payload = root / "payload"
        make_payload(payload, new_exe, install)
        www = root / "www"
        www.mkdir()
        package = zip_tree(payload, www / f"Mistery-{NEW_VERSION}-win64.zip")
        (www / "MisterySetup.exe").write_bytes(b"MZ pretend installer\n")
        print(f"  update zip       {package.stat().st_size / 1e6:.1f} MB")

        # --- the server -----------------------------------------------------
        cert, key = make_certificate(root)
        server, port = serve(www, cert, key)
        base_url = f"https://localhost:{port}"
        print(f"  serving {www} at {base_url}")

        # --- the real signing tool ------------------------------------------
        heading("Signing a manifest with packaging/sign_manifest.py")
        signed = root / "manifest.json"
        signing = subprocess.run(
            [sys.executable, str(new_src / "packaging" / "sign_manifest.py"),
             "--version", NEW_VERSION, "--package", str(package),
             "--installer", str(www / "MisterySetup.exe"), "--base-url", base_url,
             "--notes", "Playlists, categories and the lyrics screensaver.",
             "--key-file", str(key_file), "--out", str(signed)],
            capture_output=True, text=True, cwd=str(new_src))
        check(signing.returncode == 0, "sign_manifest.py signed the manifest",
              signing.stderr.strip().splitlines()[-1] if signing.returncode else "")
        if signing.returncode != 0:
            print(signing.stdout, signing.stderr)
            return 1
        good_manifest = signed.read_bytes()
        good_body = body_of(good_manifest)
        check(good_body["package"]["sha256"] == sha256(package),
              "the manifest's SHA-256 is the zip's")

        updater = Updater(install, data, cert, mutex_name)
        served = www / "manifest.json"
        (install / "updater.json").write_text(json.dumps({
            "manifest_url": f"{base_url}/manifest.json", "auto_update": True,
        }, indent=2), encoding="utf-8")

        def restore(last_quit_minutes: float = 40.0) -> None:
            """Put the 1.0.0 install back and set the clock on last-run.json."""
            shutil.rmtree(install, ignore_errors=True)
            shutil.copytree(pristine, install)
            (install / "updater.json").write_text(json.dumps({
                "manifest_url": f"{base_url}/manifest.json", "auto_update": True,
            }, indent=2), encoding="utf-8")
            for stale in data.glob("*"):
                stale.unlink()
            (data / "last-run.json").write_text(json.dumps({
                "quit": time.time() - last_quit_minutes * 60, "version": OLD_VERSION,
            }), encoding="utf-8")

        # --- 1. the happy path ----------------------------------------------
        heading("Applying a real update")
        restore()
        served.write_bytes(good_manifest)
        data_before = snapshot(data)
        started = time.monotonic()
        code = updater.run()
        took = time.monotonic() - started
        check(code == 10, f"exit code is 10 (updated), took {took:.1f} s", f"got {code}")
        check((install / "version.txt").read_text(encoding="utf-8").strip() == NEW_VERSION,
              "version.txt says 1.1.0")
        check(sha256(install / "Mistery.exe") == sha256(payload / "Mistery.exe"),
              "Mistery.exe is the new build")
        check((install / "_internal" / "marker.txt").read_text(encoding="utf-8").strip()
              == "payload 1.1.0", "_internal\\marker.txt was replaced")
        check((install / "_internal" / "added.txt").is_file(),
              "_internal\\added.txt was added")
        check((install / "_internal" / "keep-me.txt").is_file(),
              "a file the new build does not have was left alone")
        check((install / "runtime" / "mpv.exe").is_file(),
              "runtime\\mpv.exe was not touched")
        check((install / "Mistery.exe.old").is_file(),
              "the old Mistery.exe is beside it as .old")
        staged = install / "MisteryUpdate.exe.new"
        check(staged.is_file() and sha256(staged) == sha256(new_exe),
              "the new updater is staged as MisteryUpdate.exe.new")
        check(sha256(install / "MisteryUpdate.exe") == sha256(old_exe),
              "the running updater did not replace itself")
        check(not (install / "update" / f"Mistery-{NEW_VERSION}-win64.zip").exists(),
              "the download was cleaned up")
        check(snapshot(data) == data_before, "nothing in the data folder changed")
        last_check = json.loads((install / "update" / "last-check.json").read_text("utf-8"))
        check(last_check.get("applied") == NEW_VERSION,
              "update\\last-check.json records the new version")

        # --- 2. Mistery swaps the staged updater in --------------------------
        heading("Mistery.exe swapping in the staged updater")
        swap_in_new_updater = swap_in_from_main()
        check(main_calls_swap_first(),
              "main() calls _swap_in_new_updater before anything else")
        swap_in_new_updater(install)
        check(not staged.exists(), "MisteryUpdate.exe.new is gone")
        check(sha256(install / "MisteryUpdate.exe") == sha256(new_exe),
              "MisteryUpdate.exe is now the new one")
        check((install / "MisteryUpdate.exe.old").is_file(),
              "the old updater is beside it as .old")

        # --- 3. nothing to do the second time --------------------------------
        heading("The run after that")
        code = updater.run()
        check(code == 0, "exit code is 0 (up to date)", f"got {code}")
        check(not list(install.glob("*.old")), "the .old files were swept")
        check(not (install / "update" / "unpack").exists(), "the unpack folder is gone")

        # --- 4. every refusal -------------------------------------------------
        heading("Refusals")

        def refuse(label: str, manifest_bytes: bytes, expect: int = 2,
                   quit_minutes: float = 40.0, before=None, after=None,
                   expect_changed: bool = False) -> None:
            """Run one case from a fresh 1.0.0 install and say what happened.

            Every case but one wants the install untouched afterwards; the
            recycled-pid case wants exactly the opposite, which is why it can
            ask for it.
            """
            restore(quit_minutes)
            served.write_bytes(manifest_bytes)
            if before:
                before()
            try:
                code = updater.run()
            finally:
                if after:
                    after()
            version_now = (install / "version.txt").read_text("utf-8").strip()
            unchanged = (version_now == OLD_VERSION
                         and sha256(install / "Mistery.exe") == sha256(pristine / "Mistery.exe"))
            as_wanted = (not unchanged) if expect_changed else unchanged
            check(code == expect and as_wanted, label,
                  f"exit {code} (wanted {expect}), now {version_now} — "
                  f"{updater.last_log()}")
            if not expect_changed:
                leftover = list((install / "update").glob("*.zip"))
                check(not leftover, f"{label}: no download left behind",
                      ", ".join(item.name for item in leftover))

        # a tampered manifest: one byte of the signed body flipped
        envelope = json.loads(good_manifest.decode("utf-8"))
        body_bytes = bytearray(base64.b64decode(envelope["manifest"]))
        spot = body_bytes.index(b'"version"')
        body_bytes[spot + 12] ^= 0x01
        envelope["manifest"] = base64.b64encode(bytes(body_bytes)).decode()
        refuse("a tampered manifest is refused",
               json.dumps(envelope, indent=2).encode("utf-8"))

        # signed by a key this updater does not trust
        other_key = ed25519.generate_private_key()
        refuse("a manifest signed by another key is refused",
               sign_envelope(good_body, other_key))

        # the right shape, the wrong hash
        wrong_hash = json.loads(json.dumps(good_body))
        wrong_hash["package"]["sha256"] = "0" * 64
        refuse("a wrong SHA-256 is refused", sign_envelope(wrong_hash, private_key))

        # the right shape, the wrong size
        wrong_size = json.loads(json.dumps(good_body))
        wrong_size["package"]["size"] = good_body["package"]["size"] - 1
        refuse("a wrong size is refused", sign_envelope(wrong_size, private_key))

        # an older version: not a refusal, a "nothing to do"
        older = json.loads(json.dumps(good_body))
        older["version"] = "0.9.0"
        refuse("an older version is not installed", sign_envelope(older, private_key),
               expect=0)

        # http instead of https
        plain = json.loads(json.dumps(good_body))
        plain["package"]["url"] = plain["package"]["url"].replace("https://", "http://")
        refuse("an http download url is refused", sign_envelope(plain, private_key))

        # a zip that tries to climb out of the install folder
        for label, entry in (("..\\", "..\\evil.txt"), ("../", "../evil.txt"),
                             ("an absolute path", "C:/Windows/Temp/evil.txt")):
            evil_zip = www / f"evil-{abs(hash(entry)) % 10000}.zip"
            with zipfile.ZipFile(evil_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(payload / "version.txt", "version.txt")
                zf.writestr(entry, "owned\n")
            evil_body = json.loads(json.dumps(good_body))
            evil_body["package"].update({
                "name": evil_zip.name, "url": f"{base_url}/{evil_zip.name}",
                "size": evil_zip.stat().st_size, "sha256": sha256(evil_zip)})
            refuse(f"a zip holding {label} is refused",
                   sign_envelope(evil_body, private_key))
            check(not (install.parent / "evil.txt").exists()
                  and not Path("C:/Windows/Temp/evil.txt").exists(),
                  f"a zip holding {label}: nothing escaped")

        # a zip entry marked as a symlink: extracting one would write outside
        # the folder just as surely as "..", and Windows follows them now.
        link_zip = www / "link.zip"
        with zipfile.ZipFile(link_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(payload / "version.txt", "version.txt")
            info = zipfile.ZipInfo("_internal/qt.dll")
            info.create_system = 3                       # Unix, so the mode is read
            info.external_attr = (0o120777 << 16)        # S_IFLNK
            zf.writestr(info, "C:/Windows/System32/kernel32.dll")
        link_body = json.loads(json.dumps(good_body))
        link_body["package"].update({
            "name": link_zip.name, "url": f"{base_url}/{link_zip.name}",
            "size": link_zip.stat().st_size, "sha256": sha256(link_zip)})
        refuse("a zip holding a symlink is refused", sign_envelope(link_body, private_key))

        # a zip bomb: 100 MB of zeros in 100 KB of zip
        bomb_zip = www / "bomb.zip"
        with zipfile.ZipFile(bomb_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(payload / "version.txt", "version.txt")
            zf.writestr("_internal/big.bin", b"\0" * 100_000_000)
        bomb_body = json.loads(json.dumps(good_body))
        bomb_body["package"].update({
            "name": bomb_zip.name, "url": f"{base_url}/{bomb_zip.name}",
            "size": bomb_zip.stat().st_size, "sha256": sha256(bomb_zip)})
        refuse(f"a zip bomb ({bomb_zip.stat().st_size / 1e3:.0f} KB unpacking to "
               f"100 MB) is refused", sign_envelope(bomb_body, private_key))

        # The names Python's own zipfile quietly rewrites on the way in, checked
        # against the rule directly so that the rule is what is being tested.
        heading("Entry names, checked one by one")
        from updater import apply as apply_rules
        from updater.manifest import Refused as RefusedError

        nasty = ["..\\evil.txt", "../evil.txt", "/etc/passwd", "C:/evil.txt",
                 "a/../../evil.txt", "_internal/../../evil.txt", "CON",
                 "_internal/NUL.dll", "evil.exe ", "evil.exe.", "a:b.txt",
                 "we\x00ird.txt", ""]
        refused_all = True
        for name in nasty:
            try:
                apply_rules._check_name(name)
                refused_all = False
                print(f"      ACCEPTED {name!r}")
            except RefusedError:
                pass
        check(refused_all, f"all {len(nasty)} awkward zip entry names are refused")
        fine = ["Mistery.exe", "_internal/PySide6/Qt6Core.dll", "version.txt",
                "_internal/base_library.zip"]
        check(all(apply_rules._check_name(name) for name in fine),
              "the names a real build produces are accepted")

        # --- 5. Mistery is running, three ways --------------------------------
        heading("Mistery being open")
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        refuse("the single-instance mutex being held stops it", good_manifest, expect=20)
        kernel32.CloseHandle(ctypes.c_void_p(handle))

        # a process actually called Mistery.exe — ping under another name, so
        # that the test never starts the real app or touches the real library
        image_dir = root / "image"
        image_dir.mkdir()
        shutil.copy2("C:/Windows/System32/PING.EXE", image_dir / "Mistery.exe")
        pretend = subprocess.Popen([str(image_dir / "Mistery.exe"), "-n", "120", "127.0.0.1"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            refuse("a running Mistery.exe stops it", good_manifest, expect=20)
        finally:
            pretend.terminate()                 # started by this test, ended by it
            pretend.wait(timeout=10)

        # app.lock naming a process that is alive
        live = subprocess.Popen(["ping", "-n", "120", "127.0.0.1"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            refuse("app.lock naming a live process stops it", good_manifest, expect=20,
                   before=lambda: (data / "app.lock").write_text(
                       f"{live.pid},{time.time()}", encoding="utf-8"))
            # The same live pid, but claimed to have started an hour before it
            # did: that is a recycled pid, and the created-time check must see
            # through it. This is the one that defeats a stale lock file.
            refuse("app.lock with a recycled pid is seen through", good_manifest,
                   expect=10, expect_changed=True,
                   before=lambda: (data / "app.lock").write_text(
                       f"{live.pid},{time.time() - 3600}", encoding="utf-8"))
        finally:
            live.terminate()
            live.wait(timeout=10)

        # --- 6. the quiet period ---------------------------------------------
        heading("The half hour after Mistery quits")
        refuse("Mistery having quit 5 minutes ago stops it", good_manifest,
               expect=20, quit_minutes=5)
        check("quit 5 minutes ago" in updater.last_log(2), "the log says why",
              updater.last_log())

        restore()
        (data / "last-run.json").unlink()
        served.write_bytes(good_manifest)
        code = updater.run()
        check(code == 20, "no last-run.json at all stops it (a crash, not a quit)",
              f"exit {code} — {updater.last_log()}")

        # --- 7. the off switch -------------------------------------------------
        heading("The off switch")
        restore()
        (install / "updater.json").write_text(json.dumps({
            "manifest_url": f"{base_url}/manifest.json", "auto_update": False,
        }, indent=2), encoding="utf-8")
        code = updater.run()
        check(code == 20 and (install / "version.txt").read_text("utf-8").strip()
              == OLD_VERSION, "auto_update false stops it", f"exit {code}")

        # Off means off for the button too, not just for the hourly task: the
        # answer is written down so Settings can say so, and nothing is fetched.
        (install / "update" / "last-check.json").unlink(missing_ok=True)
        requests_before = len(QuietHandler.served)
        code = updater.run("--check-now")
        answer = json.loads((install / "update" / "last-check.json").read_text("utf-8"))
        check(code == 20 and answer.get("disabled") is True,
              "--check-now says 'switched off' rather than checking anyway",
              json.dumps(answer))
        check(len(QuietHandler.served) == requests_before,
              "and made no request at all",
              f"{len(QuietHandler.served) - requests_before} request(s) to the server")

        # --- 8. check-now, which the app will call -----------------------------
        heading("--check-now, the entry point the app calls")
        restore()
        code = updater.run("--check-now")
        answer = json.loads((install / "update" / "last-check.json").read_text("utf-8"))
        check(code == 0 and answer.get("available") is True
              and answer.get("latest") == NEW_VERSION,
              "--check-now reports the new version without installing it",
              json.dumps(answer))
        check((install / "version.txt").read_text("utf-8").strip() == OLD_VERSION,
              "--check-now changed nothing")

        # --- 9. the log looks after itself -------------------------------------
        heading("update.log")
        restore()
        log_file = install / "update.log"
        log_file.write_text("x" * 70_000, encoding="utf-8")   # past the 64 KB mark
        updater.run("--sweep")
        check((install / "update.log.1").is_file() and log_file.stat().st_size < 1000,
              "update.log rotates at 64 KB and keeps one old copy",
              f"{log_file.stat().st_size} bytes now, "
              f"{(install / 'update.log.1').stat().st_size} in update.log.1")

        # --- 10. a real scheduled task -----------------------------------------
        heading("A real scheduled task")
        exe = install / "MisteryUpdate.exe"
        created = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/TR", f'"{exe}"',
             "/SC", "HOURLY", "/F"], capture_output=True, text=True)
        task_registered = created.returncode == 0
        check(task_registered, f"registered {task_name}", created.stderr.strip())
        if task_registered:
            subprocess.run(["schtasks", "/Run", "/TN", task_name],
                           capture_output=True, text=True)
            for _ in range(30):
                query = subprocess.run(["schtasks", "/Query", "/TN", task_name,
                                        "/FO", "LIST", "/V"],
                                       capture_output=True, text=True)
                if "Running" not in query.stdout:
                    break
                time.sleep(1)
            # The task gets no MISTERY_* variables, so the exe uses its own
            # folder as the install (which is the throwaway one, because that
            # is where it sits) and the real data folder, read-only. With the
            # real Mistery open it answers 20; with it closed, 1, because
            # nothing is serving the manifest URL by then.
            check("Last Result" in query.stdout, "the task ran",
                  next((line.strip() for line in query.stdout.splitlines()
                        if "Last Result" in line), ""))

        # --- 11. the app's end of it -------------------------------------------
        heading("app/updates.py — the Settings screen's end of the switch")
        restore()
        served.write_bytes(good_manifest)
        # Pointed at the throwaway world *before* app.config is imported:
        # importing it builds Settings(), which writes a settings.json, and the
        # folder these tests must never create or touch is %APPDATA%\Mistery.
        os.environ["MISTERY_DATA_DIR"] = str(data)
        os.environ["SSL_CERT_FILE"] = str(cert)
        os.environ["MISTERY_TEST_MUTEX_NAME"] = mutex_name
        from app import updates as app_updates              # noqa: E402

        # install_dir() in app/config.py means "the folder Mistery.exe is in",
        # and this process is a test script sitting somewhere else entirely.
        # The one seam; everything below is the shipping code, talking to the
        # real frozen exe, through the real updater.json.
        app_updates.install_dir = lambda: install

        check(app_updates.updater_exe() == install / "MisteryUpdate.exe",
              "the app finds MisteryUpdate.exe beside it")
        started = time.monotonic()
        told = app_updates.set_auto_update(False)
        switch_ms = (time.monotonic() - started) * 1000
        check(told and app_updates.auto_update_enabled() is False,
              "the Settings switch turns updates off, and reads back off",
              f"{switch_ms:.0f} ms")
        check(json.loads((install / "updater.json").read_text("utf-8"))
              .get("manifest_url") == f"{base_url}/manifest.json",
              "and left the rest of updater.json alone")

        requests_before = len(QuietHandler.served)
        answer = app_updates.check_now()
        check(answer.get("disabled") is True and answer.get("code") == 20
              and len(QuietHandler.served) == requests_before,
              "Check now, with updates off, says so and fetches nothing",
              app_updates.describe(answer))

        check(app_updates.set_auto_update(True)
              and app_updates.auto_update_enabled() is True,
              "the switch turns them back on")
        started = time.monotonic()
        answer = app_updates.check_now()
        check_ms = (time.monotonic() - started) * 1000
        check(answer.get("available") is True and answer.get("latest") == NEW_VERSION,
              f"Check now finds {NEW_VERSION}, in {check_ms:.0f} ms",
              app_updates.describe(answer))
        check((install / "version.txt").read_text("utf-8").strip() == OLD_VERSION,
              "and installed nothing")
        # A last-check.json left by an earlier run is not an answer to this
        # check: check_now() throws away anything written before it started.
        (install / "update" / "last-check.json").write_text(json.dumps(
            {"checked": time.time() - 3600, "latest": "9.9.9", "available": True}),
            encoding="utf-8")
        stale = install / "MisteryUpdate.exe"
        stale.replace(install / "MisteryUpdate.exe.hidden")
        answer = app_updates.check_now()
        (install / "MisteryUpdate.exe.hidden").replace(stale)
        check(answer == {"code": None} and "could not start" in app_updates.describe(answer),
              "an updater that will not run does not get answered by an old file",
              json.dumps(answer))

        heading("Summary")
        failed = [label for ok, label, _ in results if not ok]
        print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
        for label in failed:
            print(f"  FAILED: {label}")
        print("\nupdate.log after all of it:\n")
        print((install / "update.log").read_text(encoding="utf-8"))
        # Two builds, an install and a payload come to about 80 MB. Kept when
        # something failed, because the folder is the evidence; swept when
        # everything passed, because it is only rubbish then.
        if not failed and "--keep" not in sys.argv:
            shutil.rmtree(root, ignore_errors=True)
            print(f"removed {root}")
        else:
            print(f"left {root} in place to look at")
        return 1 if failed else 0

    finally:
        if server is not None:
            server.shutdown()
        if task_registered:
            subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                           capture_output=True, text=True)
            print(f"deleted the scheduled task {task_name}")
        with contextlib.suppress(Exception):
            (root / "test-signing.key").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
