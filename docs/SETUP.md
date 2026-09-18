# Setting this up, once

Everything that has to be done by hand before `git push origin v1.0.0` turns
into an installer somebody can download: four decisions and eight steps, about
an hour with the waiting. After that, a release is a tag and nothing else.

Do them in order. Each one ends with **Check it worked**, because the failures
here are quiet ones: a secret that was set but empty, a public key that does not
match the one in the updater, a Railway service that starts and serves an empty
page. Each of those looks fine until somebody else's PC finds it.

Commands are for PowerShell in the repository folder. `$` is not part of them.

---

## Before you start: four decisions only you can make

**1. Public or private repository.** This one has teeth. GitHub Release assets
on a *private* repository are not publicly downloadable — every download needs a
token, so the download button on the site would break and every updater would
get a 404. If the repository is private, the installer and the update zip have
to be hosted somewhere else entirely. **Public is the assumption everything here
is built on.** Nothing in the repository is a secret: the signing key lives in a
GitHub secret and on your disk, never in the tree.

**2. Code signing.** Without a certificate, Windows SmartScreen shows *"Windows
protected your PC — Microsoft Defender SmartScreen prevented an unrecognised app
from starting"* the first time anyone runs `MisterySetup.exe`, and they have to
click *More info* → *Run anyway*. That is not a bug and cannot be worked around
from the code: it is Windows telling the truth about an unsigned binary from a
publisher it has never seen. An OV certificate is roughly £200–400 a year and
still needs reputation to build before the warning goes; an EV certificate
(around £400+, on a hardware token) clears it immediately. The honest
alternative, which is what is built here: the release notes say it, and so does
the landing page — a paragraph directly under the download button naming the
box, the *More info* → *Run anyway* way through it, and the SHA-256 further down
the page to check the file against. If you buy a certificate later, signing fits
into `.github/workflows/release.yml` as one extra step after the installer is
built.

**3. Railway plan.** The service is tiny — it holds one 900-byte manifest in
memory and renders one page — but Railway's free trial sleeps and expires. The
Hobby plan (\$5/month) is what keeps a download page reachable. If the site is
down, installed copies keep updating anyway: `updater/keys.py` falls back to the
GitHub release asset, which is where the manifest actually lives.

**4. The domain.** Railway gives you `something.up.railway.app` for free. A
custom domain is a CNAME and a setting; nothing in the code needs it, and
`MISTERY_SITE_URL` is the only place it is written down.

You also need: `git`, `gh` signed in as the account that will own the repository
(`gh auth status`), Python 3.12, and a Railway account.

---

## 1. Finish the key-less checks first

Two minutes now against a broken first release.

```powershell
python packaging\check_version.py
python packaging\test_signing.py
python packaging\test_release.py
```

The first prints the version in `app/__init__.py`. The second makes a throwaway
key, signs a real manifest and hands it to every verifier in the repository,
then changes one byte at a time and requires all of them to refuse. The third
reads the release workflow and this guide and checks that every file they name
is really there, and really where the build scripts put it — it exists because
the workflow once called a `build_installer.py` that has never existed, and
looked for the installer one folder away from where it is written.

**Check it worked:** the second ends with `all checks passed`, and the third
with `all of them agree with the build scripts`.

---

## 2. Create the repository and push

```powershell
git status                 # nothing unexpected, and no *.key file
git add -A
git commit -m "Mistery: installer, updater, site and release plumbing"
gh repo create DeroXP/Mistery --public --source . --remote origin --push
```

`gh` needs the `workflow` scope to push `.github/workflows/`. If it refuses with
*"refusing to allow an OAuth App to create or update workflow"*, run
`gh auth refresh -h github.com -s workflow` and push again.

**Check it worked:**

```powershell
gh repo view --web         # the front page is .github/README.md, not the long one
gh workflow list           # "Release" is there
```

If the long technical README shows up as the front page instead, GitHub did not
pick up `.github/README.md` — it takes precedence over the root one, so check
the file is really at `.github/README.md`.

---

## 3. Make the signing key

This is the one step that cannot be undone quietly. The private key is what
makes a machine somewhere else run new code.

```powershell
python packaging\make_key.py --out $env:USERPROFILE\Documents\mistery-signing.key
```

It prints the public key and writes `packaging\public_key.txt`. Then:

1. Copy the printed base64 line into `updater\keys.py`:

   ```python
   UPDATE_PUBLIC_KEY = "the-line-it-printed"
   ```

2. Back up the private key somewhere that is not this PC — a password manager
   entry, or a USB stick in a drawer. Lose it and you cannot ship an update that
   any installed copy will accept; leak it and whoever has it owns every machine
   Mistery is installed on.

3. Commit the public half. It is not a secret:

   ```powershell
   git add packaging\public_key.txt updater\keys.py
   git status                      # NO .key file may appear here
   git commit -m "The release signing key this build trusts"
   git push
   ```

**Check it worked:**

```powershell
python packaging\test_signing.py
python packaging\make_key.py --key-file $env:USERPROFILE\Documents\mistery-signing.key
```

