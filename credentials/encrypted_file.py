"""Credential backend using a Fernet-encrypted JSON file with PBKDF2 key derivation."""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from tradegate.credentials.base import Account, CredentialBackend

log = logging.getLogger(__name__)

SALT_SIZE = 16
PBKDF2_ITERATIONS = 600_000


class EncryptedFileBackend(CredentialBackend):
    """Store credentials in a Fernet-encrypted JSON file."""

    name = "encrypted_file"

    def __init__(self, file_path: str | Path) -> None:
        resolved = Path(file_path).resolve()
        home = Path.home().resolve()
        try:
            resolved.relative_to(home)
        except ValueError:
            raise ValueError(
                f"Credential file path must be within home directory ({home}), "
                f"got: {resolved}"
            )
        if resolved.is_symlink():
            target = resolved.resolve(strict=False)
            try:
                target.relative_to(home)
            except ValueError:
                raise ValueError(
                    f"Credential file symlink points outside home directory: "
                    f"{resolved} -> {target}"
                )
        self._path = resolved
        self._master_password: str | None = None
        self._data: dict | None = None  # cached decrypted data

    def set_master_password(self, password: str) -> None:
        """Set the master password for encryption/decryption."""
        self._master_password = password
        self._data = None  # invalidate cache

    @property
    def needs_password(self) -> bool:
        return self._master_password is None

    def is_available(self) -> bool:
        return True  # always available as a fallback

    def unlock(self, password: str) -> bool:
        """Try to unlock with the given password. Returns True on success."""
        self._master_password = password
        try:
            self._load()
            return True
        except (InvalidToken, ValueError):
            self._master_password = None
            self._data = None
            return False

    def _derive_key(self, salt: bytes) -> bytes:
        """Derive a Fernet key from the master password and salt."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=PBKDF2_ITERATIONS,
        )
        return base64.urlsafe_b64encode(kdf.derive(self._master_password.encode()))

    def _load(self) -> dict:
        """Load and decrypt the credential file."""
        if self._data is not None:
            return self._data

        if not self._path.exists():
            self._data = {"accounts": {}}
            return self._data

        raw = self._path.read_bytes()
        if len(raw) < SALT_SIZE + 1:
            raise ValueError("Corrupt credential file")

        salt = raw[:SALT_SIZE]
        token = raw[SALT_SIZE:]
        key = self._derive_key(salt)

        f = Fernet(key)
        plaintext = f.decrypt(token)
        self._data = json.loads(plaintext)
        return self._data

    def _save(self) -> None:
        """Encrypt and write the credential file."""
        if self._data is None:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._path.parent, 0o700)

        # Generate new salt each save
        salt = os.urandom(SALT_SIZE)
        key = self._derive_key(salt)
        f = Fernet(key)

        plaintext = json.dumps(self._data).encode()
        token = f.encrypt(plaintext)

        old_umask = os.umask(0o177)
        try:
            self._path.write_bytes(salt + token)
        finally:
            os.umask(old_umask)
        os.chmod(self._path, 0o600)

    def _account_key(self, platform: str, username: str) -> str:
        return f"{platform}:{username}"

    def list_accounts(self, platform: str | None = None) -> list[Account]:
        data = self._load()
        accounts = []
        for key, info in data.get("accounts", {}).items():
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
        data = self._load()
        key = self._account_key(platform, username)
        entry = data.get("accounts", {}).get(key)
        if entry:
            return entry.get("password")
        return None

    def store_account(
        self, platform: str, username: str, password: str, label: str = ""
    ) -> None:
        data = self._load()
        if "accounts" not in data:
            data["accounts"] = {}

        key = self._account_key(platform, username)
        data["accounts"][key] = {
            "platform": platform,
            "username": username,
            "password": password,
            "label": label,
        }
        self._save()

    def delete_account(self, platform: str, username: str) -> bool:
        data = self._load()
        key = self._account_key(platform, username)
        if key in data.get("accounts", {}):
            del data["accounts"][key]
            self._save()
            return True
        return False
