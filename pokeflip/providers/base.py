"""Provider interface shared by every price source."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Raised when a price source cannot be reached or returns nonsense."""


@dataclass
class CardRecord:
    """Catalog metadata for one printed card."""

    id: str
    name: str
    set_id: str = ""
    set_name: str = ""
    number: str = ""
    rarity: str = ""
    supertype: str = ""
    subtypes: str = ""
    artist: str = ""
    image_small: str = ""
    image_large: str = ""
    tcgplayer_url: str = ""
    cardmarket_url: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "set_id": self.set_id or None,
            "set_name": self.set_name,
            "number": self.number,
            "rarity": self.rarity,
            "supertype": self.supertype,
            "subtypes": self.subtypes,
            "artist": self.artist,
            "image_small": self.image_small,
            "image_large": self.image_large,
            "tcgplayer_url": self.tcgplayer_url,
            "cardmarket_url": self.cardmarket_url,
        }


@dataclass
class PriceQuote:
    """One price observation for a card printing on one marketplace.

    ``variant`` is the printing being priced (normal, holofoil,
    reverseHolofoil, ...). ``low`` is the cheapest live listing, which is what
    you can actually buy at; ``market`` is what it has been selling for, which
    is what you can realistically sell at. The gap between them is where a lot
    of single-card flips live.
    """

    card_id: str
    source: str
    variant: str
    market: float | None = None
    low: float | None = None
    mid: float | None = None
    high: float | None = None
    direct_low: float | None = None
    currency: str = "USD"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return any(v is not None and v > 0 for v in (self.market, self.mid, self.low))

    def reference_price(self) -> float | None:
        """Best available estimate of what the card is worth right now."""
        for value in (self.market, self.mid, self.low, self.high):
            if value is not None and value > 0:
                return float(value)
        return None

    def as_row(self, captured_at: str, captured_on: str) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "source": self.source,
            "variant": self.variant,
            "captured_at": captured_at,
            "captured_on": captured_on,
            "currency": self.currency,
            "market": self.market,
            "low": self.low,
            "mid": self.mid,
            "high": self.high,
            "direct_low": self.direct_low,
        }


@runtime_checkable
class PriceProvider(Protocol):
    """What every price source must implement."""

    name: str

    def search_cards(self, query: str, limit: int = 50) -> list[CardRecord]:
        """Find cards by free-text name/number query."""

    def cards_in_set(self, set_id: str) -> list[CardRecord]:
        """Every card in one set."""

    def fetch_quotes(self, card_ids: Iterable[str]) -> tuple[list[CardRecord], list[PriceQuote]]:
        """Current prices for the given cards, plus refreshed catalog metadata."""

    def list_sets(self) -> list[dict[str, Any]]:
        """Every set the source knows about."""

    def close(self) -> None:
        """Release any held connections."""
