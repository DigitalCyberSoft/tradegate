"""Main orchestrator: launch platform -> detect window -> pick account -> fill login."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import time

from tradegate.config import load_config, get_platform_config
from tradegate.credentials.manager import CredentialManager
from tradegate.platforms.base import PlatformConfig
from tradegate.platforms.registry import get_plugin
from tradegate.detection.window import create_window_detector

log = logging.getLogger(__name__)


def _get_window_geometry(wid: int) -> tuple[int, int, int, int] | None:
    """Return (x, y, width, height) for a window, or None on failure."""
    try:
        result = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", str(wid)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        vals = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k] = int(v)
        return (vals["X"], vals["Y"], vals["WIDTH"], vals["HEIGHT"])
    except (subprocess.SubprocessError, FileNotFoundError, KeyError, ValueError):
        return None


def _take_screenshot(wid: int, path: str) -> bool:
    """Capture a screenshot of the given window to *path*.

    Takes a full-screen screenshot and crops to the window geometry.
    This works reliably with Java Swing and other toolkits where
    window-specific capture (import -window) grabs the wrong content.
    """
    if sys.platform == "linux":
        geom = _get_window_geometry(wid)
        if geom is not None:
            x, y, w, h = geom
            try:
                result = subprocess.run(
                    ["import", "-window", "root",
                     "-crop", f"{w}x{h}+{x}+{y}", "+repage", path],
                    capture_output=True, timeout=10,
                )
                if result.returncode == 0:
                    return True
            except (subprocess.SubprocessError, FileNotFoundError):
                log.debug("ImageMagick import not available, trying pyautogui")

    # All platforms: pyautogui full-screen capture + crop fallback
    try:
        import pyautogui
        screenshot = pyautogui.screenshot()
        if sys.platform == "linux":
            geom = _get_window_geometry(wid)
            if geom is not None:
                x, y, w, h = geom
                screenshot = screenshot.crop((x, y, x + w, y + h))
        screenshot.save(path)
        return True
    except Exception as e:
        log.debug("pyautogui screenshot failed: %s", e)
        return False


def _prepare_for_ocr(path: str) -> None:
    """Convert screenshot to grayscale so tesseract runs faster.

    Tries ImageMagick first, PIL as fallback.
    """
    try:
        subprocess.run(
            ["convert", path, "-colorspace", "Gray", path],
            capture_output=True, timeout=10,
        )
        return
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    try:
        from PIL import Image
        img = Image.open(path)
        img = img.convert("L")
        img.save(path)
    except Exception as e:
        log.debug("PIL image prep failed: %s", e)


def _is_username_prefilled(username: str, wid: int) -> bool:
    """Screenshot a specific window and OCR it to check if the username field has text.

    Looks for any text between the 'username' and 'password' labels — if there is
    something there, the field is pre-filled.
    """
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    if os.name != "nt":
        os.chmod(path, 0o600)
    try:
        t0 = time.monotonic()
        if not _take_screenshot(wid, path):
            return False
        t1 = time.monotonic()
        _prepare_for_ocr(path)
        t2 = time.monotonic()
        ocr = subprocess.run(
            ["tesseract", path, "stdout", "--oem", "1", "--psm", "3"],
            capture_output=True, text=True, timeout=10,
        )
        t3 = time.monotonic()
        log.info("Timing: screenshot=%.2fs, grayscale=%.2fs, tesseract=%.2fs, total=%.2fs",
                 t1 - t0, t2 - t1, t3 - t2, t3 - t0)
        text = ocr.stdout.lower()
        log.info("OCR text snippet: %r", text[:200])
        # Find text between "username" and "password" labels
        m = re.search(r"username\s*\n(.*?)password", text, re.DOTALL)
        if m:
            between = m.group(1)
            # Strip OCR noise — only count alphanumeric characters
            alnum = re.sub(r"[^a-z0-9]", "", between)
            found = len(alnum) >= 3
            log.info("Text between username/password labels: %r (alnum=%r) → prefilled=%s", between.strip(), alnum, found)
            return found
        log.info("Could not find username/password labels in OCR text")
        return False
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log.debug("OCR prefill check failed: %s", e)
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _get_input_strategy(strategy: str):
    """Return an input handler based on the configured strategy.

    Strategies: "auto", "atspi", "xdotool", "pyautogui".
    """
    if strategy == "atspi":
        from tradegate.detection.atspi_fill import AtspiInspector
        return AtspiInspector()

    if strategy == "xdotool":
        from tradegate.detection.xdotool_fill import XdotoolInput
        return XdotoolInput()

    if strategy == "pyautogui":
        from tradegate.detection.pyautogui_fill import PyAutoGUIInput
        return PyAutoGUIInput()

    # "auto" — pyautogui on all platforms
    from tradegate.detection.pyautogui_fill import PyAutoGUIInput
    return PyAutoGUIInput()


def launch_with_account(
    platform_name: str,
    account,
    cred_mgr: CredentialManager,
    no_submit: bool = False,
    prev_active: int | None = None,
) -> int:
    """Launch platform and auto-fill login for an already-selected account.

    *prev_active* is the window the user was in before any tradegate UI
    opened; pass it when a picker dialog ran first, because capturing the
    active window after a dialog closes races the WM's refocus.

    Returns 0 on success, non-zero on failure.
    """
    cfg = load_config()
    general = cfg.get("general", {})

    plugin = get_plugin(platform_name)
    if plugin is None:
        print(f"Error: unknown platform '{platform_name}'", file=sys.stderr)
        return 1

    plat_cfg_dict = get_platform_config(cfg, platform_name)
    plat_cfg = PlatformConfig.from_dict(platform_name, plat_cfg_dict)

    if not plat_cfg.binary:
        from tradegate.config import CONFIG_PATH
        print(
            f"Error: no binary configured for '{platform_name}'. "
            f"Edit {CONFIG_PATH} to set the binary path.",
            file=sys.stderr,
        )
        return 1

    # Retrieve password
    password = cred_mgr.get_password(platform_name, account.username)
    if password is None:
        print(f"Error: no password found for {account.username}", file=sys.stderr)
        return 1

    # Launch platform
    cmd = plugin.get_launch_command(plat_cfg)
    env = plugin.get_launch_env(plat_cfg)
    launch_env = os.environ.copy()
    if env:
        launch_env.update(env)

    binary_path = cmd[0]
    if not os.path.isfile(binary_path):
        print(f"Error: binary not found at '{binary_path}'", file=sys.stderr)
        return 1

    # Determine the login window marker from title_pattern
    marker = plat_cfg.title_pattern or "Login"
    wm_class = plat_cfg.wm_class or ""

    # Create platform-appropriate window detector
    detector = create_window_detector()

    # Remember where the user was so the focus watchdog can hand focus
    # back after login (restore_focus).
    if prev_active is None:
        prev_active = detector.get_active_window()

    # Check if a login window is already open — if so, reuse it
    login_wid = _find_login_window(detector, marker, wm_class, plugin)
    if login_wid is not None:
        log.info("Existing login window found (wid=%d), reusing it.", login_wid)
        detector.activate_window(login_wid)
        time.sleep(0.1)
        detector.focus_window(login_wid)
    else:
        # Snapshot existing windows matching the marker so we only match the new one
        existing_login = detector.find_windows(wm_class=wm_class, title=marker) if wm_class else detector.find_windows(title=marker)

        log.info("Launching: %s", " ".join(cmd))
        try:
            subprocess.Popen(cmd, env=launch_env)
        except OSError as e:
            print(f"Error launching {platform_name}: {e}", file=sys.stderr)
            return 1

        # Wait for the new login screen
        login_wid = _wait_for_login_screen(detector, timeout=plat_cfg.window_timeout, marker=marker, exclude=existing_login, wm_class=wm_class, plugin=plugin)
        if login_wid is None:
            print(
                f"Error: timed out waiting for {platform_name} login screen "
                f"(timeout={plat_cfg.window_timeout}s).",
                file=sys.stderr,
            )
            return 1

    # Configurable delay for the form fields to be interactive
    t_start = time.monotonic()
    log.info("Waiting %.2fs for form fields to be interactive...", plat_cfg.login_ready_delay)
    time.sleep(plat_cfg.login_ready_delay)
    t_after_delay = time.monotonic()
    log.info("Ready delay done (%.2fs elapsed)", t_after_delay - t_start)

    # Check if username is already pre-filled (e.g. "Remember Me")
    username_prefilled = _is_username_prefilled(account.username, login_wid)
    t_after_ocr = time.monotonic()
    log.info("Prefill check done (%.2fs elapsed, ocr=%.2fs)", t_after_ocr - t_start, t_after_ocr - t_after_delay)
    if username_prefilled:
        log.info("Username %r already pre-filled, skipping to password.", account.username)

    # Fill login form via the configured input strategy
    auto_submit = general.get("auto_submit", False) and not no_submit
    input_handler = _get_input_strategy(plat_cfg.input_strategy)
    log.info("Using input strategy: %s", type(input_handler).__name__)

    # AT-SPI has a different call signature
    from tradegate.detection.atspi_fill import AtspiInspector
    if isinstance(input_handler, AtspiInspector):
        filled = input_handler.fill_login_form(
            app_name=plat_cfg.wm_class,
            username=account.username,
            password=password,
            field_order=plat_cfg.field_order,
            auto_submit=auto_submit,
        )
    else:
        filled = input_handler.fill_login_form(
            username=account.username,
            password=password,
            field_order=plat_cfg.field_order,
            auto_submit=auto_submit,
            username_prefilled=username_prefilled,
            expected_wid=login_wid,
        )
    t_after_fill = time.monotonic()
    log.info("Form fill done (%.2fs elapsed, fill=%.2fs)", t_after_fill - t_start, t_after_fill - t_after_ocr)
    del password

    if filled:
        print(f"Login form filled for {account.display_name} on {platform_name}.")
        _restore_focus_after_login(detector, plugin, plat_cfg, prev_active=prev_active)
        return 0
    else:
        print(
            f"Warning: could not fill login form automatically. "
            f"Window is open — please log in manually.",
            file=sys.stderr,
        )
        return 1


def _restore_focus_after_login(detector, plugin, plat_cfg, prev_active):
    """Run the focus watchdog: revert WM-side focus grabs to the platform.

    The focus shield blocks the app's own focus grabs, but the WM can hand
    a platform window focus on its own (focus succession, activation), at
    any point in the session — not just during launch. The watchdog reverts
    those unless real user input (a click, or alt/super/tab) preceded the
    change. See tradegate.detection.focus_watch for the policy.
    """
    if not plat_cfg.restore_focus:
        return
    if not plat_cfg.wm_class:
        return
    if sys.platform != "linux":
        return

    from tradegate.detection.focus_watch import FocusWatchdog, WATCH_LOG_PATH

    scope = plat_cfg.restore_focus_scope
    print(
        f"Focus watchdog running (scope={scope}, log={WATCH_LOG_PATH}). "
        f"Login is done — Ctrl-C only stops the watchdog."
    )
    watchdog = FocusWatchdog(
        wm_class=plat_cfg.wm_class,
        activate_fn=detector.activate_window,
        find_platform_windows=lambda: detector.find_windows(wm_class=plat_cfg.wm_class),
        is_login_title=plugin.is_login_screen,
        scope=scope,
        launch_timeout=plat_cfg.window_timeout + 30,
    )
    watchdog.run(seed_prev_active=prev_active)


def _find_login_window(detector, marker, wm_class, plugin, exclude=None):
    """Find a window matching *marker*/*wm_class* that the plugin considers a login screen."""
    candidates = list(detector.find_windows(wm_class=wm_class, title=marker)) if wm_class else []
    if not candidates:
        candidates = list(detector.find_windows(title=marker))
    exclude = exclude or set()
    for wid in candidates:
        if wid in exclude:
            continue
        title = detector.get_window_title(wid)
        if plugin.is_login_screen(title):
            return wid
        log.debug("Skipping wid=%d title=%r (not a login screen)", wid, title)
    return None


def _wait_for_login_screen(
    detector,
    timeout: int = 120,
    marker: str = "Login",
    exclude: set[int] | None = None,
    wm_class: str = "",
    plugin=None,
) -> int | None:
    """Poll for a window titled *marker*, activate it when found. Returns wid or None."""
    deadline = time.monotonic() + timeout
    log.info("Waiting for login screen (marker=%r, wm_class=%r, timeout=%ds)", marker, wm_class, timeout)
    while time.monotonic() < deadline:
        if plugin is not None:
            wid = _find_login_window(detector, marker, wm_class, plugin, exclude=exclude)
        else:
            wid = detector.find_window_by_title(marker, exclude=exclude, wm_class=wm_class)
        if wid is not None:
            t0 = time.monotonic()
            log.info("Login screen detected (wid=%d), activating...", wid)
            detector.activate_window(wid)
            t1 = time.monotonic()
            log.info("activate_window took %.2fs", t1 - t0)
            time.sleep(0.1)
            detector.focus_window(wid)
            t2 = time.monotonic()
            log.info("focus_window took %.2fs (total activate+focus=%.2fs)", t2 - t1 - 0.1, t2 - t0)
            return wid
        if log.isEnabledFor(logging.DEBUG):
            active_title = detector.get_active_window_title()
            log.debug("Active window: %r, waiting...", active_title)
        time.sleep(0.15)
    log.warning("Login screen not detected within %ds", timeout)
    return None


def launch_and_login(platform_name: str, no_submit: bool = False) -> int:
    """Main flow: launch platform, detect window, pick account, fill login.

    Returns 0 on success, non-zero on failure.
    """
    # 1. Load config
    cfg = load_config()
    general = cfg.get("general", {})

    # 2. Init credential manager, unlock if needed
    cred_mgr = CredentialManager(
        backend_mode=general.get("credential_backend", "keyring"),
        encrypted_file_path=general.get("encrypted_file_path", ""),
    )

    if cred_mgr.needs_unlock:
        from tradegate.ui.password_prompt import prompt_master_password

        for _attempt in range(3):
            password = prompt_master_password(max_retries=1)
            if password is None:
                print("Cancelled.", file=sys.stderr)
                return 1
            if cred_mgr.unlock_encrypted(password):
                break
        else:
            print("Error: failed to unlock encrypted credentials.", file=sys.stderr)
            return 1

    # 3. Get accounts for platform
    accounts = cred_mgr.list_accounts(platform_name)
    if not accounts:
        print(
            f"No accounts stored for '{platform_name}'. "
            f"Run: tradegate add {platform_name} --label NAME --username USER",
            file=sys.stderr,
        )
        return 1

    # 4. Pick account (GTK dialog or auto-select). Capture the user's
    # window first — the picker takes focus and its close races the WM.
    prev_active = create_window_detector().get_active_window()

    from tradegate.ui.account_picker import pick_account

    account = pick_account(accounts, platform_name, cred_manager=cred_mgr)
    if account is None:
        print("No account selected.", file=sys.stderr)
        return 1

    # 5-9. Launch and fill login
    return launch_with_account(
        platform_name, account, cred_mgr, no_submit=no_submit, prev_active=prev_active,
    )


def inspect_platform(platform_name: str) -> int:
    """Launch platform and dump AT-SPI tree for debugging."""
    from tradegate.detection.atspi_fill import AtspiInspector

    if not AtspiInspector.is_available():
        print(
            "AT-SPI is not available on this system.\n"
            "For Java apps, ensure java-atk-wrapper is installed and configured.\n"
            "Install: sudo dnf install java-atk-wrapper\n"
            "Then set: export GTK_MODULES=gail:atk-bridge\n"
            "And restart the Java application.",
            file=sys.stderr,
        )
        return 1

    cfg = load_config()
    plat_cfg_dict = get_platform_config(cfg, platform_name)
    plat_cfg = PlatformConfig.from_dict(platform_name, plat_cfg_dict)

    inspector = AtspiInspector()
    tree = inspector.dump_tree(plat_cfg.wm_class)
    print(tree)
    return 0
