"""Thinkorswim (Charles Schwab) platform plugin."""

from __future__ import annotations

import subprocess
from typing import Any

from tradegate.platforms.base import PlatformPlugin

# The login form is a JxBrowser Chromium window embedded (reparented) into
# the Java "Logon to thinkorswim" frame. Its X instance name carries the
# user-data-dir, e.g. "chromium-browser (~/.thinkorswim/login-browser-data-v30-0)".
_LOGIN_BROWSER_CLASSNAME = "thinkorswim/login-browser-data"

# Title of the embedded browser window on each login page.
_PAGE_TITLES = {
    "charles schwab log in": 1,  # "Enter Login ID" + Continue
    "password": 2,  # "Enter Password" + Log In
}


class ThinkorswimPlugin(PlatformPlugin):
    """Thinkorswim desktop application."""

    name = "thinkorswim"
    login_pages = 2

    def get_default_config(self) -> dict[str, Any]:
        return {
            "binary": "~/thinkorswim/thinkorswim",
            "wm_class": "java-lang-Thread",
            "title_pattern": "Logon to thinkorswim",
            "window_timeout": 120,
            "update_timeout": 1800,
            "login_ready_delay": 1,
            "field_order": ["username", "password"],
            "input_strategy": "",
        }

    def is_login_screen(self, window_title: str) -> bool:
        """The login frame is titled "Logon to thinkorswim".

        The updater ("thinkorswim updater") shares the java-lang-Thread class.
        """
        if not window_title:
            return False
        return window_title.strip().lower() == "logon to thinkorswim"

    def is_updating(self, detector) -> bool:
        """True while the "thinkorswim updater" window is mapped.

        The updater window stays around withdrawn after it finishes, so
        existence alone is not enough; check it is viewable.
        """
        for wid in detector.find_windows(wm_class="java-lang-Thread", title="thinkorswim updater"):
            try:
                result = subprocess.run(
                    ["xwininfo", "-id", str(wid)],
                    capture_output=True, text=True, timeout=5,
                )
            except (subprocess.SubprocessError, FileNotFoundError):
                return False
            if "Map State: IsViewable" in result.stdout:
                return True
        return False

    def get_login_page(self, detector) -> int | None:
        """Read the page from the embedded login browser's window title."""
        try:
            result = subprocess.run(
                ["xdotool", "search", "--classname", _LOGIN_BROWSER_CLASSNAME],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
        if result.returncode != 0:
            return None
        for line in result.stdout.split():
            page = _PAGE_TITLES.get(detector.get_window_title(int(line)).strip().lower())
            if page is not None:
                return page
        return None
