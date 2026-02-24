"""Command-line interface for tradegate."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from tradegate.config import load_config
from tradegate.credentials.manager import CredentialManager
from tradegate.platforms.registry import list_platforms


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tradegate",
        description="Trading platform auto-login launcher",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    sub = parser.add_subparsers(dest="command")

    # launch
    p_launch = sub.add_parser("launch", help="Launch platform and auto-fill login")
    p_launch.add_argument("platform", choices=list_platforms(), help="Platform to launch")
    p_launch.add_argument(
        "--no-submit", action="store_true",
        help="Fill credentials but don't press Enter (overrides auto_submit config)",
    )

    # add
    p_add = sub.add_parser("add", help="Store credentials for a platform")
    p_add.add_argument("platform", choices=list_platforms(), help="Platform name")
    p_add.add_argument("--label", default="", help="Friendly label for the account")
    p_add.add_argument("--username", required=True, help="Login username")

    # list
    p_list = sub.add_parser("list", help="List stored accounts")
    p_list.add_argument("platform", nargs="?", default=None, help="Filter by platform")

    # remove
    p_remove = sub.add_parser("remove", help="Delete stored credentials")
    p_remove.add_argument("platform", choices=list_platforms(), help="Platform name")
    p_remove.add_argument("username", help="Username to remove")

    # manage
    p_manage = sub.add_parser("manage", help="Open account management GUI")
    p_manage.add_argument("platform", nargs="?", default=None, help="Filter by platform")

    # inspect
    p_inspect = sub.add_parser("inspect", help="Dump AT-SPI tree for a platform (debug)")
    p_inspect.add_argument("platform", choices=list_platforms(), help="Platform name")

    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    if args.command is None:
        args.command = "manage"
        args.platform = None

    if args.command == "launch":
        sys.exit(_cmd_launch(args, no_submit=getattr(args, "no_submit", False)))
    elif args.command == "add":
        sys.exit(_cmd_add(args))
    elif args.command == "list":
        sys.exit(_cmd_list(args))
    elif args.command == "remove":
        sys.exit(_cmd_remove(args))
    elif args.command == "manage":
        sys.exit(_cmd_manage(args))
    elif args.command == "inspect":
        sys.exit(_cmd_inspect(args))


def _get_cred_manager() -> CredentialManager:
    cfg = load_config()
    general = cfg.get("general", {})
    return CredentialManager(
        backend_mode=general.get("credential_backend", "keyring"),
        encrypted_file_path=general.get("encrypted_file_path", ""),
    )


def _ensure_unlocked(mgr: CredentialManager) -> bool:
    """Unlock encrypted backend if needed. Returns True on success."""
    if not mgr.needs_unlock:
        return True

    # Try Electron prompt first, fall back to terminal getpass
    try:
        from tradegate.ui.password_prompt import prompt_master_password
        password = prompt_master_password(max_retries=3)
    except Exception:
        password = getpass.getpass("Master password: ")

    if password is None:
        return False
    return mgr.unlock_encrypted(password)


def _cmd_launch(args, no_submit: bool = False) -> int:
    from tradegate.orchestrator import launch_and_login
    return launch_and_login(args.platform, no_submit=no_submit)


def _cmd_add(args) -> int:
    mgr = _get_cred_manager()
    if not _ensure_unlocked(mgr):
        print("Error: could not unlock credential store.", file=sys.stderr)
        return 1

    # Prompt for password
    try:
        from tradegate.ui._electron import run_electron_dialog

        result = run_electron_dialog("add-password", {
            "platform": args.platform,
            "username": args.username,
        })

        if result is None or not result.get("password"):
            print("Cancelled.", file=sys.stderr)
            return 1
        password = result["password"]
    except Exception:
        password = getpass.getpass(f"Password for {args.username}: ")
        if not password:
            print("No password provided.", file=sys.stderr)
            return 1

    mgr.store_account(args.platform, args.username, password, args.label)
    label_str = f" ({args.label})" if args.label else ""
    print(f"Stored credentials for {args.username}{label_str} on {args.platform}.")
    return 0


def _cmd_list(args) -> int:
    mgr = _get_cred_manager()
    if not _ensure_unlocked(mgr):
        print("Error: could not unlock credential store.", file=sys.stderr)
        return 1

    accounts = mgr.list_accounts(args.platform)
    if not accounts:
        target = f" for '{args.platform}'" if args.platform else ""
        print(f"No accounts stored{target}.")
        return 0

    for acct in accounts:
        label = f" [{acct.label}]" if acct.label else ""
        print(f"  {acct.platform}: {acct.username}{label} (via {acct.backend})")
    return 0


def _cmd_remove(args) -> int:
    mgr = _get_cred_manager()
    if not _ensure_unlocked(mgr):
        print("Error: could not unlock credential store.", file=sys.stderr)
        return 1

    if mgr.delete_account(args.platform, args.username):
        print(f"Deleted credentials for {args.username} on {args.platform}.")
        return 0
    else:
        print(f"No credentials found for {args.username} on {args.platform}.", file=sys.stderr)
        return 1


def _cmd_manage(args) -> int:
    mgr = _get_cred_manager()
    if not _ensure_unlocked(mgr):
        print("Error: could not unlock credential store.", file=sys.stderr)
        return 1

    from tradegate.ui.account_picker import pick_account

    platform = args.platform or ""
    accounts = mgr.list_accounts(platform or None)
    selected = pick_account(accounts, platform, cred_manager=mgr)
    if selected:
        from tradegate.orchestrator import launch_with_account
        return launch_with_account(selected.platform, selected, mgr)
    return 0


def _cmd_inspect(args) -> int:
    from tradegate.orchestrator import inspect_platform
    return inspect_platform(args.platform)
