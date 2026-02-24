"""Window detection via xdotool polling."""

from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger(__name__)


class WindowDetector:
    """Detect and interact with X11 windows using xdotool."""

    def __init__(self, poll_interval: float = 1.0) -> None:
        self._poll_interval = poll_interval

    def wait_for_window(
        self,
        *,
        wm_class: str = "",
        title: str = "",
        pid: int | None = None,
        timeout: int = 120,
        exclude: set[int] | None = None,
    ) -> int | None:
        """Poll for a window matching the criteria. Returns window ID or None on timeout.

        If *exclude* is given, any window ID in that set is skipped.
        """
        deadline = time.monotonic() + timeout
        exclude = exclude or set()
        log.info(
            "Waiting for window (class=%r, title=%r, pid=%s, timeout=%ds, exclude=%d existing)",
            wm_class, title, pid, timeout, len(exclude),
        )

        while time.monotonic() < deadline:
            for wid in self._search_all(wm_class=wm_class, title=title, pid=pid):
                if wid not in exclude:
                    log.info("Found window %d", wid)
                    return wid
            time.sleep(self._poll_interval)

        log.warning("Window not found within %ds", timeout)
        return None

    def get_window_title(self, wid: int) -> str:
        """Get the title of a window by ID."""
        try:
            result = subprocess.run(
                ["xdotool", "getwindowname", str(wid)],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return ""

    def activate_window(self, wid: int) -> bool:
        """Bring window to foreground and give it focus."""
        try:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", str(wid)],
                capture_output=True, timeout=5,
            )
            return True
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def focus_window(self, wid: int) -> bool:
        """Focus a window."""
        try:
            subprocess.run(
                ["xdotool", "windowfocus", "--sync", str(wid)],
                capture_output=True, timeout=5,
            )
            return True
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def find_windows(self, *, wm_class: str = "", title: str = "") -> set[int]:
        """Return the set of all window IDs matching the criteria."""
        return set(self._search_all(wm_class=wm_class, title=title))

    def _search_all(
        self, *, wm_class: str = "", title: str = "", pid: int | None = None
    ) -> list[int]:
        """Run xdotool search and return all matching window IDs.

        xdotool can fail to combine --class and --name for some apps (e.g. Java),
        so we search by class first, then filter by title ourselves.
        """
        cmd = ["xdotool", "search"]
        if wm_class:
            cmd += ["--class", wm_class]
        elif title:
            cmd += ["--name", title]
        if pid is not None:
            cmd += ["--pid", str(pid)]

        if len(cmd) == 2:
            return []

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []
            wids = [int(line) for line in result.stdout.strip().splitlines()]
        except (subprocess.SubprocessError, FileNotFoundError, ValueError):
            return []

        # If both class and title were requested, filter by title manually
        if wm_class and title:
            filtered = []
            for wid in wids:
                wname = self.get_window_title(wid)
                if title.lower() in wname.lower():
                    filtered.append(wid)
            return filtered

        return wids
