"""LAN transfer of stored accounts (including passwords) between two installs.

Protocol v1. The *sender* is the machine that has the passwords; the user
starts it first. The *receiver* finds it on the local network.

1. Sender listens on an ephemeral TCP port and broadcasts a UDP beacon on
   DISCOVERY_PORT. It accepts exactly one connection per session, then stops
   advertising.
2. Receiver picks a beacon and connects to the beacon's *source* address
   (never an address carried inside the beacon). Every socket sends with
   IP TTL 1, so no packet survives a router: a peer that is not on the local
   network cannot complete the TCP handshake. Both ends also refuse peers
   outside private/link-local address space.
3. X25519 key agreement with a commitment:
       R -> S  hello   {commit = SHA256(pk_R)}
       S -> R  key     {pk_S}
       R -> S  reveal  {pk_R}      S checks SHA256(pk_R) == commit
   pk_R is fixed before R sees pk_S, so a man in the middle cannot search
   keys offline for colliding codes: it gets one guess per session.
4. HKDF over the shared secret, salted with the transcript hash, yields the
   session key and two 6-digit codes. Code A is shown on the sender and typed
   on the receiver; code B is shown on the receiver and typed on the sender.
   Neither side can be confirmed by reading its own screen, so a third
   machine that connects to the sender first cannot be approved by mistake.
5. After the handshake every message is AES-256-GCM encrypted with the
   transcript and direction as associated data. The sender sends the
   accounts only after both codes are confirmed. A wrong code or a cancel on
   either side aborts the session; there is no retry within a session.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

PROTOCOL = b"tradegate-transfer-v1"
VERSION = 1
DISCOVERY_PORT = 47913
BEACON_INTERVAL = 0.5
SESSION_TIMEOUT = 300.0  # seconds a sender advertises / a session may last
_POLL = 0.5  # socket timeout granularity for cancel checks
_MAX_FRAME = 1 << 20
_MAX_HOST = 64
_CODE_DIGITS = 6
_LOCAL_TTL = 1  # one hop: packets are dropped by the first router


class TransferError(Exception):
    """The transfer failed: protocol violation, network error, or timeout."""


class TransferAborted(TransferError):
    """The transfer was stopped by a user: cancel or a code that did not match."""


@dataclass(frozen=True)
class Offer:
    """A sender seen on the network."""

    host: str
    address: str
    port: int
    session_id: str


class TransferUI(Protocol):
    """What a session needs from the user interface.

    ``cancelled`` is set when the user closes the UI; sessions poll it.
    """

    cancelled: threading.Event

    def status(self, text: str) -> None: ...

    def show_code(self, title: str, code: str, text: str) -> None: ...

    def ask_code(self, title: str, text: str) -> str | None: ...

    def pick_offer(self, offers: list[Offer]) -> Offer | str | None: ...

    def interrupt(self) -> None:
        """Make a blocked ask_code return None (the peer went away)."""


# --- helpers -----------------------------------------------------------------


def format_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}"


def _codes_match(typed: str, expected: str) -> bool:
    digits = "".join(ch for ch in typed if ch.isdigit())
    return hmac.compare_digest(digits.encode(), expected.encode())


def _is_int(value: object) -> bool:
    """True for real ints; bool is an int subclass and must not pass."""
    return type(value) is int


def _is_local_peer(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_private or ip.is_link_local


def _restrict_to_local_network(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, _LOCAL_TTL)


def _hostname() -> str:
    return socket.gethostname()[:_MAX_HOST]


@dataclass(frozen=True)
class _Keys:
    key: bytes
    transcript: bytes
    code_a: str  # shown on sender, typed on receiver
    code_b: str  # shown on receiver, typed on sender


def _derive(shared: bytes, session_id: str, commit: bytes, pk_s: bytes, pk_r: bytes) -> _Keys:
    transcript = hashlib.sha256(
        PROTOCOL + session_id.encode() + commit + pk_s + pk_r
    ).digest()

    def expand(info: bytes, length: int) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(), length=length, salt=transcript,
            info=PROTOCOL + b" " + info,
        ).derive(shared)

    def code(info: bytes) -> str:
        value = int.from_bytes(expand(info, 8), "big") % 10**_CODE_DIGITS
        return f"{value:0{_CODE_DIGITS}d}"

    return _Keys(
        key=expand(b"key", 32),
        transcript=transcript,
        code_a=code(b"code-a"),
        code_b=code(b"code-b"),
    )


class _Channel:
    """Length-prefixed JSON frames over TCP, with cancel/deadline polling."""

    def __init__(self, sock: socket.socket, cancelled: threading.Event, deadline: float) -> None:
        self._sock = sock
        self._sock.settimeout(_POLL)
        self._cancelled = cancelled
        self._deadline = deadline
        self._aead: AESGCM | None = None
        self._aad_out = b""
        self._aad_in = b""

    def secure(self, keys: _Keys, *, sender: bool) -> None:
        self._aead = AESGCM(keys.key)
        mine, theirs = (b"S", b"R") if sender else (b"R", b"S")
        self._aad_out = keys.transcript + mine
        self._aad_in = keys.transcript + theirs

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        if self._aead is not None:
            nonce = os.urandom(12)
            data = nonce + self._aead.encrypt(nonce, data, self._aad_out)
        try:
            self._sock.sendall(struct.pack(">I", len(data)) + data)
        except OSError as exc:
            raise TransferError(f"Connection lost: {exc}") from exc

    def recv(self) -> dict:
        (length,) = struct.unpack(">I", self._recv_exact(4))
        if length > _MAX_FRAME:
            raise TransferError("Peer sent an oversized message")
        data = self._recv_exact(length)
        if self._aead is not None:
            if len(data) < 12 + 16:
                raise TransferError("Peer sent a truncated encrypted message")
            try:
                data = self._aead.decrypt(data[:12], data[12:], self._aad_in)
            except InvalidTag as exc:
                raise TransferError("Message failed authentication; session tampered with") from exc
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TransferError("Peer sent malformed data") from exc
        if not isinstance(obj, dict):
            raise TransferError("Peer sent malformed data")
        if obj.get("t") == "abort":
            raise TransferAborted(f"The other machine stopped the transfer: {obj.get('reason', 'cancelled')}")
        return obj

    def expect(self, kind: str) -> dict:
        obj = self.recv()
        if obj.get("t") != kind:
            raise TransferError(f"Protocol error: expected {kind!r}, got {obj.get('t')!r}")
        return obj

    def abort(self, reason: str) -> None:
        """Best-effort notice to the peer; the local failure is raised by the caller."""
        try:
            self.send({"t": "abort", "reason": reason})
        except TransferError:
            log.debug("Could not deliver abort notice", exc_info=True)

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            self._check()
            try:
                chunk = self._sock.recv(n - len(buf))
            except TimeoutError:
                continue
            except OSError as exc:
                raise TransferError(f"Connection lost: {exc}") from exc
            if not chunk:
                raise TransferError("The other machine closed the connection")
            buf.extend(chunk)
        return bytes(buf)

    def peer_readable(self, timeout: float) -> bool:
        readable, _, _ = select.select([self._sock], [], [], timeout)
        return bool(readable)

    def _check(self) -> None:
        if self._cancelled.is_set():
            raise TransferAborted("Cancelled")
        if time.monotonic() > self._deadline:
            raise TransferError("Timed out")


def _hex_field(obj: dict, field: str, length: int) -> bytes:
    value = obj.get(field)
    if not isinstance(value, str) or len(value) != length * 2:
        raise TransferError(f"Protocol error: bad {field!r}")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise TransferError(f"Protocol error: bad {field!r}") from exc


def _host_field(obj: dict) -> str:
    host = obj.get("host")
    if not isinstance(host, str) or not host or len(host) > _MAX_HOST:
        raise TransferError("Protocol error: bad host name")
    return "".join(ch for ch in host if ch.isprintable())


def _exchange(sk: X25519PrivateKey, peer_pk: bytes) -> bytes:
    try:
        return sk.exchange(X25519PublicKey.from_public_bytes(peer_pk))
    except ValueError as exc:  # low-order point -> all-zero shared secret
        raise TransferError("Peer sent an invalid public key") from exc


def _ask_code(ui: TransferUI, ch: _Channel, title: str, text: str) -> str | None:
    """ui.ask_code, returning early if the peer cancels or disconnects meanwhile.

    While a user types, the peer has nothing legitimate to send, so any
    readable data (an abort notice) or EOF ends the wait.
    """
    stop = threading.Event()
    peer_spoke = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            if ch.peer_readable(_POLL):
                peer_spoke.set()
                ui.interrupt()
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        typed = ui.ask_code(title, text)
    finally:
        stop.set()
        watcher.join()
    if peer_spoke.is_set():
        ch.recv()  # raises with the peer's abort reason, or "closed the connection"
        raise TransferError("Protocol error: unexpected message while waiting for a code")
    return typed


@contextlib.contextmanager
def _notify_peer_on_cancel(ch: _Channel, ui: TransferUI, reason: str):
    """If the local user cancels mid-session, tell the peer before closing."""
    try:
        yield
    except TransferAborted:
        if ui.cancelled.is_set():
            ch.abort(reason)
        raise


# --- sender ------------------------------------------------------------------


def _beacon_loop(message: bytes, target: tuple[str, int], stop: threading.Event) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        _restrict_to_local_network(s)
        while not stop.is_set():
            try:
                s.sendto(message, target)
            except OSError:
                # Transient (e.g. network down); the sender keeps waiting and
                # the user sees no receiver rather than a dead session.
                log.debug("Beacon send failed", exc_info=True)
            stop.wait(BEACON_INTERVAL)


def send_accounts(
    accounts: list[dict],
    ui: TransferUI,
    *,
    beacon_target: tuple[str, int] = ("255.255.255.255", DISCOVERY_PORT),
    bind_host: str = "0.0.0.0",
) -> tuple[str, int]:
    """Run a sender session. Returns (receiver host, accounts imported).

    *accounts* are dicts with platform, username, label, password.
    Raises TransferError / TransferAborted on any failure.
    """
    # Refuse up front rather than after both users have confirmed codes.
    # Encrypted frame = JSON + 12-byte nonce + 16-byte tag.
    payload_size = len(json.dumps({"t": "accounts", "accounts": accounts}).encode()) + 28
    if payload_size > _MAX_FRAME:
        raise TransferError(
            f"Too much account data for one transfer ({payload_size} bytes, "
            f"limit {_MAX_FRAME}). Send fewer accounts per session."
        )

    deadline = time.monotonic() + SESSION_TIMEOUT
    session_id = secrets.token_hex(8)

    with socket.create_server((bind_host, 0)) as listener:
        # Set before accept so the SYN-ACK is already one-hop limited.
        _restrict_to_local_network(listener)
        listener.settimeout(_POLL)
        port = listener.getsockname()[1]
        beacon = json.dumps({
            "tg": "transfer", "v": VERSION, "host": _hostname(),
            "port": port, "sid": session_id,
        }).encode()
        stop = threading.Event()
        threading.Thread(
            target=_beacon_loop, args=(beacon, beacon_target, stop), daemon=True,
        ).start()
        ui.status(
            f"Waiting for a receiver ({len(accounts)} account(s) ready to send).\n"
            "On the new machine choose Transfer → Receive."
        )
        try:
            while True:
                if ui.cancelled.is_set():
                    raise TransferAborted("Cancelled")
                if time.monotonic() > deadline:
                    raise TransferError("No receiver connected within 5 minutes")
                try:
                    conn, (peer_ip, _peer_port) = listener.accept()
                except TimeoutError:
                    continue
                _restrict_to_local_network(conn)
                if _is_local_peer(peer_ip):
                    break
                log.warning("Rejected transfer connection from non-local %s", peer_ip)
                conn.close()
        finally:
            stop.set()

    with conn:
        ch = _Channel(conn, ui.cancelled, deadline)
        with _notify_peer_on_cancel(ch, ui, "cancelled on the sender"):
            return _send_session(ch, ui, accounts, session_id, peer_ip)


def _send_session(
    ch: _Channel, ui: TransferUI, accounts: list[dict], session_id: str, peer_ip: str,
) -> tuple[str, int]:
    hello = ch.expect("hello")
    if not _is_int(hello.get("v")) or hello["v"] != VERSION or hello.get("sid") != session_id:
        raise TransferError("Receiver speaks a different protocol version or session")
    peer_host = _host_field(hello)
    commit = _hex_field(hello, "commit", 32)

    sk = X25519PrivateKey.generate()
    pk_s = sk.public_key().public_bytes_raw()
    ch.send({"t": "key", "pk": pk_s.hex(), "host": _hostname()})

    pk_r = _hex_field(ch.expect("reveal"), "pk", 32)
    if not hmac.compare_digest(hashlib.sha256(pk_r).digest(), commit):
        raise TransferError("Receiver's key does not match its commitment; session tampered with")
    keys = _derive(_exchange(sk, pk_r), session_id, commit, pk_s, pk_r)
    ch.secure(keys, sender=True)

    ui.show_code(
        f"Receiver: {peer_host} ({peer_ip})",
        format_code(keys.code_a),
        "Type this code on the receiving machine.",
    )
    ch.expect("receiver-confirmed")

    typed = _ask_code(
        ui, ch,
        f"Receiver: {peer_host} ({peer_ip})",
        "Now type the code shown on the receiving machine.",
    )
    if typed is None:
        ch.abort("cancelled on the sender")
        raise TransferAborted("Cancelled")
    if not _codes_match(typed, keys.code_b):
        ch.abort("code did not match on the sender")
        raise TransferAborted(
            "Code did not match. Nothing was sent. If you typed it correctly, "
            "another device may have intercepted the session."
        )

    ch.send({"t": "accounts", "accounts": accounts})
    done = ch.expect("done")
    imported = done.get("imported")
    if not _is_int(imported):
        raise TransferError("Protocol error: bad 'done'")
    return peer_host, imported


# --- receiver ----------------------------------------------------------------


def discover(cancelled: threading.Event, duration: float = 2.0, port: int = DISCOVERY_PORT) -> list[Offer]:
    """Listen for sender beacons for *duration* seconds."""
    found: dict[tuple[str, int, str], Offer] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", port))
        s.settimeout(_POLL)
        end = time.monotonic() + duration
        while time.monotonic() < end and not cancelled.is_set():
            try:
                data, (addr, _) = s.recvfrom(2048)
            except TimeoutError:
                continue
            offer = _parse_beacon(data, addr)
            if offer is not None:
                found[(offer.address, offer.port, offer.session_id)] = offer
    return sorted(found.values(), key=lambda o: (o.host, o.address))


def _parse_beacon(data: bytes, addr: str) -> Offer | None:
    if not _is_local_peer(addr):
        return None
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None  # not ours; other software may use the port
    if not isinstance(obj, dict) or obj.get("tg") != "transfer":
        return None
    if not _is_int(obj.get("v")) or obj["v"] != VERSION:
        return None
    port, sid, host = obj.get("port"), obj.get("sid"), obj.get("host")
    if not (_is_int(port) and 1 <= port <= 65535):
        return None
    if not (isinstance(sid, str) and len(sid) == 16):
        return None
    if not (isinstance(host, str) and 0 < len(host) <= _MAX_HOST):
        return None
    host = "".join(ch for ch in host if ch.isprintable())
    return Offer(host=host, address=addr, port=port, session_id=sid)


def receive_accounts(
    ui: TransferUI,
    import_accounts: Callable[[list[dict]], int],
    *,
    discovery_port: int = DISCOVERY_PORT,
) -> tuple[str, int]:
    """Run a receiver session. Returns (sender host, accounts imported).

    *import_accounts* stores the received accounts and returns how many were
    imported; it runs before the sender is told the result.
    """
    while True:
        ui.status("Looking for a sender on the network…")
        offers = discover(ui.cancelled, port=discovery_port)
        if ui.cancelled.is_set():
            raise TransferAborted("Cancelled")
        choice = ui.pick_offer(offers)
        if choice is None:
            raise TransferAborted("Cancelled")
        if isinstance(choice, Offer):
            offer = choice
            break

    deadline = time.monotonic() + SESSION_TIMEOUT
    conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _restrict_to_local_network(conn)
    conn.settimeout(10)
    try:
        conn.connect((offer.address, offer.port))
    except OSError as exc:
        conn.close()
        raise TransferError(
            f"Could not connect to {offer.host} ({offer.address}): {exc}. "
            "Both machines must be on the same local network."
        ) from exc

    with conn:
        ch = _Channel(conn, ui.cancelled, deadline)
        with _notify_peer_on_cancel(ch, ui, "cancelled on the receiver"):
            return _receive_session(ch, ui, import_accounts, offer)


def _receive_session(
    ch: _Channel, ui: TransferUI, import_accounts: Callable[[list[dict]], int], offer: Offer,
) -> tuple[str, int]:
    sk = X25519PrivateKey.generate()
    pk_r = sk.public_key().public_bytes_raw()
    commit = hashlib.sha256(pk_r).digest()
    ch.send({
        "t": "hello", "v": VERSION, "sid": offer.session_id,
        "host": _hostname(), "commit": commit.hex(),
    })
    key_msg = ch.expect("key")
    pk_s = _hex_field(key_msg, "pk", 32)
    peer_host = _host_field(key_msg)
    ch.send({"t": "reveal", "pk": pk_r.hex()})
    keys = _derive(_exchange(sk, pk_s), offer.session_id, commit, pk_s, pk_r)
    ch.secure(keys, sender=False)

    typed = _ask_code(
        ui, ch,
        f"Sender: {peer_host} ({offer.address})",
        "Type the code shown on the sending machine.",
    )
    if typed is None:
        ch.abort("cancelled on the receiver")
        raise TransferAborted("Cancelled")
    if not _codes_match(typed, keys.code_a):
        ch.abort("code did not match on the receiver")
        raise TransferAborted(
            "Code did not match. Nothing was received. If you typed it correctly, "
            "another device may have intercepted the session."
        )
    ch.send({"t": "receiver-confirmed"})

    ui.show_code(
        f"Sender: {peer_host} ({offer.address})",
        format_code(keys.code_b),
        "Type this code on the sending machine to release the accounts.",
    )
    accounts = _validate_accounts(ch.expect("accounts").get("accounts"))
    ui.status(f"Received {len(accounts)} account(s). Saving…")
    imported = import_accounts(accounts)
    ch.send({"t": "done", "imported": imported})
    return peer_host, imported


def _validate_accounts(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise TransferError("Protocol error: bad account list")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise TransferError("Protocol error: bad account entry")
        fields = {k: item.get(k, "") for k in ("platform", "username", "label", "password")}
        if not all(isinstance(v, str) for v in fields.values()):
            raise TransferError("Protocol error: bad account entry")
        if not fields["platform"] or not fields["username"] or not fields["password"]:
            raise TransferError("Protocol error: incomplete account entry")
        out.append(fields)
    return out
