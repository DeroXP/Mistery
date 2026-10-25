"""The two secrets of the phone remote, and the link a phone opens.

    secret   64 hex digits, made once. This PC shows it to the website when it
             connects, and the website files the connection under its hash (the
             remote id), so no other PC can take this one's place.
    key      32 hex digits: the phone's. It is in the link after the #, which a
             browser never sends anywhere; the page signs every request with it
             and api.py checks the signature. The website never sees it.

Forget phones makes a new key, and every phone linked before stops working.
"""

from __future__ import annotations

import hashlib
import secrets

from ..config import settings


def site() -> str:
    url = str(settings.get("remote_site") or "https://mistery.up.railway.app").rstrip("/")
    return url if url.startswith(("https://", "http://127.0.0.1", "http://localhost")) else \
        "https://mistery.up.railway.app"


def secret() -> str:
    value = str(settings.get("remote_secret") or "")
    if len(value) != 64:
        value = secrets.token_hex(32)
        settings.set("remote_secret", value)
    return value


def remote_id() -> str:
    """What the website files this PC under (server/relay.py remote_id)."""
    return hashlib.sha256(b"mistery-remote:" + secret().encode("ascii")).hexdigest()[:32]


def key() -> str:
    value = str(settings.get("remote_key") or "")
    if len(value) != 32:
        value = secrets.token_hex(16)
        settings.set("remote_key", value)
    return value


def key_bytes() -> bytes:
    return bytes.fromhex(key())


def phone_link() -> str:
    return f"{site()}/remote/{remote_id()}#{key()}"


def forget_phones() -> str:
    """A new key: every phone linked so far has to be linked again."""
    settings.set("remote_key", secrets.token_hex(16))
    return phone_link()


def relay_url() -> str:
    base = site()
    return ("wss://" + base[len("https://"):] if base.startswith("https://")
            else "ws://" + base[len("http://"):]) + "/relay/pc"
