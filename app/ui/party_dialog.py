"""Movie night's screens: starting one, the host's panel, and joining one.

- HostDialog. Before a movie night starts it says what is about to happen,
  including the question Windows Firewall is about to ask. While one runs it is
  the host's panel: the invite code to copy, who can get in, and when friends
  elsewhere can't yet, exactly what to type into the router's page.
- JoinDialog. Paste a code, watch it connect, or read why it didn't.
- MovieNightButton. The top bar's way in, named in words, lit while a movie
  night runs.

Both dialogs drive one PartySession (app/party/session.py), which owns the
port, the server, the sync and the player. What these screens add is words,
and the words matter most when something is in the way. On the PC this was
built on, the router does not answer UPnP at all (twelve searches in 2 s, not
one reply), so "forward the port by hand" is how every friend outside the house
gets in: that page is written to be followed without knowing what a port is.
Once it is done, and the host says so ("I've done this" under the steps, or the
box in Settings → Movie night), the panel says it in one calm line and keeps the
steps behind "Show how": that is the screen the owner sees most evenings.

Nothing here touches the network. What the host panel knows about the home
network (NetworkFacts) comes from Windows itself, read on a worker thread.
"""

from __future__ import annotations

import html
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from .. import db
from ..config import settings
from ..models import MediaItem
from ..util import fmt_clock
from .theme import C
from .widgets.empty import WrapLabel
from .widgets.flow import FlowLayout
from .widgets.icons import IconButton

_log = logging.getLogger("party.ui")

# "Works, but not for everyone": friends at your place can join and friends
# elsewhere can't yet. Green would claim too much and red would alarm; the
# theme has no amber of its own, and this is the only place that needs one.
_AMBER = "#F2B84B"

# The dialogs' width. The invite code's longer line (41 characters at 14pt)
# measures 497 px of the 552 inside its box; at 620 px wide with 15pt it
# needed 568 of the 556 there were, and the box was cut off on the right.
_WIDTH = 660


# --- the movie night mark -------------------------------------------------------

