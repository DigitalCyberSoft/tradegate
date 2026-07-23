"""TWS (Interactive Brokers Trader Workstation) platform plugin."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from tradegate.platforms.base import PlatformPlugin, PlatformConfig

log = logging.getLogger(__name__)


def _find_focus_shield() -> str:
    """Locate libtwsquiet.so: package/repo native dir, then user data dir."""
    candidates = [
        Path(__file__).resolve().parent.parent / "native" / "libtwsquiet.so",
        Path.home() / ".local" / "share" / "tradegate" / "libtwsquiet.so",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return ""


class TWSPlugin(PlatformPlugin):
    """Interactive Brokers TWS platform."""

    name = "tws"
    supports_focus_shield = True
    supports_restore_focus = True

    def get_default_config(self) -> dict[str, Any]:
        return {
            "binary": "~/Jts/tws",
            "wm_class": "install4j-jclient-Launcher",
            "title_pattern": "Login",
            "window_timeout": 120,
            "login_ready_delay": 1,
            "field_order": ["username", "password"],
            "input_strategy": "",
            "focus_shield": "enforce",
            "restore_focus": False,
            "restore_focus_scope": "session",
        }

    def get_launch_env(self, config: PlatformConfig) -> dict[str, str] | None:
        """Inject the twsquiet LD_PRELOAD shim that stops TWS from stealing focus.

        Modes: "log" observes TWS's raise/focus calls without blocking,
        "enforce" blocks them, anything else disables injection. Missing
        shim library disables injection with a warning (TWS still launches).
        """
        if sys.platform != "linux":
            return None
        mode = config.focus_shield
        if mode not in ("log", "enforce"):
            return None
        shim = _find_focus_shield()
        if not shim:
            log.warning(
                "focus_shield=%r requested but libtwsquiet.so not found; "
                "launching without it. Build it with native/build.sh", mode,
            )
            return None
        shield_log = Path.home() / ".local" / "state" / "tradegate" / "twsquiet.log"
        shield_log.parent.mkdir(parents=True, exist_ok=True)
        existing = os.environ.get("LD_PRELOAD", "")
        env = {
            "LD_PRELOAD": f"{shim}:{existing}" if existing else shim,
            "TWSQUIET_MODE": mode,
            "TWSQUIET_LOG": str(shield_log),
        }
        log.info("Focus shield active (mode=%s, lib=%s, log=%s)", mode, shim, shield_log)
        return env

    def get_launch_command(self, config: PlatformConfig) -> list[str]:
        binary = config.binary
        if binary.startswith("~"):
            from pathlib import Path
            binary = str(Path(binary).expanduser())
        return [binary]

    def is_login_screen(self, window_title: str) -> bool:
        """TWS login window is titled exactly "Login".

        Rejects post-login windows like "Login Messages", "Announcements",
        and titles containing [username] (e.g. "[papertrading123]").
        """
        if not window_title:
            return False
        return window_title.strip().lower() == "login"

    def is_main_window(self, window_title: str) -> bool:
        """The main TWS window title ends with "Interactive Brokers"
        (e.g. "P Margin Interactive Brokers"). Post-login popups
        ("Login Messages", "Announcements", "Loading...") don't match.
        """
        return "interactive brokers" in window_title.lower()
