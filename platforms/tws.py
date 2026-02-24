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
            "login_ready_delay": 8,
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
        """TWS login screen has no [username] in the title.

        Once logged in, the title contains something like [papertrading123].
        """
        if not window_title:
            return True
        # If title contains brackets with a username, we're past login
        if "[" in window_title and "]" in window_title:
            return False
        return True
