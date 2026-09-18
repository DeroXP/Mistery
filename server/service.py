"""mistery.app - the landing page and the update feed.

Five things live here and nothing else (each answers GET and HEAD):

    /                    the landing page
    /download            302 to the installer named in the signed manifest
    /api/update          that manifest, byte for byte as it was signed
    /api/health          one line for Railway's health check
    /static/...          one stylesheet, one icon, any screenshots

It holds no database, no cookies, no sessions, no analytics and no secret. The
only thing it knows that the public does not is nothing at all: the public key
in its environment is public by definition, and everything else it serves came
from a GitHub release. If someone owns this container completely, the worst they
can do is serve an old signed manifest or refuse to answer - they cannot sign a
new one, so they cannot push code to anybody's PC. That property is the whole
point of the design, and every decision in this file is downstream of keeping
it: no writing, no state, no way in.

It keeps no request log either, which is a decision made one flag away from
here: `--no-access-log` in railway.json. uvicorn's default access log writes the
caller's address for every request, Railway keeps deploy logs for weeks, and the
footer of the page promises the opposite. Leave the flag on.

Run it:

    uvicorn service:app --host 0.0.0.0 --port $PORT --no-access-log

Startup order matters. Configuration is read first and a missing variable kills
the process with a message that says what to set, because a site whose Download
button is broken is worse than a site that is plainly down.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import config as config_module
import manifest as manifest_module
import page

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
SHOTS = STATIC / "shots"

log = logging.getLogger("mistery.site")

# Headers on every single response. Each one is here for a reason:
#
#  - Content-Security-Policy: `default-src 'none'` and then only what the page
#    truly uses. The page has no JavaScript at all, so `script-src 'none'` costs
#    nothing and means an injected <script> cannot run even if some future
#    version of this file forgets to escape something. `style-src 'self'` with
#    no 'unsafe-inline' is why the stylesheet is a separate file. form-action
#    'none' because there is no form on this site and never should be;
#    frame-ancestors 'none' so nobody can frame the download button inside
#    their own page and pass it off as theirs.
#  - X-Content-Type-Options: stops a browser deciding for itself that the
#    manifest is HTML and running it.
#  - Referrer-Policy: nobody downstream - GitHub included - learns which page
#    someone came from. It is also the only "privacy setting" a page with no
#    cookies and no scripts can get wrong.
#  - Permissions-Policy: a static page has no business asking for a camera. If
#    something on this origin ever tries, it fails.
#  - Cross-Origin-Opener-Policy / -Resource-Policy: this origin should never be
#    embedded or read by another one; both are one line and rule it out.
#
# Strict-Transport-Security is added separately, and only over https, because
# sending it over plain http is meaningless and sending it from localhost would
# pin a developer's browser to https for a port that has no certificate.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        "img-src 'self'; "
        "style-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "microphone=(), payment=(), usb=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


# The two URLs that serve the manifest. Both are counted against the same rate
# limit, because they hand out the same bytes: gating the limiter on the "/api/"
# prefix alone left /update/manifest.json - the older URL, so the one an updater
# stuck in a retry loop is most likely to be holding - answering as often as it
# was asked.
MANIFEST_PATHS = frozenset({"/api/update", "/update/manifest.json"})


def counts_against_the_limit(path: str) -> bool:
    """Which requests the rate limit applies to.

    /api/health is out: Railway's health check calls it from one address every
    few seconds, and a health check that gets a 429 restarts a container that
    was working perfectly. The landing page and /static are out too - a limit
    there would only ever hit a real reader.
    """
    if path == "/api/health":
        return False
    # rstrip because /update/manifest.json/ is answered with a 307 to
    # /update/manifest.json rather than a 404, and a redirect handed out without
    # limit is still something handed out without limit.
    return path.startswith("/api/") or path.rstrip("/") in MANIFEST_PATHS


class RateLimit:
    """A fixed window per IP address, in memory, for the API and the feed.

    Deliberately the simplest thing that works. This service runs as one small
    container; a shared counter in Redis would be a second thing to deploy, a
    second thing to break, and a second place holding IP addresses - which is
    exactly what this site is trying not to have. The window resets in one step
    rather than sliding, so a client can get up to twice the limit across a
    boundary. For "stop a stuck retry loop costing us GitHub requests" that is
    entirely good enough.

    Nothing is written down: the counters live in memory, hold no more than an
    address and a number, and vanish when the container restarts.
    """

    def __init__(self, requests: int, window: int) -> None:
        self.requests = requests
        self.window = window
        self._hits: dict[str, tuple[float, int]] = {}

    def allow(self, client: str) -> tuple[bool, int]:
        """(allowed, seconds until the window resets)."""
        now = time.monotonic()
        started, count = self._hits.get(client, (now, 0))
        if now - started >= self.window:
            started, count = now, 0
        count += 1
        self._hits[client] = (started, count)
        if len(self._hits) > 10_000:
            self._forget_old(now)
        remaining = int(self.window - (now - started)) + 1
        return count <= self.requests, remaining

    def _forget_old(self, now: float) -> None:
        # An unbounded dict keyed by client address is a memory leak with a
        # patient attacker's name on it. 10,000 entries is about 1 MB; past that
        # everything whose window has expired goes.
        self._hits = {
            key: value for key, value in self._hits.items() if now - value[0] < self.window
        }


class State:
    """Everything the process holds. Built once in the lifespan, never written to after."""

    config: config_module.Config
    store: manifest_module.Store
    limiter: RateLimit
    shots: list[page.Shot]
    started_at: float
    # The rendered page, keyed by the manifest ETag it was built from. Rendering
    # costs about 0.2 ms, so this is not about speed: it is so the page's own
    # ETag is stable between requests and a browser that already has it gets a
    # 304 instead of 30 KB.
    page_cache: tuple[str, str, str] = ("", "", "")   # (manifest etag, html, etag)


state = State()


def find_shots() -> list[page.Shot]:
    """Screenshots of the real app, in filename order, captioned by filename.

    `01-home-billboard.png` becomes "Home billboard". Whoever has Mistery
    running takes the pictures; this service just shows whatever it finds, and
    shows nothing at all when it finds nothing, because a mock-up on a download
    page is a lie with a border round it.
    """
    if not SHOTS.is_dir():
        return []
    shots: list[page.Shot] = []
    for path in sorted(SHOTS.iterdir()):
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        stem = path.stem
        if "-" in stem and stem.split("-", 1)[0].isdigit():
            stem = stem.split("-", 1)[1]
        caption = stem.replace("-", " ").replace("_", " ").strip()
        shots.append(page.Shot(file_name=path.name, caption=caption[:1].upper() + caption[1:]))
    return shots


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        state.config = config_module.load()
    except config_module.ConfigurationError as exc:
        # Loud, first line, no traceback: this message is the fix, and it is
        # what the owner sees in Railway's deploy log.
        print(f"\nMistery site cannot start.\n\n{exc}\n", file=sys.stderr, flush=True)
        raise SystemExit(1) from None

    state.limiter = RateLimit(
        state.config.rate_limit_requests, state.config.rate_limit_window
    )
    state.shots = find_shots()
    state.started_at = time.time()
    client = manifest_module.make_client(state.config)
    state.store = manifest_module.Store(config=state.config, client=client)

    log.info(
        "starting: repo %s, manifest %s, trusting key %s, %d screenshot(s)",
        state.config.repo,
        "environment" if state.config.manifest_json else state.config.manifest_url,
        state.config.key_id,
        len(state.shots),
    )
    # Fetch once now so the first visitor does not wait for GitHub. A failure
    # here is not fatal: the page says there is no release yet, /api/health says
    # "missing", and the next request tries again.
    current = await state.store.get()
    if current is None:
        log.warning("no manifest yet: %s", state.store.last_error)

    try:
        yield
    finally:
        await client.aclose()


# Every route below answers HEAD as well as GET. FastAPI's @app.get registers
# GET alone, and a HEAD to the front page then answers 405 - which is what
# uptime monitors, link checkers and `curl -I` send first.
app = FastAPI(
    title="Mistery",
    docs_url=None,        # no /docs, no /redoc, no OpenAPI: this is a website, not an API
    redoc_url=None,       # product, and an unused endpoint is just more attack surface
    openapi_url=None,
    lifespan=lifespan,
)


def client_address(request: Request) -> str:
    """The address to count requests against, behind Railway's proxy.

    Railway terminates TLS and appends the real client address to
    X-Forwarded-For, so the *last* entry is the one it wrote and the only one
    worth believing - anything a client puts in that header of its own is to the
    left of it. Reading the first entry, which is the usual mistake, would let
    anybody rate-limit somebody else by forging it.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:64]
    return request.client.host if request.client else "unknown"


