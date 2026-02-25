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


def _take_screenshot(wid: int, path: str) -> bool:
    """Capture a screenshot of the given window to *path*.

    On Linux, tries ImageMagick ``import`` first (window-specific capture),
    then falls back to pyautogui (full-screen capture).
    On macOS/Windows, uses pyautogui directly.
    """
    if sys.platform == "linux":
        try:
            result = subprocess.run(
                ["import", "-window", str(wid), path],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                return True
        except (subprocess.SubprocessError, FileNotFoundError):
            log.debug("ImageMagick import not available, trying pyautogui")

    # All platforms: pyautogui full-screen capture fallback
    try:
        import pyautogui
        screenshot = pyautogui.screenshot()
        screenshot.save(path)
        return True
    except Exception as e:
        log.debug("pyautogui screenshot failed: %s", e)
        return False


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
        if not _take_screenshot(wid, path):
            return False
        ocr = subprocess.run(
            ["tesseract", path, "stdout", "--psm", "6"],
            capture_output=True, text=True, timeout=10,
        )
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


def launch_with_account(platform_name: str, account, cred_mgr: CredentialManager, no_submit: bool = False) -> int:
    """Launch platform and auto-fill login for an already-selected account.

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

    # Create platform-appropriate window detector
    detector = create_window_detector()

    # Check if a login window is already open — if so, reuse it
    login_wid = detector.find_window_by_title(marker)
    if login_wid is not None:
        log.info("Existing login window found (wid=%d), reusing it.", login_wid)
        detector.activate_window(login_wid)
        time.sleep(0.3)
        detector.focus_window(login_wid)
    else:
        # Snapshot existing windows matching the marker so we only match the new one
        existing_login = detector.find_windows(title=marker)

        log.info("Launching: %s", " ".join(cmd))
        try:
            subprocess.Popen(cmd, env=launch_env)
        except OSError as e:
            print(f"Error launching {platform_name}: {e}", file=sys.stderr)
            return 1

        # Wait for the new login screen
        login_wid = _wait_for_login_screen(detector, timeout=plat_cfg.window_timeout, marker=marker, exclude=existing_login)
        if login_wid is None:
            print(
                f"Error: timed out waiting for {platform_name} login screen "
                f"(timeout={plat_cfg.window_timeout}s).",
                file=sys.stderr,
            )
            return 1

    # Small extra delay for the form fields to be interactive
    time.sleep(1)

    # Check if username is already pre-filled (e.g. "Remember Me")
    username_prefilled = _is_username_prefilled(account.username, login_wid)
    if username_prefilled:
        log.info("Username %r already pre-filled, skipping to password.", account.username)

    # Fill login form via the configured input strategy
    auto_submit = general.get("auto_submit", False) and not no_submit
    input_handler = _get_input_strategy(plat_cfg.input_strategy)

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
    del password

    if filled:
        print(f"Login form filled for {account.display_name} on {platform_name}.")
        return 0
    else:
        print(
            f"Warning: could not fill login form automatically. "
            f"Window is open — please log in manually.",
            file=sys.stderr,
        )
        return 1


def _wait_for_login_screen(
    detector,
    timeout: int = 120,
    marker: str = "Login",
    exclude: set[int] | None = None,
) -> int | None:
    """Poll for a window titled *marker*, activate it when found. Returns wid or None."""
    deadline = time.monotonic() + timeout
    log.info("Waiting for login screen (marker=%r, timeout=%ds)", marker, timeout)
    while time.monotonic() < deadline:
        wid = detector.find_window_by_title(marker, exclude=exclude)
        if wid is not None:
            log.info("Login screen detected (wid=%d), activating...", wid)
            detector.activate_window(wid)
            time.sleep(0.3)
            detector.focus_window(wid)
            return wid
        active_title = detector.get_active_window_title()
        log.info("Active window: %r, waiting...", active_title)
        time.sleep(0.5)
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

    # 4. Pick account (GTK dialog or auto-select)
    from tradegate.ui.account_picker import pick_account

    account = pick_account(accounts, platform_name, cred_manager=cred_mgr)
    if account is None:
        print("No account selected.", file=sys.stderr)
        return 1

    # 5-9. Launch and fill login
    return launch_with_account(platform_name, account, cred_mgr, no_submit=no_submit)


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
