"""Platform plugin registry."""

from __future__ import annotations

from tradegate.platforms.base import PlatformPlugin
from tradegate.platforms.tws import TWSPlugin
from tradegate.platforms.tastytrade import TastyTradePlugin
from tradegate.platforms.thinkorswim import ThinkorswimPlugin

_PLUGINS: dict[str, PlatformPlugin] = {
    "tws": TWSPlugin(),
    "tastytrade": TastyTradePlugin(),
    "thinkorswim": ThinkorswimPlugin(),
}


def get_plugin(name: str) -> PlatformPlugin | None:
    """Look up a platform plugin by name."""
    return _PLUGINS.get(name)


def list_platforms() -> list[str]:
    """Return all registered platform names."""
    return list(_PLUGINS.keys())
