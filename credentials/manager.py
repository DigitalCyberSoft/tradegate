"""Credential manager facade aggregating multiple backends."""

from __future__ import annotations

import logging
from pathlib import Path

from tradegate.credentials.base import Account, CredentialBackend
from tradegate.credentials.keyring_backend import KeyringBackend
from tradegate.credentials.encrypted_file import EncryptedFileBackend

log = logging.getLogger(__name__)


class CredentialManager:
    """Facade that aggregates credential backends and deduplicates accounts."""

    def __init__(self, backend_mode: str = "keyring", encrypted_file_path: str = "") -> None:
        self._backends: list[CredentialBackend] = []
        self._keyring: KeyringBackend | None = None
        self._encrypted: EncryptedFileBackend | None = None

        if backend_mode in ("keyring", "both"):
            kb = KeyringBackend()
            if kb.is_available():
                self._backends.append(kb)
                self._keyring = kb
            else:
                log.warning("gnome-keyring not available, skipping keyring backend")

        if backend_mode in ("encrypted_file", "both"):
            path = encrypted_file_path or str(
                Path.home() / ".config" / "tradegate" / "credentials.enc"
            )
            eb = EncryptedFileBackend(path)
            self._backends.append(eb)
            self._encrypted = eb

        # If requested backend isn't available, ensure at least encrypted file
        if not self._backends:
            log.warning("No backends available, falling back to encrypted file")
            path = encrypted_file_path or str(
                Path.home() / ".config" / "tradegate" / "credentials.enc"
            )
            eb = EncryptedFileBackend(path)
            self._backends.append(eb)
            self._encrypted = eb

    @property
    def encrypted_backend(self) -> EncryptedFileBackend | None:
        return self._encrypted

    @property
    def needs_unlock(self) -> bool:
        """True if the encrypted backend exists and needs a master password."""
        return self._encrypted is not None and self._encrypted.needs_password

    def unlock_encrypted(self, password: str) -> bool:
        """Unlock the encrypted backend. Returns True on success."""
        if self._encrypted is None:
            return True
        return self._encrypted.unlock(password)

    @property
    def primary_backend(self) -> CredentialBackend:
        """The first available backend, used for store/delete operations."""
        return self._backends[0]

    def list_accounts(self, platform: str | None = None) -> list[Account]:
        """List accounts from all backends, deduplicated by platform+username."""
        seen: set[tuple[str, str]] = set()
        accounts: list[Account] = []

        for backend in self._backends:
            try:
                for acct in backend.list_accounts(platform):
                    key = (acct.platform, acct.username)
                    if key not in seen:
                        seen.add(key)
                        accounts.append(acct)
            except Exception:
                log.warning("Failed to list accounts from %s", backend.name, exc_info=True)

        return accounts

    def get_password(self, platform: str, username: str) -> str | None:
        """Try each backend in order for the password."""
        for backend in self._backends:
            try:
                pw = backend.get_password(platform, username)
                if pw is not None:
                    return pw
            except Exception:
                log.warning(
                    "Failed to get password from %s", backend.name, exc_info=True
                )
        return None

    @property
    def keyring_backend(self) -> KeyringBackend | None:
        return self._keyring

    @property
    def available_backend_names(self) -> list[str]:
        """Return names of backends that are configured or can be created."""
        names: list[str] = []
        if self._keyring is not None:
            names.append("keyring")
        # Encrypted file is always available as a backend
        names.append("encrypted_file")
        return names

    def ensure_encrypted_backend(self, encrypted_file_path: str = "") -> EncryptedFileBackend:
        """Ensure the encrypted file backend exists, creating it if needed."""
        if self._encrypted is not None:
            return self._encrypted
        path = encrypted_file_path or str(
            Path.home() / ".config" / "tradegate" / "credentials.enc"
        )
        eb = EncryptedFileBackend(path)
        self._backends.append(eb)
        self._encrypted = eb
        return eb

    def store_account(
        self, platform: str, username: str, password: str, label: str = "",
        backend: str | None = None,
    ) -> None:
        """Store credentials in the specified or primary backend."""
        if backend == "encrypted_file" and self._encrypted:
            self._encrypted.store_account(platform, username, password, label)
        elif backend == "keyring" and self._keyring:
            self._keyring.store_account(platform, username, password, label)
        else:
            self.primary_backend.store_account(platform, username, password, label)

    def delete_account(self, platform: str, username: str) -> bool:
        """Delete credentials from all backends."""
        deleted = False
        for backend in self._backends:
            try:
                if backend.delete_account(platform, username):
                    deleted = True
            except Exception:
                log.warning(
                    "Failed to delete from %s", backend.name, exc_info=True
                )
        return deleted
