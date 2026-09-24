"""Who this Mistery is, for as long as it is installed.

A movie night makes a certificate for the evening and throws it away (see
app/party/tls.py). Sharing cannot: a friend added in September has to recognise
this PC in March, without anybody exchanging anything again. So this is one key
and one self-signed certificate, made on first use and kept in the data folder
beside the library, and its fingerprint — the first 16 bytes of the SHA-256 of
the certificate — is what a friend stores and checks on every connection.

Both sides show a certificate. That is the difference from a movie night, where
only the host has one and the guest proves itself with the code's token:

  - the friend connecting checks the certificate it was given when it paired,
    and sends nothing to anything else (`connect` below, same rule as tls.py);
  - this PC asks for the connecting Mistery's certificate and hands the
    connection nothing until its fingerprint matches a friend it knows
    (`server_context` requests it, and the server matches it afterwards).

The certificate is its own authority (basicConstraints CA:TRUE, keyCertSign),
because that is what OpenSSL needs to accept a self-signed certificate that has
been put in its trust store by hand. It says serverAuth and clientAuth, since
the same certificate is shown from both ends. None of that is trust in the web
sense: the fingerprint is the whole of the trust, and it is checked every time.

The key is a file in the data folder (`share-identity.pem`), not a temporary
one like the movie night key: ssl can only load a key from disk, and this one
has to survive a restart. It sits where library.db sits, so anything that can
read it can already read the library it protects.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import os
import socket
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..config import data_dir
from ..party import people
from ..party.tls import MIN_PIN_BYTES, PIN_BYTES, PinMismatch

_log = logging.getLogger("share")

FILE_NAME = "share-identity.pem"
LIFETIME_DAYS = 3650            # ten years: this is who you are, not one evening
CONNECT_TIMEOUT = 8.0           # a friend's PC may be across the internet, not the room


def pin_of(certificate_der: bytes) -> bytes:
    """The fingerprint a friend stores: the first 16 bytes of the SHA-256."""
    return hashlib.sha256(certificate_der).digest()[:PIN_BYTES]


class Identity:
    """This install's lasting key and certificate, and the contexts built on it."""

    def __init__(self, path, key: ec.EllipticCurvePrivateKey,
                 certificate: x509.Certificate, person_id: str) -> None:
        self.path = path
        self.person_id = person_id
        self._key = key
        self._certificate = certificate
        self.certificate_der = certificate.public_bytes(serialization.Encoding.DER)
        self.certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
        self.pin = pin_of(self.certificate_der)
        self.expires = certificate.not_valid_after_utc.timestamp()
        self._lock = threading.Lock()
        self._server: dict[tuple[str, ...], ssl.SSLContext] = {}
        self._client: ssl.SSLContext | None = None

    def __repr__(self) -> str:
        return f"<share.Identity {self.person_id} pin={self.pin.hex()[:16]}…>"

    # --- the two contexts ---------------------------------------------------

    def server_context(self, trusted: list[str] | tuple[str, ...] = ()) -> ssl.SSLContext:
        """For the listener. `trusted` is the friends' certificates, as PEM.

        A friend's Mistery shows its certificate during the handshake, and
        OpenSSL will only ask for one if it has something to check it against —
        hence the friends' certificates as the trust store. Anything else
        (a movie night guest, a stranger, a browser) shows none, which is
        allowed here and refused later by whatever route it asks for.

        Cached per set of friends, so adding one rebuilds it and nothing else
        does. Measured: 1-2 ms to build, against 0.05 ms to fetch a cached one.
        """
        key = tuple(sorted(trusted))
        with self._lock:
            context = self._server.get(key)
            if context is None:
                context = self._make_server_context(key)
                self._server[key] = context
            return context

    def _make_server_context(self, trusted: tuple[str, ...]) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.num_tickets = 0             # every connection shows its certificate again
        context.load_cert_chain(str(self.path))
        if trusted:
            context.verify_mode = ssl.CERT_OPTIONAL
            context.load_verify_locations(cadata="\n".join(trusted))
        else:
            context.verify_mode = ssl.CERT_NONE     # nobody to recognise yet
        return context

    def client_context(self) -> ssl.SSLContext:
        """For connecting to a friend: shows this certificate, checks none.

        Whether the friend is really the friend is settled by the fingerprint in
        `connect`, after the handshake and before a byte of ours goes out.
        """
        with self._lock:
            if self._client is None:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(str(self.path))
                self._client = context
            return self._client

    def connect(self, host: str, port: int, pin: bytes,
                timeout: float = CONNECT_TIMEOUT) -> ssl.SSLSocket:
        """A connection to a friend, with their fingerprint checked first.

        The same rule as a movie night's tls.connect, and the same failure:
        PinMismatch, with the socket closed and nothing sent. The difference is
        that this end shows a certificate too, so the friend can tell who called.
        """
        pin = bytes(pin)
        if not MIN_PIN_BYTES <= len(pin) <= 32:
            raise ValueError(f"a fingerprint is {MIN_PIN_BYTES} to 32 bytes, not {len(pin)}")
        deadline = time.monotonic() + timeout
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            raw.settimeout(max(0.01, deadline - time.monotonic()))
            tls = self.client_context().wrap_socket(raw)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            raw.close()
            raise TimeoutError(f"{host}:{port} did not finish the TLS handshake in time") from None
        except BaseException:
            raw.close()
            raise
        der = tls.getpeercert(binary_form=True)
        if der is None or not hmac.compare_digest(pin_of(der)[:len(pin)], pin[:PIN_BYTES]):
            tls.close()
            raise PinMismatch(
                f"Something answered at {host}:{port}, but it is not your friend's Mistery. "
                "Nothing was sent to it.")
        tls.settimeout(timeout)
        return tls


