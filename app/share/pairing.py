"""Adding a friend: the code you send, and the exchange it starts.

Pairing is the only time two Misterys meet without already knowing each other,
so it is the one moment worth being careful about. It goes like this:

  1. You press Add friend. Mistery makes a one-time secret, and shows a code —
     the same shape as a movie night invite, made by the same encoder, with a 2
     in front instead of a 1 so a code pasted into the wrong box is named
     rather than refused as a typo. The code carries this PC's addresses, the
     port, that secret, and the fingerprint of this install's certificate.
  2. Your friend pastes it. Their Mistery connects to the address in the code,
     checks the certificate against the fingerprint in it before sending
     anything (app/party/tls.connect), and only then sends the secret with a
     hello: who they are, what they are called, and their own certificate.
  3. This PC compares the secret with the one it is waiting for, in constant
     time, and answers with the same about itself. Both sides write the other
     down, and the code is spent.

Their side cannot show a certificate during the handshake, because this PC has
nothing to check it against yet — that is exactly what is being established.
So the pairing connection is the one that carries a certificate inside the
conversation rather than in the handshake, and the secret is what makes it
safe to believe: it was in a code that only your friend was given, it works
once, and it dies after a day.

Everything after pairing is the other way round: both ends show their
certificates during the handshake and neither sends a byte to anything whose
fingerprint is not written down (app/share/identity.py).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import time

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .. import __version__
from ..config import settings
from ..party import invite as invite_codes
from ..party import people
from ..party.tls import PinMismatch
from ..party.tls import connect as pinned_connect
from . import identity as ident

_log = logging.getLogger("share")

PROTOCOL = 1
GREETING = b"MISTERY-PAIR/1"
SECRET_BYTES = invite_codes.TOKEN_BYTES         # 104 bits, the same as a movie night token
CODE_LIFETIME = 24 * 3600                       # a code left in a chat overnight still works
MAX_LINE = 8 * 1024                             # a certificate is about 700 bytes of PEM
EXCHANGE_TIMEOUT = 20.0
PENDING_SETTING = "sharing_pending"

# What a person reads when it does not work.
NOT_A_FRIEND_CODE = ("That is a movie night invite, not a friend code. Ask them to open "
                     "Friends in their Mistery and press Get my code, then send you that code.")
NOT_AN_INVITE = ("That is a friend code, not a movie night invite. Paste it into "
                 "Friends → Add theirs instead.")
EXPIRED = ("That code has expired — they last for a day. Ask your friend for a new one.")
SPENT = ("That code has already been used. Ask your friend for a new one: a code adds "
         "one friend and is then finished.")
YOURSELF = "That is this PC's own code. A friend has to send you theirs."
REFUSED = ("Your friend's Mistery did not accept the code. Ask them for a new one: a code "
           "works once, and only for a day.")
NO_ANSWER = ("Couldn't reach your friend's PC. It has to be on, with Mistery running or "
             "sharing switched on, and if they are not at your place their router needs "
             "the port forwarded — their Friends page shows how.")
BROKEN = "Something answered, but it is not speaking Mistery's language. Ask for a new code."


class PairError(Exception):
    """Pairing that cannot go on, with a sentence a person can act on."""


# --- the code -----------------------------------------------------------------

def offer(port: int, lan_ip: str | None = None, wan_ip: str | None = None, *,
          fresh: bool = False) -> str:
    """The code to send a friend, and the secret behind it, kept until it is used.

    The same code comes back while it is still good, so opening the page twice
    does not quietly cancel the code already sitting in somebody's chat. `fresh`
    makes a new one, which is what a Make a new code button does — and says so,
    because it does cancel the old one.
    """
    secret = None if fresh else pending_secret()
    if secret is None:
        secret = secrets.token_bytes(SECRET_BYTES)
        settings.set(PENDING_SETTING, {"secret": secret.hex(), "made_at": time.time()})
    code = invite_codes.Invite(wan_ip=wan_ip, lan_ip=lan_ip, port=int(port), token=secret,
                               pin=ident.identity().pin[:invite_codes.PIN_BYTES],
                               kind=invite_codes.KIND_PAIR)
    return invite_codes.encode(code)


def link(code: str) -> str:
    """The website's /add#<code> page, which opens mistery://add/<code>: the
    link a friend can click in any chat."""
    return invite_codes.web_link(code, invite_codes.KIND_PAIR)


def pending_secret() -> bytes | None:
    """The secret this PC is waiting for, or None when there is none or it is old."""
    saved = settings.get(PENDING_SETTING)
    if not isinstance(saved, dict):
        return None
    made_at = saved.get("made_at")
    if not isinstance(made_at, (int, float)) or time.time() - made_at > CODE_LIFETIME:
        return None
    try:
        secret = bytes.fromhex(str(saved.get("secret", "")))
    except ValueError:
        return None
    return secret if len(secret) == SECRET_BYTES else None


def cancel() -> None:
    """Spend or withdraw the pending code. A code adds one friend, then is done."""
    settings.set(PENDING_SETTING, None)


def matches(offered: bytes) -> bool:
    """Whether that is the secret this PC is waiting for. Constant time."""
    waiting = pending_secret()
    if waiting is None or not isinstance(offered, (bytes, bytearray)):
        return False
    return hmac.compare_digest(bytes(offered), waiting)


def read_code(text: str) -> invite_codes.Invite:
    """The friend code in what somebody pasted, or PairError saying what it is.

    A movie night invite gets its own sentence rather than "that is wrong":
    the two codes look identical to a person, which is precisely why the kind
    is in the code.
    """
    try:
        found = invite_codes.decode(text)
    except invite_codes.InviteError as problem:
        raise PairError(str(problem)) from None
    if found.kind != invite_codes.KIND_PAIR:
        raise PairError(NOT_A_FRIEND_CODE)
    return found


# --- the exchange -------------------------------------------------------------

def _hello(kind: str, port: int, lan_ip: str | None, wan_ip: str | None) -> bytes:
    me = ident.identity()
    return _encode({
        "type": kind,
        "protocol": PROTOCOL,
        "person_id": me.person_id,
        "name": people.display_name(),
        "certificate": me.certificate_pem,
        "app": __version__,
        "port": int(port),
        "lan_ip": lan_ip,
        "wan_ip": wan_ip,
    })


def _encode(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8") + b"\n"


def _read_line(sock, deadline: float, dropped: str = BROKEN) -> dict:
    """One JSON line, at most MAX_LINE bytes, before the deadline.

    Bounded twice over: a peer that sends nothing hits the deadline, and one
    that pours bytes without a newline hits the size. Both are hangs otherwise.
    `dropped` is what a connection closed or reset under us means, which only
    the caller knows: to the side that has proved who it is talking to, a
    Mistery hanging up is a Mistery saying no.
    """
    buffer = bytearray()
    while b"\n" not in buffer:
        left = deadline - time.monotonic()
        if left <= 0:
            raise PairError(NO_ANSWER)
        sock.settimeout(left)
        try:
            chunk = sock.recv(4096)
        except (TimeoutError, socket.timeout):
            raise PairError(NO_ANSWER) from None
        except (OSError, ssl.SSLError):
            raise PairError(dropped) from None
        if not chunk:
            raise PairError(dropped)
        buffer += chunk
        if len(buffer) > MAX_LINE:
            raise PairError(BROKEN)
    line = bytes(buffer).split(b"\n", 1)[0]
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PairError(BROKEN) from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise PairError(BROKEN)
    return message


def _their_certificate(message: dict) -> tuple[str, bytes]:
    """(PEM, fingerprint) out of a hello, or PairError. Parsed, not trusted."""
    pem = message.get("certificate")
    if not isinstance(pem, str) or not 100 < len(pem) < MAX_LINE:
        raise PairError(BROKEN)
    try:
        certificate = x509.load_pem_x509_certificate(pem.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise PairError(BROKEN) from None
    der = certificate.public_bytes(serialization.Encoding.DER)
    return pem, ident.pin_of(der)


def _their_name(message: dict) -> tuple[str, str]:
    """(person id, name) out of a hello, cleaned the way movie night cleans them."""
    person = message.get("person_id")
    if not people.is_person_id(person):
        raise PairError(BROKEN)
    if person == ident.identity().person_id:
        raise PairError(YOURSELF)
    return person, people.clean_name(message.get("name")) or people.FALLBACK_NAME


def _address_kind(address: str | None) -> str:
    """Which column an address they answered on belongs in."""
    try:
        parsed = ipaddress.ip_address(address or "")
    except ValueError:
        return ""
    return "lan_ip" if parsed.is_private or parsed.is_loopback else "wan_ip"


def _remember(person_id: str, name: str, pem: str, pin: bytes, message: dict,
              seen_at: str | None) -> int:
    """Write the friend down, and switch sharing on if this is the first one."""
    from .. import db

    port = message.get("port")
    port = int(port) if isinstance(port, int) and 1 <= port <= 65535 else None
    addresses: dict[str, str | None] = {"lan_ip": None, "wan_ip": None}
    # The address this exchange actually went over first: it has just been
    # shown to work. What they say about themselves fills in the rest, and can
    # be wrong (a VPN's or a virtual machine's adapter, picked as "the" one).
    for value in (seen_at, message.get("lan_ip"), message.get("wan_ip")):
        column = _address_kind(value if isinstance(value, str) else None)
        if column and not addresses[column]:
            addresses[column] = value
    friend_id = db.add_friend(person_id, name, pin.hex(), pem, port=port, **addresses)
    if not settings.get("sharing_enabled"):
        # The owner asked for this: sharing switches itself on with the first
        # friend, because a friend who cannot see anything is not a friend yet.
        settings.set("sharing_enabled", True)
        _log.info("share: sharing switched on with the first friend")
    return friend_id


def accept(sock, peer: object, offered: bytes, *, port: int,
           lan_ip: str | None = None, wan_ip: str | None = None,
           timeout: float = EXCHANGE_TIMEOUT) -> int:
    """The side that made the code. Returns the new friend's id.

    `offered` is the secret that came with the greeting line. Nothing about the
    caller is believed until it matches, and a mismatch is answered with one
    word and a closed socket: a wrong code must not be told whether it was
    close, spent or never valid.
    """
    deadline = time.monotonic() + timeout
    if not matches(offered):
        _refuse(sock, "no")
        raise PairError(SPENT if settings.get(PENDING_SETTING) else EXPIRED)

    hello = _read_line(sock, deadline)
    if hello.get("type") != "hello":
        _refuse(sock, "what")
        raise PairError(BROKEN)
    if hello.get("protocol") != PROTOCOL:
        _refuse(sock, "protocol")
        raise PairError("Your friend's Mistery speaks a different version of pairing. "
                        "One of you needs to update Mistery.")
    pem, pin = _their_certificate(hello)
    person, name = _their_name(hello)

    seen_at = peer[0] if isinstance(peer, tuple) and peer else None
    friend_id = _remember(person, name, pem, pin, hello, seen_at)
    cancel()                                    # the code adds one friend and is spent
    sock.sendall(_hello("welcome", port, lan_ip, wan_ip))
    _log.info("share: %s (%s) is now a friend", name, pin.hex()[:16])
    return friend_id


def request(code: invite_codes.Invite, *, port: int, lan_ip: str | None = None,
            wan_ip: str | None = None, timeout: float = EXCHANGE_TIMEOUT) -> int:
    """The side that was given the code. Returns the new friend's id.

    Tries the home-network address first and the internet one after, the way a
    movie night guest does, and checks the certificate against the fingerprint
    in the code before a byte of ours goes out. What comes back has to be the
    same certificate: a friend's Mistery cannot hand us one identity in the
    handshake and write down another.
    """
    if code.kind != invite_codes.KIND_PAIR:
        raise PairError(NOT_A_FRIEND_CODE)
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    for address in (code.lan_ip, code.wan_ip):
        if not address:
            continue
        left = deadline - time.monotonic()
        if left <= 1:
            break
        try:
            sock = pinned_connect(address, code.port, code.pin, timeout=min(left, 8.0))
        except PinMismatch as problem:
            last = problem
            continue                    # something else at that address; try the next
        except OSError as problem:
            last = problem
            continue
        with sock:
            return _finish(sock, code, port, lan_ip, wan_ip, deadline)
    if isinstance(last, PinMismatch):
        raise PairError(str(last))
    raise PairError(NO_ANSWER)


def _finish(sock, code: invite_codes.Invite, port: int, lan_ip: str | None,
            wan_ip: str | None, deadline: float) -> int:
    # Their certificate matched the code before any of this was sent, so what
    # is at the other end is their Mistery. One that hangs up on us — even with
    # a reset that swallows the "no" it sent first, which Windows does when a
    # socket closes with our hello still unread — has refused the code, and the
    # sentence says so rather than "that is not Mistery". Seen once in the
    # pairing test while a suite loaded the PC.
    try:
        sock.sendall(GREETING + b" " + code.token.hex().encode("ascii") + b"\n")
        sock.sendall(_hello("hello", port, lan_ip, wan_ip))
    except (OSError, ssl.SSLError):
        raise PairError(REFUSED) from None
    welcome = _read_line(sock, deadline, dropped=REFUSED)
    if welcome.get("type") != "welcome":
        raise PairError(REFUSED)
    pem, pin = _their_certificate(welcome)
    if not hmac.compare_digest(pin[:len(code.pin)], code.pin):
        # The certificate in the handshake was the right one and this is not:
        # somebody is trying to be written down as somebody else.
        raise PairError("Your friend's Mistery sent a different certificate from the one in "
                        "the code. Nothing was saved. Ask them for a new code.")
    person, name = _their_name(welcome)
    seen_at = None
    try:
        seen_at = sock.getpeername()[0]
    except OSError:
        pass
    # Where the code said they are, for whatever their welcome left out: a
    # Mistery that has not asked for its internet address yet sends none, and
    # without one this PC could only ever find them on a home network.
    for key, known in (("lan_ip", code.lan_ip), ("wan_ip", code.wan_ip)):
        if known and not welcome.get(key):
            welcome[key] = known
    friend_id = _remember(person, name, pem, pin, welcome, seen_at)
    _log.info("share: added %s (%s) as a friend", name, pin.hex()[:16])
    return friend_id


def _refuse(sock, reason: str, *, linger: float = 1.0) -> None:
    """One word, then nothing. A wrong code learns only that it was wrong.

    Then read whatever they were still sending, for up to `linger` seconds and
    MAX_LINE bytes, before the caller closes. Closing a socket with their hello
    still unread makes Windows answer with a reset, and a reset can overtake
    the "no" and wipe it on their side before they read it. Reading their
    hello first lets the close be an ordinary one.
    """
    try:
        sock.sendall(_encode({"type": "refused", "reason": reason}))
    except OSError:
        return
    end = time.monotonic() + linger
    taken = 0
    while taken < MAX_LINE:
        left = end - time.monotonic()
        if left <= 0:
            return
        try:
            sock.settimeout(left)
            chunk = sock.recv(4096)
        except (OSError, ssl.SSLError):
            return
        if not chunk:
            return
        taken += len(chunk)
