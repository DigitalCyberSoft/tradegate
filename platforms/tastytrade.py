"""TastyTrade platform plugin."""

from __future__ import annotations

import re
from typing import Any

from tradegate.platforms.base import PlatformPlugin


class TastyTradePlugin(PlatformPlugin):
    """TastyTrade desktop application."""

    name = "tastytrade"

    def get_default_config(self) -> dict[str, Any]:
        return {
            "binary": "/opt/tastytrade/bin/tastytrade",
            "wm_class": "tastytrade",
            "title_pattern": "tastytrade.+login",
            "window_timeout": 90,
            "login_ready_delay": 1,
            "field_order": ["username", "password"],
            "input_strategy": "",
        }

    def is_login_screen(self, window_title: str) -> bool:
        """Only match the tastytrade login window, not the main app."""
        if not window_title:
            return False
        return bool(re.search(r"tastytrade.+login", window_title, re.IGNORECASE))
