"""Asking the router to forward one port while a movie night runs, and giving it back.

UPnP (the Internet Gateway Device protocol) is how a program asks a home router
to forward a port: find the router with SSDP (a multicast "who is a gateway?"
on UDP 1900), read its device description over HTTP, then send it SOAP actions.
Three of them matter: GetExternalIPAddress, AddPortMapping, DeletePortMapping.
GetSpecificPortMappingEntry asks who has a port before anything is deleted, so
neither a clash nor cleaning up after a crash can take away a forward that
belongs to another device.

Standard library only. Everything is bounded, because this runs while somebody
is waiting to start a film. Measured against a pretend router on 127.0.0.1:
nothing answering the search costs 2.0 s; a router that accepts and never
answers, or answers one byte every 0.3 s, is given up on at 4.0 s; a router
that answers opens a port in about 6 ms, the note on disk included. Replies are
size-capped and parsed strictly, since anything on the home network can answer
an SSDP search, and nothing an answer says can send Mistery to another machine.

Why it fails matters more than that it failed, so UpnpError says which in plain
words: no router answered; the router refused; the port is taken by another
device; or carrier-grade NAT, where the router's own "internet" address is a
private or shared one and no port it opens can ever be reached from outside.
Short of carrier-grade NAT, a failure here is not the end for friends
elsewhere: the invite still carries the internet address (stun asks for it),
so a port forwarded by hand works, and every message says how.

A forward that is opened is written down in the data folder first, and crossed
off when it is closed. A crash leaves the note behind, and cleanup_stale()
removes the forward at the next start; one this process still has open is a
movie night going on, and it leaves that alone.

Blocking calls, all of them: run them on a worker thread, never Qt's.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape

from .. import __version__
from ..config import data_dir

_log = logging.getLogger("party.upnp")

SSDP_GROUP = ("239.255.255.250", 1900)
# Asked for by name rather than with ssdp:all, which would wake every TV,
# printer and speaker in the house. A version-2 gateway answers version-1
# searches too, so these three find every kind there is.
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
# The services that can forward a port, best first. WANPPPConnection is what a
# DSL router that dials its own PPPoE connection offers instead of WANIPConnection.
SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)

LEASE = 3 * 3600            # the router forgets the forward by itself after this; renew() extends it
DESCRIPTION = "Mistery movie night"
PROTOCOL = "TCP"

DISCOVERY_WINDOW = 2.0      # how long to listen for a router; MX is 1, so one that answers does so within 1 s
SETTLE = 0.3                # after a first answer, how long to wait for the one that is our own gateway
BUDGET = 4.0                # the most any public call here takes, whatever the router does
CLOSE_BUDGET = 3.0
DESCRIPTION_LIMIT = 256 * 1024      # real device descriptions are 2-10 KB
SOAP_LIMIT = 64 * 1024              # real SOAP replies are under 1 KB
MAX_ANSWERS = 32                    # SSDP answers looked at; anything on the network may answer

RECORD_NAME = "movie-night-ports.json"

# UpnpError.kind
NO_ROUTER = "no-router"
REFUSED = "refused"
PORT_TAKEN = "port-taken"
CGNAT = "cgnat"
NO_INTERNET = "no-internet"

# The UPnP error codes a person can do something about, in their words.
_REFUSALS = {
    402: "it did not accept the request",
    501: "it could not do it",
    606: "UPnP is on, but it is not allowed to change anything",
    725: "it only allows forwards that last until they are removed",
    728: "its list of forwarded ports is full",
    729: "that port is already used by something set up on the router itself",
}


class UpnpError(Exception):
    """Why the port is not open, as a sentence for the person hosting.

    str(error) is the whole explanation. kind (NO_ROUTER, REFUSED, PORT_TAKEN,
    CGNAT, NO_INTERNET) is for code that shows different help for each; code is
    the router's UPnP error number when it gave one; address is the router's
    own internet address when that is the problem; router is the router's
    address on the home network, where its settings page usually is.
    """

    def __init__(self, kind: str, message: str, *, code: int | None = None,
                 address: str | None = None, router: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.address = address
        self.router = router


@dataclass(frozen=True)
class Gateway:
    location: str           # where its device description is
    control_url: str        # where its SOAP actions go
    service_type: str       # one of SERVICES
    host: str               # the router's address on the home network
    name: str = ""          # what it calls itself, for messages


@dataclass
class Mapping:
    external_port: int
    internal_port: int
    internal_client: str    # the address the router forwards to: put this in the invite
    external_ip: str        # the router's internet address: and this
    lease: int              # seconds; 0 means until removed (some old routers allow nothing else)
    control_url: str
    service_type: str
    router: str
    protocol: str = PROTOCOL
    description: str = DESCRIPTION
    created: float = field(default_factory=time.time)

    @property
    def expires(self) -> float:
        """time.time() when the router forgets it by itself; 0 for never."""
        return self.created + self.lease if self.lease else 0.0


# --- which address is ours ------------------------------------------------------

# An address nobody has (TEST-NET-1, RFC 5737). "Connecting" a UDP socket to it
# sends nothing, but makes Windows pick the address it would send from.
_ANYWHERE = "192.0.2.1"


def _route_address(target: str) -> str | None:
    """The local address this PC would send from to reach target. No traffic."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((target, 9))
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address == "0.0.0.0" else address


@dataclass(frozen=True)
class _Adapter:
    name: str
    physical: bool          # an Ethernet or Wi-Fi network card, not a tunnel or other software adapter
    address: str
    gateway: str | None
    metric: int


