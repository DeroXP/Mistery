# The Mistery site

The landing page people download Mistery from, and the update feed installed
copies read. It is a small FastAPI service meant for Railway, and it does five
things:

| | |
|---|---|
| `/` | the landing page, rendered from the signed manifest so the version, size and checksum on it are the real ones |
| `/download` | a 302 to the installer named in that manifest |
| `/api/update` | the signed manifest, byte for byte as the release machine wrote it |
| `/update/manifest.json` | the same bytes at the older URL, for updaters already in the field. It shares `/api/update`'s rate limit, because it is the same answer |
| `/api/health` | one JSON line: Railway watches it, and so should you after a deploy. It reads what is cached and never waits on GitHub |
| `/static/…` | one stylesheet, one icon, and any screenshots you drop in |

**It holds no secret and it cannot sign an update.** The private key lives on
the release machine and in a GitHub Actions secret; this service knows only the
public half, which it uses to check what it serves. If somebody takes the
container, the worst they can do is serve an old (properly signed) manifest or
refuse to answer. They cannot hand anybody's PC new code. Everything else here
follows from keeping that true: no database, no cookies, no analytics, no
writes, nothing about visitors kept anywhere.

That last one includes the request log. uvicorn writes one line per request with
the caller's address in it unless you tell it not to, and Railway keeps deploy
logs — so `railway.json` starts it with `--no-access-log`, and the page's "stores
nothing" is true because of that one flag. Leave it on. (Railway's own edge sits
in front of this container and whatever it records is theirs; what this service
records is nothing.)

---

## Setting it up, once

You need: the repository on GitHub, the public key from
`packaging/public_key.txt` (one line of base64, made by `python
packaging/make_key.py`), and a Railway account.

1. **railway.com → New Project → Deploy from GitHub repo**, and pick the
   Mistery repository. Railway creates a service and immediately tries to build
   it. Let it fail; step 2 is what makes it work.

2. **Service → Settings → Source → Root Directory: `server`.**
   This is the step that matters. Without it Railway builds from the repository
   root, finds the *app's* `requirements.txt` and installs PySide6, Pillow,
   numpy and the rest — a few hundred megabytes of desktop GUI toolkit that this
   web service has no use for. With it, Railway sees only this folder: three
   dependencies, `railway.json`, and `.python-version`.

3. **Service → Variables → New Variable**

   | Name | Value |
   |---|---|
   | `MISTERY_UPDATE_PUBLIC_KEY` | the one line from `packaging/public_key.txt` |

   That is the only required one. Everything else has a default that is right.
   Paste the line exactly; base64 or hex both work, and anything that is not 32
   bytes is refused at startup rather than half-working.

4. **Service → Settings → Networking → Generate Domain** (or *Custom Domain*
   and point a CNAME at what Railway gives you). Railway terminates HTTPS for
   you; this service redirects anything that arrives as plain http to the same
   URL on the same host — the scheme changes and nothing else, so a custom
   domain keeps working whether or not you ever set `MISTERY_SITE_URL`.

5. **Deploy.** The deploy log should end with a line like

   ```
   INFO mistery.site starting: repo DeroXP/Mistery, manifest
   https://github.com/DeroXP/Mistery/releases/latest/download/mistery-update.json,
   trusting key 3f9c1a2b, 0 screenshot(s)
   ```

   If instead it says `Mistery site cannot start`, the next line says exactly
   what is missing. That is deliberate: a service that will not start gets fixed
   in two minutes, a service that starts with a broken Download button gets
   found by a stranger.

## What to check afterwards

```bash
curl https://<your-domain>/api/health
```

```json
{"status":"ok","manifest":"ok","version":"1.1.0","manifest_age_seconds":41,
 "manifest_source":"https://github.com/DeroXP/Mistery/releases/latest/download/mistery-update.json",
 "last_error":null,"trusted_key":"3f9c1a2b","screenshots":0,"uptime_seconds":73}
```

- `trusted_key` is the fingerprint of the key you pasted. Compare it with what
  `python packaging/sign_manifest.py --verify packaging/out/mistery-update.json`
  prints. If they differ, the variable is the wrong key and no update you sign
  will be served.
- `manifest: ok` means it fetched and verified the release manifest.
  `missing` means it could not (no release yet, or the release has no asset
  called `mistery-update.json`, or the signature did not check out — `last_error`
  says which). `stale` means it is serving the last good one because the most
  recent fetch failed, which is the correct behaviour and not an emergency.
- `manifest_age_seconds` is how long ago the last fetch happened, and on a quiet
  day it can be hours. This endpoint reads the cache and never fetches: a health
  check that can wait on GitHub is one that fails when GitHub is *slow* rather
  than down, and Railway answers a failed check by restarting the container into
  the same slow fetch. Loading the page or the feed is what refreshes it.
- Then open the page itself. The Download button should name the version you
  released, and the size beside it should be the installer's real size. Click
  it: it should land on the GitHub release asset.

A last check worth doing once:

```bash
curl -sI https://<your-domain>/ | findstr /i "content-security-policy strict-transport"
```

