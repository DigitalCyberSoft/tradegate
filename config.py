"""TOML configuration loading and saving for tradegate."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import tomli_w


def _get_config_dir() -> Path:
    """Return the platform-appropriate config directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tradegate"
    elif sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "tradegate"
    else:
        return Path.home() / ".config" / "tradegate"


CONFIG_DIR = _get_config_dir()
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "credential_backend": "keyring",  # "keyring", "encrypted_file", or "both"
        "encrypted_file_path": str(CONFIG_DIR / "credentials.enc"),
        "input_strategy": "auto",  # "auto" (pyautogui), "atspi", "xdotool", "pyautogui"
        "auto_submit": False,
    },
    "platforms": {
        "tws": {
            "binary": "~/Jts/tws",
            "wm_class": "install4j-jclient-Launcher",
            "title_pattern": "",
            "window_timeout": 120,
            "login_ready_delay": 1,
            "field_order": ["username", "password"],
            "input_strategy": "",  # empty = use general setting
            "focus_shield": "enforce",  # "off", "log", or "enforce" — see README
            "restore_focus": False,  # optional watchdog fallback — see README
            "restore_focus_scope": "session",  # "session" or "launch"
        },
        "tastytrade": {
            "binary": "/opt/tastytrade/bin/tastytrade",
            "wm_class": "tastytrade",
            "title_pattern": "tastytrade.+login",
            "window_timeout": 90,
            "login_ready_delay": 2,
            "field_order": ["username", "password"],
            "input_strategy": "",
        },
        "thinkorswim": {
            "binary": "",
            "wm_class": "thinkorswim-Launcher",
            "title_pattern": "thinkorswim",
            "window_timeout": 120,
            "login_ready_delay": 8,
            "field_order": ["username", "password"],
            "input_strategy": "",
        },
    },
}


def load_config() -> dict[str, Any]:
    """Load config from disk, creating default if missing."""
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    with open(CONFIG_PATH, "rb") as f:
        user_cfg = tomllib.load(f)

    # Merge defaults for any missing keys
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg)
    return merged


def save_config(cfg: dict[str, Any]) -> None:
    """Write config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(CONFIG_DIR, 0o700)
        old_umask = os.umask(0o177)
    try:
        with open(CONFIG_PATH, "wb") as f:
            tomli_w.dump(cfg, f)
    finally:
        if os.name != "nt":
            os.umask(old_umask)
    if os.name != "nt":
        os.chmod(CONFIG_PATH, 0o600)


def get_platform_config(cfg: dict[str, Any], platform: str) -> dict[str, Any]:
    """Get merged platform config (platform-specific overrides general)."""
    general = cfg.get("general", {})
    plat = cfg.get("platforms", {}).get(platform, {})

    # Platform input_strategy falls back to general
    if not plat.get("input_strategy"):
        plat["input_strategy"] = general.get("input_strategy", "auto")

    return plat


def _deep_merge(defaults: dict, overrides: dict) -> dict:
    """Recursively merge overrides into defaults."""
    result = defaults.copy()
    for key, val in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result