def lan_ip(toward: str | None = None) -> str | None:
    """This PC's address on the home network: what friends in the same house
    connect to, and what the router forwards the port to. None when offline.

    With toward (the router's address), simply the address used to reach it.

    Without, it is the address of the Ethernet or Wi-Fi adapter that has a
    gateway, not whatever the default route says. The two differ whenever a VPN
    is connected: NordVPN's tunnel, for one (NordLynx, 10.5.0.2), carries the
    default route, while the router and every device in the house are on the
    Ethernet adapter (192.168.1.x). The tunnel address would reach nobody at
    home, and a router cannot forward to it.
    """
    if toward:
        return _route_address(toward)
    return physical_ip() or _route_address(_ANYWHERE)


_lan_ip = lan_ip        # open_port's parameter of the same name (the contract's) hides it there


def physical_ip() -> str | None:
    """The Ethernet or Wi-Fi adapter's address (the one with a gateway; the
    lowest metric if there are several), or None. Unlike lan_ip() it never
    falls back to the default route, which can be a VPN's tunnel: this is the
    address stun asks from, and an answer by the tunnel would be the VPN's."""
    candidates = [a for a in _adapters() if a.physical and a.gateway]
    if candidates:
        return min(candidates, key=lambda a: a.metric).address
    return None


def vpn() -> str | None:
    """The name of the VPN that carries this PC's traffic ("NordLynx Tunnel"),
    or None: a tunnel adapter with a gateway that wins the default route over
    the real ones. Worth telling the host about, because a VPN can keep friends
    out: with its kill switch on, NordVPN lets nothing out beside its tunnel,
    so neither the router nor a STUN server can be asked (see stun)."""
    adapters = _adapters()
    real = [a.metric for a in adapters if a.physical and a.gateway]
    for adapter in adapters:
        if not adapter.physical and adapter.gateway and (not real or adapter.metric < min(real)):
            return adapter.name or "a VPN"
    return None


def _adapters() -> list[_Adapter]:
    """IPv4 adapters that are up, from Windows' GetAdaptersAddresses; [] elsewhere
    or if anything about it goes wrong (the callers all have a fallback)."""
    if sys.platform != "win32":
        return []
    try:
        return _windows_adapters()
    except Exception as exc:        # never worth failing a movie night over
        _log.debug("could not list network adapters: %s", exc)
        return []


def _windows_adapters() -> list[_Adapter]:
    import ctypes
    from ctypes import POINTER, Structure, c_int, c_ubyte, c_ulong, c_ulonglong, c_void_p, c_wchar_p

    class SocketAddress(Structure):
        _fields_ = [("sockaddr", c_void_p), ("length", c_int)]

    class Unicast(Structure):
        pass

    Unicast._fields_ = [("Length", c_ulong), ("Flags", c_ulong), ("Next", POINTER(Unicast)),
                        ("Address", SocketAddress)]

    class GatewayAddress(Structure):
        pass

    GatewayAddress._fields_ = [("Length", c_ulong), ("Reserved", c_ulong),
                               ("Next", POINTER(GatewayAddress)), ("Address", SocketAddress)]

    class Adapter(Structure):
        pass

    # IP_ADAPTER_ADDRESSES_LH as far as Ipv4Metric (224 bytes on 64-bit); the
    # rest of it is never read. Checked against ipconfig on this PC.
    Adapter._fields_ = [
        ("Length", c_ulong), ("IfIndex", c_ulong), ("Next", POINTER(Adapter)),
        ("AdapterName", c_void_p), ("FirstUnicastAddress", POINTER(Unicast)),
        ("FirstAnycastAddress", c_void_p), ("FirstMulticastAddress", c_void_p),
        ("FirstDnsServerAddress", c_void_p), ("DnsSuffix", c_wchar_p),
        ("Description", c_wchar_p), ("FriendlyName", c_wchar_p),
        ("PhysicalAddress", c_ubyte * 8), ("PhysicalAddressLength", c_ulong),
        ("Flags", c_ulong), ("Mtu", c_ulong), ("IfType", c_ulong), ("OperStatus", c_int),
        ("Ipv6IfIndex", c_ulong), ("ZoneIndices", c_ulong * 16), ("FirstPrefix", c_void_p),
        ("TransmitLinkSpeed", c_ulonglong), ("ReceiveLinkSpeed", c_ulonglong),
        ("FirstWinsServerAddress", c_void_p), ("FirstGatewayAddress", POINTER(GatewayAddress)),
        ("Ipv4Metric", c_ulong), ("Ipv6Metric", c_ulong),
    ]

    AF_INET = 2
    INCLUDE_GATEWAYS, SKIP_ANYCAST, SKIP_MULTICAST, SKIP_DNS = 0x80, 0x2, 0x4, 0x8
    ETHERNET, WIFI = 6, 71
    UP = 1
    get = ctypes.WinDLL("iphlpapi").GetAdaptersAddresses
    size = c_ulong(16 * 1024)
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = get(AF_INET, INCLUDE_GATEWAYS | SKIP_ANYCAST | SKIP_MULTICAST | SKIP_DNS,
                     None, buffer, ctypes.byref(size))
        if result != 111:           # ERROR_BUFFER_OVERFLOW: size now says how much
            break
    if result != 0:
        return []

    def ipv4(address: SocketAddress) -> str | None:
        if not address.sockaddr or address.length < 8:
            return None
        raw = ctypes.string_at(address.sockaddr, 8)
        if int.from_bytes(raw[:2], "little") != AF_INET:
            return None
        return socket.inet_ntoa(raw[4:8])

    found = []
    node = ctypes.cast(buffer, POINTER(Adapter))
    while node:
        adapter = node.contents
        if adapter.Length < ctypes.sizeof(Adapter):
            break                   # not the structure this was written for: trust none of it
        gateway = None
        hop = adapter.FirstGatewayAddress
        while hop and gateway is None:
            gateway = ipv4(hop.contents.Address)
            hop = hop.contents.Next
        unicast = adapter.FirstUnicastAddress
        while unicast and adapter.OperStatus == UP:
            address = ipv4(unicast.contents.Address)
            if address and not ipaddress.IPv4Address(address).is_link_local:
                found.append(_Adapter(
                    name=adapter.Description or adapter.FriendlyName or "",
                    physical=(adapter.IfType in (ETHERNET, WIFI)
                              and _hardware(adapter.IfIndex, adapter.IfType) is not False),
                    address=address, gateway=gateway, metric=adapter.Ipv4Metric))
            unicast = unicast.contents.Next
        node = adapter.Next
    return found


