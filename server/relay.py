"""The phone remote: this site passes a phone's requests to its owner's PC.

A Mistery whose owner switched the phone remote on keeps one WebSocket open to
this site (/relay/pc). A phone opens /remote/<id> (the page, remote_page.py) and
asks /r/<id>/... for the library, a picture, a song or a film; each request is
handed to that PC over its WebSocket, and the answer comes back the same way and
straight out to the phone. Nothing is kept: no request, no answer, no file.

What this service still never holds:

  - the phone's key. The link the PC shows is /remote/<id>#<key>, and what comes
    after # never leaves the phone; the page signs each request with it (HMAC)
    and the PC checks the signature. This service cannot make one, so it cannot
    ask anybody's PC for anything.
  - anything after the answer has gone. Bytes pass through memory on their way
    out, a chunk at a time, and that is all. Films and songs do pass through,
    which is what the owner chose (2026-10-25, "anywhere, through the website");
    the PC sends phone-sized copies (AAC music, 720p film) to keep that small.
  - a cookie, a session or an account.

What it does hold, while a PC is connected: the PC's id (a hash of a secret the
PC keeps; showing the secret is how a PC proves the id is its own) and a count of
the bytes it has relayed today, so one PC cannot run up this site's bandwidth
without limit (RELAY_DAILY_BYTES), and no more than RELAY_MAX_PCS are connected.

The conversation with the PC, over its WebSocket:

    PC -> site   {"t": "hello", "secret": <64 hex>, "version": ...}
    site -> PC   {"t": "ready", "id": <32 hex>}
    site -> PC   {"t": "req", "n": 7, "method": "GET", "path": "/api/library",
                  "query": "", "headers": {...}, "body": <base64>}
    PC -> site   {"t": "head", "n": 7, "status": 200, "headers": {...}}
    PC -> site   binary: 4 bytes of n, big-endian, then up to CHUNK bytes of body
    PC -> site   {"t": "end", "n": 7}            (or {"t": "fail", "n": 7})
    site -> PC   {"t": "more", "n": 7, "bytes": k}   room for k more bytes
    site -> PC   {"t": "cancel", "n": 7}             the phone stopped listening

The PC sends no more than WINDOW bytes of one answer ahead of the phone (the
"more" messages give room back as the phone takes them), so a slow phone holds
its own answer back and nothing else, and this site's memory stays small.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import struct
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import remote_page

log = logging.getLogger("mistery.relay")

router = APIRouter()

CHUNK = 64 * 1024
WINDOW = 1024 * 1024                    # bytes a PC may send of one answer ahead of the phone
MAX_BODY = 64 * 1024                    # a phone's request body: commands, never files
MAX_WAITING = 12                        # requests one PC may be answering at once
HEAD_TIMEOUT = 45.0                     # the PC's first word on a request (a film's first segment takes a while)
IDLE_TIMEOUT = 90.0                     # between two pieces of one answer
_ID = re.compile(r"[0-9a-f]{32}")
_SECRET = re.compile(r"[0-9a-f]{64}")
# What a PC's answer may say about itself. Anything else it sends is dropped:
# no Set-Cookie, no Location to elsewhere, no header that changes how a browser
# treats this origin.
_PASSED = {"content-type", "content-length", "content-range", "accept-ranges", "etag",
           "last-modified", "cache-control"}
# What a phone's request may say to the PC: the signature, a range, a type.
_FORWARDED = {"range", "content-type", "if-none-match", "x-mistery-time", "x-mistery-nonce",
              "x-mistery-sig"}
# Whatever a PC sends, it is never run as a page on this origin.
RELAYED_CSP = "default-src 'none'; sandbox"


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


RELAY_MAX_PCS = _int_env("MISTERY_RELAY_MAX_PCS", 50)
RELAY_DAILY_BYTES = _int_env("MISTERY_RELAY_DAILY_GB", 40) * 1024 ** 3


def remote_id(secret: str) -> str:
    """The id a PC is reached by: from its secret, so only it can claim it."""
    return hashlib.sha256(b"mistery-remote:" + secret.encode("ascii")).hexdigest()[:32]


class _Answer:
    def __init__(self) -> None:
        self.head: asyncio.Future = asyncio.get_running_loop().create_future()
        self.body: asyncio.Queue = asyncio.Queue()
        self.finished = False


class _Pc:
    """One connected PC."""

    def __init__(self, socket: WebSocket, rid: str) -> None:
        self.socket = socket
        self.id = rid
        self.answers: dict[int, _Answer] = {}
        self.counter = 0
        self.sending = asyncio.Lock()
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.bytes_today = 0
        self.closed = False

    async def send(self, message: dict) -> None:
        if self.closed:
            return
        async with self.sending:
            try:
                await self.socket.send_text(json.dumps(message, separators=(",", ":")))
            except Exception:                   # noqa: BLE001 - a socket that has gone
                self.closed = True

    def counted(self, size: int) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.day:
            self.day, self.bytes_today = day, 0
        self.bytes_today += size

    def over_today(self) -> bool:
        self.counted(0)
        return self.bytes_today >= RELAY_DAILY_BYTES

    def fail_all(self) -> None:
        self.closed = True
        for answer in self.answers.values():
            if not answer.head.done():
                answer.head.set_result(None)
            answer.body.put_nowait(None)


PCS: dict[str, _Pc] = {}


@router.websocket("/relay/pc")
async def pc_socket(socket: WebSocket) -> None:
    await socket.accept()
    try:
        hello = json.loads(await asyncio.wait_for(socket.receive_text(), 15))
    except Exception:                           # noqa: BLE001 - not a Mistery
        await socket.close(code=4400)
        return
    secret = hello.get("secret") if isinstance(hello, dict) else None
    if hello.get("t") != "hello" or not isinstance(secret, str) or not _SECRET.fullmatch(secret):
        await socket.close(code=4400)
        return
    rid = remote_id(secret)
    old = PCS.get(rid)
    if old is None and len(PCS) >= RELAY_MAX_PCS:
        await socket.send_text(json.dumps({"t": "full"}))
        await socket.close(code=4429)
        return
    pc = _Pc(socket, rid)
    if old is not None:
        # The same PC again (Mistery restarted, the network changed): the new
        # connection is the one that works, and keeps today's count.
        pc.day, pc.bytes_today = old.day, old.bytes_today
        old.fail_all()
        try:
            await old.socket.close(code=4000)
        except Exception:                       # noqa: BLE001
            pass
    PCS[rid] = pc
    await pc.send({"t": "ready", "id": rid})
    log.info("a PC connected (%d now)", len(PCS))
    try:
        while True:
            message = await socket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                data = message["bytes"]
                if len(data) < 4:
                    continue
                answer = pc.answers.get(struct.unpack(">I", data[:4])[0])
                if answer is not None and not answer.finished:
                    answer.body.put_nowait(data[4:])
                continue
            try:
                said = json.loads(message.get("text") or "")
            except ValueError:
                continue
            if not isinstance(said, dict):
                continue
            answer = pc.answers.get(said.get("n")) if isinstance(said.get("n"), int) else None
            if answer is None:
                continue
            kind = said.get("t")
            if kind == "head" and not answer.head.done():
                answer.head.set_result(said)
            elif kind in ("end", "fail"):
                if not answer.head.done():
                    answer.head.set_result(None)
                answer.finished = True
                answer.body.put_nowait(None)
    except WebSocketDisconnect:
        pass
    finally:
        pc.fail_all()
        if PCS.get(rid) is pc:
            del PCS[rid]
        log.info("a PC went (%d now)", len(PCS))


def _problem(status: int, words: str) -> JSONResponse:
    return JSONResponse({"error": words}, status_code=status,
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": RELAYED_CSP})


@router.api_route("/r/{rid}/{path:path}", methods=["GET", "HEAD", "POST"])
async def relay(rid: str, path: str, request: Request) -> Response:
    if not _ID.fullmatch(rid):
        return _problem(404, "No such remote.")
    pc = PCS.get(rid)
    if pc is None or pc.closed:
        return _problem(503, "Your PC isn't connected right now. Mistery has to be running on it, "
                             "with the phone remote switched on.")
    if len(pc.answers) >= MAX_WAITING:
        return _problem(429, "Too much at once. Try again in a moment.")
    if pc.over_today():
        return _problem(429, "This PC has used today's share of the site. It starts again tomorrow (UTC).")
    body = b""
    if request.method == "POST":
        body = await request.body()
        if len(body) > MAX_BODY:
            return _problem(413, "That request is too big.")
    pc.counter += 1
    n = pc.counter
    answer = _Answer()
    pc.answers[n] = answer
    headers = {name: value[:512] for name, value in request.headers.items() if name in _FORWARDED}
    await pc.send({"t": "req", "n": n, "method": request.method, "path": "/" + path[:2000],
                   "query": request.scope.get("query_string", b"").decode("latin-1")[:2000],
                   "headers": headers, "body": base64.b64encode(body).decode("ascii")})
    pc.counted(len(body))
    try:
        head = await asyncio.wait_for(answer.head, HEAD_TIMEOUT)
    except asyncio.TimeoutError:
        head = None
    if head is None:
        pc.answers.pop(n, None)
        await pc.send({"t": "cancel", "n": n})
        return _problem(504, "Your PC didn't answer in time.")
    status = head.get("status") if isinstance(head.get("status"), int) else 502
    passed = {}
    for name, value in (head.get("headers") or {}).items():
        if isinstance(name, str) and isinstance(value, str) and name.lower() in _PASSED:
            passed[name.lower()] = value[:512]
    passed.setdefault("cache-control", "no-store")
    passed["content-security-policy"] = RELAYED_CSP

    async def body_out():
        done = False
        try:
            if request.method == "HEAD":
                done = True
                return
            while True:
                try:
                    chunk = await asyncio.wait_for(answer.body.get(), IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    return
                if chunk is None:
                    done = True
                    return
                pc.counted(len(chunk))
                yield chunk
                await pc.send({"t": "more", "n": n, "bytes": len(chunk)})
        finally:
            pc.answers.pop(n, None)
            if not done:
                await pc.send({"t": "cancel", "n": n})

    return StreamingResponse(body_out(), status_code=status if 100 <= status <= 599 else 502,
                             headers=passed)


# --- the page -------------------------------------------------------------------------------

@router.api_route("/remote", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def remote_help() -> Response:
    """/remote with no id: what this is and where the link comes from."""
    return HTMLResponse(remote_page.render_help(),
                        headers={"Content-Security-Policy": remote_page.CSP, "Cache-Control": "no-cache"})


@router.api_route("/remote/{rid}", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def remote(rid: str) -> Response:
    if not _ID.fullmatch(rid):
        return HTMLResponse(remote_page.render_help(), status_code=404,
                            headers={"Content-Security-Policy": remote_page.CSP})
    return HTMLResponse(remote_page.render(rid),
                        headers={"Content-Security-Policy": remote_page.CSP, "Cache-Control": "no-cache"})


def health() -> dict:
    """For /api/health: how many PCs are connected, nothing about which."""
    return {"remote_pcs": len(PCS)}
