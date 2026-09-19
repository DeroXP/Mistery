"""This PC's internet address, asked of a public STUN server when the router won't say.

A friend outside the house connects to the host's internet address, so the
invite has to carry it. UPnP's GetExternalIPAddress is the first way to learn
it. When the router does not answer UPnP at all, as on the PC this was written
on, this is the second, and it is what makes a port forwarded by hand usable. STUN
(RFC 5389) is the question every video call asks: one 20-byte UDP datagram to
a public server, whose answer says which address it arrived from.

It is asked from the Ethernet or Wi-Fi adapter's own address, never by the
default route. With a VPN such as NordVPN connected, the default route is the
VPN's tunnel, and asked that way Cloudflare answered in 13 ms with the tunnel's
exit address: a friend given that one would be knocking on a VPN server.
Windows sends a datagram out of the adapter that owns its source address (for
one from a 192.168.1.x address it picks the Ethernet adapter and the home
router, checked), which gets round the tunnel's routes, but not round a VPN
that drops everything outside its tunnel: asked that way, a PC with NordVPN's
kill switch on got no answer at all in 2 s. Then the
answer here is None, never the tunnel's, and friends elsewhere can only join
once the VPN is paused or lets Mistery bypass it (upnp.vpn() names it).

What goes out: a bare Binding request, with nothing about Mistery or the movie
night in it, to Cloudflare's server and then, if that has not answered,
Google's. What is believed: only an answer from the server that was asked (the
socket is connected, and Windows then drops datagrams from anyone else), with
the magic cookie and this request's own 96-bit transaction id, exactly as long
as it says it is and no longer than 548 bytes, whose XOR-MAPPED-ADDRESS (else
MAPPED-ADDRESS) is a public IPv4 address. Anything else is ignored and the wait
goes on, so a forged or broken datagram costs time, never a wrong address.

Behind carrier-grade NAT the answer is the carrier's address, which nobody can
forward a port on; upnp says so when the router answers, and nothing here can.

Blocking, for up to 2 s in all, the name lookups included: call it on a worker
thread. It shares nothing with upnp, so it can run while upnp.open_port does.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import socket
import struct
import threading
import time

from . import upnp

_log = logging.getLogger("party.stun")

SERVERS = (("stun.cloudflare.com", 3478), ("stun.l.google.com", 19302))
TIMEOUT = 2.0
# RFC 5389's first retransmission timeout. A server that answers at all does so
# in tens of milliseconds, so a request with no answer by then was most likely
# lost on the way, and is sent once more (the same transaction, so either copy's
# answer counts).
RESEND = 0.5
# RFC 5389 7.1: with the path's MTU unknown, 576 bytes less the IP and UDP
# headers. A real Binding response is under 100 bytes.
MAX_DATAGRAM = 548

COOKIE = 0x2112A442
_COOKIE_BYTES = COOKIE.to_bytes(4, "big")
_BINDING_REQUEST = 0x0001
_BINDING_SUCCESS = 0x0101
_BINDING_ERROR = 0x0111
_MAPPED_ADDRESS = 0x0001
_XOR_MAPPED_ADDRESS = 0x0020
_IPV4 = 0x01
_REFUSED = object()         # the server answered this request with an error: ask the next one


def public_ip(lan_ip: str | None = None, timeout: float = TIMEOUT,
              servers=SERVERS) -> str | None:
    """This PC's public IPv4 address as a STUN server sees it, or None.

    lan_ip is the address to ask from: upnp.lan_ip()'s, the Ethernet or Wi-Fi
    adapter's. None means upnp.physical_ip(), and when there is no such adapter
    nothing is sent at all. servers are (name or address, port) pairs, asked in
    order; the tests point them at a pretend server on 127.0.0.1.
    """
    started = time.monotonic()
    deadline = started + timeout
    source = lan_ip if lan_ip is not None else upnp.physical_ip()
    if not source:
        _log.info("STUN: no Ethernet or Wi-Fi adapter to ask from")
        return None
    servers = list(servers)
    for index, (host, port) in enumerate(servers):
        now = time.monotonic()
        if now >= deadline:
            break
        # Each server gets an equal share of the time left: Cloudflare half,
        # Google the rest. One that answers at all needs tens of milliseconds.
        until = now + (deadline - now) / (len(servers) - index)
        address = _resolve(host, port, until)
        if address is None:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.bind((source, 0))
            except OSError as exc:
                # Not an address of this PC any more: the adapter went down, or
                # the network changed. Asking by the default route instead could
                # be asking through a VPN, and no answer is better than that one.
                _log.info("STUN: cannot send from %s: %s", source, exc)
                return None
            try:
                sock.connect(address)
                answer = _ask(sock, until)
            except OSError as exc:
                _log.info("STUN: %s did not work: %s", host, exc)
                continue
        if answer:
            # Not the address itself: a log gets pasted into bug reports.
            _log.info("STUN: %s answered in %.0f ms", host, (time.monotonic() - started) * 1000)
            return answer
    _log.info("STUN: no answer in %.1f s", time.monotonic() - started)
    return None


def _resolve(host: str, port: int, until: float) -> tuple[str, int] | None:
    """host's IPv4 address, by until or not at all. getaddrinfo takes no
    timeout, and Windows gives a DNS server that does not answer several
    seconds, so the lookup runs on a thread of its own that nobody waits for
    past until."""
    try:
        return str(ipaddress.IPv4Address(host)), port       # an address already: nothing to look up
    except ValueError:
        pass
    found: list[tuple[str, int]] = []

    def look() -> None:
        try:
            found.append(socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][:2])
        except (OSError, IndexError) as exc:
            _log.info("STUN: cannot look up %s: %s", host, exc)

    lookup = threading.Thread(target=look, name="party-stun-lookup", daemon=True)
    lookup.start()
    lookup.join(max(0.0, until - time.monotonic()))
    return found[0] if found else None


def _ask(sock: socket.socket, until: float) -> str | None:
    """One Binding transaction on a connected socket: the address, or None by until."""
    transaction = secrets.token_bytes(12)
    request = struct.pack("!HHI", _BINDING_REQUEST, 0, COOKIE) + transaction
    sock.send(request)
    resend_at = time.monotonic() + RESEND if RESEND else None
    while True:
        now = time.monotonic()
        if now >= until:
            return None
        if resend_at is not None and now >= resend_at:
            sock.send(request)
            resend_at = None
        sock.settimeout(max(0.001, min(until, resend_at or until) - now))
        try:
            data = sock.recv(2048)
        except TimeoutError:
            continue
        except ConnectionResetError:
            # How Windows reports that nothing listens there: the "port
            # unreachable" that came back for the request, at the next receive.
            return None
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10040:     # over 2048 bytes: not a STUN answer
                continue
            raise
        answer = _parse(data, transaction)
        if answer is _REFUSED:
            return None
        if answer:
            return answer


def _parse(data: bytes, transaction: bytes):
    """The public IPv4 address in a Binding success response to transaction;
    _REFUSED for an error response to it; None for anything else."""
    if not 20 <= len(data) <= MAX_DATAGRAM:
        return None
    kind, length, cookie = struct.unpack_from("!HHI", data)
    if cookie != COOKIE or data[8:20] != transaction or length != len(data) - 20 or length % 4:
        return None             # not STUN, not ours, or cut short (or padded out) on the way
    if kind == _BINDING_ERROR:
        return _REFUSED
    if kind != _BINDING_SUCCESS:
        return None
    values: dict[int, bytes] = {}
    at = 20
    while at < len(data):       # a multiple of 4 all the way, so a header always fits
        attribute, size = struct.unpack_from("!HH", data, at)
        value, at = data[at + 4:at + 4 + size], at + 4 + size + -size % 4
        if at > len(data):
            return None         # an attribute that runs past the end of the message
        # Unknown attributes are skipped, even the ones RFC 5389 says to refuse
        # a response for: old servers still send RFC 3489's SOURCE-ADDRESS and
        # CHANGED-ADDRESS, and nothing here acts on anything but the address.
        values.setdefault(attribute, value)
    xor = _XOR_MAPPED_ADDRESS in values
    value = values[_XOR_MAPPED_ADDRESS] if xor else values.get(_MAPPED_ADDRESS)
    if value is None or len(value) != 8 or value[1] != _IPV4:
        return None             # no address, or not an IPv4 one
    raw = bytes(a ^ b for a, b in zip(value[4:], _COOKIE_BYTES)) if xor else value[4:]
    address = ipaddress.IPv4Address(raw)
    if not address.is_global or address.is_multicast:
        return None             # nothing a friend could connect to
    return str(address)
