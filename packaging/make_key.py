r"""Make the Ed25519 key pair that signs Mistery's updates. Run this once, ever.

    python packaging\make_key.py --out %USERPROFILE%\Documents\mistery-signing.key

Two halves, and the difference between them is the whole security story.

The **private key** (32 bytes) signs release manifests. Anyone who has it can
make every installed copy of Mistery download and run whatever they like, and no
amount of HTTPS or careful server configuration changes that. It goes in exactly
two places: the file you choose here — back it up somewhere offline — and the
GitHub repository secret MISTERY_SIGNING_KEY. Never in this repository, never on
the web server, never pasted into a chat window.

The **public key** (32 bytes) can only check signatures. It is not a secret. It
gets committed as packaging/public_key.txt, embedded in the updater, and set as
a Railway variable so the server can notice a bad upload.

Losing the private key is survivable but tedious: generate a new pair, ship an
updater carrying the new public key, and everyone already installed runs the new
installer by hand once — because their updater will correctly refuse manifests
signed by a key it has never heard of. That is the system working.

This script refuses to write the private key anywhere inside the repository, so
it cannot be committed by accident. .gitignore blocks *.key and *.pem as well;
belt and braces, because there is no undoing a leaked signing key.

To print the public half of a key you already have (it is derived from the
private key, so it is never lost separately):

    python packaging\make_key.py --key-file %USERPROFILE%\Documents\mistery-signing.key
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
PUBLIC_KEY_FILE = HERE / "public_key.txt"

# The same file that signs releases, so a key generated here cannot turn out to
# be a key sign_manifest.py derives a different public half from.
sys.path.insert(0, str(HERE))
import ed25519  # noqa: E402


def refuse_if_inside_repo(path: Path) -> None:
    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return
    raise SystemExit(
        f"\nRefusing to write the private key to {path}: that is inside the\n"
        f"repository ({REPO_ROOT}). One `git add -A` and the key is public.\n"
        f"Put it somewhere personal, for example:\n"
        f"    %USERPROFILE%\\Documents\\mistery-signing.key\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="where to write the private key (outside this repo)")
    parser.add_argument("--key-file", help="an existing private key: print its public half")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing key file (you almost never want this)")
    parser.add_argument("--no-write-public", action="store_true",
                        help="do not update packaging/public_key.txt")
    args = parser.parse_args(argv)

    if args.key_file:
        text = Path(os.path.expandvars(args.key_file)).expanduser().read_text(
            encoding="utf-8").strip()
        private = base64.b64decode(text, validate=True)
        if len(private) != 32:
            raise SystemExit(f"{args.key_file} is {len(private)} bytes, not 32")
        public = ed25519.public_key(private)
        print(base64.b64encode(public).decode("ascii"))
        return 0

    if not args.out:
        parser.error("--out is required: choose a path outside the repository")

    out = Path(os.path.expandvars(args.out)).expanduser()
    refuse_if_inside_repo(out)
    if out.exists() and not args.force:
        raise SystemExit(
            f"\n{out} already exists. If that is your signing key, keep it:\n"
            f"generating a new one makes every installed copy of Mistery stop\n"
            f"accepting updates until it is reinstalled by hand. Use --force\n"
            f"only if you mean exactly that.\n"
        )

    ed25519.selftest()          # never hand out a key from a build that fails RFC 8032

    private = ed25519.generate_private_key()   # the OS random source, nothing else
    public = ed25519.public_key(private)
    private_b64 = base64.b64encode(private).decode("ascii")
    public_b64 = base64.b64encode(public).decode("ascii")
    # The same eight characters sign_manifest.key_id() stamps into the envelope.
    key_name = hashlib.sha256(public).hexdigest()[:8]

    # Sign something and check it verifies before telling anyone this key works.
    probe = b"mistery key check"
    if not ed25519.verify(public, probe, ed25519.sign(private, probe)):
        raise SystemExit("this key pair cannot verify its own signature — stop, "
                         "something is wrong with packaging/ed25519.py")

    out.parent.mkdir(parents=True, exist_ok=True)
    # One line and nothing else: `gh secret set MISTERY_SIGNING_KEY < thisfile`
    # must send the key and only the key. newline="\n" so a CRLF never rides
    # along into the secret.
    out.write_text(private_b64 + "\n", encoding="ascii", newline="\n")

    if not args.no_write_public:
        PUBLIC_KEY_FILE.write_text(public_b64 + "\n", encoding="ascii", newline="\n")

    written_public = "(not written)" if args.no_write_public else str(PUBLIC_KEY_FILE)
    print(f"""
Private key   {out}
              {out.stat().st_size} bytes, one line of base64. Back it up offline.
              Nobody but you and GitHub Actions ever needs to read it.

Public key    {public_b64}
              written to {written_public}
              key id {key_name}, which is what the updater's log, the server's
              /api/health and the manifest envelope all call this key.

Four things to do with these, in order — docs/SETUP.md has the long version.

1. Give the private key to GitHub Actions, and to nothing else:

       gh secret set MISTERY_SIGNING_KEY < "{out}"
       gh secret list          # MISTERY_SIGNING_KEY should be listed

   GitHub will not show it back to you afterwards. That is the point.

2. Put the public key in the updater, so installed copies trust this key and
   only this key. In updater\\keys.py:

       UPDATE_PUBLIC_KEY = "{public_b64}"

   packaging/public_key.txt now holds the same line, and the updater build
   refuses to make an exe whose key disagrees with it.

3. Give the same public key to the web service, so it can spot a bad upload
   before anyone downloads it. In Railway, under Variables:

       MISTERY_UPDATE_PUBLIC_KEY={public_b64}

4. Commit the public key — it is not a secret — and check the private one has
   stayed out:

       git add packaging/public_key.txt updater/keys.py
       git status              # no .key file may appear here

On a shared PC, lock the key file down to your account as well:

    icacls "{out}" /inheritance:r /grant:r "%USERNAME%":F
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