def paint_people(painter: QPainter, rect: QRectF, color: QColor, stroke: float = 1.85) -> None:
    """Two people, the one in front a little larger: the movie night mark.

    Authored in a 24x24 box like widgets/icons.py, and drawn here rather than
    added to its table, because nothing but movie night uses it.
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = min(rect.width(), rect.height()) / 24.0
    painter.translate(rect.center().x() - 12 * scale, rect.center().y() - 12 * scale)
    painter.scale(scale, scale)
    pen = QPen(color, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # The friend behind: a smaller head, and only the shoulder that shows.
    painter.drawEllipse(QPointF(16.4, 7.9), 2.7, 2.7)
    behind = QPainterPath()
    behind.moveTo(15.2, 12.9)
    behind.cubicTo(15.7, 12.7, 16.1, 12.6, 16.6, 12.6)
    behind.cubicTo(19.2, 12.6, 21.2, 14.5, 21.2, 18.2)
    painter.drawPath(behind)
    # The one in front.
    painter.drawEllipse(QPointF(9.0, 8.6), 3.4, 3.4)
    front = QPainterPath()
    front.moveTo(2.8, 20.0)
    front.cubicTo(2.8, 15.6, 5.6, 13.6, 9.0, 13.6)
    front.cubicTo(12.4, 13.6, 15.2, 15.6, 15.2, 20.0)
    painter.drawPath(front)
    painter.restore()


class MovieNightButton(IconButton):
    """The top bar's movie night button: Join when idle, the panel while one runs.

    The people mark and the words "Movie night". The mark alone, with only a
    tooltip to say what it was, sat among the icons at the far end of the bar,
    and a friend sent a code found Join by hovering over icons or by
    scrolling Home, whose Movie nights section starts at y=1164 of a 732 px
    page in a 1280x800 window. The top bar is on every page, Home's first
    screen included, so the words are there wherever a friend looks first.
    They cost the bar 80 px (116 wide, from 36), taken from the line of
    library news, which gives way first: at the 960 px minimum it keeps 30 px
    of the 110 it had; from 1280 up it still shows whole.

    An IconButton with no glyph of its own (paint_icon draws nothing for an
    unknown name), so the hover plate and colours are the top bar's. A red dot
    on the mark says a movie night is running.
    """

    LABEL = "Movie night"
    _INSET = 8          # the mark sits where it sat in the 36 px square button
    _GAP = 7
    _END = 12

    def __init__(self, parent=None) -> None:
        super().__init__("", size=36, icon_size=20,
                         tooltip="Movie night: join a friend's with their code", parent=parent)
        self._live = False
        self._fit_width()

    @property
    def live(self) -> bool:
        return self._live

    def set_live(self, live: bool, tooltip: str) -> None:
        self._live = bool(live)
        self.setToolTip(tooltip)
        self.update()

    def _fit_width(self) -> None:
        """As wide as the mark and the words need in the font it paints with.
        That font arrives with the top bar's style sheet, after construction,
        so a FontChange measures again."""
        words = self.fontMetrics().horizontalAdvance(self.LABEL)
        self.setFixedWidth(self._INSET + 20 + self._GAP + words + self._END)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._fit_width()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        hovered = self.underMouse() and self.isEnabled()
        color = QColor(C.TEXT if hovered or self._live else C.TEXT_DIM)
        icon = QRectF(self._INSET, (self.height() - 20) / 2, 20, 20)
        paint_people(painter, icon, color)
        painter.setPen(color)
        painter.setFont(self.font())
        words = QRectF(icon.right() + self._GAP, 0, self.width() - icon.right() - self._GAP, self.height())
        painter.drawText(words, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.LABEL)
        if self._live:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QColor(C.BG), 2.0))
            painter.setBrush(QColor(C.ACCENT))
            painter.drawEllipse(QPointF(icon.right() - 1.5, 9.5), 4.2, 4.2)


# --- what Windows knows about the home network ----------------------------------

@dataclass(frozen=True)
class NetworkFacts:
    """What the host panel says about this PC's network, read from Windows.

    lan_ip is where a forward must point and router is where its settings page
    is; network and category are how Windows files the home network, which
    decides the box to tick when Windows Firewall asks; computer is this PC's
    name, which is how a router's list of devices shows it. Any of them may be
    None (no network, an older Windows): the panel then says less, never
    something made up.
    """

    lan_ip: str | None = None
    router: str | None = None
    vpn: str | None = None              # the adapter carrying the default route, when it is a tunnel
    network: str | None = None          # "ATTa1b2c3d": ATT and seven characters on an AT&T gateway
    category: str | None = None         # "public" | "private" | "domain"
    computer: str | None = None         # "DESKTOP-7Q2K4LM"


def att_gateway(network: str | None, router: str | None) -> bool:
    """An AT&T gateway: they name the network ATT and seven characters and sit
    at 192.168.1.254, and there a forward lives under Firewall → NAT/Gaming."""
    return bool(network and network.startswith("ATT") and router == "192.168.1.254")


def vpn_name(adapter: str | None) -> str:
    """What people call their VPN. Windows names the adapter, and NordVPN's is
    "NordLynx Tunnel", a name nobody would recognise as the app they installed."""
    name = adapter or ""
    if "nordlynx" in name.lower() or "nordvpn" in name.lower():
        return "NordVPN"
    return f"Your VPN ({name})" if name else "Your VPN"


def look_up_network() -> NetworkFacts:
    """NetworkFacts for this PC. Local only: no packet leaves the PC.

    Run it off Qt's thread. On the PC this was written on it takes about 10 ms
    (83 ms the first time in a run, while the upnp module loads); nothing in it
    waits on the network.
    """
    from ..party import upnp

    lan = router = vpn = network = category = None
    try:
        lan = upnp.physical_ip() or upnp.lan_ip()
        # The gateway of the adapter that holds that address: the router's own
        # address on the home network, where its settings page is. upnp keeps
        # the adapter list to itself; it is read here, never changed.
        router = next((a.gateway for a in upnp._adapters() if a.address == lan), None)
        vpn = upnp.vpn()
    except Exception as exc:            # a missing fact is never worth an error on screen
        _log.debug("could not list the network adapters: %s", exc)
    if lan and sys.platform == "win32":
        try:
            network, category = _windows_network(lan)
        except Exception as exc:
            _log.debug("could not ask Windows about the network: %s", exc)
    return NetworkFacts(lan, router, vpn, network, category, computer_name())


def computer_name() -> str | None:
    """This PC's name, as a router's list of devices shows it: an AT&T
    gateway's NAT/Gaming page lists a PC by the name Windows gives it
    (COMPUTERNAME), so that first; the host name otherwise."""
    try:
        name = os.environ.get("COMPUTERNAME") or socket.gethostname()
    except OSError:
        return None
    name = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    return name[:63] or None


def _windows_network(lan_ip: str) -> tuple[str | None, str | None]:
    """The name Windows gives the network lan_ip is on, and how it files it.

    Through the Network List Manager (COM, called with ctypes; its methods are
    reached by their places in the vtable, which netlistmgr.h fixes). Every
    network connection names its adapter; the one whose interface holds
    lan_ip is the home network. A VPN is a second network with a category of
    its own, and friends at home never come in through it. Checked against
    Get-NetConnectionProfile on the PC this was written on: the same name,
    interface and category (Public).
    """
    import ctypes
    import struct
    from ctypes import POINTER, byref, c_int, c_ubyte, c_ulong, c_ulonglong, c_ushort, c_void_p

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort),
                    ("Data4", c_ubyte * 8)]

    def guid(text: str) -> GUID:
        value = GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(value))
        return value

    def method(obj: c_void_p, index: int, *argtypes):
        vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtable[index])

    def release(obj: c_void_p) -> None:
        if obj:
            vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
            ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtable[2])(obj)

    iphlpapi = ctypes.WinDLL("iphlpapi")

    # Which interface holds the address: GetIpAddrTable's rows are 24 bytes,
    # the address (network order) first and the interface index after it.
    size = c_ulong(0)
    iphlpapi.GetIpAddrTable(None, byref(size), False)
    table = ctypes.create_string_buffer(max(size.value, 4))
    if iphlpapi.GetIpAddrTable(table, byref(size), False) != 0:
        return None, None
    wanted = socket.inet_aton(lan_ip)
    rows = struct.unpack_from("<I", table, 0)[0]
    home = next((index for raw, index in (struct.unpack_from("<4sI", table, 4 + row * 24)
                                          for row in range(min(rows, 256))) if raw == wanted), None)
    if home is None:
        return None, None

    def interface_of(adapter: GUID) -> int | None:
        luid = c_ulonglong()
        index = c_ulong()
        if iphlpapi.ConvertInterfaceGuidToLuid(byref(adapter), byref(luid)) != 0:
            return None
        if iphlpapi.ConvertInterfaceLuidToIndex(byref(luid), byref(index)) != 0:
            return None
        return index.value

    ole32 = ctypes.oledll.ole32
    ole32.CoInitializeEx(None, 0)           # this thread's own apartment (multithreaded)
    manager, connections = c_void_p(), c_void_p()
    try:
        ole32.CoCreateInstance(byref(guid("{DCB00C01-570F-4A9B-8D69-199FDBA5723B}")), None, 0x17,
                               byref(guid("{DCB00000-570F-4A9B-8D69-199FDBA5723B}")), byref(manager))
        method(manager, 9, POINTER(c_void_p))(manager, byref(connections))    # GetNetworkConnections
        for _ in range(64):
            connection, fetched = c_void_p(), c_ulong()
            method(connections, 8, c_ulong, POINTER(c_void_p), POINTER(c_ulong))(
                connections, 1, byref(connection), byref(fetched))              # Next
            if not fetched.value:
                break
            network = c_void_p()
            try:
                adapter = GUID()
                method(connection, 12, POINTER(GUID))(connection, byref(adapter))    # GetAdapterId
                if interface_of(adapter) != home:
                    continue
                method(connection, 7, POINTER(c_void_p))(connection, byref(network))  # GetNetwork
                name = c_void_p()
                method(network, 7, POINTER(c_void_p))(network, byref(name))           # GetName
                text = ctypes.wstring_at(name.value) if name.value else ""
                ctypes.windll.oleaut32.SysFreeString(name)
                kind = c_int()
                method(network, 18, POINTER(c_int))(network, byref(kind))             # GetCategory
                return (text or None), {0: "public", 1: "private", 2: "domain"}.get(kind.value)
            finally:
                release(network)
                release(connection)
    finally:
        release(connections)
        release(manager)
        ole32.CoUninitialize()
    return None, None


def _program_name() -> str:
    """The name Windows Firewall's question will use. Mistery.bat starts
    pythonw.exe, whose file description is "Python" (python.exe's too, checked
    on the PC this was written on): a host told to allow "Mistery" would be
    looking for a name that is not on the box."""
    if getattr(sys, "frozen", False):
        return "Mistery"
    return "Python" if Path(sys.executable).stem.lower().startswith("python") else "Mistery"


def firewall_note(facts: NetworkFacts | None) -> str:
    """The Windows Firewall sentence, as rich text, for before the question comes.

    The first movie night on a PC makes Windows ask whether the program may
    accept connections. Cancel, or a tick only against the other kind of
    network, and nobody gets in, not even at home. The PC this was written on
    files its home network as Public: the box that matters there is Public
    networks, whatever the word "home" suggests. So the sentence names the box.
    """
    who = _program_name()
    runs = " (that is Mistery, which runs on Python)" if who == "Python" else ""
    ask = f"Windows will ask whether <b>{who}</b> may use the network{runs}."
    network = f" ({html.escape(facts.network)})" if facts and facts.network else ""
    category = facts.category if facts else None
    if category == "public":
        tick = (f"Choose <b>Allow access</b> with <b>Public networks</b> ticked: Windows files "
                f"your home network{network} as Public.")
    elif category in ("private", "domain"):
        tick = (f"Choose <b>Allow access</b> with <b>Private networks</b> ticked, the kind "
                f"your home network{network} is.")
    else:
        tick = ("Choose <b>Allow access</b>, with the box ticked for the kind of network you "
                "are on, Private or Public (Windows Settings → Network &amp; internet says which).")
    return f"{ask} {tick} If you choose Cancel, nobody can join — not even at home."


def firewall_fix() -> str:
    """For a host who already chose Cancel: Windows remembers it and never asks again."""
    who = _program_name()
    return ("Chose Cancel by mistake? Windows remembers and won't ask again. Open Windows "
            "Security → Firewall &amp; network protection → Allow an app through firewall → "
            f"Change settings, and tick <b>{who}</b> for your kind of network.")


VPN_LINE = ("Using a VPN? If friends can't get in, pause it during movie night, or let Mistery "
            "bypass it (split tunnelling).")


# --- the port, in the host's words ----------------------------------------------

@dataclass(frozen=True)
class PortView:
    """How the host panel shows the port: a tone, a headline, a paragraph, and
    where the steps for forwarding it by hand go: open under it (steps: still
    to do), behind a "Show how" (how: the owner says they are done), or nowhere."""

    tone: str               # "good" | "partial" | "noted" | "blocked" | "busy"
    title: str
    text: str               # rich text
    steps: bool = False
    how: bool = False


# "noted" is the owner's own forward: calm, and not green, because green here
# means a router that said yes, and nothing answered for this one.
_TONES = {"good": C.SUCCESS, "partial": _AMBER, "noted": C.INFO, "blocked": _AMBER,
          "busy": C.TEXT_FAINT}


def port_view(status, facts: NetworkFacts | None, upnp_on: bool = True) -> PortView:
    """The words for the session's PortStatus (app/party/session.py).

    state   checking  the port is being opened
            upnp      the router opened it: friends anywhere can join
            manual    it didn't, but the invite carries the internet address,
                      so a forward made by hand works: the steps go under it.
                      With status.forwarded (the owner has said they made that
                      forward) one calm line instead, the steps behind "Show how"
            lan       no internet address at all: the code works at home only
            cgnat     nothing outside can reach this PC: carrier-grade NAT,
                      another router in front, or a router with no internet
            failed    the router gave the port to another device
    The session's sentence (status.text) is shown as it is wherever the panel
    has no better words of its own; it names the real port and address.
    """
    if status is None:
        return PortView("busy", "Opening the port…", "")
    port = status.port or 42170
    text = html.escape(status.text or "")
    lan = status.lan_ip or (facts.lan_ip if facts else None)
    here = f"this PC ({lan})" if lan else "this PC"
    tunnel = status.vpn or (facts.vpn if facts else None)
    if status.state == "upnp":
        return PortView("good", "Friends anywhere can join",
                        f"Your router opened port {port} for this movie night, and closes it "
                        "again when the movie night ends.")
    if status.state == "manual" and status.forwarded:
        # The screen this owner sees most: UPnP silent, the internet address
        # found, the port forwarded by hand. On their word only: without UPnP
        # nothing here can see a forward, and one pointing at the PC's old
        # address looks just the same. So it says what they did and what to do
        # now, and claims nothing about who can get in. A friend who can't is
        # told to have the forward checked (sync.unreachable).
        return PortView("noted", f"You've forwarded port {port} to this PC",
                        "One code for friends at your place and elsewhere.", how=True)
    if status.state == "manual":
        first = ("Your router didn't open the port by itself." if upnp_on else
                 "Mistery doesn't ask your router to open ports: UPnP is off in Settings → "
                 "Movie night.")
        return PortView(
            "partial", "Friends at your place can join now",
            f"{first} For friends elsewhere, forward TCP port {port} to {here} in your router's "
            "settings, as below. This same code then works for them too.", steps=True)
    if status.state == "lan":
        if tunnel:
            text = (f"{html.escape(vpn_name(tunnel))} is on, so Mistery can't find your internet "
                    "address: pause it for movie night, or let Mistery bypass it (split "
                    "tunnelling). Then end this movie night and start it again: the new code "
                    "will carry the address friends elsewhere need.")
        else:
            text = ("Mistery couldn't find your internet address, so this code only works on "
                    "your home network. Check that you're online, then end this movie night and "
                    "start it again.")
        return PortView("blocked", "Only friends at your place can join", text)
    if status.state == "cgnat":
        # upnp's sentence already says what is going on and what can be done,
        # different for each of the three causes. The one that is nobody's
        # doing gets a word of its own: nothing at home can change it.
        if "carrier-grade" in (status.text or ""):
            text += ("<br><br>Nothing on your PC or your router can change that: it is how your "
                     "provider connects your home, and many do.")
        return PortView("blocked", "Friends elsewhere can't get in", text)
    if status.state == "failed":
        return PortView("blocked", f"Port {port} is taken on your router", text)
    return PortView("busy", "Opening the port…", text)


def _fields(rows) -> str:
    """A router page's fields and what to type in each, as a small table."""
    field = ("<tr><td style='color:%s; padding: 1px 18px 1px 0'>{}</td>"
             "<td style='font-family: Consolas, monospace; padding: 1px 0'>{}</td></tr>" % C.TEXT_DIM)
    return ("<table cellspacing='0' style='margin: 2px 0 8px 18px'>"
            + "".join(field.format(name, value) for name, value in rows) + "</table>")


