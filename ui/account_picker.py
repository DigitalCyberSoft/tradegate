"""Electron-based account picker dialog with account management."""

from __future__ import annotations

import logging

from tradegate.config import load_config, save_config
from tradegate.credentials.base import Account
from tradegate.credentials.manager import CredentialManager
from tradegate.ui._electron import run_electron_dialog

log = logging.getLogger(__name__)


def pick_account(
    accounts: list[Account],
    platform: str = "",
    cred_manager: CredentialManager | None = None,
) -> Account | None:
    """Show a dialog to pick an account. Returns selected Account or None.

    If cred_manager is provided, Add/Edit/Delete actions are handled
    in a loop — the picker re-opens after each management action.
    """
    if not accounts and cred_manager is None:
        return None

    # Auto-select if only one account (only in non-management mode)
    if len(accounts) == 1 and cred_manager is None:
        return accounts[0]

    cfg = load_config()
    auto_submit = cfg.get("general", {}).get("auto_submit", False)

    while True:
        data = {
            "platform": platform,
            "auto_submit": auto_submit,
            "accounts": [
                {
                    "label": acct.label or acct.username,
                    "username": acct.username,
                    "platform": acct.platform,
                    "backend": acct.backend,
                }
                for acct in accounts
            ],
        }

        result = run_electron_dialog("account-picker", data)
        if result is None:
            return None

        action = result.get("action", "login")

        if action == "add":
            if cred_manager is not None:
                if _handle_add(cred_manager, platform):
                    accounts = cred_manager.list_accounts(platform or None)
                continue

            return None

        idx = result.get("index", -1)
        if idx < 0 or idx >= len(accounts):
            if cred_manager is not None:
                continue  # empty list, keep picker open for Add
            return None

        selected = accounts[idx]

        if action == "login":
            new_auto_submit = result.get("auto_submit", auto_submit)
            if new_auto_submit != auto_submit:
                auto_submit = new_auto_submit
                cfg["general"]["auto_submit"] = auto_submit
                save_config(cfg)
            return selected

        if cred_manager is None:
            return selected

        if action == "delete":
            if _handle_delete(cred_manager, selected, platform):
                accounts = cred_manager.list_accounts(platform or None)

        elif action == "edit":
            if _handle_edit(cred_manager, selected, platform):
                accounts = cred_manager.list_accounts(platform or None)


def _handle_add(mgr: CredentialManager, platform: str) -> bool:
    """Show add-account dialog and store. Returns True if saved."""
    from tradegate.platforms.registry import list_platforms

    result = run_electron_dialog("add-account", {
        "platform": platform,
        "platforms": list_platforms(),
        "backends": mgr.available_backend_names,
        "backend": mgr.available_backend_names[0] if mgr.available_backend_names else "keyring",
    })

    if result is None:
        return False

    acct_platform = result.get("platform")
    username = result.get("username")
    password = result.get("password")
    backend_choice = result.get("backend", "keyring")
    if not acct_platform or not username or not password:
        return False

    # If encrypted_file selected, ensure backend exists and is unlocked
    if backend_choice == "encrypted_file":
        eb = mgr.ensure_encrypted_backend()
        if eb.needs_password:
            if not _unlock_encrypted(mgr):
                return False

    label = result.get("label", "")
    mgr.store_account(acct_platform, username, password, label, backend=backend_choice)
    log.info("Added account %s on %s (backend: %s)", username, acct_platform, backend_choice)
    return True


def _unlock_encrypted(mgr: CredentialManager) -> bool:
    """Prompt for master password to unlock the encrypted backend.

    Shows a new-password setup flow if the encrypted file doesn't exist yet,
    otherwise prompts to unlock the existing file.
    """
    eb = mgr.encrypted_backend
    if eb is None or not eb.needs_password:
        return True

    from pathlib import Path
    file_exists = eb._path.exists()

    if not file_exists:
        # New encrypted file — ask user to set a master password
        result = run_electron_dialog("set-master-password", {})
        if result is None:
            return False
        password = result.get("password")
        if not password:
            return False
        eb.set_master_password(password)
        return True

    # Existing file — unlock loop
    for attempt in range(3):
        result = run_electron_dialog("password-prompt", {
            "attempt": attempt,
            "max_retries": 3,
        })
        if result is None:
            return False
        password = result.get("password")
        if password and mgr.unlock_encrypted(password):
            return True

    return False


def _handle_delete(
    mgr: CredentialManager, account: Account, platform: str
) -> bool:
    """Show confirm dialog and delete if confirmed. Returns True if deleted."""
    result = run_electron_dialog("confirm-delete", {
        "platform": platform or account.platform,
        "username": account.username,
        "label": account.label,
    })

    if result and result.get("confirmed"):
        mgr.delete_account(account.platform, account.username)
        log.info("Deleted account %s on %s", account.username, account.platform)
        return True
    return False


def _handle_edit(
    mgr: CredentialManager, account: Account, platform: str
) -> bool:
    """Show edit dialog and save changes. Returns True if saved."""
    backend = account.backend

    # Ensure encrypted backend is unlocked if this account lives there
    if backend == "encrypted_file":
        eb = mgr.ensure_encrypted_backend()
        if eb.needs_password:
            if not _unlock_encrypted(mgr):
                return False

    result = run_electron_dialog("edit-account", {
        "platform": platform or account.platform,
        "username": account.username,
        "label": account.label,
    })

    if result is None:
        return False

    new_label = result.get("label", account.label)
    new_password = result.get("password")

    if new_password:
        mgr.store_account(
            account.platform, account.username, new_password, new_label,
            backend=backend,
        )
        log.info("Updated account %s (with new password)", account.username)
    elif new_label != account.label:
        current_pw = mgr.get_password(account.platform, account.username)
        if current_pw:
            mgr.store_account(
                account.platform, account.username, current_pw, new_label,
                backend=backend,
            )
            log.info("Updated label for %s to '%s'", account.username, new_label)
    return True