def requested_path(request: Request) -> str:
    """The path the router will match, which is not always `request.url.path`.

    Starlette builds `request.url` out of the Host header: with `Host:
    example.com/zzz`, a request for /api/update comes back as
    `https://example.com/zzz/api/update` and `request.url.path` is
    `/zzz/api/update`. Routing uses the raw ASGI path and answers normally, so
    anything here deciding what a request *is* has to read the same field.
    Measured before this was fixed: 8 out of 8 requests to /api/update carrying
    that Host header got 200 and the signed manifest, from an address the
    limiter had already cut off at 3.
    """
    return request.scope.get("path", "")


def https_target(request: Request) -> str | None:
    """The same URL over https, on the same host the visitor asked for.

    The host comes from the request, not from MISTERY_SITE_URL, and that is a
    correction: sending someone to the *configured* host meant that with a
    custom domain and MISTERY_SITE_URL forgotten, RAILWAY_PUBLIC_DOMAIN still
    held the *.up.railway.app name and every plain-http visitor to the real
    domain was permanently redirected off it. With neither variable set it was
    worse - the target became whatever Host header arrived, so a request
    claiming `Host: evil.example.com` got a 308 to evil.example.com with this
    site's name on it. Upgrading the scheme and leaving the host alone cannot be
    either of those things.

    None when there is no host to use at all (an HTTP/1.0 client with no Host
    header, and nothing configured). Then there is nowhere to send them and the
    request is answered as it arrived.
    """
    host = request.headers.get("host", "").strip()
    # An authority has no spaces, no slashes and no path. Anything else is
    # malformed or somebody being clever, and the configured host is used.
    if not host or len(host) > 255 or any(c in host for c in " /\\?#@"):
        host = urlsplit(state.config.site_url).netloc
    if not host:
        return None
    raw_query = request.scope.get("query_string", b"").decode("latin-1")
    query = f"?{raw_query}" if raw_query else ""
    return f"https://{host}{requested_path(request)}{query}"


