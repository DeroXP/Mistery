"""The invite code: everything a friend's Mistery needs to find yours and trust it.

A code is 13 groups of 5, made to be pasted into Discord or a text:

    1AB2C-D3EF4-...-XY9Z0

What it carries, in 65 characters of 5 bits (325 bits: 288 of them the
addresses, port, token and pin, 2 spare, and the version and checksum):

    version      which layout this is (1), so an older Mistery can say "update
                 Mistery" instead of "that code is wrong"
    internet IP  the home's public IPv4: the router's, from UPnP, or a public
                 STUN server's answer when the router won't say (stun);
                 0.0.0.0 when there is none
    LAN IP       this PC's address on the home network; 0.0.0.0 when there is none
    port         the one port the movie night listens on
    token        104 random bits: the password for this movie night. The host
                 answers nothing useful to anyone without it.
    pin          104 bits of the SHA-256 of this session's certificate. A guest
                 checks it before sending a single byte (tls.connect), so nobody
                 in between can pass themselves off as the host.
    checksum     6 characters

Why it is shaped like that:

- Crockford's base32 alphabet, 0-9 and A-Z without I, L, O and U: five bits a
  character, and the letters people mix up with digits are read as the digits
  they look like (O is 0, I and L are 1), so there is nothing ambiguous to get
  wrong. Case does not matter. No U, so no rude words by accident.
- The checksum is the BCH code Bitcoin addresses use (bech32m, BIP-350), over
  this alphabet. It is guaranteed to catch any mistake in up to four characters
  (a typo, two characters swapped), where a hash cut to the same six characters
  would only make that likely. The test tries every one-character typo of a
  code, 2015 of them, and every one is caught before anything connects.
- Always the same length: both addresses are always there, zero when absent. A
  missing or extra character is caught by the shape alone.
- 104 bits for the token and the pin rather than the 96 that would do, because
  the layout then comes out at exactly 13 groups of 5.
- A later layout may be longer, but must keep two things: the version as the
  first character, and this checksum over the whole code. Then this version
  recognises it as newer at any length, and says "update Mistery".

decode() is for whatever a person pastes: the code alone, a whole Discord
message with the code somewhere in it, a mistery://join/ link, in any case, with
the dashes turned into spaces or en dashes by some chat app on the way.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import unicodedata
from dataclasses import dataclass

VERSION = 1
TOKEN_BYTES = 13            # 104 bits
PIN_BYTES = 13              # 104 of the certificate SHA-256's 256 bits
LINK_PREFIX = "mistery://join/"

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_VALUE = {c: i for i, c in enumerate(ALPHABET)}
_VALUE.update({"O": 0, "I": 1, "L": 1})

_GROUP = 5
_GROUPS = 13
_PAYLOAD_BITS = 32 + 32 + 16 + 8 * TOKEN_BYTES + 8 * PIN_BYTES     # 288
_DATA_SYMBOLS = -(-_PAYLOAD_BITS // 5)                            # 58, 2 bits spare
_CHECK_SYMBOLS = 6
_SYMBOLS = 1 + _DATA_SYMBOLS + _CHECK_SYMBOLS                     # version + data + check
assert _SYMBOLS == _GROUP * _GROUPS

# bech32m (BIP-173's generator, BIP-350's constant). The prefix is BIP-173's
# "human-readable part" expansion of "mistery": it is never typed, but it makes
# a Mistery checksum different from anybody else's, so some other bech32-style
# string that happens to be 65 characters long does not read as an invite.
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_CONSTANT = 0x2BC830A3
_DOMAIN = [ord(c) >> 5 for c in "mistery"] + [0] + [ord(c) & 31 for c in "mistery"]

# A Discord message is at most 4000 characters (with Nitro). Anything longer is
# not a message somebody pasted; it is refused before any work is done on it.
# The worst paste of this size, 20,000 characters of code-shaped groups where
# every position has to be tried, takes 94 ms to refuse (measured); a real
# message with a code in it takes 0.08 ms.
MAX_PASTE = 20_000

_CODE_CHAR = "0-9A-Za-z"
# Whitespace and every dash a chat app or a word processor might turn "-" into.
_SEPARATOR = r"[\s\-\u2010-\u2015\u2212]"
# Overlapping on purpose (the lookahead): in "movie night 1AB2C-..." the words
# "movie" and "night" are five letters too, and a plain search would take them
# as the first two groups, fail the checksum and skip past the real code.
_CANDIDATE = re.compile(
    rf"(?<![{_CODE_CHAR}])(?=([{_CODE_CHAR}]{{{_GROUP}}}"
    rf"(?:{_SEPARATOR}*[{_CODE_CHAR}]{{{_GROUP}}}){{{_GROUPS - 1}}})(?![{_CODE_CHAR}]))")
_PARTIAL = re.compile(
    rf"(?<![{_CODE_CHAR}])[{_CODE_CHAR}]{{3,7}}(?:{_SEPARATOR}+[{_CODE_CHAR}]{{3,7}}){{3,}}")
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"))

# What a person is told. Every one says what to do next.
EMPTY = "Paste the invite code your friend sent you."
TOO_LONG = "That is far too long to be an invite. Paste just the code, or the message it came in."
NOT_FOUND = ("There is no invite code in that. A code is 13 groups of 5 letters and "
             "numbers, like 1AB2C-D3EF4-… Ask your friend to send it again.")
PARTIAL = ("That looks like an invite code with a character missing or extra — a whole "
           "one is 13 groups of 5. Copy it again, all of it.")
MISTYPED = ("That invite code has a mistake in it: a character is wrong. Copy it again, "
            "or ask your friend to send it again.")
NEWER = ("This invite was made by a newer version of Mistery. Update Mistery, then "
         "paste it again.")
NO_ADDRESS = "This invite has no address to connect to. Ask your friend to send a new one."
DAMAGED = "This invite is damaged. Ask your friend to start the movie night again and send the new code."
TWO_CODES = "That has two different invite codes in it. Paste just the one you want to join."


class InviteError(ValueError):
    """An invite that cannot be used, with a sentence saying what to do about it."""


@dataclass(frozen=True)
class Invite:
    wan_ip: str | None      # the host's internet address, None when unknown
    lan_ip: str | None      # the host's address on its own network, None when unknown
    port: int
    token: bytes            # TOKEN_BYTES of randomness, the movie night's password
    pin: bytes              # the leading PIN_BYTES of the certificate's SHA-256
    version: int = VERSION


def new_token() -> bytes:
    """A fresh movie night password, exactly as long as an invite carries:
    encode() refuses any other length rather than cut one short."""
    return secrets.token_bytes(TOKEN_BYTES)


def encode(invite: Invite) -> str:
    """The code for an invite, as 13 dash-separated groups of 5.

    A missing token or pin, or one the wrong size, is a bug in the caller and
    raises ValueError. No address at all is a real situation (a PC that is not
    on any network), and raises InviteError with a sentence for the host.
    """
    if invite.version != VERSION:
        raise ValueError(f"this Mistery writes invite version {VERSION}, not {invite.version}")
    token = bytes(invite.token)
    if len(token) != TOKEN_BYTES:
        raise ValueError(f"the token must be {TOKEN_BYTES} bytes, not {len(token)}")
    pin = bytes(invite.pin)
    if len(pin) < PIN_BYTES:
        raise ValueError(f"the pin must be at least {PIN_BYTES} bytes, not {len(pin)}")
    if not isinstance(invite.port, int) or not 1 <= invite.port <= 65535:
        raise ValueError(f"not a port: {invite.port!r}")
    wan = _address_number(invite.wan_ip)
    lan = _address_number(invite.lan_ip)
    if not wan and not lan:
        raise InviteError("Mistery could not find an address for this PC on any network, so "
                          "there is nothing a friend could connect to. Check that you are online.")

    number = wan
    number = number << 32 | lan
    number = number << 16 | invite.port
    number = number << (8 * TOKEN_BYTES) | int.from_bytes(token, "big")
    number = number << (8 * PIN_BYTES) | int.from_bytes(pin[:PIN_BYTES], "big")
    number <<= _DATA_SYMBOLS * 5 - _PAYLOAD_BITS
    data = [(number >> (5 * i)) & 31 for i in reversed(range(_DATA_SYMBOLS))]
    symbols = [VERSION] + data
    symbols += _checksum(symbols)
    text = "".join(ALPHABET[s] for s in symbols)
    return "-".join(text[i:i + _GROUP] for i in range(0, len(text), _GROUP))


def link(invite: Invite | str) -> str:
    """mistery://join/<code>, for friends who have the link handler installed."""
    code = invite if isinstance(invite, str) else encode(invite)
    return LINK_PREFIX + code


