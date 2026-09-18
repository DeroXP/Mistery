r"""The release workflow and the build scripts still mean the same files.

    python packaging\test_release.py

This exists because of a real failure. `.github/workflows/release.yml` called
`packaging/build_installer.py`, which has never existed — the script is
`build_setup.py` — and then looked for the installer at
`packaging/out/MisterySetup.exe` in five more places, while build_setup.py
writes it to `packaging/out/setup/MisterySetup.exe` because `setup\` is
PyInstaller's --distpath. The missing script name was caught by the workflow's
own preflight. The wrong folder was caught by nobody: the first step that would
have noticed is the size check, twenty minutes into a build, with the tag
already pushed.

So: read the workflow as text, pull out every `packaging/...` path it names, and
check each one against reality. Source files must exist. Build outputs must be
what the build scripts say they produce — asked of the scripts themselves
(`build_setup.installer_path()`, `make_package`'s name and out folder,
`sign_manifest.MANIFEST_NAME`), never copied here, because a second hard-coded
copy of a path is the bug this file is about.

The same check runs over `.github/README.md` and `docs/SETUP.md`, which tell
people how to build this by hand and were also naming build_installer.py.

Standard library only, and no network: it runs in the workflow before pip has
installed anything, and it has to work on a machine with nothing on it.

Takes about a second. Exit status 0 if everything agrees, 1 if it does not, and
it prints every disagreement rather than stopping at the first.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
DOCUMENTS = (ROOT / ".github" / "README.md", ROOT / "docs" / "SETUP.md")

sys.path.insert(0, str(HERE))

import build_setup            # noqa: E402  builds MisterySetup.exe
import make_package           # noqa: E402  builds the update zip
import sign_manifest          # noqa: E402  builds and signs the manifest

problems: list[str] = []
checks = 0


def check(condition: bool, complaint: str) -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(complaint)


# Paths that only exist while a build is running, or after one. Everything else
# a workflow names under packaging/ has to be a file in the tree.
#
# `packaging/out` itself is in here, not only what is under it: the folder is
# made by the first build script that runs, so on a fresh checkout - which is
# every run of this workflow - it is not there yet. Requiring it turned this
# file from a check into a step that fails every release, which is what running
# it against a clean copy of the tree showed in about a second.
BUILD_OUTPUTS = ("packaging/out",)

# Written by packaging/make_key.py during setup and committed then — step 3 of
# docs/SETUP.md. The workflow's preflight already says this in its own words,
# with the instruction attached, so this file must not fail a fresh clone for it.
SET_UP_LATER = ("packaging/public_key.txt",)

PATH_IN_TEXT = re.compile(r"packaging[/\\][\w./\\-]*[\w-]")


def named_paths(text: str) -> list[str]:
    """Every packaging path a file mentions, in forward slashes, deduplicated.

    Backslashes as well as slashes: docs/SETUP.md writes Windows paths, because
    the commands in it are PowerShell and that is what people will type.
    """
    seen: dict[str, None] = {}
    for match in PATH_IN_TEXT.finditer(text):
        seen[match.group(0).replace("\\", "/")] = None
    return list(seen)


def looks_like_a_file(path: str) -> bool:
    """A path with an extension is a file. `packaging/installer` is a folder."""
    return Path(path).suffix != ""


def check_named_files(where: Path) -> None:
    """Every packaging file a document or the workflow names is really there."""
    text = where.read_text(encoding="utf-8")
    label = where.relative_to(ROOT).as_posix()
    for path in named_paths(text):
        if path.startswith(BUILD_OUTPUTS) or path in SET_UP_LATER:
            continue
        if not looks_like_a_file(path):
            check((ROOT / path).is_dir(), f"{label} names {path}, which is not a folder here")
            continue
        check((ROOT / path).is_file(),
              f"{label} names {path}, which is not in the tree"
              + (" — the installer script is packaging/build_setup.py"
                 if "build_installer" in path else ""))


def main() -> int:
    check(WORKFLOW.is_file(), f"{WORKFLOW} is missing — there is no release workflow")
    if not WORKFLOW.is_file():
        print("\n".join(problems))
        return 1

    workflow = WORKFLOW.read_text(encoding="utf-8")

    check_named_files(WORKFLOW)
    for document in DOCUMENTS:
        check(document.is_file(), f"{document.relative_to(ROOT).as_posix()} is missing")
        if document.is_file():
            check_named_files(document)

    # --- the installer, the one that was wrong ------------------------------
    #
    # build_setup.py is asked where it puts the exe rather than told.
    wanted = build_setup.installer_path().relative_to(ROOT).as_posix()
    check(f"INSTALLER: {wanted}" in workflow,
          f"the workflow's INSTALLER is not {wanted} — that is where "
          "packaging/build_setup.py writes MisterySetup.exe")

    # And every step has to use that one definition. A second written-out path
    # to the installer anywhere in this file is the sixth copy, waiting to go
    # stale the next time the build moves. The name on its own is fine - the
    # release notes say "Download MisterySetup.exe" and should.
    written_out = re.compile(r"[\w./\\-]+[/\\]MisterySetup\.exe")
    for line_number, line in enumerate(workflow.splitlines(), start=1):
        if not written_out.search(line) or line.lstrip().startswith("#"):
            continue
        check(line.strip().startswith("INSTALLER:"),
              f"release.yml:{line_number} writes out a path to MisterySetup.exe "
              f"instead of using $env:INSTALLER: {line.strip()}")

    check("python packaging/build_setup.py" in workflow,
          "the workflow does not run packaging/build_setup.py, which is what "
          "builds the installer")

    # --- the update zip -----------------------------------------------------
    #
    # make_package writes <out>/Mistery-<version>-win64.zip, and the workflow
    # builds its path as packaging/out/ + whatever `--print-name` says. So ask
    # it the same way the workflow does, rather than writing the name out again.
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        make_package.main(["--print-name"])
    name = printed.getvalue().strip()
    check(name.startswith("Mistery-") and name.endswith(".zip"),
          f"make_package's name is {name}, which the workflow's glob "
          "packaging/out/Mistery-*-win64.zip will not match")
    check("packaging/out/Mistery-*-win64.zip" in workflow,
          "the kept-artifact glob no longer matches the update zip's name")
    check('"package_path=packaging/out/$package"' in workflow,
          "the workflow no longer builds the zip's path from --print-name and "
          "make_package's own out folder")
    check(make_package.HERE / "out" == ROOT / "packaging" / "out",
          "make_package writes somewhere other than packaging/out, which is "
          "where the workflow looks for the zip")

    # --- the manifest -------------------------------------------------------
    manifest = f"packaging/out/{sign_manifest.MANIFEST_NAME}"
    check(f'--out        "{manifest}"' in workflow,
          f"the workflow signs to something other than {manifest}")
    check(f'python packaging/sign_manifest.py --verify {manifest}' in workflow,
          f"the workflow does not verify {manifest} with the public key alone")

    # --- the four files a release is ----------------------------------------
    #
    # Read the $assets list itself rather than counting mentions: a manifest
    # that is built, signed and verified and then left off the upload is a
    # release whose updaters see nothing, and it would look fine from a count.
    assets = workflow.partition("$assets = @(")[2].partition(")")[0]
    check(assets.strip() != "", "the workflow no longer has an $assets list")
    for asset, what in (
        ("${{ steps.names.outputs.package_path }}", "the update zip"),
        ("$env:INSTALLER", "MisterySetup.exe"),
        (manifest, "the signed manifest"),
        ("packaging/out/SHA256SUMS.txt", "the checksums"),
    ):
        check(asset in assets,
              f"{what} ({asset}) is not in the release's assets any more")

    print(f"release.yml, .github/README.md and docs/SETUP.md: {checks} checks")
    if problems:
        for problem in problems:
            print(f"  FAIL  {problem}")
        print(f"\n{len(problems)} of {checks} failed")
        return 1
    print("all of them agree with the build scripts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
