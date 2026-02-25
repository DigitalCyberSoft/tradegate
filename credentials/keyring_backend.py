"""Credential backend using the cross-platform keyring library.

Uses the system keyring (macOS Keychain, Windows Credential Locker,
or SecretService/gnome-keyring on Linux).
"""

from __future__ import annotations

import json
import logging

from tradegate.credentials.base import Account, CredentialBackend

log = logging.getLogger(__name__)

SERVICE_NAME = "tradegate"
# Keyring entry that stores the JSON index of all accounts
_INDEX_USERNAME = "__tradegate_account_index__"


class KeyringBackend(CredentialBackend):
    """Store credentials in the OS keyring via the ``keyring`` library."""

    name = "keyring"

    def __init__(self) -> None:
        self._kr = None

    def _get_keyring(self):
        if self._kr is None:
            import keyring
            self._kr = keyring
        return self._kr

    def is_available(self) -> bool:
        try:
            kr = self._get_keyring()
            # Verify a usable backend is present (not the fail backend)
            backend = kr.get_keyring()
            return not type(backend).__name__.endswith("Fail")
        except Exception:
            log.debug("keyring not available", exc_info=True)
            return False

    def _load_index(self) -> dict[str, dict]:
        """Load the account index from the keyring.

        The index maps ``"platform:username"`` to ``{"platform", "username", "label"}``.
        """
        kr = self._get_keyring()
        raw = kr.get_password(SERVICE_NAME, _INDEX_USERNAME)
        if raw:
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                log.warning("Corrupt keyring account index, resetting")
        return {}

    def _save_index(self, index: dict[str, dict]) -> None:
        kr = self._get_keyring()
        kr.set_password(SERVICE_NAME, _INDEX_USERNAME, json.dumps(index))

    @staticmethod
    def _key(platform: str, username: str) -> str:
        return f"{platform}:{username}"

    def list_accounts(self, platform: str | None = None) -> list[Account]:
        index = self._load_index()
        accounts = []
        for info in index.values():
            if platform and info.get("platform") != platform:
                continue
            accounts.append(
                Account(
                    platform=info["platform"],
                    username=info["username"],
                    label=info.get("label", ""),
                    backend=self.name,
                )
            )
        return accounts

    def get_password(self, platform: str, username: str) -> str | None:
        kr = self._get_keyring()
        return kr.get_password(SERVICE_NAME, self._key(platform, username))

    def store_account(
        self, platform: str, username: str, password: str, label: str = ""
    ) -> None:
        kr = self._get_keyring()
        key = self._key(platform, username)

        # Store the password
        kr.set_password(SERVICE_NAME, key, password)

        # Update the account index
        index = self._load_index()
        index[key] = {
            "platform": platform,
            "username": username,
            "label": label,
        }
        self._save_index(index)

    def migrate_from_secretstorage(self) -> int:
        """One-time migration from old secretstorage entries. Returns count migrated."""
        try:
            import secretstorage
        except ImportError:
            return 0

        try:
            conn = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(conn)
            if collection.is_locked():
                collection.unlock()
        except Exception:
            log.debug("secretstorage migration: could not open collection", exc_info=True)
            return 0

        index = self._load_index()
        kr = self._get_keyring()
        count = 0

        for item in collection.search_items({"application": "tradegate"}):
            attrs = item.get_attributes()
            platform = attrs.get("platform", "")
            username = attrs.get("username", "")
            if not platform or not username:
                continue
            key = self._key(platform, username)
            if key in index:
                continue
            password = item.get_secret().decode("utf-8")
            label = attrs.get("label", "")
            kr.set_password(SERVICE_NAME, key, password)
            index[key] = {"platform": platform, "username": username, "label": label}
            count += 1

        if count:
            self._save_index(index)
            log.info("Migrated %d account(s) from secretstorage", count)
        return count

    def delete_account(self, platform: str, username: str) -> bool:
        kr = self._get_keyring()
        key = self._key(platform, username)

        # Remove from index
        index = self._load_index()
        had_entry = key in index
        index.pop(key, None)
        self._save_index(index)

        # Delete the password
        try:
            kr.delete_password(SERVICE_NAME, key)
        except Exception:
            pass

        return had_entry
