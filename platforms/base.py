"""Abstract platform plugin and associated dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldLocator:
    """Describes how to locate a form field for AT-SPI."""

    role: str = ""  # AT-SPI role name (e.g. "ENTRY", "PASSWORD_TEXT")
    name: str = ""  # AT-SPI accessible name
    index: int = 0  # positional index among matching fields


@dataclass
class PlatformConfig:
    """Runtime config for a platform, merged from defaults + user config."""

    name: str = ""
    binary: str = ""
    wm_class: str = ""
    title_pattern: str = ""
    window_timeout: int = 120
    login_ready_delay: float = 8
    field_order: list[str] = field(default_factory=lambda: ["username", "password"])
    input_strategy: str = "auto"
    focus_shield: str = "off"  # "off", "log", or "enforce" (Linux/X11 only)
    restore_focus: bool = False  # revert WM-side focus steals to the user's window
    restore_focus_scope: str = "session"  # "session" (until platform exits) or "launch"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> PlatformConfig:
        return cls(
            name=name,
            binary=d.get("binary", ""),
            wm_class=d.get("wm_class", ""),
            title_pattern=d.get("title_pattern", ""),
            window_timeout=d.get("window_timeout", 120),
            login_ready_delay=d.get("login_ready_delay", 8),
            field_order=d.get("field_order", ["username", "password"]),
            input_strategy=d.get("input_strategy", "auto"),
            focus_shield=d.get("focus_shield", "off"),
            restore_focus=d.get("restore_focus", False),
            restore_focus_scope=d.get("restore_focus_scope", "session"),
        )


class PlatformPlugin(ABC):
    """ABC for trading platform plugins."""

    name: str = ""
    # Capability flags surfaced in the settings UI. A platform that doesn't
    # implement the corresponding behavior keeps these False so the UI can
    # disable the controls instead of offering settings that do nothing.
    supports_focus_shield: bool = False
    supports_restore_focus: bool = False

    @abstractmethod
    def get_default_config(self) -> dict[str, Any]:
        """Return default config dict for this platform."""

    def get_launch_command(self, config: PlatformConfig) -> list[str]:
        """Return the command to launch this platform."""
        binary = config.binary
        if binary.startswith("~"):
            from pathlib import Path
            binary = str(Path(binary).expanduser())
        return [binary]

    def get_launch_env(self, config: PlatformConfig) -> dict[str, str] | None:
        """Return extra environment variables, or None for inherit."""
        return None

    def is_login_screen(self, window_title: str) -> bool:
        """Determine if the current window title indicates a login screen."""
        return True

    def is_main_window(self, window_title: str) -> bool:
        """Determine if the window title is the platform's main (post-login) window.

        Used by restore_focus to know when login completed. Platforms that
        don't implement this never trigger focus restoration.
        """
        return False
