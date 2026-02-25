"""Keystroke-based form filling via pyautogui (cross-platform)."""

from __future__ import annotations

import logging
import subprocess
import sys
import time

log = logging.getLogger(__name__)

_MACOS = sys.platform == "darwin"
_MOD_KEY = "command" if _MACOS else "ctrl"


def _clipboard_paste(value: str) -> bool:
    """Copy *value* to the system clipboard using platform-native tools.

    Returns True if the copy succeeded (the caller still needs to send
    the paste hotkey).
    """
    try:
        if _MACOS:
            subprocess.run(
                ["pbcopy"], input=value.encode("utf-8"),
                check=True, timeout=5,
            )
        elif sys.platform == "win32":
            # PowerShell's Set-Clipboard works in all modern Windows
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Set-Clipboard -Value $input"],
                input=value.encode("utf-16-le"),
                check=True, timeout=5,
            )
        else:
            # Linux — prefer xclip, fall back to xsel
            for cmd in (["xclip", "-selection", "clipboard"],
                        ["xsel", "--clipboard", "--input"]):
                try:
                    subprocess.run(
                        cmd, input=value.encode("utf-8"),
                        check=True, timeout=5,
                    )
                    return True
                except FileNotFoundError:
                    continue
            return False
        return True
    except Exception as e:
        log.debug("clipboard copy failed: %s", e)
        return False


class PyAutoGUIInput:
    """Fill login forms by simulating keystrokes with pyautogui.

    Works on Linux (X11), macOS, and Windows.
    """

    def __init__(self, typing_interval: float = 0.012) -> None:
        self._typing_interval = typing_interval
        self._pag = None

    def _get_pag(self):
        if self._pag is None:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.01
            self._pag = pyautogui
        return self._pag

    @staticmethod
    def is_available() -> bool:
        try:
            import pyautogui  # noqa: F401 — raises on headless Linux
            return True
        except Exception:
            return False

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

        The expected_wid parameter is accepted for interface compatibility
        with XdotoolInput but is not used for focus verification
        (pyautogui targets the active window).

        Returns True if keystrokes were sent successfully.
        """
        if field_order is None:
            field_order = ["username", "password"]

        pag = self._get_pag()
        values = {"username": username, "password": password}

        for i, field_name in enumerate(field_order):
            value = values.get(field_name, "")
            if not value:
                continue

            if field_name == "username" and username_prefilled:
                log.info("Skipping pre-filled username field.")
                continue

            if field_name == "password" and username_prefilled:
                # Cursor is already on the password field
                pass
            elif i == 0:
                # Navigate to the first field
                pag.press("tab")
                time.sleep(0.05)
                pag.hotkey("shift", "tab")
                time.sleep(0.05)
            else:
                pag.press("tab")
                time.sleep(0.1)

            # Clear existing content and type
            pag.hotkey(_MOD_KEY, "a")
            time.sleep(0.02)
            pag.press("backspace")
            time.sleep(0.02)

            if not self._type_text(value):
                log.warning("Failed to type into field %r", field_name)
                return False
            time.sleep(0.05)

        if auto_submit:
            time.sleep(0.2)
            pag.press("return")

        return True

    def _type_text(self, value: str) -> bool:
        """Type *value* into the currently focused field.

        Prefers clipboard paste (instant) over per-character typewrite.
        Falls back to ``typewrite()`` for ASCII text if clipboard paste fails.
        """
        pag = self._get_pag()

        # Clipboard paste — fast for any text, required for non-ASCII
        if _clipboard_paste(value):
            pag.hotkey(_MOD_KEY, "v")
            return True

        # Fallback: per-character typing (ASCII only)
        if value.isascii():
            try:
                pag.typewrite(value, interval=self._typing_interval)
                return True
            except Exception:
                log.debug("typewrite() also failed")

        log.warning("Could not type text: clipboard paste and typewrite both failed")
        return False
