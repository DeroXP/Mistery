"""The session's certificate, and a guest's pinned connection to it.

A movie night has no certificate authority, and should not have one. The host
makes a certificate for this session alone (EC P-256, valid for 24 hours), and
the invite code carries the first 104 bits of its SHA-256. A guest's Mistery
connects with TLS, looks at the certificate the host presented, and hangs up
before sending a single byte of its own unless it is that one. That is what
makes it safe to send the token next: it only ever goes to the certificate the
invite named, never to whoever happens to answer at that address.

The standard library's ssl does the TLS; `cryptography` only makes the
certificate, because ssl cannot. ssl also cannot load a key from memory, only
from a file, so the key is written for the 3 ms load_cert_chain takes (measured)
and deleted again — encrypted, under a random password that never leaves this
process, so even those milliseconds on disk give nobody a usable key.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import ssl
import tempfile
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PIN_BYTES = 16              # Identity.pin: the first 16 bytes of the certificate's SHA-256
MIN_PIN_BYTES = 12          # the least connect() accepts: 96 bits (invites carry 13 bytes)
LIFETIME = 24 * 3600        # a certificate outlives any movie night; a new one is made for each
CONNECT_TIMEOUT = 5.0


class PinMismatch(ConnectionError):
    """That address answered with somebody else's certificate. Nothing was sent to it.

    A ConnectionError on purpose: code that tries one address after another and
    catches OSError moves on to the next address, which is the right thing to do
    (a guest's own network can have a different PC at the host's LAN address).
    """


class Identity:
    """One session's key and self-signed certificate. The key stays in memory."""

    def __init__(self, key: ec.EllipticCurvePrivateKey, certificate: x509.Certificate) -> None:
        self._key = key
        self._certificate = certificate
        self.certificate_der = certificate.public_bytes(serialization.Encoding.DER)
        self.pin = hashlib.sha256(self.certificate_der).digest()[:PIN_BYTES]
        self.expires = certificate.not_valid_after_utc.timestamp()
        self._context: ssl.SSLContext | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"<Identity pin={self.pin.hex()} expires={self.expires:.0f}>"

    def server_context(self) -> ssl.SSLContext:
        """The context the listener wraps every connection in. Made once."""
        with self._lock:
            if self._context is None:
                self._context = self._make_server_context()
            return self._context

    def _make_server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # No session tickets: a guest never resumes a session (every connection
        # shows the certificate again, and the pin is checked every time), so
        # tickets would only be bytes sent for nothing after each handshake.
        context.num_tickets = 0
        password = secrets.token_bytes(32)
        pem = self._key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(password))
        pem += self._certificate.public_bytes(serialization.Encoding.PEM)
        handle, path = tempfile.mkstemp(prefix="mistery-party-", suffix=".pem")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(pem)
            context.load_cert_chain(path, password=password)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return context


def new_identity(ips=()) -> Identity:
    """A fresh key and certificate for one movie night.

    ips are the addresses the host can be reached at (None and duplicates are
    skipped); they go into the certificate's subject alternative names. Nothing
    checks them — the pin is what a guest trusts — but a certificate that says
    where it belongs is the honest kind. Measured: 15-18 ms for the first one
    in a process, while OpenSSL sets itself up, then 0.1 ms. (Importing this
    module costs about 250 ms, most of it cryptography's: import it when a
    movie night starts, not when Mistery does.)
    """
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mistery movie night")])
    addresses = []
    for ip in ips:
        if ip:
            address = ipaddress.ip_address(ip)
            if address not in addresses:
                addresses.append(address)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Five minutes back, so a guest whose clock runs a little behind would
        # still see a valid certificate if it ever checked dates.
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(seconds=LIFETIME))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    )
    if addresses:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(a) for a in addresses]), critical=False)
    return Identity(key, builder.sign(key, hashes.SHA256()))


def _client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # There is no authority to ask and no hostname to check: the pin is the
    # whole of the trust, and connect() checks it itself, straight after the
    # handshake and before anything is sent.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


_CLIENT = _client_context()


def connect(host: str, port: int, pin: bytes, timeout: float = CONNECT_TIMEOUT) -> ssl.SSLSocket:
    """A TLS connection to the host, or an OSError. Blocks: call it on a thread.

    Returns only once the certificate the host presented has been hashed and
    its first len(pin) bytes compared with pin (12 to 32 bytes; an invite has
    13). Otherwise the socket is closed without a byte of application data
    having gone to it, and PinMismatch is raised.

    timeout bounds the whole thing, connecting and the handshake together: a
    peer that accepts and then dribbles out a byte at a time does not get to
    hold the caller for timeout seconds per byte. The socket that comes back
    has timeout set as its per-operation timeout.
    """
    pin = bytes(pin)
    if not MIN_PIN_BYTES <= len(pin) <= 32:
        raise ValueError(f"a pin is {MIN_PIN_BYTES} to 32 bytes, not {len(pin)}")
    deadline = time.monotonic() + timeout
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        # The handshake gets whatever is left. CPython holds a whole handshake
        # to the socket's timeout, not each read within it: measured, a peer
        # sending one byte every 0.25 s is given up on at the timeout.
        raw.settimeout(max(0.01, deadline - time.monotonic()))
        tls = _CLIENT.wrap_socket(raw)
    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
        # What CPython raises when the deadline falls mid-record. It is a timeout.
        raw.close()
        raise TimeoutError(f"{host}:{port} did not finish the TLS handshake in time") from None
    except BaseException:
        raw.close()             # a no-op once wrap_socket has taken it over and closed it
        raise
    der = tls.getpeercert(binary_form=True)
    if der is None or not hmac.compare_digest(hashlib.sha256(der).digest()[:len(pin)], pin):
        # close(), not unwrap(): nothing more goes out, not even a goodbye.
        tls.close()
        raise PinMismatch(
            f"Something answered at {host}:{port}, but it is not your friend's Mistery: "
            "its certificate is not the one in the invite. Nothing was sent to it.")
    tls.settimeout(timeout)
    return tls