The second prints the public key again, derived from the private one. It must be
the same line that is in `packaging\public_key.txt` and in `updater\keys.py`.
Three places, one key.

---

## 4. Give the private key to GitHub Actions

```powershell
gh secret set MISTERY_SIGNING_KEY < $env:USERPROFILE\Documents\mistery-signing.key
gh secret list
```

Nothing else ever gets this key. Not Railway, not the repository, not a chat
window. GitHub will not show it back to you afterwards, which is the point.

**Check it worked:** `gh secret list` shows `MISTERY_SIGNING_KEY` with today's
date. There is no way to read it back and verify the contents — the first real
check is step 5, where the workflow refuses to sign with a key that does not
match `packaging/public_key.txt`.

---

## 5. Cut the first release

Pushing a tag builds it and publishes it. That is fine for this one — nothing is
installed anywhere yet, so there is nobody to hand a bad release to. For a later
release you want to look at first, start the workflow by hand instead (Actions →
Release → Run workflow, with the tag selected); started that way it defaults to
a draft, and you publish it when you are happy.

```powershell
# app\__init__.py holds the version. The tag must match it exactly.
python packaging\check_version.py           # prints e.g. 1.0.0

git tag -a v1.0.0 -m "First release: the whole app, installed properly."
git push origin v1.0.0
gh run watch                                # about 20 minutes
```

The tag's message becomes the one line the updater shows people, so write it for
them, not for you.

**Check it worked:**

```powershell
gh run list --workflow=release.yml --limit 1
gh release view v1.0.0
```

The release should hold four files: `MisterySetup.exe`,
`Mistery-1.0.0-win64.zip`, `mistery-update.json` and `SHA256SUMS.txt`.

Then verify the manifest yourself, with nothing but the public key:

```powershell
gh release download v1.0.0 --pattern mistery-update.json --dir .
python packaging\sign_manifest.py --verify mistery-update.json
```

It prints the version, both files with their sizes and hashes, and — because
`updater/keys.py` now carries the key — a line saying the updater in this build
accepts it. If it does not verify, **do not publish the release**: something is
wrong with the key, and jump to *When something goes wrong* below.

If it was a draft — because you started the workflow by hand — publish it:

```powershell
gh release edit v1.0.0 --draft=false
```

---

## 6. Deploy the site on Railway

The service is `server/`, and `server/railway.json` already says how to build it,
how to start it, and where its health check is. Railway reads that file from the
service's root directory, so the one setting that matters is the root.

