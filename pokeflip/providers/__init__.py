"""Price providers.

A provider knows how to look up cards and quote current prices. Swapping the
source of truth is a config change, not a code change.
"""

from __future__ import annotations

from ..config import Config
from .base import CardRecord, PriceProvider, PriceQuote, ProviderError
from .fixture import FixtureProvider
from .pokemontcg import PokemonTcgProvider

REGISTRY = {
    "pokemontcg": PokemonTcgProvider,
    "fixture": FixtureProvider,
}


def build_provider(config: Config) -> PriceProvider:
    name = (config.provider.name or "pokemontcg").lower()
    try:
        cls = REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ProviderError(f"unknown provider {name!r}; available: {known}") from None
    return cls(config)


__all__ = [
    "CardRecord",
    "FixtureProvider",
    "PokemonTcgProvider",
    "PriceProvider",
    "PriceQuote",
    "ProviderError",
    "REGISTRY",
    "build_provider",
]
