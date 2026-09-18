"""Fetching, checking and caching the one document this service is about.

The manifest is written and signed on the machine that cuts a release, uploaded
to the GitHub Release as `manifest.json`, and this server only ever *repeats*
it - byte for byte, including the signature. It does not build one, it does not
hash a file and fill the gaps in, and it holds no key that could sign one.

The server checks the signature anyway, before the manifest is cached or served.
That check is not what protects users: the updater on someone's PC verifies with
its own embedded key, and it must, because the bytes travel over a network
neither of us controls. Checking here means a bad upload is caught by the first
person who loads the landing page, rather than by an updater at 4am.

What happens when GitHub has a bad minute: the server keeps serving the last
manifest it verified and says `stale` on /api/health. An updater asking "is
there anything newer" gets a signed answer that was true five minutes ago, which
is the right answer, instead of a 502.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx

import signing
from config import Config

log = logging.getLogger("mistery.manifest")


@dataclass
class Current:
    """What the server can say right now. Never half filled in."""

    raw: bytes                  # served exactly as signed, not re-serialised
    body: dict[str, Any]        # the verified body, for the page and /download
    etag: str
    fetched_at: float
    source: str                 # "environment" or the URL it came from

    @property
    def version(self) -> str:
        return str(self.body.get("version", ""))

    @property
    def installer(self) -> dict[str, Any]:
        return self.body.get("installer") or {}

    @property
    def package(self) -> dict[str, Any]:
        return self.body.get("package") or {}

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at


@dataclass
class Store:
    config: Config
    client: httpx.AsyncClient
    current: Current | None = None
    last_error: str | None = None
    last_attempt: float = 0.0
    fetches: int = 0
    _lock: anyio.Lock = field(default_factory=anyio.Lock)

    def peek(self) -> Current | None:
        """What we hold right now, without going anywhere for it.

        /api/health uses this. A health check that can wait on a fetch is a
        health check that can fail because GitHub is slow: Railway gives the
        check 30 seconds (server/railway.json), restarts the container when it
        runs out, and the fresh container fetches from the same slow source and
        does it again. Reading the cache instead means the health check answers
        in microseconds whatever the network is doing, and the fetch is left to
        the requests that actually need the bytes - the page and the feed.
        """
        return self.current

    async def get(self) -> Current | None:
        """The manifest, refreshed if what we hold has gone stale."""
        cached = self.current
        if cached is not None and cached.age_seconds < self.config.manifest_ttl:
            return cached

        async with self._lock:
            # Someone may have refreshed it while we waited for the lock. Without
            # this second look, a burst of visitors after a cold start would each
            # fetch from GitHub in turn.
            cached = self.current
            if cached is not None and cached.age_seconds < self.config.manifest_ttl:
                return cached
            # Do not hammer a source that is failing: after a failure, wait a
            # tenth of the TTL (at least 5 s) before trying again.
            if (
                self.last_error
                and time.time() - self.last_attempt < max(5.0, self.config.manifest_ttl / 10)
            ):
                return cached
            await self._refresh()
        return self.current

    async def _refresh(self) -> None:
        self.last_attempt = time.time()
        try:
            # One deadline over the whole read, not just over each socket read.
            # httpx's timeout is per operation, so a source that answers and
            # then trickles never trips it: measured, a source sending one byte
            # every two seconds against a five-second timeout was still being
            # read 25 seconds later, when the test gave up on it. The byte cap
            # below bounds the bytes; this bounds the time. It matters because
            # this runs holding the lock every other request waits on, so a
            # trickle upstream would otherwise hold the whole site open.
            with anyio.fail_after(self.config.fetch_timeout):
                raw, source = await self._read_source()
            body = signing.verify_manifest(raw, self.config.public_key)
        except TimeoutError:
            # Named rather than swallowed into the generic message below: bare
            # "TimeoutError:" in /api/health tells the owner nothing.
            self.last_error = (
                f"the manifest source did not finish inside "
                f"{self.config.fetch_timeout:.0f}s; keeping the cached manifest"
            )
            log.warning("manifest refresh failed, keeping the cached one: %s", self.last_error)
            return
        except Exception as exc:  # noqa: BLE001 - every failure has one answer: keep what we had
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("manifest refresh failed, keeping the cached one: %s", self.last_error)
            return

        previous = self.current
        new_version = body.get("version")
        if previous is not None and previous.version != new_version:
            # Worth a line either way: forwards is a release, backwards is a
            # rollback. The server follows both, because a release pulled for
            # being broken has to be able to go away again.
            log.info("manifest version %s -> %s", previous.version, new_version)
        elif previous is None:
            log.info("manifest %s loaded from %s", new_version, source)

        self.fetches += 1
        self.current = Current(
            raw=raw,
            body=body,
            # The ETag is over the bytes served, so a conditional request can be
            # answered 304 without re-verifying anything.
            etag='"' + hashlib.sha256(raw).hexdigest()[:32] + '"',
            fetched_at=time.time(),
            source=source,
        )
        self.last_error = None

    async def _read_source(self) -> tuple[bytes, str]:
        if self.config.manifest_json:
            return self.config.manifest_json.encode("utf-8"), "environment"

        url = self.config.manifest_url
        # follow_redirects: GitHub's "latest release asset" URL is two redirects
        # from the storage host that actually has the file. The byte cap below
        # is what makes following them safe.
        async with self.client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > signing.MAX_MANIFEST_BYTES:
                    raise ValueError(
                        f"whatever is at {url} is over {signing.MAX_MANIFEST_BYTES} "
                        f"bytes; not reading any further"
                    )
                chunks.append(chunk)
        return b"".join(chunks), url

    def status(self) -> str:
        """One word for /api/health: ok, stale, or missing."""
        if self.current is None:
            return "missing"
        if self.last_error:
            return "stale"
        return "ok"


def make_client(config: Config) -> httpx.AsyncClient:
    """One client for the process, with a timeout on every phase.

    A fetch with no timeout is how a small web service dies quietly: one hung
    socket holds a worker, then ten do, and the landing page stops answering
    because GitHub's CDN is having a moment. Ten seconds is generous for a
    900-byte file.

    These are per-operation timeouts, which a source that trickles slips
    between. The same number is used again as one overall deadline in
    Store._refresh, and that is the one that actually stops a slow source.
    """
    timeout = httpx.Timeout(config.fetch_timeout, connect=min(5.0, config.fetch_timeout))
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,      # set per call, so nothing follows one by accident
        headers={"User-Agent": f"mistery-site (+https://github.com/{config.repo})"},
        limits=httpx.Limits(max_connections=4),
    )
