"""Electron front end and account plumbing for LAN account transfer."""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading

from tradegate.credentials.manager import CredentialManager
from tradegate.credentials.transfer import (
    Offer,
    TransferAborted,
    TransferError,
    receive_accounts,
    send_accounts,
)
from tradegate.platforms.registry import list_platforms
from tradegate.ui._electron import start_stream_dialog

log = logging.getLogger(__name__)

_CLOSED = {"ev": "closed"}
_INTERRUPT = {"ev": "interrupt"}


class ElectronTransferUI:
    """Drives the streaming ``transfer`` dialog; implements transfer.TransferUI."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._events: queue.Queue[dict] = queue.Queue()
        self._proc = start_stream_dialog("transfer", {"title": "Transfer Accounts"})
        threading.Thread(target=self._read_events, daemon=True).start()

    # -- plumbing --

    def _read_events(self) -> None:
        for line in self._proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                # Electron/Chromium may print diagnostics on stdout; only our
                # JSON lines are events.
                log.debug("Ignoring non-event output from transfer dialog")
                continue
            if ev.get("ev") == "cancel":
                self.cancelled.set()
            self._events.put(ev)
        # Window closed (by the user or a crash): stop the session.
        self.cancelled.set()
        self._events.put(_CLOSED)

    def _update(self, state: dict) -> None:
        if self._proc.poll() is not None:
            self.cancelled.set()
            return
        try:
            self._proc.stdin.write(json.dumps(state) + "\n")
            self._proc.stdin.flush()
        except BrokenPipeError:
            self.cancelled.set()

    def _wait(self, *kinds: str, interruptible: bool = False) -> dict | None:
        """Block for the next event of one of *kinds*; None if cancelled/closed.

        Only interruptible waits (code entry) end on interrupt(); a stale
        interrupt must not dismiss a later screen such as the result.
        """
        while True:
            ev = self._events.get()
            if ev is _INTERRUPT:
                if interruptible:
                    return None
                continue
            if ev.get("ev") in ("cancel", "closed"):
                # Leave the marker for any later wait.
                self._events.put(ev)
                return None
            if ev.get("ev") in kinds:
                return ev

    def close(self) -> None:
        if self._proc.poll() is None:
            self._update({"state": "close"})
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGTERM)
                self._proc.wait(timeout=3)

    # -- TransferUI --

    def choose_role(self) -> str | None:
        self._update({"state": "choose"})
        ev = self._wait("role")
        return ev.get("value") if ev else None

    def status(self, text: str) -> None:
        self._update({"state": "status", "text": text})

    def show_code(self, title: str, code: str, text: str) -> None:
        self._update({"state": "code", "title": title, "code": code, "text": text})

    def ask_code(self, title: str, text: str) -> str | None:
        self._update({"state": "ask", "title": title, "text": text})
        ev = self._wait("code", interruptible=True)
        return ev.get("value") if ev else None

    def pick_offer(self, offers: list[Offer]) -> Offer | str | None:
        self._update({
            "state": "pick",
            "offers": [{"host": o.host, "address": o.address} for o in offers],
        })
        ev = self._wait("pick", "refresh")
        if ev is None:
            return None
        if ev["ev"] == "refresh":
            return "refresh"
        idx = ev.get("index")
        if isinstance(idx, int) and 0 <= idx < len(offers):
            return offers[idx]
        return "refresh"

    def interrupt(self) -> None:
        self._events.put(_INTERRUPT)

    def finish(self, ok: bool, text: str) -> None:
        self._update({"state": "finish", "ok": ok, "text": text})
        self._wait("close")


def run_transfer(mgr: CredentialManager, role: str | None = None) -> int:
    """Run a send or receive session in the transfer dialog. Returns exit code."""
    ui = ElectronTransferUI()
    try:
        if role is None:
            role = ui.choose_role()
            if role is None:
                return 1
        text = _send(mgr, ui) if role == "send" else _receive(mgr, ui)
        ui.finish(True, text)
        return 0
    except TransferAborted as exc:
        ui.finish(False, f"Transfer stopped. {exc}")
        return 1
    except TransferError as exc:
        ui.finish(False, f"Transfer failed: {exc}")
        return 1
    except Exception as exc:
        # Top of the transfer flow: show the failure in the window the user
        # is looking at instead of letting the dialog vanish.
        log.exception("Transfer failed")
        ui.finish(False, f"Transfer failed: {type(exc).__name__}: {exc}")
        return 1
    finally:
        ui.close()


def _send(mgr: CredentialManager, ui: ElectronTransferUI) -> str:
    stored = mgr.list_accounts()
    if mgr.last_errors:
        raise TransferError("Could not read credentials: " + "; ".join(mgr.last_errors))

    accounts: list[dict] = []
    missing: list[str] = []
    for acct in stored:
        password = mgr.get_password(acct.platform, acct.username)
        if password is None:
            missing.append(f"{acct.username} ({acct.platform})")
            continue
        accounts.append({
            "platform": acct.platform,
            "username": acct.username,
            "label": acct.label,
            "password": password,
        })
    if not accounts:
        raise TransferError("This machine has no accounts with stored passwords to send.")

    host, imported = send_accounts(accounts, ui)
    text = f"Sent {len(accounts)} account(s) to {host}; it saved {imported}."
    if missing:
        text += "\nNot sent (no password stored here): " + ", ".join(missing)
    return text


def _receive(mgr: CredentialManager, ui: ElectronTransferUI) -> str:
    known = set(list_platforms())
    skipped: list[str] = []

    def import_accounts(accounts: list[dict]) -> int:
        count = 0
        for a in accounts:
            if a["platform"] not in known:
                skipped.append(f"{a['username']} ({a['platform']})")
                continue
            mgr.store_account(a["platform"], a["username"], a["password"], a["label"])
            count += 1
        return count

    host, imported = receive_accounts(ui, import_accounts)
    text = f"Saved {imported} account(s) from {host}."
    if skipped:
        text += "\nSkipped (platform not supported here): " + ", ".join(skipped)
    return text
