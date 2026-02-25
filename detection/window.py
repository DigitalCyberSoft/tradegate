"""Cross-platform window detection.

Provides a common interface with platform-specific backends:
- Linux: xdotool (X11)
- macOS: osascript (AppleScript)
- Windows: pywinauto
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

log = logging.getLogger(__name__)


class WindowDetector:
    """Detect and interact with desktop windows.

    The polling logic (wait_for_window) is shared; platform-specific
    operations are delegated to methods that subclasses override.
    """

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
        raise NotImplementedError

    def activate_window(self, wid: int) -> bool:
        """Bring window to foreground and give it focus."""
        raise NotImplementedError

    def focus_window(self, wid: int) -> bool:
        """Focus a window."""
        raise NotImplementedError

    def find_windows(self, *, wm_class: str = "", title: str = "") -> set[int]:
        """Return the set of all window IDs matching the criteria."""
        return set(self._search_all(wm_class=wm_class, title=title))

    def find_window_by_title(self, title: str, exclude: set[int] | None = None, wm_class: str = "") -> int | None:
        """Find a window whose title contains *title*, skipping IDs in *exclude*.

        If *wm_class* is given, search by class first (more reliable for Java
        apps) and then verify the title matches.
        """
        exclude = exclude or set()
        if wm_class:
            # Search by class, filter by title
            for wid in self._search_all(wm_class=wm_class, title=title):
                if wid not in exclude:
                    return wid
        for wid in self._search_all(title=title):
            if wid in exclude:
                continue
            wname = self.get_window_title(wid)
            if title.lower() in wname.lower():
                return wid
        return None

    def get_active_window_title(self) -> str:
        """Return the title of the currently active window."""
        return ""

    def _search_all(
        self, *, wm_class: str = "", title: str = "", pid: int | None = None
    ) -> list[int]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Linux backend (xdotool / X11)
# ---------------------------------------------------------------------------

class XdotoolWindowDetector(WindowDetector):
    """Detect and interact with X11 windows using xdotool."""

    def get_window_title(self, wid: int) -> str:
        try:
            result = subprocess.run(
                ["xdotool", "getwindowname", str(wid)],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return ""

    def activate_window(self, wid: int) -> bool:
        try:
            subprocess.run(
                ["xdotool", "windowactivate", str(wid)],
                capture_output=True, timeout=5,
            )
            return True
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def focus_window(self, wid: int) -> bool:
        try:
            result = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", str(wid)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return False
            vals = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k] = int(v)
            cx = vals["X"] + vals["WIDTH"] // 2
            cy = vals["Y"] + vals["HEIGHT"] // 2
            import pyautogui
            pyautogui.click(cx, cy)
            return True
        except Exception:
            return False

    def get_active_window_title(self) -> str:
        try:
            result = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return ""

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


# ---------------------------------------------------------------------------
# macOS backend (osascript / AppleScript)
# ---------------------------------------------------------------------------

class MacOSWindowDetector(WindowDetector):
    """Detect and interact with macOS windows using AppleScript via osascript.

    Window IDs on macOS are process-level indices; we use a composite
    ``(pid << 16) | window_index`` integer so the ID fits the same int API.
    """

    def _run_osascript(self, script: str) -> str:
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return ""

    @staticmethod
    def _encode_id(pid: int, index: int) -> int:
        return (pid << 16) | (index & 0xFFFF)

    @staticmethod
    def _decode_id(wid: int) -> tuple[int, int]:
        return (wid >> 16, wid & 0xFFFF)

    def get_window_title(self, wid: int) -> str:
        pid, idx = self._decode_id(wid)
        script = (
            f'tell application "System Events"\n'
            f'  set proc to first process whose unix id is {pid}\n'
            f'  return name of window {idx + 1} of proc\n'
            f'end tell'
        )
        return self._run_osascript(script)

    def activate_window(self, wid: int) -> bool:
        pid, idx = self._decode_id(wid)
        script = (
            f'tell application "System Events"\n'
            f'  set proc to first process whose unix id is {pid}\n'
            f'  set frontmost of proc to true\n'
            f'  perform action "AXRaise" of window {idx + 1} of proc\n'
            f'end tell'
        )
        return bool(self._run_osascript(script)) or True

    def focus_window(self, wid: int) -> bool:
        return self.activate_window(wid)

    def get_active_window_title(self) -> str:
        script = (
            'tell application "System Events"\n'
            '  set fp to first process whose frontmost is true\n'
            '  return name of front window of fp\n'
            'end tell'
        )
        return self._run_osascript(script)

    def _search_all(
        self, *, wm_class: str = "", title: str = "", pid: int | None = None
    ) -> list[int]:
        # Build AppleScript to enumerate windows from all (or one) process
        if pid is not None:
            proc_filter = f'whose unix id is {pid}'
        else:
            proc_filter = 'whose background only is false'

        script = (
            f'set output to ""\n'
            f'tell application "System Events"\n'
            f'  repeat with proc in (every process {proc_filter})\n'
            f'    set pid to unix id of proc\n'
            f'    set pname to name of proc\n'
            f'    set idx to 0\n'
            f'    repeat with w in (every window of proc)\n'
            f'      set wname to name of w\n'
            f'      set output to output & pid & "\\t" & idx & "\\t" & pname & "\\t" & wname & "\\n"\n'
            f'      set idx to idx + 1\n'
            f'    end repeat\n'
            f'  end repeat\n'
            f'end tell\n'
            f'return output'
        )

        raw = self._run_osascript(script)
        if not raw:
            return []

        results: list[int] = []
        for line in raw.splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            try:
                w_pid = int(parts[0])
                w_idx = int(parts[1])
            except ValueError:
                continue
            w_pname = parts[2]
            w_title = parts[3]

            # Match by wm_class (process name) and/or title
            if wm_class and wm_class.lower() not in w_pname.lower():
                continue
            if title and title.lower() not in w_title.lower():
                continue

            results.append(self._encode_id(w_pid, w_idx))

        return results


# ---------------------------------------------------------------------------
# Windows backend (pywinauto)
# ---------------------------------------------------------------------------

class Win32WindowDetector(WindowDetector):
    """Detect and interact with Windows windows using pywinauto/win32gui."""

    def _find_all_windows(self) -> list[dict]:
        """Return list of dicts with handle, title, class_name, pid."""
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32

        windows: list[dict] = []

        @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)

            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            windows.append({
                "handle": hwnd,
                "title": title,
                "class_name": cls_buf.value,
                "pid": pid.value,
            })
            return True

        user32.EnumWindows(callback, 0)
        return windows

    def get_window_title(self, wid: int) -> str:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            length = user32.GetWindowTextLengthW(wid)
            if length == 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(wid, buf, length + 1)
            return buf.value
        except Exception:
            return ""

    def activate_window(self, wid: int) -> bool:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetForegroundWindow(wid)
            return True
        except Exception:
            return False

    def focus_window(self, wid: int) -> bool:
        return self.activate_window(wid)

    def get_active_window_title(self) -> str:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value
        except Exception:
            return ""

    def _search_all(
        self, *, wm_class: str = "", title: str = "", pid: int | None = None
    ) -> list[int]:
        results: list[int] = []
        for w in self._find_all_windows():
            if pid is not None and w["pid"] != pid:
                continue
            if wm_class and wm_class.lower() not in w["class_name"].lower():
                continue
            if title and title.lower() not in w["title"].lower():
                continue
            results.append(w["handle"])
        return results


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_window_detector(poll_interval: float = 0.2) -> WindowDetector:
    """Return a WindowDetector appropriate for the current platform."""
    if sys.platform == "darwin":
        return MacOSWindowDetector(poll_interval)
    elif sys.platform == "win32":
        return Win32WindowDetector(poll_interval)
    else:
        return XdotoolWindowDetector(poll_interval)
