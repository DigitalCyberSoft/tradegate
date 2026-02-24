"""Thinkorswim (Charles Schwab) platform plugin."""

from __future__ import annotations

from typing import Any

from tradegate.platforms.base import PlatformPlugin


class ThinkorswimPlugin(PlatformPlugin):
    """Thinkorswim desktop application."""

    name = "thinkorswim"

    def get_default_config(self) -> dict[str, Any]:
        return {
            "binary": "",
            "wm_class": "thinkorswim-Launcher",
            "title_pattern": "thinkorswim",
            "window_timeout": 120,
            "login_ready_delay": 8,
            "field_order": ["username", "password"],
            "input_strategy": "",
        }
