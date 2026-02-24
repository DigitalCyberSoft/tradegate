"""Bridge to Electron UI dialogs."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)

_ELECTRON_DIR = os.path.join(os.path.dirname(__file__), "electron")
_MAIN_JS = os.path.join(_ELECTRON_DIR, "main.js")


def _find_electron() -> str | None:
    """Locate the electron binary: local node_modules first, then system."""
    local = os.path.join(_ELECTRON_DIR, "node_modules", ".bin", "electron")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return shutil.which("electron")


def run_electron_dialog(dialog_type: str, data: dict, timeout: int = 120) -> dict | None:
    """Launch an Electron dialog and return the result dict, or None on cancel/error.

    Args:
        dialog_type: Name of the dialog (matches HTML filename without extension).
        data: Dictionary to pass to the dialog as init data.
        timeout: Maximum seconds to wait for the dialog process.

    Returns:
        Parsed JSON dict from stdout, or None if cancelled/error.
    """
    electron = _find_electron()
    if electron is None:
        raise RuntimeError(
            "Electron binary not found. "
            "Run 'npm install' in tradegate/ui/electron or install electron globally."
        )

    cmd = [electron, _MAIN_JS, "--dialog", dialog_type]
    log.debug("Running electron dialog: type=%s", dialog_type)

    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(data),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("Electron dialog timed out after %ds", timeout)
        return None

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            log.debug("Electron stderr: %s", stderr)
        # Non-zero exit typically means user cancelled
        return None

    stdout = result.stdout.strip()
    if not stdout:
        return None

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("Failed to parse Electron output (%d bytes)", len(stdout))
        return None