def decode(text: str) -> Invite:
    """The invite in whatever a person pasted, or InviteError saying what is wrong.

    Never anything else: not a crash on a huge paste, on emoji or on an empty
    box. Every stretch of the text shaped like a code is tried; a code with a
    typo is refused on its checksum, before anything connects anywhere.
    """
    if not isinstance(text, str) or not text.strip():
        raise InviteError(EMPTY)
    if len(text) > MAX_PASTE:
        raise InviteError(TOO_LONG)
    # NFKC turns full-width letters (from a Japanese or Chinese keyboard) and
    # other look-alike forms into plain ones; zero-width characters that some
    # apps slip into copied text are dropped.
    cleaned = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)

    found: list[Invite] = []
    problems: list[str] = []
    for match in _CANDIDATE.finditer(cleaned):
        try:
            invite = _unpack(match.group(1))
        except InviteError as problem:
            problems.append(str(problem))
            continue
        if invite not in found:
            found.append(invite)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise InviteError(TWO_CODES)
    if NEWER in problems or _newer(cleaned):
        raise InviteError(NEWER)
    for message in (NO_ADDRESS, DAMAGED):
        if message in problems:
            raise InviteError(message)
    if problems:
        raise InviteError(problems[0])
    if _PARTIAL.search(cleaned):
        raise InviteError(PARTIAL)
    raise InviteError(NOT_FOUND)


# --- inside -------------------------------------------------------------------

