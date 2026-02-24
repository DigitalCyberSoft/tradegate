"""Credential backend using gnome-keyring via secretstorage (D-Bus Secret Service)."""

from __future__ import annotations

import logging

from tradegate.credentials.base import Account, CredentialBackend

log = logging.getLogger(__name__)

ATTR_APP = "application"
ATTR_PLATFORM = "platform"
ATTR_USERNAME = "username"
ATTR_LABEL = "label"
APP_NAME = "tradegate"


class KeyringBackend(CredentialBackend):
    """Store credentials in gnome-keyring via D-Bus Secret Service API."""

    name = "keyring"

    def __init__(self) -> None:
        self._connection = None
        self._collection = None

    def _connect(self):
        """Lazily connect to D-Bus and open the default collection."""
        if self._connection is not None:
            return

        import secretstorage

        self._connection = secretstorage.dbus_init()
        self._collection = secretstorage.get_default_collection(self._connection)

        if self._collection.is_locked():
            self._collection.unlock()

    def is_available(self) -> bool:
        try:
            self._connect()
            return True
        except Exception:
            log.debug("gnome-keyring not available", exc_info=True)
            return False

    def list_accounts(self, platform: str | None = None) -> list[Account]:
        self._connect()
        attrs = {ATTR_APP: APP_NAME}
        if platform:
            attrs[ATTR_PLATFORM] = platform

        accounts = []
        for item in self._collection.search_items(attrs):
            item_attrs = item.get_attributes()
            accounts.append(
                Account(
                    platform=item_attrs.get(ATTR_PLATFORM, ""),
                    username=item_attrs.get(ATTR_USERNAME, ""),
                    label=item_attrs.get(ATTR_LABEL, ""),
                    backend=self.name,
                )
            )
        return accounts

    def get_password(self, platform: str, username: str) -> str | None:
        self._connect()
        attrs = {ATTR_APP: APP_NAME, ATTR_PLATFORM: platform, ATTR_USERNAME: username}

        for item in self._collection.search_items(attrs):
            secret = item.get_secret()
            if secret:
                return secret.decode("utf-8")
        return None

    def store_account(
        self, platform: str, username: str, password: str, label: str = ""
    ) -> None:
        self._connect()
        attrs = {
            ATTR_APP: APP_NAME,
            ATTR_PLATFORM: platform,
            ATTR_USERNAME: username,
            ATTR_LABEL: label,
        }

        # Delete existing entry first to avoid duplicates
        self.delete_account(platform, username)

        item_label = f"tradegate: {platform}/{username}"
        if label:
            item_label = f"tradegate: {label} ({platform}/{username})"

        self._collection.create_item(item_label, attrs, password.encode("utf-8"))

    def delete_account(self, platform: str, username: str) -> bool:
        self._connect()
        attrs = {ATTR_APP: APP_NAME, ATTR_PLATFORM: platform, ATTR_USERNAME: username}
        deleted = False
        for item in self._collection.search_items(attrs):
            item.delete()
            deleted = True
        return deleted
