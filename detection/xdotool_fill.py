"""Keystroke-based form filling via xdotool."""

from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger(__name__)


class XdotoolInput:
    """Fill login forms by simulating keystrokes with xdotool.

    Sends keystrokes to the currently active window — no window ID targeting.
    When fill_login_form() is given expected_wid, every keystroke is
    preceded by a check that that window is still active.
    """

    def __init__(self, typing_delay_ms: int = 12) -> None:
        self._typing_delay = typing_delay_ms
        self._guard_wid: int | None = None

    def fill_login_form(
        self,
        username: str,
        password: str,
        field_order: list[str] | None = None,
        auto_submit: bool = False,
        username_prefilled: bool = False,
        expected_wid: int | None = None,
    ) -> bool:
        """Tab-navigate and type into login form fields.

        If username_prefilled is True, the username field is skipped and
        only the password is filled (cursor assumed to be on password).

        If expected_wid is given, verifies focus hasn't shifted to another
        window before each field fill and before auto-submit. Returns False
        (abort) if focus doesn't match.

        Returns True if keystrokes were sent successfully.
        """
        if field_order is None:
            field_order = ["username", "password"]

        values = {"username": username, "password": password}
        self._guard_wid = expected_wid
        try:
            return self._fill(values, field_order, auto_submit, username_prefilled, expected_wid)
        finally:
            self._guard_wid = None

    def _fill(self, values, field_order, auto_submit, username_prefilled, expected_wid) -> bool:
        for i, field_name in enumerate(field_order):
            value = values.get(field_name, "")
            if not value:
                continue

            # Skip username if already pre-filled
            if field_name == "username" and username_prefilled:
                log.info("Skipping pre-filled username field.")
                continue

            if expected_wid is not None and not self._verify_active_window(expected_wid):
                log.warning("Focus lost before filling %r — aborting.", field_name)
                return False

            if field_name == "password" and username_prefilled:
                # Cursor is already on the password field — no navigation needed
                pass
            elif i == 0:
                # Ensure we're on the first field
                if not self._send_key("Tab"):
                    return False
                time.sleep(0.05)
                if not self._send_key("shift+Tab"):
                    return False
                time.sleep(0.05)
            else:
                if not self._send_key("Tab"):
                    return False
                time.sleep(0.1)

            # Clear existing content and type
            if not self._send_key("ctrl+a"):
                return False
            time.sleep(0.02)
            if not self._send_key("Delete"):
                return False
            time.sleep(0.02)
            if not self._type_text(value):
                log.warning("Failed to type into field %r", field_name)
                return False
            time.sleep(0.05)

        if auto_submit:
            if expected_wid is not None and not self._verify_active_window(expected_wid):
                log.warning("Focus lost before auto-submit — aborting.")
                return False
            time.sleep(0.2)
            if not self._send_key("Return"):
                return False

        return True

    def _verify_active_window(self, expected_wid: int) -> bool:
        """Check that the currently active window matches expected_wid."""
        try:
            result = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return False
            active_wid = int(result.stdout.strip())
            return active_wid == expected_wid
        except (subprocess.SubprocessError, FileNotFoundError, ValueError):
            return False

    def _send_key(self, key: str) -> bool:
        if not self._guard_ok(key):
            return False
        return self._run_xdotool(["key", key])

    def _guard_ok(self, what: str) -> bool:
        """Refuse to send input if the guarded window is no longer active."""
        if self._guard_wid is None or self._verify_active_window(self._guard_wid):
            return True
        log.warning("Focus left window %d before %r — not sending.", self._guard_wid, what)
        return False

    def _type_text(self, text: str) -> bool:
        """Type *text* into the currently focused field.

        Prefers clipboard paste via xclip + Ctrl+V (instant).
        Falls back to xdotool type if clipboard tools are unavailable.
        """
        # Clipboard paste — fast for any text
        if self._clipboard_paste(text):
            return self._send_key("ctrl+v")

        # Fallback: per-character xdotool type. The guard runs once before
        # the whole string; xdotool types it in a single invocation.
        if not self._guard_ok("type"):
            return False
        cmd = ["xdotool", "type", "--delay", str(self._typing_delay), "--clearmodifiers", "--file", "-"]
        try:
            result = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=10)
            if result.returncode != 0:
                log.debug("xdotool type failed: %s", result.stderr.decode(errors="replace"))
                return False
            return True
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            log.warning("xdotool type error: %s", e)
            return False

    @staticmethod
    def _clipboard_paste(value: str) -> bool:
        """Copy *value* to the X11 clipboard using xclip or xsel."""
        for cmd in (["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"]):
            try:
                subprocess.run(
                    cmd, input=value.encode("utf-8"),
                    check=True, timeout=5, capture_output=True,
                )
                return True
            except (FileNotFoundError, subprocess.SubprocessError):
                continue
        log.debug("No clipboard tool available (tried xclip, xsel)")
        return False

    def _run_xdotool(self, args: list[str]) -> bool:
        cmd = ["xdotool"] + args
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode != 0:
                log.debug("xdotool failed: %s", result.stderr.decode(errors="replace"))
                return False
            return True
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            log.warning("xdotool error: %s", e)
            return False
