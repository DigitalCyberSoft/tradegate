"""TWS (Interactive Brokers Trader Workstation) platform plugin."""

from __future__ import annotations

from typing import Any

from tradegate.platforms.base import PlatformPlugin, PlatformConfig


class TWSPlugin(PlatformPlugin):
    """Interactive Brokers TWS platform."""

    name = "tws"

    def get_default_config(self) -> dict[str, Any]:
        return {
            "binary": "~/Jts/tws",
            "wm_class": "install4j-jclient-Launcher",
            "title_pattern": "Login",
            "window_timeout": 120,
            "login_ready_delay": 1,
            "field_order": ["username", "password"],
            "input_strategy": "",
        }

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
