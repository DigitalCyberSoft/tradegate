"""Resident focus watchdog: reverts window-manager-side focus steals.

The LD_PRELOAD focus shield blocks the platform's own focus grabs, but the
WM itself can hand a platform window focus (focus succession when a focused
window closes, activation of demands-attention windows, etc.). Those grants
are indistinguishable from legitimate ones at the X protocol level, so the
only reliable discriminator is physical user input:

- A mouse button press within INTENT_MS of the focus change is intent
  (clicking the window, its taskbar button, a panel item, a notification).
- An Alt/Super/Tab press within INTENT_MS is intent (alt-tab and friends).
- Plain typing is NOT intent: letter keys cannot activate a window through
  the WM, and keystrokes leaking into a freshly self-raised window are
  exactly the failure being prevented.

Input is observed via XInput2 raw events on a dedicated X connection, the
active window via PropertyNotify on the root window — no polling loops.

Every decision is appended to a log file so behavior is diagnosable after
the fact even when tradegate was launched from a .desktop file with no
terminal attached.
"""

from __future__ import annotations

import logging
import os
import select
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _ensure_xinput_usable() -> None:
    """Register and repair python-xlib's XInput2 support.

    python-xlib 0.33 (Fedora's python3-xlib) ships XInput2 but (a) omits it
    from the auto-loaded extension list and (b) its EventMask.pack_value
    calls rq.encode_array, which does not exist in that release — so
    xinput_select_events raises AttributeError for every argument shape.

    Upstream: python-xlib/python-xlib#254 (encode_array referenced but never
    defined). Remove both shims once the installed python-xlib defines
    rq.encode_array and lists XInputExtension in ext.__extensions__.
    """
    import Xlib.ext
    import Xlib.protocol.rq as rq

    if not any(e[0] == "XInputExtension" for e in Xlib.ext.__extensions__):
        Xlib.ext.__extensions__ = tuple(Xlib.ext.__extensions__) + (
            ("XInputExtension", "xinput"),
        )

    if not hasattr(rq, "encode_array"):
        import array

        def encode_array(data):
            return array.array(rq.array_unsigned_codes[4], data).tobytes()

        rq.encode_array = encode_array

INTENT_MS = 1500
LIFECYCLE_CHECK_S = 5.0
STARTUP_GRACE_S = 60.0
MAX_REVERTS_PER_MIN = 4
FIGHT_PAUSE_S = 300.0

WATCH_LOG_PATH = Path.home() / ".local" / "state" / "tradegate" / "focuswatch.log"

_INTENT_KEYSYMS = (
    "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
    "Tab", "ISO_Left_Tab",
)