Both should be there, and there should be no `set-cookie` anywhere on this
site, ever.

## The variables

| Name | Required | Default | What it is for |
|---|---|---|---|
| `MISTERY_UPDATE_PUBLIC_KEY` | **yes** | — | The Ed25519 public key the manifest must be signed with. Base64 or hex. |
| `MISTERY_REPO` | no | `DeroXP/Mistery` | Used for the Source and Releases links and to build the default manifest URL. |
| `MISTERY_MANIFEST_URL` | no | `https://github.com/<repo>/releases/latest/download/mistery-update.json` | Where the signed manifest lives. The default follows GitHub's "latest release" alias, so cutting a release publishes it with no change here. Must be https. |
| `MISTERY_MANIFEST_JSON` | no | — | A whole signed manifest pasted in as one variable. Wins over the URL. For the day GitHub is down, or to pin an older release by hand. It still has to carry a good signature. |
| `MISTERY_MANIFEST_TTL` | no | `300` | Seconds before the server looks for a new manifest. |
| `MISTERY_FETCH_TIMEOUT` | no | `10` | One deadline over the whole fetch — connect, redirects and body. A source that answers and then trickles is dropped on it, which a per-read timeout never does. |
| `MISTERY_RATE_LIMIT_REQUESTS` | no | `60` | Requests per window per IP on `/api/*` and `/update/manifest.json`. `/api/health` is exempt. |
| `MISTERY_RATE_LIMIT_WINDOW` | no | `3600` | The window, in seconds. |
| `MISTERY_SITE_URL` | no | Railway's own domain | The absolute https URL of the site, used for the canonical link and the Open Graph tags. Not used for the http→https redirect: that goes to the host the visitor asked for. |

## Publishing a release, from this service's side

Nothing to do. `.github/workflows/release.yml` uploads `mistery-update.json` to
the GitHub Release; within `MISTERY_MANIFEST_TTL` seconds (five minutes by
default) the page and the feed are showing it. The asset must be called exactly
`mistery-update.json`, because that is the name the default URL asks for — it is
the one thing this service and the release workflow have to agree on, and
`MISTERY_MANIFEST_URL` is the way to fix it if it ever changes.

To roll a release back, delete or replace that asset on GitHub — the server
follows a manifest backwards as willingly as forwards, because a release pulled
for being broken has to be able to go away.

## Screenshots

Drop PNG, JPEG or WebP files into `server/static/shots/` and redeploy. The file
name becomes the caption and decides the order: `01-home-billboard.png` shows as
"Home billboard". With no files there, the page has no screenshots section at
all — there is no mock-up standing in for one, because a picture of a thing that
does not exist is the worst thing a download page can do.

## Running it locally

```bash
cd server
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
set MISTERY_UPDATE_PUBLIC_KEY=<the line from packaging/public_key.txt>
.venv\Scripts\python -m uvicorn service:app --port 8000
```

Then <http://127.0.0.1:8000>. Plain http is fine locally: the https redirect
only fires when a proxy tells us the browser used http, which nothing does on
localhost.

With no release published anywhere, the page will say so and `/api/update` will
answer 503. To see it with a release, sign a manifest with a throwaway key
(`python packaging/sign_manifest.py …`) and put the whole file in
`MISTERY_MANIFEST_JSON`.

## The tests

```bash
cd server
python test_service.py
```

It starts real uvicorn processes on 127.0.0.1:8731, checks the Ed25519 code
against RFC 8032's published vectors (and against `cryptography` if it is
installed), builds a manifest with the real release signer and serves it, then
checks every route, every security header, the ETags, the rate limit and the
three ways round it somebody would try, the http→https redirect, that a source
which trickles is dropped on the deadline, that the access log really is off,
and the three ways this service is allowed to be unhappy: nothing released yet,
a manifest signed by the wrong key, and no configuration at all. 103 checks,
about 26 seconds, no network access. It starts on 127.0.0.1:8731 and refuses to
run if something is already there; `MISTERY_TEST_PORT` moves it.

## If something goes wrong

**"Mistery site cannot start"** — read the next line of the deploy log. It names
the variable and what it should contain.

**The page says "No release published yet" but you have released one** —
`/api/health` will say why in `last_error`. The usual causes are an asset that
is not called `mistery-update.json`, a draft release (GitHub's `latest` alias skips
drafts), or `MISTERY_UPDATE_PUBLIC_KEY` being a different key from the one that
signed it.

**An updater is being rate-limited** — the limit is per IP and applies to
`/api/*` and `/update/manifest.json`, which share one budget because they serve
the same bytes. 60 an hour is far more than the once-an-hour an updater asks
for, so a client hitting it is stuck in a retry loop; raising
`MISTERY_RATE_LIMIT_REQUESTS` hides that rather than fixing it.

**You think the server is compromised** — there is nothing to rotate here,
because there is nothing here to steal: no key, no token, no data. Redeploy from
the repository, and check that `/api/update` still serves a manifest whose
signature verifies against `packaging/public_key.txt`. The release signing key
is what matters, and it has never been on this machine.
