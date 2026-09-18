"""Everything this service claims, checked against a running copy of it.

Run it from this directory, with the service's own requirements installed:

    python test_service.py

It starts real uvicorn processes on 127.0.0.1:8731 (an unremarkable high port,
picked so a developer's own 8000 stays free), talks to them over a real socket,
and shuts them down again. Nothing here touches the network or anything outside
this repository.

Three kinds of check, because three different things can be wrong:

  1. the signature code, against RFC 8032's published vectors and - when it is
     installed - against `cryptography`, so "we verify Ed25519" is not taken on
     faith;
  2. the manifest store, against a fake GitHub built out of an httpx transport
     and a real socket that trickles, so the redirect-following, the byte cap,
     the overall deadline and the "keep the old one when the fetch fails"
     behaviour are exercised without waiting on anyone's CDN;
  3. the service itself, black box: every route, every security header, the
     rate limit and the four ways round it somebody would try, the http→https
     redirect, the ETags, the access log that is not kept, and the three ways
     it is allowed to be unhappy - no release published, a manifest that does
     not verify, and no configuration at all.

The manifest it serves in these tests is signed by packaging/sign_manifest.py,
with a throwaway key made here and thrown away at the end. That is deliberate:
the release signer and this server have to agree byte for byte about what a
manifest is, and the only way to know they do is to make one with the real
signer and hand it to the real server.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# 8731 is an unremarkable high port, picked so a developer's own 8000 stays
# free. MISTERY_TEST_PORT moves it, which is worth having when two runs of this
# file overlap on one machine - see Server.__enter__ for what that looked like.
PORT = int(os.environ.get("MISTERY_TEST_PORT", "8731"))
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "packaging"))

import signing                          # noqa: E402  the server's verify-only half
import ed25519 as release_ed25519       # noqa: E402  packaging/ed25519.py, which can sign
import sign_manifest                    # noqa: E402  packaging/sign_manifest.py, the real signer

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed.append(name)
        print(f"  FAIL  {name}{('  - ' + detail) if detail else ''}")


# --- 1. the signature code ---------------------------------------------------


def test_signing() -> None:
    print("\nsigning")

    # RFC 8032 section 7.1, vectors 1 and 3.
    vectors = [
        ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
         "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
         "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ]
    for index, (pub, message, signature) in enumerate(vectors, 1):
        ok = signing.verify_bytes(
            bytes.fromhex(pub), bytes.fromhex(message), bytes.fromhex(signature))
        check(f"RFC 8032 vector {index} verifies", ok)
        mauled = bytearray(bytes.fromhex(signature))
        mauled[0] ^= 1
        check(
            f"RFC 8032 vector {index} rejects a flipped bit",
            not signing.verify_bytes(bytes.fromhex(pub), bytes.fromhex(message), bytes(mauled)),
        )

    # Non-canonical S: same statement, second encoding. RFC 8032 says reject.
    private = release_ed25519.generate_private_key()
    public = release_ed25519.public_key(private)
    signature = release_ed25519.sign(private, b"hello")
    s = int.from_bytes(signature[32:], "little")
    over = signature[:32] + int.to_bytes(s + release_ed25519._L, 32, "little")
    check("a non-canonical S is rejected", not signing.verify_bytes(public, b"hello", over))

    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519 as real
    except ImportError:
        print("  skip  cross-check against `cryptography` (not installed here)")
    else:
        mismatches = 0
        started = time.perf_counter()
        for _ in range(100):
            key = real.Ed25519PrivateKey.generate()
            raw_private = key.private_bytes_raw()
            raw_public = key.public_key().public_bytes_raw()
            message = os.urandom(200)
            theirs = key.sign(message)
            if release_ed25519.public_key(raw_private) != raw_public:
                mismatches += 1
            if release_ed25519.sign(raw_private, message) != theirs:
                mismatches += 1
            if not signing.verify_bytes(raw_public, message, theirs):
                mismatches += 1
        elapsed = (time.perf_counter() - started) * 1000
        check(f"100 keys agree with `cryptography` ({elapsed:.0f} ms for 300 operations)",
              mismatches == 0, f"{mismatches} mismatches")

    started = time.perf_counter()
    for _ in range(20):
        signing.verify_bytes(public, b"x" * 900, signature)
    print(f"  time  one verify: {(time.perf_counter() - started) / 20 * 1000:.1f} ms")


def make_manifest(version: str = "1.1.0", private_key: bytes | None = None) -> tuple[bytes, bytes]:
    """A real signed manifest from the real signer. Returns (envelope, public key)."""
    private_key = private_key or release_ed25519.generate_private_key()
    body = {
        "schema": sign_manifest.SCHEMA,
        "version": version,
        "released": "2026-09-17T18:40:00Z",
        "notes": "Playlists, categories and the lyrics screensaver.",
        "package": {
            "name": f"Mistery-{version}-win64.zip",
            "url": f"https://github.com/DeroXP/Mistery/releases/download/v{version}/Mistery-{version}-win64.zip",
            "size": 181_403_648,
            "sha256": "a" * 64,
        },
        "installer": {
            "name": "MisterySetup.exe",
            "url": f"https://github.com/DeroXP/Mistery/releases/download/v{version}/MisterySetup.exe",
            "size": 8_812_544,
            "sha256": "b" * 64,
        },
    }
    return sign_manifest.build_envelope(body, private_key), release_ed25519.public_key(private_key)


def test_manifest_agreement() -> None:
    print("\nthe signer and the server agree")
    envelope, public = make_manifest()
    body = signing.verify_manifest(envelope, public)
    check("the server verifies what packaging/sign_manifest.py signs", body["version"] == "1.1.0")
    check("the envelope is the schema the updater expects",
          json.loads(envelope)["schema"] == signing.SCHEMA == 1)

    _, other_public = make_manifest()
    try:
        signing.verify_manifest(envelope, other_public)
    except signing.BadManifest:
        check("another key's manifest is rejected", True)
    else:
        check("another key's manifest is rejected", False, "it was accepted")

    # Change one character inside the signed body and re-encode it: the outer
    # JSON stays perfectly well formed, which is exactly the attack.
    document = json.loads(envelope)
    tampered_body = base64.b64decode(document["manifest"]).replace(b"1.1.0", b"9.9.9")
    document["manifest"] = base64.b64encode(tampered_body).decode()
    try:
        signing.verify_manifest(json.dumps(document).encode(), public)
    except signing.BadManifest:
        check("a rewritten version number is rejected", True)
    else:
        check("a rewritten version number is rejected", False, "it was accepted")

    # Signed, but nonsense: an http URL that /download would have redirected to.
    document = json.loads(envelope)
    body = json.loads(base64.b64decode(document["manifest"]))
    body["installer"]["url"] = "http://example.invalid/evil.exe"
    private = release_ed25519.generate_private_key()
    resigned = sign_manifest.build_envelope(body, private)
    try:
        signing.verify_manifest(resigned, release_ed25519.public_key(private))
    except signing.BadManifest:
        check("a signed manifest with an http download URL is still refused", True)
    else:
        check("a signed manifest with an http download URL is still refused", False)


# --- 2. the manifest store ---------------------------------------------------


def test_store() -> None:
    print("\nfetching the manifest")
    import config as config_module
    import manifest as manifest_module

    envelope, public = make_manifest()
    environment = {"MISTERY_UPDATE_PUBLIC_KEY": base64.b64encode(public).decode()}
    conf = config_module.load(environment)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.host == "github.com":
            # What GitHub actually does with a "latest release asset" URL: a 302
            # to the storage host, and the file is on the other end of it.
            return httpx.Response(302, headers={"location": "https://objects.invalid/m.json"})
        if calls["n"] > 3:
            # The second fetch (calls 3 and 4) fails at the storage host.
            return httpx.Response(500)
        return httpx.Response(200, content=envelope)

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        store = manifest_module.Store(config=conf, client=client)
        current = await store.get()
        check("a manifest behind a redirect is fetched and verified",
              current is not None and current.version == "1.1.0")
        check("the ETag is over the bytes served", current.etag.startswith('"'))
        check("health says ok", store.status() == "ok")

        # Force a refresh and let the fake GitHub fail.
        store.current.fetched_at = 0
        current = await store.get()
        check("a failed refresh keeps the manifest it had",
              current is not None and current.version == "1.1.0")
        check("health says stale after a failed refresh", store.status() == "stale")

        await client.aclose()

        # A source that never stops sending: the cap has to stop it.
        def flood(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * (signing.MAX_MANIFEST_BYTES + 10))

        client = httpx.AsyncClient(transport=httpx.MockTransport(flood))
        store = manifest_module.Store(config=conf, client=client)
        current = await store.get()
        check("an oversized manifest is refused", current is None)
        check("and the reason is the size", "over" in (store.last_error or ""))
        await client.aclose()

    asyncio.run(run())


def test_slow_source() -> None:
    """A source that answers and then dribbles, which is how a CDN degrades.

    Every read lands inside httpx's per-read timeout, so httpx never fires. Only
    an overall deadline stops it, and it has to stop it, because the fetch runs
    holding the lock every other request queues behind.
    """
    print("\na manifest source that trickles")
    import dataclasses
    import config as config_module
    import manifest as manifest_module

    envelope, public = make_manifest()
    conf = config_module.load({
        "MISTERY_UPDATE_PUBLIC_KEY": base64.b64encode(public).decode(),
        "MISTERY_FETCH_TIMEOUT": "1",
    })

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     b"Content-Length: 900\r\n\r\n")
        await writer.drain()
        try:
            for _ in range(900):          # one byte every half second: 7 minutes of it
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.5)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async def run() -> None:
        source = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = source.sockets[0].getsockname()[1]
        # http and a dataclass built by hand: config.load refuses anything but
        # https, and rightly, but there is no certificate on a test socket.
        slow = dataclasses.replace(
            conf, manifest_url=f"http://127.0.0.1:{port}/m.json", manifest_json="")
        client = manifest_module.make_client(slow)
        store = manifest_module.Store(config=slow, client=client)
        # Start from a manifest it has already verified: the point is that a bad
        # fetch does not cost us the good answer we were giving before it.
        store.current = manifest_module.Current(
            raw=envelope, body=signing.verify_manifest(envelope, public),
            etag='"seed"', fetched_at=0.0, source="seed")

        started = time.perf_counter()
        current = await asyncio.wait_for(store.get(), timeout=20)
        elapsed = time.perf_counter() - started
        check(f"a trickling source is dropped on the deadline ({elapsed:.1f}s, deadline 1s)",
              elapsed < 5, f"{elapsed:.1f}s")
        check("and the manifest it already had keeps being served",
              current is not None and current.version == "1.1.0")
        check("health says stale, and last_error says what happened",
              store.status() == "stale" and "did not finish" in (store.last_error or ""),
              store.last_error or "(none)")

        # /api/health reads the cache and never waits for a fetch. Railway gives
        # the health check 30 seconds before it restarts the container, and a
        # check that can queue behind a slow GitHub is a restart loop with a
        # cold start and another slow fetch at the end of it.
        store.current.fetched_at = 0.0
        store.last_error = None          # otherwise the back-off skips the fetch
        fetching = asyncio.create_task(store.get())
        await asyncio.sleep(0.2)         # long enough to be inside the fetch, holding the lock
        started = time.perf_counter()
        peeked = store.peek()
        peek_ms = (time.perf_counter() - started) * 1000
        check(f"a health check reads the cache while a fetch holds the lock ({peek_ms:.3f} ms)",
              peeked is not None and peek_ms < 5)
        await fetching
        source.close()
        await client.aclose()

    asyncio.run(run())


# --- 3. the service, black box -----------------------------------------------


class Server:
    """A real uvicorn process, started and stopped by us and nobody else."""

    def __init__(self, environment: dict[str, str], log_level: str = "warning",
                 extra_args: list[str] | None = None) -> None:
        self.environment = environment
        # warning by default, because a test run should be quiet. The access-log
        # test needs info, which is what Railway runs at, to see the lines it is
        # checking are not there.
        self.log_level = log_level
        self.extra_args = extra_args or []
        self.process: subprocess.Popen | None = None
        self.output = ""

    def __enter__(self) -> "Server":
        # Refuse to run against a server we did not start. uvicorn exits when
        # the port is taken, and the wait loop below would then cheerfully talk
        # to whatever was already there: two overlapping runs of this file
        # produced three failures against a rate limit the *other* run had
        # spent, which is a confusing half hour nobody needs twice.
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                raise RuntimeError(
                    f"something is already listening on 127.0.0.1:{PORT}. Stop it, or "
                    f"set MISTERY_TEST_PORT to a free port and run this again."
                )
        env = dict(os.environ)
        env.update(self.environment)
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "service:app",
             "--host", "127.0.0.1", "--port", str(PORT),
             "--log-level", self.log_level, *self.extra_args],
            cwd=str(HERE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.process.poll() is not None:
                self.output = self.process.stdout.read()
                return self
            try:
                httpx.get(f"{BASE}/api/health", timeout=1.0)
                return self
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError("the server did not come up within 30 seconds")

    def __exit__(self, *exc) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.output = self.process.communicate(timeout=10)[0]
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.output = self.process.communicate()[0]


def test_service_with_release() -> None:
    print("\nthe service, with a release published")
    envelope, public = make_manifest()
    environment = {
        "MISTERY_UPDATE_PUBLIC_KEY": base64.b64encode(public).decode(),
        "MISTERY_MANIFEST_JSON": envelope.decode(),
        "MISTERY_RATE_LIMIT_REQUESTS": "5",
        "MISTERY_RATE_LIMIT_WINDOW": "60",
        "MISTERY_SITE_URL": "https://mistery.example",
    }
    with Server(environment):
        started = time.perf_counter()
        home = httpx.get(f"{BASE}/", timeout=10)
        first_load_ms = (time.perf_counter() - started) * 1000
        check("GET / is 200", home.status_code == 200, str(home.status_code))
        check("the page names the version from the manifest", "Download Mistery 1.1.0" in home.text)
        check("the page shows the installer's real size", "8.4 MB" in home.text)
        check("the page does not claim Mistery finds or streams anything",
              "does not find, stream" in home.text)
        # The SmartScreen box is the most likely reason a download never
        # becomes an install, and for a while this page was the only one of
        # the three that said nothing about it. Four checks, because each
        # sentence is doing a different job: what Windows will say, why, the
        # way through it, and how to check the file is the right one.
        # One line, so a sentence that wraps in the source still matches.
        flat = " ".join(home.text.split())
        check("the page warns about SmartScreen before the download",
              "Windows protected your PC" in flat)
        check("the page says the installer is not code-signed",
              "not code-signed" in flat)
        check("the page says which buttons to click",
              "More info" in flat and "Run anyway" in flat)
        check("the warning points at the checksum further down",
              'href="#checksum"' in home.text and 'id="checksum"' in home.text)
        # 444.7 MB measured from the real build on 2026-09-17: 179.0 of app and
        # updater, 254.4 of mpv and ffmpeg, 11.3 of uninstaller. "About 300 MB"
        # was a guess made before there was an installer to measure.
        check("the page does not still claim 300 MB on disk",
              "300 MB on disk" not in flat)
        check("the page says about what the install really costs",
              "425 MB on disk" in flat)
        check("there is no JavaScript on the page", "<script" not in home.text.lower())
        check("nothing is loaded from another origin",
              "http://" not in home.text.replace("http://www.w3.org", "")
              and "https://github.com/DeroXP" in home.text)
        check("no cookie is set anywhere", "set-cookie" not in home.headers)
        print(f"  time  first page load {first_load_ms:.0f} ms, {len(home.content)} bytes")

        for header, expected in [
            ("content-security-policy", "default-src 'none'"),
            ("x-content-type-options", "nosniff"),
            ("referrer-policy", "no-referrer"),
            ("x-frame-options", "DENY"),
            ("permissions-policy", "camera=()"),
            ("cross-origin-opener-policy", "same-origin"),
        ]:
            check(f"header {header}", expected in home.headers.get(header, ""),
                  home.headers.get(header, "(absent)"))
        check("no HSTS over plain http", "strict-transport-security" not in home.headers)

        # HEAD, which is what an uptime monitor or `curl -I` sends first.
        head = httpx.head(f"{BASE}/")
        check("HEAD / is 200, not 405", head.status_code == 200, str(head.status_code))
        check("and carries the same headers", "content-security-policy" in head.headers)
        check("HEAD /api/update is 200", httpx.head(f"{BASE}/api/update").status_code == 200)

        # The page's own ETag.
        again = httpx.get(f"{BASE}/", headers={"If-None-Match": home.headers["etag"]})
        check("a second visit with the ETag is a 304", again.status_code == 304)

        # The update feed.
        feed = httpx.get(f"{BASE}/api/update")
        check("GET /api/update is 200", feed.status_code == 200)
        check("it is byte for byte the manifest that was signed", feed.content == envelope)
        check("it is served as JSON", feed.headers["content-type"].startswith("application/json"))
        body = signing.verify_manifest(feed.content, public)
        check("what it serves verifies against the release key", body["version"] == "1.1.0")
        cached = httpx.get(f"{BASE}/api/update", headers={"If-None-Match": feed.headers["etag"]})
        check("a conditional request for the manifest is a 304", cached.status_code == 304)
        alias = httpx.get(f"{BASE}/update/manifest.json")
        check("the /update/manifest.json alias serves the same bytes", alias.content == envelope)

        # The download redirect.
        download = httpx.get(f"{BASE}/download", follow_redirects=False)
        check("GET /download is a 302", download.status_code == 302, str(download.status_code))
        check("it points at the installer in the manifest",
              download.headers.get("location") == body["installer"]["url"])

        health = httpx.get(f"{BASE}/api/health")
        check("GET /api/health is 200", health.status_code == 200)
        payload = health.json()
        check("health reports the version", payload["version"] == "1.1.0")
        check("health reports the key fingerprint",
              payload["trusted_key"] == signing.key_id(public))
        check("health says nothing about any visitor",
              not any(key in payload for key in ("ip", "user", "client", "requests")))

        check("robots.txt keeps crawlers out of the API",
              "Disallow: /api/" in httpx.get(f"{BASE}/robots.txt").text)
        css = httpx.get(f"{BASE}/static/site.css")
        check("the stylesheet is served", css.status_code == 200
              and css.headers["content-type"].startswith("text/css"))
        check("static files are cached for a day", "max-age=86400" in css.headers.get("cache-control", ""))
        icon = httpx.get(f"{BASE}/static/icon.png")
        check("the icon is served", icon.status_code == 200 and len(icon.content) > 1000)
        check("/favicon.ico redirects to it",
              httpx.get(f"{BASE}/favicon.ico", follow_redirects=False).status_code == 301)

        missing = httpx.get(f"{BASE}/not-a-page")
        check("a wrong URL gets the site's own 404 page",
              missing.status_code == 404 and "Mistery" in missing.text)
        check("a wrong API URL gets JSON, not HTML",
              httpx.get(f"{BASE}/api/nope").headers["content-type"].startswith("application/json"))

        # http, as Railway's proxy reports it. The redirect changes the scheme
        # and nothing else: sending people to the *configured* host meant that a
        # custom domain with MISTERY_SITE_URL unset bounced every plain-http
        # visitor onto the .up.railway.app name, permanently and uncached.
        redirect = httpx.get(f"{BASE}/?from=email", follow_redirects=False,
                             headers={"X-Forwarded-Proto": "http", "Host": "mistery.app"})
        check("plain http is redirected to https", redirect.status_code == 308)
        check("to the host the visitor asked for, query and all",
              redirect.headers.get("location") == "https://mistery.app/?from=email",
              redirect.headers.get("location", "(none)"))
        check("and the redirect is not one a browser keeps",
              "no-store" in redirect.headers.get("cache-control", ""),
              redirect.headers.get("cache-control", "(none)"))
        forged = httpx.get(f"{BASE}/", follow_redirects=False,
                           headers={"X-Forwarded-Proto": "http", "Host": "evil.example.com"})
        check("a Host header can only ever redirect to itself, never off to another site",
              forged.headers.get("location") == "https://evil.example.com/",
              forged.headers.get("location", "(none)"))
        junk = httpx.get(f"{BASE}/", follow_redirects=False,
                         headers={"X-Forwarded-Proto": "http", "Host": "evil.example.com/x"})
        check("a Host that is not a host falls back to the configured site",
              junk.headers.get("location") == "https://mistery.example/",
              junk.headers.get("location", "(none)"))
        secure = httpx.get(f"{BASE}/", headers={"X-Forwarded-Proto": "https"})
        check("HSTS is set over https",
              "max-age=63072000" in secure.headers.get("strict-transport-security", ""))

        # The rate limit: 5 per window in this test.
        codes = []
        for index in range(8):
            response = httpx.get(f"{BASE}/api/update",
                                 headers={"X-Forwarded-For": "203.0.113.9"})
            codes.append(response.status_code)
        check("the sixth API request from one address is refused",
              codes[:5] == [200] * 5 and codes[5:] == [429] * 3, str(codes))
        limited = httpx.get(f"{BASE}/api/update", headers={"X-Forwarded-For": "203.0.113.9"})
        check("a refusal says when to come back", limited.headers.get("retry-after", "").isdigit())
        check("another address is unaffected",
              httpx.get(f"{BASE}/api/update",
                        headers={"X-Forwarded-For": "198.51.100.4"}).status_code == 200)
        check("the health check is never rate limited",
              all(httpx.get(f"{BASE}/api/health",
                            headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 200
                  for _ in range(8)))
        check("the landing page is never rate limited",
              httpx.get(f"{BASE}/", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 200)

        # /update/manifest.json serves the same bytes as /api/update, so it
        # spends the same budget. Gating the limiter on the "/api/" prefix alone
        # left the older URL - the one an updater stuck in a retry loop is most
        # likely to be holding - answering all day from an address that had
        # already been cut off: measured 8 out of 8 at 200 with the limit at 3.
        one_client = {"X-Forwarded-For": "198.51.100.200"}
        mixed = [httpx.get(f"{BASE}/api/update", headers=one_client).status_code
                 for _ in range(3)]
        mixed += [httpx.get(f"{BASE}/update/manifest.json", headers=one_client).status_code
                  for _ in range(4)]
        check("the alias spends the same budget as /api/update",
              mixed == [200] * 5 + [429] * 2, str(mixed))

        # And the other way round the limiter: Starlette builds request.url out
        # of the Host header, so `Host: mistery.example/zzz` on a request for
        # /api/update reads as /zzz/api/update to a middleware that asks
        # request.url.path - while the router matches the real path and serves
        # the manifest anyway. Measured before this was fixed: 8 of 8 at 200
        # from an address the limiter had already cut off at 3.
        sneaky = {"X-Forwarded-For": "198.51.100.201", "Host": "mistery.example/zzz"}
        through = [httpx.get(f"{BASE}/api/update", headers=sneaky).status_code
                   for _ in range(8)]
        check("a Host header with a path in it does not dodge the limit either",
              through[:5] == [200] * 5 and through[5:] == [429] * 3, str(through))

        # And nor does a trailing slash: /update/manifest.json/ is answered with
        # a 307 to /update/manifest.json rather than a 404, and an unlimited
        # supply of redirects is still an unlimited supply of answers.
        slashed = {"X-Forwarded-For": "198.51.100.202"}
        with_slash = [httpx.get(f"{BASE}/update/manifest.json/", headers=slashed,
                                follow_redirects=False).status_code for _ in range(8)]
        check("nor does a trailing slash on the alias",
              with_slash[:5] == [307] * 5 and with_slash[5:] == [429] * 3, str(with_slash))

        # A client can put anything it likes in X-Forwarded-For; Railway appends
        # the address it actually saw, so only the last entry counts. If the
        # first entry were used, anyone could dodge the limit - or spend
        # somebody else's - by inventing a new left-hand address each time.
        spoofed = [
            httpx.get(f"{BASE}/api/update",
                      headers={"X-Forwarded-For": f"10.0.0.{index}, 198.51.100.77"}).status_code
            for index in range(8)
        ]
        check("a forged X-Forwarded-For prefix does not dodge the limit",
              spoofed[:5] == [200] * 5 and spoofed[5:] == [429] * 3, str(spoofed))

        # Static files: Starlette resolves the path before opening anything, but
        # this is the one bug that would hand out config.py, so it is checked.
        check("/static cannot be walked out of",
              httpx.get(f"{BASE}/static/../service.py").status_code in (404, 400),
              str(httpx.get(f"{BASE}/static/../service.py").status_code))
        check("nor with an encoded slash",
              httpx.get(f"{BASE}/static/..%2Fservice.py").status_code in (404, 400))
        check("the update feed sets no cookie either",
              "set-cookie" not in httpx.get(f"{BASE}/api/update",
                                            headers={"X-Forwarded-For": "192.0.2.5"}).headers)


def test_service_without_release() -> None:
    print("\nthe service, before anything has been released")
    _, public = make_manifest()
    environment = {
        "MISTERY_UPDATE_PUBLIC_KEY": base64.b64encode(public).decode(),
        # An https URL that resolves to nothing: the same shape of failure as a
        # repository with no release yet.
        "MISTERY_MANIFEST_URL": "https://mistery-no-such-host.invalid/manifest.json",
        "MISTERY_FETCH_TIMEOUT": "2",
    }
    with Server(environment):
        home = httpx.get(f"{BASE}/", timeout=20)
        check("the page still loads", home.status_code == 200)
        check("it says there is no release rather than offering a broken button",
              "No release published yet" in home.text)
        check("no download button points anywhere but GitHub", 'href="/download"' not in home.text)
        # Nothing to download, so no SmartScreen warning: a page that warns
        # about a file it is not offering reads as a page that is broken.
        check("and no SmartScreen warning about a download that is not there",
              "Windows protected your PC" not in home.text)
        feed = httpx.get(f"{BASE}/api/update")
        check("the update feed is a 503, not an empty 200", feed.status_code == 503)
        check("the download is a 503 too", httpx.get(f"{BASE}/download").status_code == 503)
        check("health says degraded", httpx.get(f"{BASE}/api/health").json()["status"] == "degraded")


def test_screenshots() -> None:
    """Drop two PNGs in and the page grows a section; take them away and it does not.

    The page has no screenshots yet, because only somebody with Mistery running
    and a real library in it can take them, and docs/SETUP.md asks the owner to
    do it in five minutes. This is so those five minutes are not spent finding
    out that the file names were wrong: two files go into static/shots, a real
    server starts, and the page and /api/health are asked what they saw.
    """
    print("\nscreenshots, once somebody drops them in")
    import service                          # noqa: PLC0415  for SHOTS, not to run it
    service.SHOTS.mkdir(parents=True, exist_ok=True)
    # A 1x1 PNG. The service never decodes these - it serves the bytes and reads
    # the name - so the smallest legal file is the honest fixture.
    one_pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    made = []
    for name in ("01-home-billboard.png", "02-lyrics-screensaver.png"):
        path = service.SHOTS / name
        if path.exists():        # a real screenshot the owner added: leave it alone
            continue
        path.write_bytes(one_pixel)
        made.append(path)
    if len(made) != 2:
        print("  skip  static/shots already holds pictures, which is the point of it")
        return

    envelope, public = make_manifest()
    environment = {
        "MISTERY_UPDATE_PUBLIC_KEY": base64.b64encode(public).decode(),
        "MISTERY_MANIFEST_JSON": envelope.decode(),
    }
    try:
        with Server(environment):
            home = httpx.get(f"{BASE}/", timeout=10)
            check("the page grows a screenshots section", "What it looks like" in home.text)
            check("the file name becomes the caption",
                  "Home billboard" in home.text and "Lyrics screensaver" in home.text)
            check("the leading number orders them and is not shown",
                  home.text.index("Home billboard") < home.text.index("Lyrics screensaver")
                  and "01-home" not in home.text.split("<figcaption>")[1])
            check("the pictures themselves are served",
                  httpx.get(f"{BASE}/static/shots/01-home-billboard.png").status_code == 200)
            check("health counts them, so a deploy can be checked from outside",
                  httpx.get(f"{BASE}/api/health").json()["screenshots"] == 2)
    finally:
        for path in made:
            path.unlink(missing_ok=True)

    with Server(environment):
        home = httpx.get(f"{BASE}/", timeout=10)
        check("with none there the section is gone rather than empty",
              "What it looks like" not in home.text)
        check("and no placeholder took its place",
              "<figure" not in home.text)


def test_service_with_forged_manifest() -> None:
    print("\nthe service, handed a manifest signed by the wrong key")
    envelope, _ = make_manifest()
    _, other_public = make_manifest()          # a key that did not sign it
    environment = {
        "MISTERY_UPDATE_PUBLIC_KEY": base64.b64encode(other_public).decode(),
        "MISTERY_MANIFEST_JSON": envelope.decode(),
    }
    with Server(environment) as server:
        check("the update feed refuses to serve it",
              httpx.get(f"{BASE}/api/update").status_code == 503)
        check("the page offers no download",
              "No release published yet" in httpx.get(f"{BASE}/").text)
        check("the download is refused", httpx.get(f"{BASE}/download").status_code == 503)
    check("and it said why in the log", "signature does not verify" in server.output.lower(),
          server.output[-300:])


def test_no_request_log() -> None:
    """The page says this site stores nothing. The access log was the exception.

    uvicorn's default writes one line per request with the caller's address in
    it, and Railway keeps deploy logs. Both halves are checked here: that the
    deployed start command turns it off, and that turning it off is what stops
    the address being written - a check that only looked for absence would pass
    just as happily against a server that was never asked anything.
    """
    print("\nthe request log this site does not keep")
    command = json.loads((HERE / "railway.json").read_text(encoding="utf-8"))["deploy"]["startCommand"]
    check("railway.json starts uvicorn with --no-access-log",
          "--no-access-log" in command, command)

    envelope, public = make_manifest()
    environment = {
        "MISTERY_UPDATE_PUBLIC_KEY": base64.b64encode(public).decode(),
        "MISTERY_MANIFEST_JSON": envelope.decode(),
        # What a container behind a proxy sets so uvicorn believes the forwarded
        # address - which is the address that would end up in the log.
        "FORWARDED_ALLOW_IPS": "*",
    }
    visitor = {"X-Forwarded-For": "7.7.7.7", "X-Forwarded-Proto": "https"}

    with Server(environment, log_level="info", extra_args=["--no-access-log"]) as quiet:
        for path in ("/", "/download", "/api/update"):
            httpx.get(f"{BASE}{path}", headers=visitor, follow_redirects=False)
        time.sleep(0.3)
    logged = [line for line in quiet.output.splitlines() if '"GET /' in line]
    check("three requests, and not one of them written down", not logged,
          "\n".join(logged[:3]))
    check("the visitor's address appears nowhere in the output",
          "7.7.7.7" not in quiet.output, quiet.output[-300:])

    with Server(environment, log_level="info") as noisy:
        httpx.get(f"{BASE}/download", headers=visitor, follow_redirects=False)
        time.sleep(0.3)
    check("(and without the flag uvicorn writes it, which is why the flag is there)",
          "7.7.7.7" in noisy.output, noisy.output[-300:])


def test_missing_configuration() -> None:
    print("\nthe service, with nothing configured")
    environment = dict(os.environ)
    environment.pop("MISTERY_UPDATE_PUBLIC_KEY", None)
    process = subprocess.run(
        [sys.executable, "-m", "uvicorn", "service:app",
         "--host", "127.0.0.1", "--port", str(PORT + 1), "--log-level", "warning"],
        cwd=str(HERE), env=environment, capture_output=True, text=True, timeout=60,
    )
    output = process.stdout + process.stderr
    check("it refuses to start", process.returncode != 0, f"exit {process.returncode}")
    check("and the message says exactly what to set",
          "MISTERY_UPDATE_PUBLIC_KEY is not set" in output, output[-400:])
    check("and where to set it", "Railway" in output)

    print("\nthe service, with a public key that is not one")
    environment["MISTERY_UPDATE_PUBLIC_KEY"] = "this is not a key"
    process = subprocess.run(
        [sys.executable, "-m", "uvicorn", "service:app",
         "--host", "127.0.0.1", "--port", str(PORT + 1), "--log-level", "warning"],
        cwd=str(HERE), env=environment, capture_output=True, text=True, timeout=60,
    )
    output = process.stdout + process.stderr
    check("it refuses to start", process.returncode != 0)
    check("and says the key is unusable", "unusable" in output, output[-400:])


def main() -> int:
    print(f"testing the Mistery site on port {PORT}, python {sys.version.split()[0]}")
    test_signing()
    test_manifest_agreement()
    test_store()
    test_slow_source()
    test_service_with_release()
    test_service_without_release()
    test_screenshots()
    test_service_with_forged_manifest()
    test_no_request_log()
    test_missing_configuration()

    print(f"\n{passed} checks passed, {len(failed)} failed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