def _polymod(values, check: int = 1) -> int:
    """The BCH remainder after values; check carries on from an earlier part."""
    for value in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            if (top >> i) & 1:
                check ^= _GENERATOR[i]
    return check


def _checksum(symbols: list[int]) -> list[int]:
    remainder = _polymod(_DOMAIN + symbols + [0] * _CHECK_SYMBOLS) ^ _CONSTANT
    return [(remainder >> 5 * (5 - i)) & 31 for i in range(_CHECK_SYMBOLS)]


# A later layout may well be longer (IPv6 addresses need more room), but it will
# keep what this version can check without knowing it: the version as the first
# character, and the same checksum over the whole code. A code of another
# length whose checksum holds and whose version is higher is a newer Mistery's,
# and the person is told to update, not that they mistyped it.
_RUN = re.compile(rf"[{_CODE_CHAR}]+(?:{_SEPARATOR}+[{_CODE_CHAR}]+)*")
_WORD = re.compile(rf"[{_CODE_CHAR}]+")
_OTHER_LENGTHS = range(40, 201)     # 40 symbols hold a 96-bit token and pin; 200 is far past two IPv6 addresses
_WORDS_BEFORE = 12                  # words that may come before a code in one run: "hello there movie night"
_DOMAIN_CHECK = _polymod(_DOMAIN)


def _newer(cleaned: str) -> bool:
    """Whether cleaned holds a code of another length from a newer Mistery.
    Only asked once nothing in it decoded. At most 12 starting words a run and
    200 symbols from each, so the worst paste stays well inside the bound."""
    for run in _RUN.finditer(cleaned):
        words = _WORD.findall(run.group())
        left = sum(map(len, words))
        for start in range(min(len(words), _WORDS_BEFORE)):
            if left < _OTHER_LENGTHS[0]:
                break                   # too few symbols from here on to be any code
            left -= len(words[start])
            first = _VALUE.get(words[start][0].upper())
            if first is None or first <= VERSION:
                continue
            check, count = _DOMAIN_CHECK, 0
            for word in words[start:]:
                symbols = [_VALUE.get(char) for char in word.upper()]
                count += len(symbols)
                if None in symbols or count > _OTHER_LENGTHS[-1]:
                    break
                check = _polymod(symbols, check)
                if count != _SYMBOLS and count in _OTHER_LENGTHS and check == _CONSTANT:
                    return True
    return False


def _address_number(address: str | None) -> int:
    """An IPv4 address as a number, 0 for none. Only a usable host address:
    the tests run on 127.0.0.1, so loopback is allowed; 0.0.0.0/8 (0 means "no
    address" here), multicast and the reserved 240/4 are not."""
    if address is None or address == "":
        return 0
    try:
        parsed = ipaddress.IPv4Address(address)
    except (ipaddress.AddressValueError, ValueError, TypeError):
        raise ValueError(f"not an IPv4 address: {address!r}") from None
    if not _usable(parsed):
        raise ValueError(f"not an address a computer can have: {address}")
    return int(parsed)


def _usable(address: ipaddress.IPv4Address) -> bool:
    return not (address.is_multicast or address.is_reserved
                or address in ipaddress.IPv4Network("0.0.0.0/8"))


def _unpack(code: str) -> Invite:
    symbols = []
    for char in code.upper():
        if char in _VALUE:
            symbols.append(_VALUE[char])
        elif char.isalnum():
            raise InviteError(MISTYPED)     # U, or a letter outside the alphabet
    if len(symbols) != _SYMBOLS:
        raise InviteError(PARTIAL)
    if _polymod(_DOMAIN + symbols) != _CONSTANT:
        raise InviteError(MISTYPED)
    if symbols[0] > VERSION:
        raise InviteError(NEWER)
    if symbols[0] != VERSION:
        raise InviteError(DAMAGED)

    number = 0
    for value in symbols[1:1 + _DATA_SYMBOLS]:
        number = number << 5 | value
    spare = _DATA_SYMBOLS * 5 - _PAYLOAD_BITS
    if number & ((1 << spare) - 1):
        raise InviteError(DAMAGED)          # only a made-up code sets the spare bits
    number >>= spare
    pin = (number & ((1 << 8 * PIN_BYTES) - 1)).to_bytes(PIN_BYTES, "big")
    number >>= 8 * PIN_BYTES
    token = (number & ((1 << 8 * TOKEN_BYTES) - 1)).to_bytes(TOKEN_BYTES, "big")
    number >>= 8 * TOKEN_BYTES
    port = number & 0xFFFF
    number >>= 16
    lan = number & 0xFFFFFFFF
    wan = number >> 32

    if not wan and not lan:
        raise InviteError(NO_ADDRESS)
    addresses = []
    for value in (wan, lan):
        if not value:
            addresses.append(None)
            continue
        address = ipaddress.IPv4Address(value)
        if not _usable(address):
            raise InviteError(DAMAGED)
        addresses.append(str(address))
    if not port:
        raise InviteError(DAMAGED)
    return Invite(wan_ip=addresses[0], lan_ip=addresses[1], port=port, token=token, pin=pin)