def _hardware(index: int, kind: int) -> bool | None:
    """Windows' own word on whether an interface is a real network card (the
    HardwareInterface flag in its MIB_IF_ROW2), or None when it will not say.

    The interface type is not enough: on the PC this was written on, two
    adapters that are not network cards call themselves Ethernet (Bluetooth
    tethering and the kernel debugger's), and a VPN's adapter can do the same
    (NordVPN's say they are virtual). Checked there against Get-NetAdapter,
    all 8 interfaces agreeing.
    """
    import ctypes

    row = ctypes.create_string_buffer(2048)         # MIB_IF_ROW2 is 1352 bytes on 64-bit Windows
    struct.pack_into("<I", row, 8, index)           # InterfaceIndex (with InterfaceLuid 0, this is used)
    try:
        if ctypes.WinDLL("iphlpapi").GetIfEntry2(row) != 0:
            return None
    except (OSError, AttributeError):
        return None
    if struct.unpack_from("<I", row, 1128)[0] != kind:
        return None                 # Type is not where it should be: trust nothing else in the row
    return bool(row.raw[1152] & 1)  # InterfaceAndOperStatusFlags.HardwareInterface


# --- the note of what is open ---------------------------------------------------

_record_lock = threading.Lock()


def record_path() -> Path:
    """Where open forwards are written down, so a crash cannot lose one."""
    return data_dir() / RECORD_NAME


def _read_record(path: Path) -> list[dict]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _log.warning("unreadable %s (%s): starting it again", path.name, exc)
        return []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _write_record(path: Path, entries: list[dict]) -> None:
    if not entries:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(entries, file, indent=1)
            file.flush()
            os.fsync(file.fileno())     # a note that only reaches the disk after the crash is no note
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _same_forward(entry: dict, mapping: Mapping) -> bool:
    return (entry.get("control_url") == mapping.control_url
            and entry.get("external_port") == mapping.external_port
            and entry.get("protocol") == mapping.protocol)


def _remember(mapping: Mapping) -> None:
    path = record_path()
    with _record_lock:
        entries = [e for e in _read_record(path) if not _same_forward(e, mapping)]
        try:
            _write_record(path, entries + [asdict(mapping)])
        except OSError as exc:
            # Not worth refusing a movie night over: without the note, a crash
            # leaves the forward on the router until its lease runs out.
            _log.warning("could not write %s: %s", path.name, exc)


def _forget(mapping: Mapping) -> None:
    path = record_path()
    with _record_lock:
        try:
            _write_record(path, [e for e in _read_record(path) if not _same_forward(e, mapping)])
        except OSError as exc:
            _log.warning("could not update %s: %s", path.name, exc)


# --- talking to the router -------------------------------------------------------

