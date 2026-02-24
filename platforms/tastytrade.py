"""TastyTrade platform plugin."""

from __future__ import annotations

from typing import Any

from tradegate.platforms.base import PlatformPlugin


class TastyTradePlugin(PlatformPlugin):
    """TastyTrade desktop application."""

    name = "tastytrade"

    def get_default_config(self) -> dict[str, Any]:
        return {
            "binary": "/opt/tastytrade/bin/tastytrade",
            "wm_class": "tastytrade",
            "title_pattern": "login",
            "window_timeout": 90,
            "login_ready_delay": 5,
            "field_order": ["username", "password"],
            "input_strategy": "",
        }