_NEW_ADDRESS = ("Routers can give this PC a new address later. If friends elsewhere stop getting in "
                "some day, check that the forward still points at this PC, or reserve its address in "
                "the router (often called DHCP reservation).")


def router_steps(port: int, lan_ip: str | None, router: str | None,
                 facts: NetworkFacts | None) -> str:
    """What to do on the router's settings page, as rich text: the fields most
    routers ask for, filled in with this PC's values. On an AT&T gateway, the
    fields it asks for, in its own words (_att_steps)."""
    lan = html.escape(lan_ip) if lan_ip else "this PC's address"
    name = html.escape(facts.computer) if facts and facts.computer else ""
    if att_gateway(facts.network if facts else None, router):
        return _att_steps(port, lan, name, router)
    where = (f"Open your router's settings page, <b>http://{html.escape(router)}</b>, in a "
             "browser." if router else "Open your router's settings page in a browser. Its "
             "address is usually printed on the router's label.")
    # Some routers list devices by name and some want the address typed in: both.
    device = f"{lan} (this PC, {name})" if name else f"{lan} (this PC)"
    fields = _fields((("Name", "Mistery"), ("Protocol", "TCP"), ("External port", str(port)),
                      ("Internal port", str(port)), ("Device or IP address", device)))
    return (
        f"<p style='margin:0 0 8px 0'><b>1.</b> {where}</p>"
        "<p style='margin:0 0 4px 0'><b>2.</b> Add a port forward (or \u201cvirtual server\u201d) "
        f"with:</p>{fields}"
        "<p style='margin:0 0 8px 0'><b>3.</b> Save. Friends elsewhere can then join with this "
        "same code.</p>"
        f"<p style='margin:0; color:{C.TEXT_FAINT}'>{_NEW_ADDRESS}</p>")


def _att_steps(port: int, lan: str, name: str, router: str | None) -> str:
    """The same on an AT&T gateway (the BGW kind), in the words its pages use.
    A forward there takes two pages:
    Custom Services makes the service (Service Name, Global Port Range, Base
    Host Port, Protocol), then NAT/Gaming gives it to a device, picked by name
    from "Needed by Device". The generic External port / Internal port / IP
    address matched neither page, and nothing said which name in that list is
    this PC. Written from AT&T's published layout: the owner's own gateway was
    not opened to check it."""
    page = f"<b>http://{html.escape(router)}</b>" if router else "your gateway's settings page"
    device = f"{name} (this PC, {lan})" if name else f"this PC ({lan})"
    service = _fields((("Service Name", "Mistery"), ("Global Port Range", f"{port} to {port}"),
                       ("Base Host Port", str(port)), ("Protocol", "TCP")))
    give = _fields((("Service", "Mistery"), ("Needed by Device", device)))
    return (
        f"<p style='margin:0 0 8px 0'><b>1.</b> Open {page} in a browser and go to <b>Firewall → "
        "NAT/Gaming</b>. When it asks for the Device Access Code, that is printed on the "
        "gateway's label.</p>"
        "<p style='margin:0 0 4px 0'><b>2.</b> Press <b>Custom Services</b> and add one with:</p>"
        f"{service}"
        "<p style='margin:0 0 8px 18px'>then <b>Add</b>, and go back to NAT/Gaming.</p>"
        "<p style='margin:0 0 4px 0'><b>3.</b> On NAT/Gaming, choose:</p>"
        f"{give}"
        "<p style='margin:0 0 8px 18px'>then <b>Add</b>. Friends elsewhere can then join with this "
        "same code.</p>"
        f"<p style='margin:0; color:{C.TEXT_FAINT}'>{_NEW_ADDRESS}</p>")


# --- shared pieces ----------------------------------------------------------------

# Sizes and families go in each label's own style sheet, never setFont: the
# main window's `QWidget { font-size: 10pt }` replaces a setFont size the
# moment the label is polished (probed: 16.5pt set, 10pt shown), whereas a
# label's own sheet wins, and is applied as soon as it is set, so WrapLabel
# measures with the font it will paint with.
_MONO = 'font-family: "Cascadia Mono", Consolas, "Courier New", monospace;'


class _Words(WrapLabel):
    """WrapLabel for a column the dialog sizes. WrapLabel reports its current
    width as its minimum, right for EmptyState's fixed 560 px column; in a
    scroll area that minimum (640 px for a label not yet laid out) made the
    page wider than the dialog, and the text ran off its right edge. The width
    here is the layout's to give; the height still comes from QTextDocument."""

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        width = max(120, self.width())
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        # The height follows from the width the layout gives (height for
        # width), so the minimum claims one line and no more.
        return QSize(120, self.fontMetrics().height())


def _text(text: str = "", size: float = 10.0, color: str = C.TEXT_DIM, bold: bool = False,
          rich: bool = True) -> WrapLabel:
    label = _Words(text)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    label.setStyleSheet(f"color: {color}; font-size: {size}pt;"
                        + (" font-weight: 700;" if bold else ""))
    return label


def _button(text: str, kind: str = "") -> QPushButton:
    button = QPushButton(text)
    if kind:
        button.setObjectName(kind)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class _Spinner(QWidget):
    """A small turning arc: something is happening, and nothing is stuck."""

    def __init__(self, size: int = 18, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(QSize(size, size))
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._turn)

    def _turn(self) -> None:
        self._angle = (self._angle + 24) % 360
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(C.TEXT), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        painter.drawArc(rect, -self._angle * 16, 270 * 16)


