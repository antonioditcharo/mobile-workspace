"""Price providers.

A provider knows how to look up cards and quote current prices. Swapping the
source of truth is a config change, not a code change.

Not every source can do both jobs. eBay has real transaction prices but no card
catalog, so ``build_catalog_provider`` picks a source that does when the price
provider cannot answer catalog questions.
"""

from __future__ import annotations

from typing import Callable

from ..config import Config
from .base import CardRecord, PriceProvider, PriceQuote, ProviderError
from .ebay import EbayProvider
from .fixture import FixtureProvider
from .pokemontcg import PokemonTcgProvider

CatalogLookup = Callable[[str], "CardRecord | None"]

REGISTRY = {
    "pokemontcg": PokemonTcgProvider,
    "fixture": FixtureProvider,
    "ebay": EbayProvider,
}

# Fallback when the configured price provider has no catalog of its own.
DEFAULT_CATALOG_PROVIDER = "pokemontcg"


def _resolve(name: str):
    try:
        return REGISTRY[name.lower()]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ProviderError(f"unknown provider {name!r}; available: {known}") from None


def build_provider(config: Config, catalog: CatalogLookup | None = None) -> PriceProvider:
    """The configured price source."""
    cls = _resolve(config.provider.name or "pokemontcg")
    return cls(config, catalog)


def build_catalog_provider(config: Config) -> PriceProvider:
    """A source that can answer catalog questions.

    Usually the same object as the price provider; only different when the
    price source has no card database behind it.
    """
    cls = _resolve(config.provider.name or "pokemontcg")
    if getattr(cls, "provides_catalog", True):
        return cls(config, None)
    fallback = config.provider.catalog_name or DEFAULT_CATALOG_PROVIDER
    return _resolve(fallback)(config, None)


def provides_catalog(config: Config) -> bool:
    return bool(getattr(_resolve(config.provider.name or "pokemontcg"),
                        "provides_catalog", True))


__all__ = [
    "CardRecord",
    "CatalogLookup",
    "EbayProvider",
    "FixtureProvider",
    "PokemonTcgProvider",
    "PriceProvider",
    "PriceQuote",
    "ProviderError",
    "REGISTRY",
    "build_catalog_provider",
    "build_provider",
    "provides_catalog",
]