_identity: Identity | None = None
_making = threading.Lock()


def identity() -> Identity:
    """This install's identity, made on first use and kept in the data folder.

    Making one costs about 20 ms (OpenSSL's first key); loading it again is
    about 1 ms. Nothing in Mistery needs it until sharing is switched on or a
    friend connects, so it is made then rather than at startup.
    """
    global _identity
    with _making:
        if _identity is None:
            path = data_dir() / FILE_NAME
            _identity = _load(path) or _make(path)
        return _identity


def forget() -> None:
    """Drop the cached identity, so the next call reads the file again. Tests."""
    global _identity
    with _making:
        _identity = None


def person_id() -> str:
    """This install's id, which is movie night's: people.person_id().

    Sharing and movie night are the same person on the wire. A friend who
    watched something with you and a friend browsing your library are the same
    friend, and one id is what keeps them that way.
    """
    return people.person_id()


def _load(path) -> Identity | None:
    try:
        pem = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as problem:
        _log.warning("share: cannot read %s (%s); making a new identity", FILE_NAME, problem)
        return None
    try:
        key = serialization.load_pem_private_key(pem, password=None)
        certificate = x509.load_pem_x509_certificate(pem)
    except Exception as problem:            # noqa: BLE001 - a damaged file is not a crash
        _log.warning("share: %s is damaged (%s); making a new identity", FILE_NAME, problem)
        return None
    if certificate.not_valid_after_utc.timestamp() < time.time():
        _log.info("share: the identity in %s has expired; making a new one", FILE_NAME)
        return None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        _log.warning("share: %s holds a key of a kind we do not make; replacing it", FILE_NAME)
        return None
    return Identity(path, key, certificate, person_id())


def _make(path) -> Identity:
    """A new key and certificate, written where only this account can read them."""
    who = person_id()
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Mistery"),
        # The person id, so two certificates from one PC can be told apart in a
        # log without anything having to decode the fingerprint.
        x509.NameAttribute(NameOID.SERIAL_NUMBER, who),
    ])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=LIFETIME_DAYS))
        # Its own authority: OpenSSL only accepts a self-signed certificate from
        # a trust store as a root, and a root has to say it is one.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=True, key_cert_sign=True,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        # Shown from both ends, so it has to be allowed in both.
        .add_extension(x509.ExtendedKeyUsage(
            [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pem += certificate.public_bytes(serialization.Encoding.PEM)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written through a temporary file in the same folder, so a power cut cannot
    # leave half an identity behind, and opened 0600 where the platform has it.
    temporary = path.with_suffix(".new")
    handle = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "wb") as file:
        file.write(pem)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    _log.info("share: made this install's identity, fingerprint %s", pin_of(
        certificate.public_bytes(serialization.Encoding.DER)).hex()[:16])
    return Identity(path, key, certificate, who)