class _Dot(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(QSize(10, 10))
        self._color = QColor(C.TEXT_FAINT)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class _Disclosure(QWidget):
    """A heading that opens and closes the text under it: "If friends can't get in"."""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._title = title
        self._toggle = QPushButton()
        self._toggle.setObjectName("Disclosure")
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.clicked.connect(lambda: self.set_open(not self.body.isVisible()))
        layout.addWidget(self._toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.body = _text("", 9.5, C.TEXT_DIM)
        layout.addWidget(self.body)
        self.set_open(False)

    def set_open(self, open_: bool) -> None:
        self.body.setVisible(open_)
        self._toggle.setText(("▾  " if open_ else "▸  ") + self._title)


_STYLE = f"""
QDialog#MovieNight {{ background: {C.BG_ELEV}; }}
#Eyebrow {{ color: {C.ACCENT}; font-size: 9pt; font-weight: 700; letter-spacing: 1.2px; }}
#CodeBox, #StatusBox, #StepsBox, #NoteBox {{
    background: {C.BG};
    border: 1px solid {C.BORDER};
    border-radius: 10px;
}}
#StepsBox {{ background: {C.SURFACE}; border-color: {C.BORDER_STRONG}; }}
#Person {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER_STRONG};
    border-radius: 13px;
    padding: 4px 12px;
    color: {C.TEXT};
    font-size: 9.5pt;
}}
QPushButton#Disclosure {{
    background: transparent; border: none; padding: 2px 0;
    color: {C.TEXT_DIM}; font-weight: 600;
}}
QPushButton#Disclosure:hover {{ color: {C.TEXT}; }}
QPushButton#Danger {{
    background: transparent; border: 1px solid rgba(255, 90, 90, 0.55);
    color: {C.DANGER}; font-weight: 600; padding: 9px 18px; border-radius: 6px;
}}
QPushButton#Danger:hover {{ background: rgba(255, 90, 90, 0.12); }}
QPushButton#Primary {{ padding: 10px 24px; font-size: 10.5pt; }}
QPushButton#Ghost {{ padding: 10px 20px; font-size: 10pt; }}
QLineEdit#CodeInput {{ padding: 11px 13px; {_MONO} font-size: 11pt; font-weight: 600; }}
"""


class _MovieNightDialog(QDialog):
    """What both dialogs share: the frame, the header and the session.

    The content scrolls when the screen is too short for it (the forwarding
    steps make the host panel about 1130 px tall); the buttons never do.
    With them inside the scroll area, End movie night sat half cut off at the
    bottom of the panel on the PC this was written on (a 150 % display).
    """

    def __init__(self, session, title: str, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.setObjectName("MovieNight")
        self.setWindowTitle(title)
        self.setStyleSheet(_STYLE)
        self.setFixedWidth(_WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self._scroll)
        page = QWidget()
        self._scroll.setWidget(page)
        self.body = QVBoxLayout(page)
        self.body.setContentsMargins(32, 26, 32, 8)
        self.body.setSpacing(0)
        self.footer_bar = QWidget()
        self.footer = QHBoxLayout(self.footer_bar)
        self.footer.setContentsMargins(32, 14, 32, 22)
        self.footer.setSpacing(10)
        outer.addWidget(self.footer_bar)

        self.eyebrow = QLabel("MOVIE NIGHT")
        self.eyebrow.setObjectName("Eyebrow")
        self.body.addWidget(self.eyebrow)
        self.body.addSpacing(6)
        self.headline = _text("", 16.5, C.TEXT, bold=True, rich=False)
        self.body.addWidget(self.headline)
        self.subline = _text("", 10.0, C.TEXT_DIM, rich=False)
        self.body.addSpacing(4)
        self.body.addWidget(self.subline)
        self.body.addSpacing(18)
        # A word about why this screen is showing instead of the one asked for
        # ("You're already hosting a movie night…"); hidden when there is none.
        self.notice_box = QWidget()
        notice = QVBoxLayout(self.notice_box)
        notice.setContentsMargins(0, 0, 0, 16)
        self.notice = _text("", 10.0, _AMBER, rich=False)
        notice.addWidget(self.notice)
        self.notice_box.setVisible(False)
        self.body.addWidget(self.notice_box)

    def say_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice_box.setVisible(bool(text))

    def fit(self) -> None:
        """Take the height the content needs at the dialog's width, up to 90 %
        of the screen; past that the page scrolls."""
        page = self._scroll.widget()
        # Every cached height goes first. A label's new text reaches its own
        # layout at once, but the boxes around it only through posted events,
        # and each widget's layout item keeps its own height-for-width cache
        # besides, cleared by nothing but that widget's updateGeometry().
        # Measured straight after a change, the panel came out 160 px too tall
        # (735 needed, 895 given; a second fit a moment later gave 737).
        for root in (page, self.footer_bar):
            for widget in root.findChildren(QWidget):
                widget.updateGeometry()
            for layout in root.findChildren(QLayout):
                layout.invalidate()
            root.layout().invalidate()
        # A footer of buttons alone has no height-for-width (Qt says -1).
        footer = (self.footer.totalHeightForWidth(self.width()) if self.footer.hasHeightForWidth()
                  else self.footer.totalSizeHint().height())
        wanted = page.layout().totalHeightForWidth(self.width()) + footer + 2
        screen = self.screen() or QGuiApplication.primaryScreen()
        limit = int(screen.availableGeometry().height() * 0.9) if screen else 900
        # A scrollbar only when the content really is taller than the screen
        # allows. Left to decide by itself, QScrollArea keeps a scrollbar that
        # an earlier, taller state needed (it measures at the width left beside
        # it, so as not to flicker), and the panel scrolled by 16 px it did not
        # need to, its text wrapped narrower around a bar with nothing to do.
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if wanted <= limit
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFixedHeight(max(200, min(wanted, limit)))


def _box(name: str, margins=(20, 16, 20, 16), spacing: int = 10) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName(name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return frame, layout


def split_code(code: str) -> str:
    """The code on two lines of 7 and 6 groups, as it is shown: 41 characters
    at 14pt fit the panel, 77 on one line would not. Whitespace between groups
    is fine for decode(), so even a copy of the two lines by hand still joins."""
    groups = code.split("-")
    return "-".join(groups[:7]) + "\n" + "-".join(groups[7:]) if len(groups) > 7 else code


def per_friend_line(mbps: float | None) -> str:
    """What one friend costs in upload, in plain terms. The host's own upload is
    never guessed at: Mistery does not measure it, and a guess would be wrong."""
    if not mbps:
        return ""
    amount = max(1, round(mbps))
    return f"Each friend takes about {amount} Mbit/s of your upload, the film as it is."


def _people_names(people: list, me: str) -> list[tuple[str, str, bool]]:
    """(name, note, is it me) for each person the session lists."""
    shown = []
    for person in people or []:
        if not isinstance(person, dict):
            continue
        name = str(person.get("name") or "Friend")
        mine = person.get("id") == me
        notes = []
        if mine:
            notes.append("you")
        elif person.get("host"):
            notes.append("host")
        if person.get("buffering"):
            notes.append("buffering…")
        shown.append((name, ", ".join(notes), mine))
    return shown


def guest_names(session) -> list[str]:
    """Everyone in the host's movie night but the host: who an end would end it for."""
    return [name for name, _note, mine in _people_names(session.people, session.me_id) if not mine]


def ask_to_end(parent, session, quitting: bool = False) -> bool:
    """Whether the movie night this PC hosts may end now: at once when nobody
    else is in it, and only on a yes when somebody is, because it ends for them
    too. End movie night on the panel asks this, and so does closing Mistery
    (quitting), which ends it just the same: before it asked, a friend was told
    the movie night had ended 0.13 s after the window's close button, and
    Continue on Home makes a new certificate, so every one of them then needs a
    new code. Keep watching is the default, for an Enter pressed by habit, and
    what Esc and the question's own close button answer."""
    if session is None or session.role != "host":
        return True
    guests = guest_names(session)
    if not guests:
        return True
    box = QMessageBox(parent)
    box.setWindowTitle("End movie night")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(f"End the movie night for {and_list(guests)}" + (" and quit Mistery?" if quitting else "?"))
    box.setInformativeText("Where you all got to is kept: continue it from Home another time, with a new "
                           "code for your friends.")
    end = box.addButton("End it and quit" if quitting else "End it", QMessageBox.ButtonRole.DestructiveRole)
    keep = box.addButton("Keep watching", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.exec()
    return box.clickedButton() is end


# --- the host -------------------------------------------------------------------

class HostDialog(_MovieNightDialog):
    """Starting a movie night, and the host's panel while it runs.

    One window for the whole evening, shown and hidden rather than rebuilt, and
    modeless: the film plays underneath, and the host keeps it open while
    pasting the code into Discord. Closing it keeps the movie night going;
    only End movie night ends it.

    While it runs it is in front of the player, and the active window, so the
    keys a host reaches for work on it too: Space (and the media keys) play
    and pause the room as they do in the player, and with friends in and the
    room held, "Play for everyone" is the button Enter presses. Before, focus
    sat on the scroll area, Enter went to a hidden "Continue it" and did
    nothing, and a host who pressed Space with the friends in got no play and
    no word why (the room stayed at seq 0).
    """

    settings_requested = Signal()           # a port problem: Settings → Movie night
    _facts_ready = Signal(object)

    READY, STARTING, RUNNING, ENDED, FAILED = "ready", "starting", "running", "ended", "failed"

    def __init__(self, session, parent=None) -> None:
        super().__init__(session, "Movie night", parent)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.state = self.READY
        self.item: MediaItem | None = None
        self.facts: NetworkFacts | None = None
        self._previous = None               # an earlier movie night of the same thing
        self._episodes: list[MediaItem] = []
        self._facts_ready.connect(self._on_facts)
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(3500)
        self._copied_timer.timeout.connect(lambda: self.copied.setVisible(False))

        # --- ready: what is about to happen ------------------------------------
        self.ready_panel = QWidget()
        ready = QVBoxLayout(self.ready_panel)
        ready.setContentsMargins(0, 0, 0, 0)
        ready.setSpacing(14)
        # Captioned like Settings' comboboxes, which draw no arrow: the caption
        # is what says there is a choice.
        self.episode_row = QWidget()
        episode_row = QHBoxLayout(self.episode_row)
        episode_row.setContentsMargins(0, 0, 0, 0)
        episode_row.setSpacing(14)
        caption = QLabel("Episode")
        caption.setStyleSheet(f"color: {C.TEXT_DIM};")
        episode_row.addWidget(caption)
        self.episode_choice = QComboBox()
        self.episode_choice.setMaxVisibleItems(16)
        self.episode_choice.setCursor(Qt.CursorShape.PointingHandCursor)
        self.episode_choice.currentIndexChanged.connect(self._on_episode_chosen)
        episode_row.addWidget(self.episode_choice, 1)
        ready.addWidget(self.episode_row)
        self.about = _text("", 10.0, C.TEXT_DIM)
        ready.addWidget(self.about)
        self.previous = QFrame()
        previous = QHBoxLayout(self.previous)
        previous.setContentsMargins(0, 0, 0, 0)
        previous.setSpacing(12)
        self.previous_text = _text("", 9.5, C.TEXT_DIM)
        previous.addWidget(self.previous_text, 1)
        self.continue_button = _button("Continue it", "Ghost")
        self.continue_button.clicked.connect(self._continue_previous)
        previous.addWidget(self.continue_button, 0, Qt.AlignmentFlag.AlignVCenter)
        ready.addWidget(self.previous)
        note_box, note = _box("NoteBox", spacing=8)
        heading = _text("Before you start", 10.0, C.TEXT, bold=True, rich=False)
        note.addWidget(heading)
        self.firewall = _text("", 9.5, C.TEXT_DIM)
        note.addWidget(self.firewall)
        self.vpn_note = _text(VPN_LINE, 9.5, C.TEXT_DIM, rich=False)
        note.addWidget(self.vpn_note)
        ready.addWidget(note_box)
        self.body.addWidget(self.ready_panel)

        # --- starting ------------------------------------------------------------
        self.progress_panel = QWidget()
        starting = QVBoxLayout(self.progress_panel)
        starting.setContentsMargins(0, 4, 0, 4)
        starting.setSpacing(14)
        progress = QHBoxLayout()
        progress.setSpacing(12)
        self.spinner = _Spinner()
        progress.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignTop)
        self.progress_text = _text("", 10.5, C.TEXT, rich=False)
        progress.addWidget(self.progress_text, 1)
        starting.addLayout(progress)
        # This is the moment Windows asks (the listener starts here), so the
        # question is repeated while the answer can still be the right one.
        self.starting_note = _text("", 9.5, C.TEXT_FAINT)
        starting.addWidget(self.starting_note)
        self.body.addWidget(self.progress_panel)

        # --- running: the code ----------------------------------------------------
        self.running_panel = QWidget()
        running = QVBoxLayout(self.running_panel)
        running.setContentsMargins(0, 0, 0, 0)
        running.setSpacing(18)

        code_box, code = _box("CodeBox", margins=(22, 18, 22, 18), spacing=12)
        label = QLabel("INVITE CODE")
        label.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 8.5pt; font-weight: 700;"
                            " letter-spacing: 1.2px;")
        code.addWidget(label)
        self.code_label = QLabel()
        self.code_label.setStyleSheet(f"color: {C.TEXT}; {_MONO} font-size: 14pt; "
                                      "font-weight: 600; letter-spacing: 1px;")
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setCursor(Qt.CursorShape.IBeamCursor)
        code.addWidget(self.code_label)
        copy_row = QHBoxLayout()
        copy_row.setSpacing(10)
        self.copy_code = _button("Copy code", "Primary")
        self.copy_code.clicked.connect(self._copy_code)
        copy_row.addWidget(self.copy_code)
        self.copy_link = _button("Copy link", "Ghost")
        # Nothing registers mistery:// with Windows yet, so the link is not
        # something to click: it is the code saying what it is for, and Join
        # takes it pasted just the same.
        self.copy_link.setToolTip("The same code as a mistery://join/ link, which says what it is "
                                  "for. Friends paste it into Join movie night just the same.")
        self.copy_link.clicked.connect(self._copy_link)
        copy_row.addWidget(self.copy_link)
        self.copied = QLabel()
        self.copied.setStyleSheet(f"color: {C.SUCCESS}; font-weight: 600;")
        self.copied.setVisible(False)
        copy_row.addSpacing(6)
        copy_row.addWidget(self.copied)
        copy_row.addStretch(1)
        code.addLayout(copy_row)
        # Where Join is, said so a friend finds it at once: the top bar's
        # "Movie night", on every page. Home's own Join sits below the fold.
        self.code_hint = _text("Send it to your friends. In their Mistery, they press <b>Movie "
                               "night</b> at the top of the window and paste it in.",
                               9.5, C.TEXT_FAINT)
        code.addWidget(self.code_hint)
        running.addWidget(code_box)

        # --- running: who can get in ----------------------------------------------
        status_box, status = _box("StatusBox", spacing=10)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.status_dot = _Dot()
        head.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_title = QLabel()
        self.status_title.setStyleSheet(f"color: {C.TEXT}; font-size: 11pt; font-weight: 700;")
        head.addWidget(self.status_title, 1)
        status.addLayout(head)
        self.status_text = _text("", 9.8, C.TEXT_DIM)
        status.addWidget(self.status_text)
        # Once the owner has said the port is forwarded, the steps wait behind
        # this instead of filling the panel every evening.
        self._how_open = False
        self.how_button = QPushButton()
        self.how_button.setObjectName("Disclosure")
        self.how_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.how_button.clicked.connect(self._toggle_how)
        status.addWidget(self.how_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.steps_box, steps = _box("StepsBox", margins=(18, 14, 18, 14), spacing=6)
        steps_title = _text("How to forward the port", 10.0, C.TEXT, bold=True, rich=False)
        steps.addWidget(steps_title)
        self.steps_text = _text("", 9.5, C.TEXT)
        steps.addWidget(self.steps_text)
        # Small, and last: the steps are what matters here. Pressed, it saves
        # party_forwarded as the box in Settings does, and the panel turns into
        # the one calm line at once.
        self.done_button = _button("I've done this", "Chip")
        self.done_button.setToolTip("Tells Mistery the port is forwarded, so this panel stops "
                                    "showing the steps. It can't check the router for you. "
                                    "Untick it in Settings → Movie night.")
        self.done_button.clicked.connect(self._forwarded_by_hand)
        steps.addSpacing(4)
        steps.addWidget(self.done_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.untick_note = _text("Not forwarded after all? Untick it in Settings → Movie night.",
                                 9.0, C.TEXT_FAINT, rich=False)
        steps.addWidget(self.untick_note)
        status.addWidget(self.steps_box)
        self.upnp_hint = _text("", 9.0, C.TEXT_FAINT)
        status.addWidget(self.upnp_hint)
        # Whenever a VPN carries this PC's traffic, whatever the port says: with
        # its kill switch on, NordVPN lets nothing in or out beside its tunnel,
        # forward or no forward.
        self.vpn_line = _text("", 9.5, C.TEXT_DIM)
        status.addWidget(self.vpn_line)
        self.port_button = _button("Choose another port", "Ghost")
        self.port_button.clicked.connect(self._open_settings)
        status.addWidget(self.port_button, 0, Qt.AlignmentFlag.AlignLeft)
        running.addWidget(status_box)

        # --- running: the room ---------------------------------------------------
        room = QVBoxLayout()
        room.setSpacing(10)
        self.watching = QLabel()
        self.watching.setStyleSheet(f"color: {C.TEXT}; font-size: 10.5pt; font-weight: 700;")
        room.addWidget(self.watching)
        self.people_row = QWidget()
        self.people_flow = FlowLayout(self.people_row, margin=0, h_spacing=8, v_spacing=8)
        room.addWidget(self.people_row)
        self.bandwidth = _text("", 9.5, C.TEXT_FAINT, rich=False)
        room.addWidget(self.bandwidth)
        running.addLayout(room)

        self.help = _Disclosure("If friends can't get in")
        running.addWidget(self.help)
        self.body.addWidget(self.running_panel)

        # --- ended / failed ----------------------------------------------------------
        self.outcome = _text("", 10.5, C.TEXT)
        self.body.addWidget(self.outcome)

        # --- the buttons, below the scrolling part ---------------------------------------
        self.body.addStretch(1)
        footer = self.footer
        # One short line beside the buttons ("Closing this window keeps the
        # movie night going."), so a plain label: nothing here needs to wrap.
        # Empty beside Play for everyone, which leaves no room for it.
        self.footer_note = QLabel()
        self.footer_note.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 9pt;")
        footer.addWidget(self.footer_note, 1)
        self.settings_button = _button("Open Settings", "Ghost")
        self.settings_button.clicked.connect(self._open_settings)
        footer.addWidget(self.settings_button)
        self.cancel_button = _button("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        footer.addWidget(self.cancel_button)
        self.end_button = _button("End movie night", "Danger")
        # Never Enter's, even with focus: it keeps focus after "Keep watching",
        # and with the friends gone by then, an Enter meant for Close would end
        # the movie night there and then, unasked (test_fix_panel_keys).
        self.end_button.setAutoDefault(False)
        self.end_button.clicked.connect(self._end)
        footer.addWidget(self.end_button)
        self.start_button = _button("Start movie night", "Primary")
        self.start_button.clicked.connect(self._start_clicked)
        footer.addWidget(self.start_button)
        self.retry_button = _button("Try again", "Primary")
        self.retry_button.clicked.connect(lambda: self.prepare(self.item) if self.item else None)
        footer.addWidget(self.retry_button)
        self.close_button = _button("Close")
        self.close_button.clicked.connect(self.hide)
        footer.addWidget(self.close_button)
        # With friends in and the room held (at the start, or paused): the next
        # thing to do, where the host is looking. The footer never scrolls, so
        # it shows even when the forwarding steps make the page scroll.
        self.play_button = _button("Play for everyone", "Primary")
        self.play_button.clicked.connect(self._play_for_everyone)
        footer.addWidget(self.play_button)

        # The player's keys, on the panel in front of it: the room's play and
        # pause, as PlayerView.toggle_pause and its _party_keys make them. A
        # shortcut is asked before the focused button, which would otherwise
        # take Space as a second click on itself. Media keys too: one Qt did
        # not claim would reach Windows, and Windows gives it to the paused
        # music's media session, which would start over the film. Only while
        # the movie night runs; the rest of the time Space is the buttons'.
        self._room_keys = [
            QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._toggle_room,
                      autoRepeat=False),
            QShortcut(QKeySequence(Qt.Key.Key_MediaTogglePlayPause), self, activated=self._toggle_room,
                      autoRepeat=False),
            QShortcut(QKeySequence(Qt.Key.Key_MediaPlay), self,
                      activated=lambda: self.session.intent("play"), autoRepeat=False),
            QShortcut(QKeySequence(Qt.Key.Key_MediaPause), self,
                      activated=lambda: self.session.intent("pause"), autoRepeat=False),
        ]

        session.changed.connect(self._on_changed)
        session.message.connect(self._on_message)
        session.error.connect(self._on_error)
        session.ended.connect(self._on_ended)
        self._show_state(self.READY)

    # --- entry ---------------------------------------------------------------------

    def prepare(self, item: MediaItem) -> None:
        """Before a movie night of item: what happens, and the Start button.
        With one already on, that one instead: there is one port, and one room."""
        self._look_up_network()
        self.say_notice("")
        if self.session.role == "guest":
            self.outcome.setText("You're in a friend's movie night. Leave it before you start "
                                 "one of your own.")
            self._show_state(self.FAILED)
            return
        if self.session.role == "host":
            if self.item is None or item.id != self.item.id:
                self.say_notice("You're already hosting a movie night. End it before you start "
                                "another.")
            self.show_running()
            return
        self.item = item
        self._fill_episodes(item)
        self._show_item(item, choosing=len(self._episodes) > 1)
        self._show_state(self.READY)

    def start(self, item: MediaItem, party_id: str | None = None,
              start_at: float | None = None) -> None:
        """Start at once: Continue on Home, or Start in the ready panel."""
        if self.session.role is not None:
            self.prepare(item)
            return
        self.item = item
        self._look_up_network()
        self.say_notice("")
        self._show_item(item)
        self.progress_text.setText("Getting the movie night ready…")
        self._show_state(self.STARTING)
        quality = str(settings.get("party_quality", "auto") or "auto")
        try:
            # False comes with an `error` first (the file has gone, say), which
            # has already turned this panel into the sentence.
            self.session.start_host(item, quality, party_id=party_id, start_at=start_at)
        except Exception as exc:            # a bug, but the host still deserves a sentence
            _log.exception("start_host failed")
            self._on_error(f"The movie night couldn't start: {exc}")

    def show_running(self) -> None:
        """The movie night that is on, as it is now: starting, or running."""
        self._look_up_network()
        if self.session.role != "host":
            return
        if self.session.phase == "on":
            self._show_state(self.RUNNING)
        else:
            self.progress_text.setText(self.session.status or "Getting the movie night ready…")
            self._show_state(self.STARTING)

    # --- ready ---------------------------------------------------------------------

    def _fill_episodes(self, item: MediaItem) -> None:
        """For an episode, the rest of its show, so the party's episode can be
        picked here: the host's own next episode (7, say) is often not the
        one the friends are on (3)."""
        self.episode_choice.blockSignals(True)
        self.episode_choice.clear()
        self._episodes = []
        if item.is_episode and item.show_id:
            self._episodes = [MediaItem.from_row(row) for row in db.episodes_for_show(item.show_id)]
            for episode in self._episodes:
                self.episode_choice.addItem(episode.display_title or episode.title, episode.id)
            index = self.episode_choice.findData(item.id)
            self.episode_choice.setCurrentIndex(max(0, index))
        self.episode_row.setVisible(len(self._episodes) > 1)
        self.episode_choice.blockSignals(False)

    def _on_episode_chosen(self, index: int) -> None:
        if 0 <= index < len(self._episodes):
            self.item = self._episodes[index]
            self._show_item(self.item, choosing=True)
            self.fit()

    def _show_item(self, item: MediaItem, choosing: bool = False) -> None:
        """The title at the top. While the episode chooser shows, the episode is
        named there alone: above it and in the sentence as well, it read three
        times in a row."""
        from ..util import fmt_duration

        if item.is_episode:
            show = db.get_show(item.show_id) if item.show_id else None
            show_title = str(show["title"] or "") if show else ""
            self.headline.setText(show_title or item.title)
            bits = [] if choosing else [item.code, item.title]
            name = show_title or item.title
        else:
            self.headline.setText(item.title or Path(item.path).stem)
            bits = [str(item.year)] if item.year else []
            name = item.title
        if item.duration and not choosing:
            bits.append(fmt_duration(item.duration))
        self.subline.setText("  ·  ".join(b for b in bits if b))
        self.subline.setVisible(bool(self.subline.text()))
        self.about.setText(
            "Friends who have Mistery watch it with you, in sync: anyone can pause, play or skip, "
            "and everyone follows. Where you all get to is kept for this movie night, so your own "
            f"place in <i>{html.escape(name or 'it')}</i> stays where it is.")
        self._show_previous(item)

    def _show_previous(self, item: MediaItem) -> None:
        """Offer to carry on an earlier movie night of the same thing."""
        from ..party import people

        self._previous = None
        if item.id:
            for row in db.recent_parties(40):
                if row["role"] == "host" and row["media_id"] == item.id:
                    self._previous = row
                    break
        if self._previous is None:
            self.previous.setVisible(False)
            return
        row = self._previous
        me = people.person_id()
        others = [p.name for p in people.parse_members(row["members"]) if p.id != me]
        who = f" with {and_list(others)}" if others else ""
        self.previous_text.setText(
            f"You watched this{html.escape(who)} {when_text(row['updated_at'])}, up to "
            f"<b>{fmt_clock(row['position'])}</b>.")
        self.continue_button.setText(f"Continue from {fmt_clock(row['position'])}")
        self.previous.setVisible(True)

    def _continue_previous(self) -> None:
        row = self._previous
        if row is not None and self.item is not None:
            self.start(self.item, party_id=row["party_id"], start_at=float(row["position"] or 0))

    def _start_clicked(self) -> None:
        if self.item is not None:
            self.start(self.item)

    # --- the session's news ---------------------------------------------------------

    def _on_message(self, text: str) -> None:
        if self.state == self.STARTING and text:
            self.progress_text.setText(text)

    def _on_changed(self) -> None:
        session = self.session
        if session.role != "host":
            return          # `ended` says why; a guest's news is the join dialog's
        if session.phase == "on" and self.state == self.STARTING:
            self._show_state(self.RUNNING)
        elif session.phase == "on" and self.state == self.RUNNING:
            self._refresh_running()         # people, the port, the title: whatever moved
            self.fit()
        elif self.state == self.STARTING and session.status:
            self.progress_text.setText(session.status)

    def _on_error(self, text: str) -> None:
        # A start that failed, or a movie night that stopped under the host (the
        # player could not open the film): the session has ended it already.
        if self.state == self.STARTING or (self.state == self.RUNNING and self.session.role != "host"):
            self.outcome.setText(html.escape(text))
            self._show_state(self.FAILED)

    def _on_ended(self, reason: str) -> None:
        if self.state == self.STARTING:
            self.outcome.setText(html.escape(reason) if reason else
                                 "The movie night stopped before it started.")
            self._show_state(self.FAILED)
        elif self.state == self.RUNNING:
            self.outcome.setText(html.escape(reason) if reason else "The movie night has ended.")
            self._show_state(self.ENDED)
            if not reason:
                self.hide()                 # the host ended it: nothing more to say

    # --- running -----------------------------------------------------------------------

    def _refresh_running(self) -> None:
        session = self.session
        self.code_label.setText(split_code(session.invite_code or ""))
        self.copy_link.setVisible(bool(session.invite_link))
        if self.item is None and session.media_title:
            self.headline.setText(session.media_title)      # opened from somewhere with no item
        status = session.port_status
        facts = self.facts
        upnp_on = bool(settings.get("party_upnp", True))
        view = port_view(status, facts, upnp_on)
        self.status_dot.set_color(_TONES.get(view.tone, C.TEXT_FAINT))
        self.status_title.setText(view.title)
        self.status_text.setText(view.text)
        self.status_text.setVisible(bool(view.text))
        lan = (status.lan_ip if status else None) or (facts.lan_ip if facts else None)
        open_steps = view.steps or (view.how and self._how_open)
        self.steps_box.setVisible(open_steps)
        if open_steps:
            router = (status.router if status else None) or (facts.router if facts else None)
            self.steps_text.setText(router_steps(self._port(), lan, router, facts))
        self.done_button.setVisible(view.steps)         # still to do: "I've done this"
        self.untick_note.setVisible(view.how)           # done, on the owner's word: how to take it back
        self.how_button.setVisible(view.how)
        self.how_button.setText("▾  Hide how" if self._how_open else "▸  Show how")
        self.upnp_hint.setVisible(view.steps)
        self.upnp_hint.setText(
            "Or switch UPnP on in the router's settings, and Mistery opens the port by itself "
            "next time." if upnp_on else
            "Or switch UPnP on in Settings → Movie night, and Mistery asks the router to open "
            "the port by itself.")
        self.port_button.setVisible(status is not None and status.state == "failed")

        shown = _people_names(session.people, session.me_id)
        self.people_flow.clear()        # unparented as well as deleted: a pill only taken out stayed painted
        for name, note, _mine in shown:
            pill = QLabel(f"{html.escape(name)}" + (
                f" <span style='color:{C.TEXT_FAINT}'>({html.escape(note)})</span>" if note else ""))
            pill.setObjectName("Person")
            pill.setTextFormat(Qt.TextFormat.RichText)
            self.people_flow.addWidget(pill)
            # Shown now, not on the queued call a layout makes for a new child:
            # until then Qt sizes it as nothing, and fit() left the row of
            # names 28 px short, so the panel scrolled.
            pill.show()
        self.watching.setText("Watching" if not shown else
                              f"Watching  ·  {len(shown)}" if len(shown) > 1 else
                              "Watching  ·  just you so far")
        self.bandwidth.setText(per_friend_line(session.needed_mbps))
        self.bandwidth.setVisible(bool(self.bandwidth.text()))
        tunnel = (status.vpn if status else None) or (facts.vpn if facts else None)
        # A VPN on is said where it shows, forward or no forward; "lan" with a
        # VPN is already the VPN's own sentence, so not twice there.
        said = bool(tunnel) and not (status is not None and status.state == "lan")
        self.vpn_line.setText(f"<b>{html.escape(vpn_name(tunnel))} is on.</b> If friends can't get "
                              "in, pause it during movie night, or let Mistery bypass it (split "
                              "tunnelling)." if said else "")
        self.vpn_line.setVisible(said)
        check = ""
        if view.how:
            # What a friend who can't get in is told to ask for (sync.unreachable).
            here = f" ({html.escape(lan)})" if lan else ""
            check = (f"You've said port {self._port()} is forwarded to this PC. If friends elsewhere "
                     "can't get in, check on the router that the forward still points at this "
                     f"PC{here}: routers sometimes give a PC a new address, and from here a forward "
                     "to the old one looks just the same.")
        self.help.body.setText("<br><br>".join(part for part in (
            check, firewall_note(facts), firewall_fix(), "" if tunnel else VPN_LINE) if part))
        playable = self._can_play()
        self.play_button.setVisible(playable)
        # The note gives its words up to the button, beside which they would
        # not fit, and stays, empty: its stretch is what holds the buttons at
        # their own size on the right (hidden, the three grew to fill the row).
        self.footer_note.setText("" if playable else "Closing this window keeps the movie night going.")
        self._pick_default()

    def _can_play(self) -> bool:
        """Friends are in and the room is held where it is, at the start or
        paused: Play for everyone is the next thing to do. Not while the room
        waits for somebody's stream (it carries on by itself when they are
        ready), and not at the end, where there is nothing left to play."""
        session = self.session
        if self.state != self.RUNNING or session.role != "host" or session.phase != "on":
            return False
        state = session.state
        if state is None or state.playing or state.waiting or state.cause == "end":
            return False
        return bool(guest_names(session))

    def _play_for_everyone(self) -> None:
        """The room plays, for everyone at once a moment from now (sync.Hub._lead:
        0.25 s and the slowest friend's round trip), and the panel steps aside
        for the film. The player's menu brings it back."""
        self.session.intent("play")
        self.hide()

    def _toggle_room(self) -> None:
        """Space on the panel: what Space does in the player behind it. The room's
        state decides, and while it waits for somebody, play goes on without them."""
        if self.state == self.RUNNING and self.session.role == "host":
            self.session.toggle()

    def _copy_code(self) -> None:
        code = self.session.invite_code or ""
        if code:
            QGuiApplication.clipboard().setText(code)
            self._say_copied("Copied. Paste it into Discord or a text.")

    def _copy_link(self) -> None:
        link = self.session.invite_link or ""
        if link:
            QGuiApplication.clipboard().setText(link)
            self._say_copied("Link copied.")

    def _say_copied(self, text: str) -> None:
        self.copied.setText(text)
        self.copied.setVisible(True)
        self._copied_timer.start()

    def _forwarded_by_hand(self) -> None:
        """"I've done this" under the steps: the owner's word that the router now
        forwards the port here. Saved as the box in Settings saves it; the
        session's port status follows the setting, so the panel is the calm
        line from this moment, the steps behind "Show how"."""
        settings.set("party_forwarded", True)
        self._how_open = False
        self._refresh_running()
        self.fit()

    def _toggle_how(self) -> None:
        self._how_open = not self._how_open
        self._refresh_running()
        self.fit()

    def _end(self) -> None:
        if ask_to_end(self, self.session):
            self.session.leave()

    def _cancel(self) -> None:
        if self.state == self.STARTING:
            # The panel first: leaving says `ended` at once, and that must not
            # read as a movie night that failed.
            self._show_state(self.READY)
            self.session.leave()
            return
        self.hide()

    def _open_settings(self) -> None:
        self.hide()
        self.settings_requested.emit()

    # --- plumbing ------------------------------------------------------------------------

    def _port(self) -> int:
        try:
            return int(self.session.port or settings.get("party_port", 42170))
        except (TypeError, ValueError):
            return 42170

    def _look_up_network(self) -> None:
        def work() -> None:
            facts = look_up_network()
            try:
                self._facts_ready.emit(facts)
            except RuntimeError:
                pass                    # the window is already gone

        threading.Thread(target=work, name="movie-night-network", daemon=True).start()

    def _on_facts(self, facts: NetworkFacts) -> None:
        self.facts = facts
        self.firewall.setText(firewall_note(facts))
        self.starting_note.setText(firewall_note(facts))
        self.vpn_note.setText(VPN_LINE if not facts.vpn else
                              f"{html.escape(vpn_name(facts.vpn))} is on. If friends can't get "
                              "in, pause it during movie night, or let Mistery bypass it (split "
                              "tunnelling).")
        if self.state == self.RUNNING:
            self._refresh_running()
        self.fit()

    def _show_state(self, state: str) -> None:
        self.state = state
        self.ready_panel.setVisible(state == self.READY)
        self.progress_panel.setVisible(state == self.STARTING)
        self.running_panel.setVisible(state == self.RUNNING)
        self.outcome.setVisible(state in (self.ENDED, self.FAILED))
        self.start_button.setVisible(state == self.READY)
        self.cancel_button.setVisible(state in (self.READY, self.STARTING))
        self.end_button.setVisible(state == self.RUNNING)
        self.close_button.setVisible(state in (self.RUNNING, self.ENDED, self.FAILED))
        self.retry_button.setVisible(state == self.FAILED and self.item is not None
                                     and self.session.role is None)
        # Only when the sentence sends the host there: server.py's and upnp's
        # port sentences end "Choose another port in Settings → Movie night".
        self.settings_button.setVisible(state == self.FAILED and "Settings" in self.outcome.text())
        self.eyebrow.setText("MOVIE NIGHT  ·  ON NOW" if state == self.RUNNING else "MOVIE NIGHT")
        self.footer_note.setText("Closing this window keeps the movie night going."
                                 if state == self.RUNNING else "")
        self.play_button.setVisible(False)          # the running panel's to show (_refresh_running)
        for shortcut in self._room_keys:
            shortcut.setEnabled(state == self.RUNNING)
        if state == self.READY:
            self.firewall.setText(firewall_note(self.facts))
        elif state == self.STARTING:
            self.starting_note.setText(firewall_note(self.facts))
        elif state == self.RUNNING:
            self._refresh_running()
        self._pick_default()
        self.fit()

    def _pick_default(self) -> None:
        """The button Enter presses: the one this state is for. Left to itself,
        QDialog made the first button in the tab order the default when the
        panel was first shown, "Continue it", which is hidden in every state
        but one, so Enter did nothing at all. Never End movie night: a key
        pressed by habit must not end anybody's evening. While a movie night
        starts, Start (hidden then) keeps it, so Enter does nothing."""
        if self.state == self.RUNNING:
            button = self.play_button if self.play_button.isVisibleTo(self) else self.close_button
        elif self.state == self.FAILED and self.retry_button.isVisibleTo(self):
            button = self.retry_button
        elif self.state in (self.ENDED, self.FAILED):
            button = self.close_button
        else:
            button = self.start_button
        # Off first: a button with focus is already marked default while it has
        # it, and marking it again would leave the dialog's own default as it was.
        button.setDefault(False)
        button.setDefault(True)


# --- joining --------------------------------------------------------------------

class JoinDialog(_MovieNightDialog):
    """Joining a friend's movie night: paste, watch it connect, or read why not.

    The code is checked here before anything connects (invite.decode catches
    every one-character typo), so a mistyped code gets its sentence at once
    instead of after eight seconds of trying. Everything after that is the
    session's: what it is doing while it connects, and the sentence when it
    can't.
    """

    IDLE, CONNECTING, FAILED, JOINED = "idle", "connecting", "failed", "joined"

    def __init__(self, session, parent=None) -> None:
        super().__init__(session, "Join a movie night", parent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.state = self.IDLE
        self._last_length = 0
        self._needs_fresh_code = False
        self.headline.setText("Join a friend's movie night")
        self.subline.setText("Paste the invite code they sent you. The whole message it came in "
                             "works too, or a mistery://join/ link.")

        self.code_row = QWidget()
        row = QHBoxLayout(self.code_row)
        row.setContentsMargins(0, 0, 0, 12)
        row.setSpacing(10)
        self.code_input = QLineEdit()
        self.code_input.setObjectName("CodeInput")
        self.code_input.setPlaceholderText("1AB2C-D3EF4-…")
        self.code_input.setClearButtonEnabled(True)
        self.code_input.textChanged.connect(self._on_text)
        row.addWidget(self.code_input, 1)
        # Never the button Enter presses. QDialog made it the default (the first
        # button in the tab order), and Enter in the box with a mistyped code
        # went on to press it: what the friend had typed was replaced by
        # whatever was on the clipboard. Enter is Join's (below): the box passes
        # Return on to the dialog, and the dialog presses its default button.
        self.paste_button = _button("Paste", "Ghost")
        self.paste_button.setAutoDefault(False)
        self.paste_button.clicked.connect(self._paste)
        row.addWidget(self.paste_button)
        self.body.addWidget(self.code_row)

        self.check = _text("", 9.8, C.TEXT_DIM)
        self.body.addWidget(self.check)

        # The steps so far above, faint, lined up with the one it is on now,
        # which has the spinner beside it.
        self.progress_panel = QWidget()
        progress = QVBoxLayout(self.progress_panel)
        progress.setContentsMargins(0, 6, 0, 0)
        progress.setSpacing(6)
        done_row = QHBoxLayout()
        done_row.setSpacing(0)
        done_row.addSpacing(30)
        self.steps_done = _text("", 9.5, C.TEXT_FAINT, rich=False)
        done_row.addWidget(self.steps_done, 1)
        progress.addLayout(done_row)
        now_row = QHBoxLayout()
        now_row.setSpacing(12)
        self.spinner = _Spinner()
        now_row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        self.step_now = _text("", 10.5, C.TEXT, rich=False)
        now_row.addWidget(self.step_now, 1)
        progress.addLayout(now_row)
        self.body.addWidget(self.progress_panel)

        self.failure_box, failure = _box("NoteBox", spacing=8)
        self.failure_title = _text("", 10.5, C.TEXT, bold=True, rich=False)
        failure.addWidget(self.failure_title)
        self.failure_text = _text("", 9.8, C.TEXT_DIM)
        failure.addWidget(self.failure_text)
        self.body.addWidget(self.failure_box)

        self.body.addSpacing(16)
        self.name_note = _text("", 9.0, C.TEXT_FAINT)
        self.body.addWidget(self.name_note)

        self.body.addStretch(1)
        footer = self.footer
        footer.addStretch(1)
        # Neither takes Enter by having focus. While a join connects, the box is
        # disabled and Qt hands its focus to Cancel: a second Enter, pressed by
        # habit, cancelled the join just started, and after a failure Enter was
        # Cancel instead of Try again. Esc still cancels; Space still presses.
        self.cancel_button = _button("Cancel")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self._cancel)
        footer.addWidget(self.cancel_button)
        self.leave_button = _button("Leave movie night", "Danger")
        self.leave_button.setAutoDefault(False)
        self.leave_button.clicked.connect(self._leave)
        footer.addWidget(self.leave_button)
        self.join_button = _button("Join", "Primary")
        self.join_button.clicked.connect(self._join_clicked)
        self.join_button.setDefault(True)           # Enter, in the box or anywhere: Join (or Try again)
        footer.addWidget(self.join_button)

        session.changed.connect(self._on_changed)
        session.message.connect(self._on_message)
        session.error.connect(self._on_error)
        session.ended.connect(self._on_ended)
        self._show_state(self.IDLE)

    # --- entry ------------------------------------------------------------------------

    def open_fresh(self) -> None:
        """Ready for a code; or, during a join or a movie night, how that is going."""
        from ..party import people

        self.name_note.setText(f"Friends will see you as <b>{html.escape(people.display_name())}"
                               "</b>. You can change that in Settings → Movie night.")
        if self.session.role == "guest":
            if self.session.phase == "on":
                self._show_state(self.JOINED)
            else:
                self._show_state(self.CONNECTING)
                self._step(self.session.status)
            return
        if self.session.role == "host":
            self.failure_title.setText("You're hosting a movie night")
            self.failure_text.setText("End it before you join someone else's.")
            self._show_state(self.FAILED)
            self.join_button.setVisible(False)      # nothing to join or try again here
            return
        self.code_input.setText("")
        self._say("")
        self._show_state(self.IDLE)
        self.code_input.setFocus()

    # --- the code -----------------------------------------------------------------------

    def _paste(self) -> None:
        self.code_input.setText(QGuiApplication.clipboard().text())
        self._validate(show=True)

    def _on_text(self, text: str) -> None:
        if self.state == self.FAILED:
            self._show_state(self.IDLE)
        # A paste arrives all at once; typing arrives a character at a time and
        # is only judged once it is long enough to be a whole code (65 symbols).
        jumped = len(text) - self._last_length >= 10
        self._last_length = len(text)
        compact = sum(ch.isalnum() for ch in text)
        if jumped or compact >= 65:
            self._validate(show=True)
        elif self.check.text():
            self._say("")

    def _validate(self, show: bool):
        """The invite in the box, or None with the reason shown."""
        from ..party import invite

        text = self.code_input.text()
        try:
            found = invite.decode(text)
        except invite.InviteError as problem:
            if show:
                self._say(f"<span style='color:{C.DANGER}'>{html.escape(str(problem))}</span>")
            return None
        canonical = invite.encode(found)
        if text.strip() != canonical:
            # The code alone, as the host's panel shows it, rather than the
            # whole Discord message it was found in; from its first group.
            self.code_input.blockSignals(True)
            self.code_input.setText(canonical)
            self._last_length = len(canonical)
            self.code_input.blockSignals(False)
        self.code_input.setCursorPosition(0)
        if show:
            # What the code holds, never a promise of where it works: its
            # internet address is STUN's answer and the forward behind it is the
            # host's word (party_forwarded), so a friend told "it works from
            # anywhere" could read that and then "Couldn't reach the host's PC".
            # Only on their home network is exact, and says so.
            if found.wan_ip and found.lan_ip:
                has = "It has their home network's address and their internet address."
            elif found.lan_ip:
                has = "It works only on their home network."
            else:
                has = "It has their internet address."
            self._say(f"<span style='color:{C.SUCCESS}'>That's an invite code.</span> {has}")
        return canonical

    def _say(self, text: str) -> None:
        self.check.setText(text)
        self.check.setVisible(bool(text))
        self.fit()

    def _join_clicked(self) -> None:
        if self.state == self.CONNECTING or self.session.role:
            return
        code = self._validate(show=True)
        if code is None:
            self.code_input.setFocus()
            return
        self.steps_done.setText("")
        self.steps_done.setVisible(False)
        self.step_now.setText("Starting…")
        self._show_state(self.CONNECTING)
        try:
            self.session.join(code)
        except Exception as exc:            # a bug, but a sentence all the same
            _log.exception("join failed")
            self._fail("Couldn't join", f"Something went wrong inside Mistery: {exc}")

    # --- the session's news ---------------------------------------------------------------

    def _step(self, text: str) -> None:
        """A new step of connecting; the one before it joins the list above.
        Getting as far as "Saying hello…" means a pinned connection worked, and
        tls.connect only returns one once the certificate matches the code, so
        that is said out loud, as the step it is."""
        now = self.step_now.text()
        if not text or text == now:
            return
        done = [line for line in self.steps_done.text().split("\n") if line]
        if now and now != "Starting…":
            done.append(now)
        if text == "Saying hello…":
            done.append("It's really them: their Mistery matches the code.")
        self.steps_done.setText("\n".join(done))
        self.steps_done.setVisible(bool(done))
        self.step_now.setText(text)
        self.fit()

    def _on_message(self, text: str) -> None:
        if self.state == self.CONNECTING:
            self._step(text)

    def _on_changed(self) -> None:
        if self.state != self.CONNECTING:
            return
        if session_joined(self.session):
            self._show_state(self.JOINED)
            self.accept()                   # the player takes over from here
        elif self.session.role == "guest":
            self._step(self.session.status)

    def _on_error(self, text: str) -> None:
        if self.state != self.CONNECTING:
            return
        from ..party import sync            # imported by the session already

        # The host's own words when it ended ("Sam ended the movie night."),
        # and what to do about it, which only the guest's side can say.
        if text.endswith("ended the movie night.") or "ended before you got in" in text:
            self._fail("The movie night has ended", text + " If you still want to watch, ask "
                       "them to start it again and send you the new code.", fresh_code=True)
        else:
            # A certificate that does not match, or a token the host no longer
            # takes, is this code finished with: trying it again cannot work.
            self._fail("Couldn't join", text, fresh_code=text in (sync.NOT_THEM, sync.TURNED_DOWN))

    def _on_ended(self, reason: str) -> None:
        if self.state == self.CONNECTING:       # an end with no error before it
            self._fail("The movie night has ended", (reason or "The movie night ended.")
                       + " If you still want to watch, ask them to start it again and send you "
                       "the new code.", fresh_code=True)
        elif self.state == self.JOINED:
            self._show_state(self.IDLE)

    def _fail(self, title: str, text: str, fresh_code: bool = False) -> None:
        self.failure_title.setText(title)
        self.failure_text.setText(html.escape(text))
        self._needs_fresh_code = fresh_code
        self._show_state(self.FAILED)
        # Back in the box, enabled again: where a new code is pasted, and where
        # Enter is Try again (or Join, for a new code).
        self.code_input.setFocus()

    # --- buttons -------------------------------------------------------------------------

    def _cancel(self) -> None:
        if self.state == self.CONNECTING:
            # The dialog first: leaving says `ended` at once, and that must not
            # read as a failed join.
            self._show_state(self.IDLE)
            self.session.leave()
            return
        self.reject()

    def reject(self) -> None:
        """Esc or the window's close button: while connecting, that is Cancel.
        A join left going behind a closed window would open the player out of
        nowhere, or fail with nobody to tell."""
        if self.state == self.CONNECTING:
            self._show_state(self.IDLE)
            self.session.leave()
        super().reject()

    def _leave(self) -> None:
        self.session.leave()
        self.reject()

    def _show_state(self, state: str) -> None:
        self.state = state
        connecting = state == self.CONNECTING
        joined = state == self.JOINED
        self.code_input.setEnabled(not connecting)
        self.paste_button.setEnabled(not connecting)
        self.code_row.setVisible(not joined)    # the code is spent once you are in
        self.progress_panel.setVisible(connecting)
        self.failure_box.setVisible(state == self.FAILED)
        # What the code check said belongs to the code, not to a connection
        # that failed: next to "Couldn't join", "That's an invite code" read
        # like a contradiction.
        self.check.setVisible(state == self.IDLE and bool(self.check.text()))
        self.join_button.setVisible(not joined and not connecting)
        self.join_button.setEnabled(not connecting)
        # "Try again" only where it can work: a host who has since forwarded
        # the port. A finished code needs a new one pasted, and then it's Join.
        self.join_button.setText("Try again" if state == self.FAILED and not self._needs_fresh_code
                                 else "Join")
        self.leave_button.setVisible(joined)
        self.cancel_button.setText("Close" if joined else "Cancel")
        if joined:
            host = self.session.host_name
            title = self.session.media_title
            self.headline.setText(f"You're in {host}'s movie night" if host else
                                  "You're in a movie night")
            self.subline.setText(f"Watching {title}." if title else "")
        else:
            self.headline.setText("Join a friend's movie night")
            self.subline.setText("Paste the invite code they sent you. The whole message it came "
                                 "in works too, or a mistery://join/ link.")
        self.fit()


def session_joined(session) -> bool:
    """Whether a guest's session has got in: the host has welcomed it, and the
    player is about to open. The one place this module decides it."""
    return session.role == "guest" and session.phase == "on"


# --- words ------------------------------------------------------------------------

def and_list(names: list[str]) -> str:
    """"Sam", "Sam and Alex", "Sam, Alex and Jo"."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def when_text(stamp: float | None, now: float | None = None) -> str:
    """"today", "yesterday", "on Tuesday", "on 12 Sep": when a movie night was."""
    if not stamp:
        return ""
    now = now if now is not None else time.time()
    day = time.localtime(stamp)
    today = time.localtime(now)
    days = (time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
            - time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))) // 86400
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "on " + time.strftime("%A", day)
    return "on " + time.strftime("%d %b", day).lstrip("0")