1. [railway.com/new](https://railway.com/new) → **Deploy from GitHub repo** →
   `DeroXP/Mistery`. The first deploy will fail — it is building the repository
   root, which is a media player, not a web service. Expected; keep going.
2. Service → **Settings** → **Root Directory**: `server`

   Do this before anything else. Skipping it is what produces
   *"No start command detected"* from Railpack: it is looking at the repository
   root, which is a desktop player with no web app in it. The start command
   lives in `server/railway.json` (and again in `server/Procfile`), and Railway
   only reads those from the service's root directory.
3. Service → **Variables** → add:

   | Name | Value |
   | --- | --- |
   | `MISTERY_UPDATE_PUBLIC_KEY` | the base64 line from `packaging\public_key.txt` |
   | `MISTERY_REPO` | `DeroXP/Mistery` |
   | `MISTERY_SITE_URL` | your public URL, once you know it (optional) |

   `MISTERY_SITE_URL` takes `mistery.up.railway.app` or
   `https://mistery.up.railway.app` — the bare domain Railway shows you is
   read as https. An explicit `http://` is still refused, because this
   service is only ever reached over https.

   Everything else has a default that works: the manifest URL is
   `https://github.com/DeroXP/Mistery/releases/latest/download/mistery-update.json`,
   the cache is 5 minutes, the rate limit is 60 requests an hour per address.
   `server/config.py` lists the lot, with what each one is for.
4. Service → **Settings** → **Networking** → **Generate Domain**.
5. Redeploy. This one should build `server/` and pass its health check.

**Screenshots are the biggest thing still missing from the page.** It describes
a dark visual library in careful prose — the vinyl screensaver, the poster-sized
lyrics, Continue Watching previewing from the frame you stopped at — and then
asks a stranger to accept an unsigned 86 MB .exe without showing them any of it.
Nobody but you can fix that: they have to be pictures of the real app with a real
library in it, and a mock-up would be worse than nothing.

Take four, with Win+Shift+S or Win+PrtSc, and drop them into
`server/static/shots/`:

| File | What to shoot |
| --- | --- |
| `01-home-billboard.png` | Home, with the billboard and Continue Watching |
| `02-now-playing.png` | a film playing, controls up |
| `03-lyrics-screensaver.png` | the lyrics screensaver, mid-song |
| `04-music-library.png` | the music library, albums in a grid |

The number orders them and the rest of the name becomes the caption, so
`03-lyrics-screensaver.png` reads as *Lyrics screensaver*. Push, and
`/api/health` will report `"screenshots": 4` — which is how you know the deploy
picked them up. The page renders without them, with no section and no
placeholder.

The service refuses to start without `MISTERY_UPDATE_PUBLIC_KEY`, on purpose: a
site that starts anyway would serve a download button that goes nowhere, and
somebody would find that before you did.

**Check it worked:**

```powershell
curl.exe https://<your-domain>/api/health
```

It answers with `"status": "ok"`, `"version"` — the release it is serving — and
`"trusted_key"`, the first eight characters of the key it checks signatures
with. Those eight characters must match the `key id` that
`packaging\make_key.py` printed. Then open the site itself: the
download button should say the version from step 5 and hand you the same
`MisterySetup.exe` the release page has, and the paragraph under the button
should say *Windows protected your PC* — that is the page preparing people for
SmartScreen rather than letting it surprise them.

---

## 7. Install it on a real PC and watch the loop close

Ideally not this one — a PC that has never had Mistery on it tells you more.

1. Download `MisterySetup.exe` from the site. Note what SmartScreen says; that
   is what everyone else will see.
2. Install, start Mistery, point it at a folder of films, let it scan.
3. Check the update task exists and is scheduled:

   ```powershell
   schtasks /query /tn MisteryUpdate /v /fo list
   ```

   It must run **only when you are logged on** (interactive). The updater checks
   a `Local\` mutex to tell whether Mistery is running, and that name only exists
   inside your own logon session.
4. Check what the app thinks it is:

   ```powershell
   type $env:LOCALAPPDATA\Programs\Mistery\version.txt
   ```

**Check the update actually applies:** cut a second release (step 8), close
Mistery, wait, and run the task by hand rather than waiting an hour:

```powershell
schtasks /run /tn MisteryUpdate
# thirty seconds later
type $env:LOCALAPPDATA\Programs\Mistery\update.log
type $env:LOCALAPPDATA\Programs\Mistery\version.txt
```

The log says what it decided and why: not yet half an hour since Mistery quit,
no newer version, or downloaded-verified-replaced. The version file is the last
word on whether it worked.

---

## 8. Every release after this one

```powershell
# 1. bump the version in app\__init__.py, e.g. 1.0.0 -> 1.1.0
git add app\__init__.py
git commit -m "1.1.0"
git push

# 2. tag it, with the line people will read
git tag -a v1.1.0 -m "Playlists, categories and the lyrics screensaver."
git push origin v1.1.0

# 3. watch
gh run watch
```

That is the whole ritual. The workflow refuses to build if the tag and
`app/__init__.py` disagree, so the failure mode is a failed run, not a release
that lies about its version. The site picks up the new manifest within five
minutes, and installed copies take it within the hour — the first time they are
closed and have been for thirty minutes.

Version numbers are semver and are never reused: an installed copy compares
numbers, so 1.1.0 is the only way to replace 1.0.0.

---

## When something goes wrong

**The workflow fails on "The tag and the code must agree".** The tag says one
thing, `app/__init__.py` another. Fix the code, then move the tag:

```powershell
git tag -d v1.1.0
git push origin :refs/tags/v1.1.0
# bump app\__init__.py, commit, push
git tag -a v1.1.0 -m "..." ; git push origin v1.1.0
```

**The workflow fails on "the repository secret MISTERY_SIGNING_KEY is not
set".** Step 4 did not take, or the secret was set on a different repository.
Run `gh secret list` in this repository folder.

**It fails on "this signing key is not the key this repository ships".** The
secret and `packaging/public_key.txt` are different keys. Derive the public half
of the key you have and compare:

```powershell
python packaging\make_key.py --key-file $env:USERPROFILE\Documents\mistery-signing.key
type packaging\public_key.txt
```

If they differ, the secret is the wrong key — set it again from the right file.
Do **not** regenerate a key to make the error go away; see the next entry.

**It fails on "the updater in this build refuses this manifest".** `updater/
keys.py` and `packaging/public_key.txt` disagree. Paste the right line into
`keys.py`, commit, move the tag.

**A release turned out to be broken.** Delete the release and the tag, then
re-cut the previous version as `latest`:

```powershell
gh release delete v1.1.0 --yes --cleanup-tag
```

The site follows `releases/latest`, so it goes back to the older signed manifest
within five minutes. Copies that already updated stay updated — an installed
copy never goes backwards, because it only accepts a *newer* version. Fix
forward with 1.1.1.

**The signing key leaked, or you lost it.** Generate a new pair
(`make_key.py --out ... --force`), put the new public key in `updater/keys.py`
and `packaging/public_key.txt`, set the new secret, and ship a release. Everyone
already installed will correctly refuse it, because their exe trusts the old
key — they have to run the new installer by hand once. Say so on the release
page. If the old key leaked rather than was lost, say that too.

---

## What is where, afterwards

| Thing | Lives in | Who can read it |
| --- | --- | --- |
| private signing key | `%USERPROFILE%\Documents\mistery-signing.key`, your backup, and the GitHub secret | you, and one workflow step |
| public key | `packaging/public_key.txt`, `updater/keys.py`, a Railway variable | everyone, by design |
| installer, update zip, manifest | the GitHub Release | everyone |
| the site's configuration | Railway variables | you |
| anything about the people who use it | nowhere — it is not collected | — |