class _NoAnswer(OSError):
    """No complete answer in time. sent says whether the request went out at all:
    a forward that was asked for and never confirmed may still have been made."""

    def __init__(self, message: str, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


class _Refusal(Exception):
    """The router answered with an error: an HTTP status, and usually a UPnP code."""

    def __init__(self, status: int, code: int | None, description: str) -> None:
        super().__init__(f"HTTP {status}" + (f", UPnP error {code}" if code else "")
                         + (f": {description}" if description else ""))
        self.status = status
        self.code = code
        self.description = description
        self.holder: str | None = None      # for 718: which device has the port


class _Garbled(Exception):
    """An answer that is not what UPnP says an answer looks like."""


_HOME_NETWORKS = [ipaddress.IPv4Network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")]
_URL_CHARACTERS = re.compile(r"[!-~]+")        # printable ASCII, no spaces, no line breaks


def _home_address(address: str) -> bool:
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return any(parsed in network for network in _HOME_NETWORKS)


def _router_url(url: str, host: str | None = None) -> bool:
    """An http:// URL at an address on the home network (or this PC, for the
    tests), and at host when given. What an SSDP answer or a device description
    says comes from whatever answered, so it does not get to send Mistery
    anywhere else — not to the internet, not to a second machine."""
    if not isinstance(url, str) or len(url) > 512 or not _URL_CHARACTERS.fullmatch(url):
        return False
    try:
        parts = urlsplit(url)
        parts.port                              # raises on a port that is not a number
    except ValueError:
        return False
    if parts.scheme != "http" or not parts.hostname:
        return False
    if host is not None and parts.hostname != host:
        return False
    return _home_address(parts.hostname)


def _msearch(target: str, destination: tuple[str, int]) -> bytes:
    return ("M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {destination[0]}:{destination[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 1\r\n"
            f"ST: {target}\r\n\r\n").encode("ascii")


@dataclass(frozen=True)
class _Answer:
    source: str
    location: str
    server: str


def _parse_answer(data: bytes, source: str) -> _Answer | None:
    lines = data[:4096].decode("latin-1").splitlines()
    if not lines or not re.match(r"HTTP/1\.[01] 200\b", lines[0], re.IGNORECASE):
        return None
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    location = headers.get("location", "")
    # The description must be on the device that answered: an answer does not
    # get to send Mistery to some other machine on the network either.
    if not _home_address(source) or not _router_url(location, source):
        return None
    return _Answer(source, location, headers.get("server", "")[:100])


def _send_searches(sock: socket.socket, sends: list[tuple[str | None, tuple[str, int]]]) -> None:
    for interface, destination in sends:
        if interface:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
            except OSError as exc:
                _log.debug("cannot search from %s: %s", interface, exc)
                continue
        for target in SEARCH_TARGETS:
            try:
                sock.sendto(_msearch(target, destination), destination)
            except OSError as exc:
                _log.debug("M-SEARCH to %s:%d failed: %s", *destination, exc)
                break
        else:
            _log.debug("M-SEARCH (%d kinds of gateway) to %s:%d%s", len(SEARCH_TARGETS),
                       *destination, f" from {interface}" if interface else "")


def _search(ssdp_to: tuple[str, int], deadline: float) -> list[_Answer]:
    """The routers that answered an M-SEARCH, this PC's own gateway first."""
    adapters = _adapters()
    gateways = {a.gateway for a in adapters if a.physical and a.gateway}
    multicast = ipaddress.IPv4Address(ssdp_to[0]).is_multicast
    if multicast:
        interfaces = [a.address for a in adapters if a.physical] or [_route_address(_ANYWHERE)]
        sends = [(interface, ssdp_to) for interface in interfaces if interface]
        # Straight to the router too (UPnP 1.1 allows a unicast M-SEARCH).
        # Multicast leaves by the default route unless told otherwise, and with
        # a VPN connected that is the tunnel, where no router is listening.
        sends += [(None, (gateway, ssdp_to[1])) for gateway in sorted(gateways)]
    else:
        sends = [(None, ssdp_to)]

    answers: list[_Answer] = []
    started = time.monotonic()
    end = min(deadline, started + DISCOVERY_WINDOW)
    resend_at: float | None = started + 0.5
    settle_until: float | None = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        if not multicast:
            # One address asked: answer on the address it is asked from (127.0.0.1
            # in the tests) rather than on every interface.
            sock.bind((_route_address(ssdp_to[0]) or "", 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        _send_searches(sock, sends)
        while len(answers) < MAX_ANSWERS:
            now = time.monotonic()
            if resend_at is not None and now >= resend_at:
                resend_at = None
                if not answers:
                    _send_searches(sock, sends)     # once more, in case a datagram was lost
            stop = end if settle_until is None else min(end, settle_until)
            if now >= stop:
                break
            sock.settimeout(max(0.005, min(stop, resend_at or stop) - now))
            try:
                data, (source, _port) = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except ConnectionResetError:
                # Windows reports "nothing listens there" for an earlier UDP
                # send on the next receive (a router with UPnP switched off can
                # answer the unicast search that way). Not the end of the search.
                _log.debug("an address searched says nothing listens on UDP %d", ssdp_to[1])
                continue
            except OSError as exc:
                _log.debug("SSDP receive failed: %s", exc)
                break
            answer = _parse_answer(data, source)
            if answer is None or any(a.location == answer.location for a in answers):
                continue
            _log.debug("SSDP answer from %s after %.0f ms: %s (%s)", source,
                       (time.monotonic() - started) * 1000, answer.location, answer.server)
            answers.append(answer)
            if source in gateways:
                break                               # our own router: nothing better is coming
            if settle_until is None:
                settle_until = time.monotonic() + SETTLE
    answers.sort(key=lambda a: a.source not in gateways)
    return answers


def _http(method: str, url: str, deadline: float, body: bytes = b"",
          headers: dict | None = None, limit: int = SOAP_LIMIT) -> tuple[int, bytes]:
    """One HTTP exchange with the router, over by deadline whatever it does.

    Not urllib: its timeout is per read, so a router sending a byte every
    second (or a device on the network pretending to be one) could hold a
    movie night up for as long as it liked. Here every read gets only the
    time that is left, and the answer is capped at limit bytes.
    """
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 80
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    lines = [f"{method} {target} HTTP/1.1", f"HOST: {host}:{port}", "CONNECTION: close",
             f"USER-AGENT: Windows UPnP/1.1 Mistery/{__version__}"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    if method == "POST":
        lines.append(f"CONTENT-LENGTH: {len(body)}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body

    left = deadline - time.monotonic()
    if left <= 0:
        raise _NoAnswer("no time left to ask", sent=False)
    try:
        sock = socket.create_connection((host, port), timeout=left)
    except OSError as exc:
        raise _NoAnswer(f"could not connect to {host}:{port}: {exc}", sent=False) from None
    with sock:
        try:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            sock.sendall(request)
        except OSError as exc:
            raise _NoAnswer(f"could not send to {host}:{port}: {exc}", sent=True) from None
        received = b""
        while True:
            answer = _complete(received, closed=False)
            if answer is not None:
                return answer
            left = deadline - time.monotonic()
            if left <= 0:
                raise _NoAnswer(f"{host}:{port} did not answer in time", sent=True)
            sock.settimeout(left)
            try:
                chunk = sock.recv(16384)
            except OSError as exc:
                raise _NoAnswer(f"{host}:{port} did not answer: {exc}", sent=True) from None
            if not chunk:
                answer = _complete(received, closed=True)
                if answer is None:
                    raise _NoAnswer(f"{host}:{port} hung up without answering", sent=True)
                return answer
            received += chunk
            if len(received) > limit:
                raise _Garbled(f"{host}:{port} sent more than {limit} bytes")


def _complete(data: bytes, closed: bool) -> tuple[int, bytes] | None:
    """(status, body) once data holds a whole HTTP response, else None."""
    head, blank, rest = data.partition(b"\r\n\r\n")
    if not blank:
        if closed and data:
            raise _Garbled("an answer with no end to its headers")
        return None
    lines = head.decode("latin-1").split("\r\n")
    status = re.match(r"HTTP/1\.[01] (\d{3})", lines[0])
    if not status:
        raise _Garbled(f"not an HTTP answer: {lines[0][:40]!r}")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _dechunk(rest)
        if body is None and closed:
            raise _Garbled("a chunked answer cut short")
        return None if body is None else (int(status.group(1)), body)
    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError:
            raise _Garbled("a Content-Length that is not a number") from None
        if length < 0:
            raise _Garbled("a negative Content-Length")
        if len(rest) >= length:
            return int(status.group(1)), rest[:length]
        if closed:
            raise _Garbled("an answer shorter than it said it was")
        return None
    return (int(status.group(1)), rest) if closed else None


def _dechunk(data: bytes) -> bytes | None:
    body, position = b"", 0
    while True:
        line_end = data.find(b"\r\n", position)
        if line_end < 0:
            return None
        try:
            size = int(data[position:line_end].split(b";")[0].strip(), 16)
        except ValueError:
            raise _Garbled("a chunk size that is not a number") from None
        if size == 0:
            return body
        start = line_end + 2
        if len(data) < start + size + 2:
            return None
        body += data[start:start + size]
        position = start + size + 2


_DECLARATION = re.compile(rb"<!(?!--|\[CDATA\[)")


def _xml(data: bytes) -> ET.Element:
    """A router's XML, refusing any with a DTD: no UPnP document has one, and
    entity declarations are how a small reply is made to expand into gigabytes."""
    if _DECLARATION.search(data):
        raise _Garbled("XML with a document type declaration")
    try:
        return ET.fromstring(data.strip())      # some routers send a blank line before <?xml
    except ET.ParseError as exc:
        raise _Garbled(f"not XML: {exc}") from None


def _name(element: ET.Element) -> str:
    """The tag without its namespace: routers disagree about namespaces."""
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _display_name(texts: dict) -> str:
    name = texts.get("friendlyName") or " ".join(
        filter(None, (texts.get("manufacturer"), texts.get("modelName"))))
    return " ".join("".join(c for c in name if c.isprintable()).split())[:60]


def _read_description(location: str, deadline: float) -> Gateway:
    status, body = _http("GET", location, deadline, limit=DESCRIPTION_LIMIT)
    if status != 200:
        raise _Garbled(f"its description answered HTTP {status}")
    root = _xml(body)
    host = urlsplit(location).hostname
    base = location
    texts: dict[str, str] = {}
    services = []
    for element in root.iter():
        name = _name(element)
        if name == "URLBase" and (element.text or "").strip():
            base = element.text.strip()
        elif name in ("friendlyName", "manufacturer", "modelName") and name not in texts:
            texts[name] = (element.text or "").strip()
        elif name == "service":
            fields = {_name(child): (child.text or "").strip() for child in element}
            services.append((fields.get("serviceType", ""), fields.get("controlURL", "")))
    for wanted in SERVICES:
        for service_type, control in services:
            if service_type == wanted and control:
                control_url = urljoin(base, control)
                if _router_url(control_url, host):
                    return Gateway(location, control_url, service_type, host, _display_name(texts))
    raise _Garbled("it offers no service that can forward a port")


def _soap(control_url: str, service_type: str, action: str, arguments: list[tuple[str, object]],
          deadline: float) -> dict[str, str]:
    """One UPnP action; the values the router answered with, by name."""
    body = ('<?xml version="1.0"?>\r\n'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service_type}">'
            + "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in arguments)
            + f"</u:{action}></s:Body></s:Envelope>\r\n").encode("utf-8")
    _log.debug("SOAP %s to %s", action, control_url)
    status, reply = _http("POST", control_url, deadline, body, {
        "CONTENT-TYPE": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{service_type}#{action}"',
    })
    if status == 200:
        for element in _xml(reply).iter():
            if _name(element) == action + "Response":
                return {_name(child): (child.text or "").strip() for child in element}
        raise _Garbled(f"no {action}Response in its answer")
    code, description = None, ""
    try:
        for element in _xml(reply).iter():
            if _name(element) == "errorCode":
                code = int((element.text or "").strip())
            elif _name(element) == "errorDescription":
                description = " ".join((element.text or "").split())[:100]
    except (_Garbled, ValueError):
        pass
    raise _Refusal(status, code, description)


# --- what the rest of Mistery calls ---------------------------------------------

# One conversation with the router at a time: a cleanup and a movie night
# starting at the same moment must not interleave their requests.
_router_lock = threading.Lock()

# The forwards this process opened and has not closed yet: movie nights going
# on now. The note in the data folder cannot tell one of those from what a
# crash left behind (the lock only puts the two calls in order: a cleanup that
# ran after open_port would find the new forward in the note, see it pointing
# at this PC, and delete it). This can. Guarded by _router_lock.
_open_here: set[tuple[str, int, str]] = set()


def _key(mapping: Mapping) -> tuple[str, int, str]:
    return mapping.control_url, mapping.external_port, mapping.protocol


# When UPnP fails, the invite still carries the internet address (stun asks a
# public server for it), so a port forwarded by hand just works: every message
# says what works now and what to do for everyone else, never "only at home" —
# except where that is the truth (carrier-grade NAT, no internet at all).
_AT_HOME = " Friends at your place can join now."


def _called(name: str) -> str:
    return f" ({name})" if name else ""


def _manual(port: int | None, client: str | None, alternative: str = "") -> str:
    what = f"TCP port {port} to this PC ({client})" if port and client else "the movie night's port to this PC"
    return f" For friends elsewhere, forward {what} in your router's settings{alternative}."


def _no_router(port: int | None, client: str | None) -> UpnpError:
    tunnel = vpn()
    # The VPN is named because it is the other likely reason. The PC this was
    # written on had one connected when its router did not answer at all (not
    # to 12 searches in 2 s, multicast and straight to 192.168.1.254 alike),
    # and that VPN let nothing else out beside its tunnel either (see stun).
    why = (f" UPnP is probably switched off in its settings, or the VPN on this PC ({tunnel}) is "
           "in the way." if tunnel else " UPnP is probably switched off in its settings.")
    return UpnpError(NO_ROUTER, "Your router didn't answer when Mistery asked it to open a port." + why
                     + _AT_HOME + _manual(port, client, ", or switch UPnP on there"))


def _went_quiet(name: str, router: str, port: int | None, client: str | None,
                problem: Exception | None = None) -> UpnpError:
    """A router that stopped answering, or answered nonsense: either way, no port."""
    said = ("answered, but not in a way Mistery could understand" if isinstance(problem, _Garbled)
            else "answered, then stopped responding")
    return UpnpError(NO_ROUTER, f"Your router{_called(name)} {said}." + _AT_HOME
                     + _manual(port, client), router=router)


def _why(refusal: _Refusal) -> str:
    said = _REFUSALS.get(refusal.code) or refusal.description or f"HTTP {refusal.status}"
    return f"{said} (UPnP error {refusal.code})" if refusal.code else said


def _refused(refusal: _Refusal, mapping: Mapping, name: str) -> UpnpError:
    if refusal.code == 718:
        holder = f" ({refusal.holder})" if refusal.holder else ""
        return UpnpError(
            PORT_TAKEN, f"Your router has already given port {mapping.external_port} to another "
            f"device on your network{holder}." + _AT_HOME + " For friends elsewhere, choose a "
            "different port in Settings → Movie night, or remove that forward in the router's "
            "settings.", code=718, router=mapping.router)
    return UpnpError(
        REFUSED, f"Your router{_called(name)} would not open port {mapping.external_port}: "
        f"{_why(refusal)}." + _AT_HOME + _manual(mapping.external_port, mapping.internal_client)
        + f" Its settings page is usually at http://{mapping.router}/.",
        code=refusal.code, router=mapping.router)


_SHARED = ipaddress.IPv4Network("100.64.0.0/10")    # RFC 6598, carrier-grade NAT


def _unreachable(address: str) -> tuple[str, str] | None:
    """Why nobody on the internet could reach a router whose own internet
    address is this, or None when they could."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        parsed = None
    if (parsed is None or parsed.is_unspecified or parsed.is_loopback or parsed.is_link_local
            or parsed.is_multicast or parsed.is_reserved):
        return NO_INTERNET, ("Your router answered, but says it has no internet address at the "
                             "moment, so only friends at your place can join. Check that it is "
                             "online, then try again.")
    if parsed in _SHARED:
        return CGNAT, (
            "Your internet provider shares one internet address between many homes "
            f"(carrier-grade NAT: your router's own address is {address}), so no port it opens "
            "can be reached from outside, and a port forwarded by hand cannot be either."
            + _AT_HOME + " For friends elsewhere, one of them can host instead, or you can ask "
            "your provider for a public IP address.")
    if not parsed.is_global:
        return CGNAT, (
            f"Your router is connected through another router (its own internet address, {address}, "
            "is a private one), so a port it opens cannot be reached from outside." + _AT_HOME
            + " For friends elsewhere, forward the port on the other router too, or let one of "
            "them host.")
    return None


def find_gateway(gateway_url: str | None = None, *, timeout: float = BUDGET,
                 ssdp_to: tuple[str, int] = SSDP_GROUP) -> Gateway:
    """The router that can forward ports. Searched for with SSDP, or read straight
    from gateway_url (a device description's address) with no search at all,
    which is how the tests point this at a pretend router on 127.0.0.1. ssdp_to
    is where the search goes, the multicast group unless a test says otherwise.
    UpnpError(NO_ROUTER) when there is none."""
    return _find(gateway_url, time.monotonic() + timeout, ssdp_to)


def _find(gateway_url: str | None, deadline: float, ssdp_to: tuple[str, int] = SSDP_GROUP,
          port: int | None = None, client: str | None = None) -> Gateway:
    if gateway_url:
        if not _router_url(gateway_url):
            raise ValueError(f"not an http:// address on the home network: {gateway_url!r}")
        places = [(urlsplit(gateway_url).hostname, gateway_url)]
    else:
        answers = _search(ssdp_to, deadline)
        if not answers:
            raise _no_router(port, client)
        places = [(answer.source, answer.location) for answer in answers]
    problems = []
    for source, location in places:
        try:
            return _read_description(location, deadline)
        except (_NoAnswer, _Garbled) as exc:
            _log.info("%s: %s", location, exc)
            problems.append(exc)
    if gateway_url and isinstance(problems[0], _NoAnswer) and not problems[0].sent:
        raise _no_router(port, client)          # nothing listening there at all
    if any(isinstance(p, _Garbled) and "no service" in str(p) for p in problems):
        raise UpnpError(REFUSED, f"A router answered ({places[0][0]}), but it does not offer port "
                        "forwarding through UPnP." + _AT_HOME + _manual(port, client),
                        router=places[0][0])
    raise _went_quiet("", places[0][0], port, client, problems[0])


def _external_address(gateway: Gateway, deadline: float, port: int | None = None,
                      client: str | None = None) -> str:
    try:
        reply = _soap(gateway.control_url, gateway.service_type, "GetExternalIPAddress", [], deadline)
    except (_NoAnswer, _Garbled) as exc:
        _log.info("GetExternalIPAddress: %s", exc)
        raise _went_quiet(gateway.name, gateway.host, port, client, exc) from None
    except _Refusal as refusal:
        raise UpnpError(REFUSED, f"Your router{_called(gateway.name)} would not say what its "
                        f"internet address is: {_why(refusal)}." + _AT_HOME
                        + _manual(port, client), code=refusal.code, router=gateway.host) from None
    address = reply.get("NewExternalIPAddress", "").strip()
    problem = _unreachable(address)
    if problem:
        raise UpnpError(problem[0], problem[1], address=address or None, router=gateway.host)
    return address


def external_ip(gateway_url: str | None = None, timeout: float = BUDGET) -> str:
    """The router's internet address, the one friends elsewhere connect to.

    UpnpError when no router answers, and also when the address it reports
    could not be reached from outside anyway: CGNAT when it is shared
    (100.64.0.0/10) or private (another router in front), NO_INTERNET when
    the router has none. error.address has the address in those cases.
    """
    deadline = time.monotonic() + timeout
    return _external_address(_find(gateway_url, deadline), deadline)


def open_port(internal_port: int, lan_ip: str | None = None, lease: int = LEASE,
              gateway_url: str | None = None, *, timeout: float = BUDGET) -> Mapping:
    """Ask the router to forward TCP internal_port, the same number outside, to
    this PC. The Mapping says what it agreed to: internal_client and
    external_ip are the two addresses that go in the invite.

    lan_ip is the address to forward to. None is the better choice: the
    address this PC reaches the router from, known once the router is found.

    Nothing is asked for when the router's own internet address shows that no
    port could be reached anyway (carrier-grade NAT): that is UpnpError(CGNAT)
    straight after GetExternalIPAddress. The forward is written down in the
    data folder before the router is asked, and crossed off by close_port().
    """
    if not isinstance(internal_port, int) or not 1 <= internal_port <= 65535:
        raise ValueError(f"not a port: {internal_port!r}")
    started = time.monotonic()
    deadline = started + timeout
    with _router_lock:
        fallback = lan_ip or _lan_ip()
        gateway = _find(gateway_url, deadline, port=internal_port, client=fallback)
        external = _external_address(gateway, deadline, internal_port, fallback)
        client = lan_ip or _route_address(gateway.host) or fallback
        if not client:
            raise _went_quiet(gateway.name, gateway.host, None, None)
        mapping = Mapping(external_port=internal_port, internal_port=internal_port,
                          internal_client=client, external_ip=external, lease=max(0, int(lease)),
                          control_url=gateway.control_url, service_type=gateway.service_type,
                          router=gateway.host)
        _remember(mapping)          # before asking: a crash mid-request still leaves the note
        try:
            _add(mapping, deadline)
        except _Refusal as refusal:
            _forget(mapping)
            raise _refused(refusal, mapping, gateway.name) from None
        except (_NoAnswer, _Garbled) as exc:
            if isinstance(exc, _NoAnswer) and not exc.sent:
                _forget(mapping)    # never asked, so nothing to clean up later
            _log.info("AddPortMapping: %s", exc)
            raise _went_quiet(gateway.name, gateway.host, internal_port, client, exc) from None
        _remember(mapping)          # the lease may have changed (error 725)
        _open_here.add(_key(mapping))
    _log.info("port %d forwarded by %s%s to %s, lease %s, in %.0f ms", internal_port, gateway.host,
              _called(gateway.name), client, f"{mapping.lease} s" if mapping.lease else "permanent",
              (time.monotonic() - started) * 1000)
    return mapping


def renew(mapping: Mapping, timeout: float = BUDGET) -> Mapping:
    """Ask for the same forward again with a fresh lease. A movie night can
    outlast the 3-hour lease, so call this well before mapping.expires.
    UpnpError, as open_port."""
    deadline = time.monotonic() + timeout
    with _router_lock:
        try:
            _add(mapping, deadline)
        except _Refusal as refusal:
            raise _refused(refusal, mapping, "") from None
        except (_NoAnswer, _Garbled) as exc:
            _log.info("AddPortMapping (renewal): %s", exc)
            raise _went_quiet("", mapping.router, mapping.external_port,
                              mapping.internal_client, exc) from None
        _remember(mapping)
        _open_here.add(_key(mapping))
    return mapping


def _add(mapping: Mapping, deadline: float) -> None:
    """AddPortMapping, and the two refusals that have a way round."""
    for attempt in range(3):
        try:
            _soap(mapping.control_url, mapping.service_type, "AddPortMapping", [
                ("NewRemoteHost", ""), ("NewExternalPort", mapping.external_port),
                ("NewProtocol", mapping.protocol), ("NewInternalPort", mapping.internal_port),
                ("NewInternalClient", mapping.internal_client), ("NewEnabled", 1),
                ("NewPortMappingDescription", mapping.description),
                ("NewLeaseDuration", mapping.lease)], deadline)
            mapping.created = time.time()
            return
        except _Refusal as refusal:
            if refusal.code == 725 and mapping.lease:
                # Some version-1 routers only make forwards that last until they
                # are removed. The note in the data folder is what gets this one
                # removed if Mistery never gets to close it.
                mapping.lease = 0
                continue
            if refusal.code == 718 and attempt == 0:
                try:
                    owner = _owner(mapping, deadline)
                except (_Refusal, _NoAnswer, _Garbled):
                    owner = "unknown"
                if owner == "ours":
                    # Ours already, with an older description or lease the
                    # router will not overwrite: take it off, then ask again.
                    _delete(mapping, deadline)
                    continue
                if owner not in ("gone", "unknown"):
                    refusal.holder = owner
            raise
    raise _Refusal(500, None, "it kept refusing")


def _owner(mapping: Mapping, deadline: float) -> str:
    """Who the router forwards this port to now: "ours", "gone", "unknown" (a
    router without GetSpecificPortMappingEntry), or the other device's address."""
    try:
        entry = _soap(mapping.control_url, mapping.service_type, "GetSpecificPortMappingEntry", [
            ("NewRemoteHost", ""), ("NewExternalPort", mapping.external_port),
            ("NewProtocol", mapping.protocol)], deadline)
    except _Refusal as refusal:
        if refusal.code == 714:             # NoSuchEntryInArray
            return "gone"
        if refusal.code in (401, 602):      # no such action / not implemented
            return "unknown"
        raise
    client = entry.get("NewInternalClient", "")
    if client == mapping.internal_client and entry.get("NewInternalPort") == str(mapping.internal_port):
        return "ours"
    return client or "another device"


def _delete(mapping: Mapping, deadline: float) -> None:
    """DeletePortMapping. A router that no longer has the forward counts as done."""
    try:
        _soap(mapping.control_url, mapping.service_type, "DeletePortMapping", [
            ("NewRemoteHost", ""), ("NewExternalPort", mapping.external_port),
            ("NewProtocol", mapping.protocol)], deadline)
    except _Refusal as refusal:
        if refusal.code != 714:     # NoSuchEntryInArray: gone already (lease ran out, router restarted)
            raise


def close_port(mapping: Mapping, timeout: float = CLOSE_BUDGET) -> bool:
    """Take the forward off the router. True once the router has confirmed it is
    gone, or says it never had it. False when it did not answer: the note stays
    in the data folder, and the next cleanup_stale() tries again.

    Never raises. Ending a movie night has to work whatever the router is doing.
    """
    deadline = time.monotonic() + timeout
    with _router_lock:
        # No longer a movie night going on, whatever the router says next: if
        # it does not answer, the forward is a leftover like any other.
        _open_here.discard(_key(mapping))
        try:
            _delete(mapping, deadline)
        except (_NoAnswer, _Garbled, _Refusal) as exc:
            _log.warning("port %d is still open on %s: %s", mapping.external_port, mapping.router, exc)
            return False
        _forget(mapping)
    _log.info("port %d closed on %s", mapping.external_port, mapping.router)
    return True


def cleanup_stale(record: Path | None = None, timeout: float = CLOSE_BUDGET) -> bool:
    """Take off the router any forward an earlier run opened and never closed
    (Mistery ended in Task Manager, the PC switched off mid-film), and cross it
    off the note. record is the note, record_path() when None.

    Call it at start, on a worker thread. A forward is only deleted while the
    router still has it pointing at this PC: once its lease is over the router
    has forgotten it by itself, and a port that has since gone to another
    device is left alone. A forward this process opened and has not closed is a
    movie night going on now, not a leftover, and is not touched, so calling
    this at any moment is safe. True when nothing stale is left; False when the
    router did not answer, and the note stays for next time.
    """
    path = Path(record) if record else record_path()
    deadline = time.monotonic() + timeout
    with _router_lock:
        left, stale = [], 0
        for entry in _read_record(path):
            mapping = _mapping_from(entry)
            if mapping is None:
                _log.warning("%s: dropping an entry that is not a forward: %r", path.name, entry)
                continue
            if _key(mapping) in _open_here:
                left.append(entry)
                continue
            if mapping.lease and time.time() > mapping.expires + 60:
                _log.info("port %d on %s ran out by itself", mapping.external_port, mapping.router)
                continue
            try:
                owner = _owner(mapping, deadline)
                if owner in ("ours", "unknown"):
                    # "unknown" too: inside the lease nothing else can have been
                    # given the port while ours held it, so it can only be ours.
                    _delete(mapping, deadline)
                    _log.info("removed port %d that an earlier run left open on %s",
                              mapping.external_port, mapping.router)
                elif owner != "gone":
                    _log.info("port %d on %s now goes to %s; left alone",
                              mapping.external_port, mapping.router, owner)
            except (_NoAnswer, _Garbled, _Refusal) as exc:
                _log.info("port %d on %s: not removed yet (%s)", mapping.external_port,
                          mapping.router, exc)
                left.append(entry)
                stale += 1
        with _record_lock:
            try:
                _write_record(path, left)
            except OSError as exc:
                _log.warning("could not update %s: %s", path.name, exc)
    return not stale


_MAPPING_FIELDS = {
    "external_port": int, "internal_port": int, "internal_client": str, "external_ip": str,
    "lease": int, "control_url": str, "service_type": str, "router": str, "protocol": str,
    "description": str, "created": (int, float),
}


def _mapping_from(entry: dict) -> Mapping | None:
    """A Mapping from the note, or None when the entry is not a believable one."""
    try:
        values = {name: entry[name] for name in _MAPPING_FIELDS}
    except KeyError:
        return None
    if not all(isinstance(values[name], kind) and not isinstance(values[name], bool)
               for name, kind in _MAPPING_FIELDS.items()):
        return None
    if (not 1 <= values["external_port"] <= 65535 or values["protocol"] not in ("TCP", "UDP")
            or values["service_type"] not in SERVICES or not _router_url(values["control_url"])):
        return None
    return Mapping(**values)
