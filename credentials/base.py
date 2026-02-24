"""Abstract credential backend and Account dataclass."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Account:
    """A stored trading account."""

    platform: str
    username: str
    label: str = ""
    backend: str = ""  # which backend stored this

    @property
    def display_name(self) -> str:
        if self.label:
            return f"{self.label} ({self.username})"
        return self.username


class CredentialBackend(ABC):
    """ABC for credential storage backends."""

    name: str = "base"

    @abstractmethod
    def list_accounts(self, platform: str | None = None) -> list[Account]:
        """List stored accounts, optionally filtered by platform."""

    @abstractmethod
    def get_password(self, platform: str, username: str) -> str | None:
        """Retrieve password for a given platform/username."""

    @abstractmethod
    def store_account(
        self, platform: str, username: str, password: str, label: str = ""
    ) -> None:
        """Store or update credentials."""

    @abstractmethod
    def delete_account(self, platform: str, username: str) -> bool:
        """Delete stored credentials. Returns True if found and deleted."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is usable on the current system."""