@app.middleware("http")
async def gate(request: Request, call_next):
    """Three things, in this order: force https, rate-limit the feed, add headers."""
    # Railway terminates TLS and tells us what the browser actually used. There
    # is no way to answer an http request securely, so it is not answered: the
    # redirect happens before anything else in this file runs. /api/health is
    # the exception - Railway's own health check reaches the container over the
    # internal network, and a health check that gets a redirect is a deploy that
    # never goes live.
    path = requested_path(request)
    insecure = (
        path != "/api/health"
        and request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "http"
    )
    target = https_target(request) if insecure else None
    if target:
        # 308 because this site is https and always will be. no-store because a
        # redirect a browser remembers is a bad thing to be wrong about, and
        # Strict-Transport-Security on the https answer is what actually saves
        # the second visit a round trip.
        response: Response = RedirectResponse(
            target, status_code=308, headers={"Cache-Control": "no-store"}
        )
    elif counts_against_the_limit(path):
        allowed, retry_after = state.limiter.allow(client_address(request))
        if not allowed:
            response = JSONResponse(
                {"error": "too many requests", "retry_after_seconds": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https":
        # Two years, and only over https. Served from localhost it would pin a
        # developer's browser to a scheme their test server does not speak.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    if path.startswith("/static/"):
        # The stylesheet and the icon change when a deploy changes them, and a
        # deploy is not frequent. A day of caching, revalidated by the ETag
        # StaticFiles already sets.
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
    return response


def not_modified(request: Request, etag: str) -> bool:
    """True when the caller already has this exact thing."""
    known = request.headers.get("if-none-match", "")
    return any(part.strip().removeprefix("W/") == etag for part in known.split(","))


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def landing(request: Request) -> Response:
    current = await state.store.get()
    key = current.etag if current else "none"
    if state.page_cache[0] != key:
        html = page.render(state.config, current, state.shots)
        etag = '"' + hashlib.sha256(html.encode("utf-8")).hexdigest()[:32] + '"'
        state.page_cache = (key, html, etag)
    _, html, etag = state.page_cache

    if not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag})
    return HTMLResponse(
        html,
        headers={
            "ETag": etag,
            # Short, and revalidated: a release cut five minutes ago should show
            # up on the page within five minutes, not when a CDN feels like it.
            "Cache-Control": "public, max-age=300, must-revalidate",
        },
    )


