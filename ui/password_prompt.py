"""Electron-based master password prompt dialog."""

from __future__ import annotations

from tradegate.ui._electron import run_electron_dialog


def prompt_master_password(max_retries: int = 3) -> str | None:
    """Show a password dialog. Returns password string or None if cancelled.

    Allows up to max_retries attempts (for use with encrypted file unlock).
    """
    for attempt in range(max_retries):
        data = {"attempt": attempt, "max_retries": max_retries}
        result = run_electron_dialog("password-prompt", data)

        if result is not None and result.get("password"):
            return result["password"]
        if result is None:
            return None

    return None