class FocusWatchdog:
    """Watch _NET_ACTIVE_WINDOW and revert non-user-initiated grabs.

    Parameters are injected so tests can substitute the action layer:
    *activate_fn(wid) -> bool* re-activates a window; *find_platform_windows()
    -> set[int]* enumerates the platform's windows (lifecycle + fallback
    matching); *is_login_title(title) -> bool* marks windows that must never
    be bounced.
    """

    def __init__(
        self,
        wm_class: str,
        activate_fn,
        find_platform_windows,
        is_login_title,
        scope: str = "session",
        launch_timeout: float = 120.0,
        intent_ms: int = INTENT_MS,
        log_path: Path | str = WATCH_LOG_PATH,
        observe: bool = False,
    ) -> None:
        self._wm_class = wm_class
        self._activate = activate_fn
        self._find_platform = find_platform_windows
        self._is_login_title = is_login_title
        self._scope = scope
        self._launch_timeout = launch_timeout
        self._intent_ms = intent_ms
        self._log_path = Path(log_path)
        # Observe mode logs the verdict it *would* act on but never calls
        # activate_fn — for validating detection against a live desktop
        # without moving the user's focus.
        self._observe = observe

        self._last_button_ms = 0.0
        self._last_intent_key_ms = 0.0
        self._last_good: int | None = None
        self._reverts: list[float] = []  # monotonic times of recent reverts
        self._fight_pause_until = 0.0
        self._reverted_this_episode = False
        self._logf = None

    # -- logging ---------------------------------------------------------

    def _wlog(self, msg: str) -> None:
        line = f"[focuswatch pid={os.getpid()} t={time.monotonic():.3f}] {msg}"
        log.info("%s", msg)
        try:
            if self._logf is None:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._logf = open(self._log_path, "a", buffering=1)
            self._logf.write(line + "\n")
        except OSError as e:
            # Boundary: the watchdog must keep protecting focus even if its
            # log file is unwritable; stderr still carries the message.
            log.warning("focuswatch log write failed: %s", e)

    # -- X helpers -------------------------------------------------------

    def _connect(self):
        _ensure_xinput_usable()
        from Xlib import X, display
        from Xlib.ext import xinput

        self._dpy = display.Display()
        self._root = self._dpy.screen().root
        self._net_active = self._dpy.intern_atom("_NET_ACTIVE_WINDOW")

        # Raw input on a second connection would race the main loop; one
        # connection carries both raw input and property events.
        self._root.change_attributes(event_mask=X.PropertyChangeMask)
        try:
            self._dpy.xinput_query_version()
            self._root.xinput_select_events([
                (xinput.AllMasterDevices,
                 xinput.RawButtonPressMask | xinput.RawKeyPressMask),
            ])
            self._dpy.sync()
            self._raw_input_ok = True
        except Exception as e:  # xinput missing/old server: degrade, loudly
            self._raw_input_ok = False
            self._wlog(f"XInput2 unavailable ({e}); intent detection degraded to alt-key polling")

        self._intent_keycodes = set()
        for name in _INTENT_KEYSYMS:
            from Xlib import XK
            keysym = XK.string_to_keysym(name)
            if keysym:
                code = self._dpy.keysym_to_keycode(keysym)
                if code:
                    self._intent_keycodes.add(code)
        self._dpy.flush()

    def _wid_class(self, wid: int) -> str:
        from Xlib.error import XError
        try:
            win = self._dpy.create_resource_object("window", wid)
            hint = win.get_wm_class()
            return hint[1] if hint else ""
        except XError:
            return ""

    def _wid_title(self, wid: int) -> str:
        from Xlib.error import XError
        try:
            win = self._dpy.create_resource_object("window", wid)
            name = win.get_wm_name()
            if isinstance(name, bytes):
                return name.decode("utf-8", "replace")
            return name or ""
        except XError:
            return ""

    def _active_window(self) -> int | None:
        from Xlib import Xatom
        prop = self._root.get_full_property(self._net_active, Xatom.WINDOW)
        if prop and prop.value:
            wid = int(prop.value[0])
            return wid or None
        return None

    def _alt_held(self) -> bool:
        keymap = self._dpy.query_keymap()
        for code in self._intent_keycodes:
            if keymap[code // 8] & (1 << (code % 8)):
                return True
        return False

    # -- intent ----------------------------------------------------------

    def _user_intent(self) -> tuple[bool, str]:
        now = time.monotonic() * 1000.0
        if self._last_button_ms and now - self._last_button_ms <= self._intent_ms:
            return True, f"button {now - self._last_button_ms:.0f}ms ago"
        if self._last_intent_key_ms and now - self._last_intent_key_ms <= self._intent_ms:
            return True, f"alt/super/tab {now - self._last_intent_key_ms:.0f}ms ago"
        if self._alt_held():
            return True, "alt/super held"
        if not self._raw_input_ok:
            # Degraded mode has no button visibility; only the alt checks
            # above apply. Report that so log readers know the basis.
            return False, "no alt held (degraded mode, buttons invisible)"
        return False, "no recent click or switcher key"

    def _note_raw_event(self, ev) -> None:
        from Xlib.ext import xinput
        if ev.evtype == xinput.RawButtonPress:
            self._last_button_ms = time.monotonic() * 1000.0
        elif ev.evtype == xinput.RawKeyPress:
            if ev.data.detail in self._intent_keycodes:
                self._last_intent_key_ms = time.monotonic() * 1000.0

    # -- reverts ---------------------------------------------------------

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        if now < self._fight_pause_until:
            return True
        self._reverts = [t for t in self._reverts if now - t < 60.0]
        if len(self._reverts) >= MAX_REVERTS_PER_MIN:
            self._fight_pause_until = now + FIGHT_PAUSE_S
            self._wlog(
                f"{MAX_REVERTS_PER_MIN} reverts inside a minute — pausing "
                f"{FIGHT_PAUSE_S:.0f}s to avoid fighting the user"
            )
            return True
        return False

    def _handle_active_change(self) -> None:
        wid = self._active_window()
        if wid is None:
            return
        wclass = self._wid_class(wid)
        if wclass != self._wm_class:
            self._last_good = wid
            self._reverted_this_episode = False
            return

        title = self._wid_title(wid)
        if self._is_login_title(title):
            self._wlog(f"active→platform login window 0x{wid:x}; leaving it alone")
            return
        if self._reverted_this_episode:
            return  # our own re-activation hasn't landed yet

        intent, why = self._user_intent()
        if intent:
            self._wlog(f"active→platform 0x{wid:x} ({title!r}): user intent ({why}); allowed")
            return
        if self._last_good is None:
            self._wlog(f"active→platform 0x{wid:x}: steal suspected but no restore target yet")
            return
        if self._observe:
            self._wlog(
                f"active→platform 0x{wid:x} ({title!r}): STEAL ({why}); "
                f"would revert to 0x{self._last_good:x} (observe mode)"
            )
            return
        if self._rate_limited():
            return
        if self._activate(self._last_good):
            self._reverts.append(time.monotonic())
            self._reverted_this_episode = True
            self._wlog(
                f"active→platform 0x{wid:x} ({title!r}): STEAL ({why}); "
                f"reverted to 0x{self._last_good:x}"
            )
        else:
            self._wlog(
                f"steal by 0x{wid:x} but re-activating 0x{self._last_good:x} "
                f"failed; clearing restore target"
            )
            self._last_good = None

    # -- main loop -------------------------------------------------------

    def run(self, seed_prev_active: int | None = None) -> None:
        """Block and guard until the platform exits (or launch timeout)."""
        try:
            self._connect()
        except Exception as e:
            # Boundary: a watchdog that cannot connect must not take down
            # a login that already succeeded.
            self._wlog(f"cannot start (X connection failed: {e}); focus unguarded")
            return

        if seed_prev_active is not None and self._wid_class(seed_prev_active) != self._wm_class:
            self._last_good = seed_prev_active

        started = time.monotonic()
        deadline = started + self._launch_timeout if self._scope == "launch" else None
        self._wlog(
            f"watching (class={self._wm_class!r}, scope={self._scope}, "
            f"intent_ms={self._intent_ms}, raw_input={self._raw_input_ok}, "
            f"seed=0x{(self._last_good or 0):x})"
        )

        fd = self._dpy.fileno()
        last_lifecycle = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                if deadline is not None and now > deadline:
                    self._wlog("launch-scope timeout reached; stopping")
                    return
                if now - last_lifecycle > LIFECYCLE_CHECK_S:
                    last_lifecycle = now
                    if now - started > STARTUP_GRACE_S and not self._find_platform():
                        self._wlog("platform windows gone; stopping")
                        return

                select.select([fd], [], [], 1.0)
                while self._dpy.pending_events():
                    from Xlib import X
                    ev = self._dpy.next_event()
                    if getattr(ev, "evtype", None) is not None and hasattr(ev, "data"):
                        self._note_raw_event(ev)
                    elif ev.type == X.PropertyNotify and ev.atom == self._net_active:
                        self._handle_active_change()
        except KeyboardInterrupt:
            self._wlog("interrupted; stopping")
        finally:
            if self._logf is not None:
                self._logf.close()
            try:
                self._dpy.close()
            except Exception:
                pass