@app.api_route("/download", methods=["GET", "HEAD"])
async def download() -> Response:
    """Straight to the installer asset named in the signed manifest.

    A redirect, not a proxy: the installer is tens of megabytes and GitHub's
    release storage is better at handing those out than a small container is.
    302 rather than 301 because the target changes with every release, and a
    browser that cached a permanent redirect would keep downloading an old one.

    The URL can only have come out of a manifest whose signature verified, and
    signing.py has already refused anything that is not https, so this cannot be
    turned into an open redirect by whoever is upstream of us.
    """
    current = await state.store.get()
    url = (current.installer.get("url") if current else None) or ""
    if not url:
        return HTMLResponse(
            page.render_error(
                503,
                "There is no download yet",
                "No signed release has been published, so there is nothing here to give you. "
                "The releases page on GitHub is the place to look.",
            ),
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


@app.api_route("/api/update", methods=["GET", "HEAD"])
@app.api_route("/update/manifest.json", methods=["GET", "HEAD"])     # the URL some updaters in the field may hold
async def api_update(request: Request) -> Response:
    """The signed manifest, byte for byte as the release machine wrote it.

    Not re-serialised, not re-ordered, not re-wrapped. The signature covers
    exact bytes, and a JSON library that helpfully reorders a key would break
    every updater in the field while looking completely fine in a browser.
    """
    current = await state.store.get()
    if current is None:
        return JSONResponse(
            {
                "error": "no manifest",
                "detail": "No signed manifest has been published yet, or this service "
                          "could not verify the one it fetched.",
            },
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    if not_modified(request, current.etag):
        return Response(status_code=304, headers={"ETag": current.etag})
    return Response(
        content=current.raw,
        media_type="application/json",
        headers={
            "ETag": current.etag,
            "Cache-Control": f"public, max-age={state.config.manifest_ttl}, must-revalidate",
        },
    )


@app.api_route("/api/health", methods=["GET", "HEAD"])
async def api_health() -> JSONResponse:
    """What Railway checks, and what the owner reads after a deploy.

    It says nothing about anyone who has visited: a version, a status word, a
    key fingerprint and how long the process has been up. The fingerprint is
    there so the owner can see at a glance that the variable they pasted is the
    key they think it is, without the key itself being interesting to read.

    It reads the cached manifest and never fetches one. A health check that can
    wait on GitHub is a health check that fails when GitHub is slow rather than
    down - Railway allows it 30 seconds, restarts the container, and the new one
    waits on the same slow source. `manifest_age_seconds` is therefore how long
    ago somebody asking for the page or the feed last caused a fetch, which on a
    quiet day can be hours. That is information, not a fault.
    """
    current = state.store.peek()
    status = state.store.status()
    return JSONResponse(
        {
            "status": "ok" if status != "missing" else "degraded",
            "manifest": status,
            "version": current.version if current else None,
            "manifest_age_seconds": int(current.age_seconds) if current else None,
            "manifest_source": current.source if current else None,
            "last_error": state.store.last_error,
            "trusted_key": state.config.key_id,
            "screenshots": len(state.shots),
            "uptime_seconds": int(time.time() - state.started_at),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.api_route("/robots.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
async def robots() -> PlainTextResponse:
    # The landing page is meant to be found. The update feed is not a page and
    # has no business in a search index.
    return PlainTextResponse(
        "User-agent: *\nAllow: /\nDisallow: /api/\n",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
async def favicon() -> RedirectResponse:
    # Browsers ask for this whatever the page says. One permanent redirect is
    # cheaper than a 404 on every visit.
    return RedirectResponse("/static/icon.png", status_code=301)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """A wrong URL gets the site's own page, not a framework's white one."""
    if requested_path(request).startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:
        return HTMLResponse(
            page.render_error(
                404, "That page is not here",
                "There are only a few pages on this site, and this is not one of them.",
            ),
            status_code=404,
        )
    return HTMLResponse(
        page.render_error(exc.status_code, "Something went wrong", str(exc.detail)),
        status_code=exc.status_code,
    )


# Mounted last so a route above always wins. StaticFiles handles ETags,
# Last-Modified and conditional requests; html=False means a missing file is a
# 404 rather than an accidental directory listing.
app.mount("/static", StaticFiles(directory=str(STATIC), html=False), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "service:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
        # No access log, here and in railway.json's start command. uvicorn's
        # default writes one line per request with the caller's address in it
        # ('INFO: 7.7.7.7:0 - "GET /download HTTP/1.1" 302 Found', measured),
        # and Railway keeps deploy logs. The page says this site stores nothing
        # about its visitors; a log of every visitor's address would make that
        # sentence false, and the sentence is worth more than the log.
        access_log=False,
    )
