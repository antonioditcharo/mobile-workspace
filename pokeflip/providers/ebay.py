"""eBay price provider - actual transactions, not marketplace averages.

This is the only source here that can answer *how fast a card sells*, which is
the difference between "cheap" and "cheap because nobody wants it".

Two eBay APIs are used:

* **Browse** gives live listings. Every developer account can call it. It
  supplies the listing floor and how many copies are up right now.
* **Marketplace Insights** gives the last 90 days of *sold* items. It needs
  separate approval from eBay. With it, ``market`` is what copies actually sold
  for and ``sales_count`` is a real velocity figure. Without it the provider
  still works from listings alone, and says so rather than quietly presenting
  asking prices as sale prices.

eBay is a marketplace, not a card database, so it cannot answer catalog
questions. Keep ``pokemontcg`` as the catalog provider and point
``provider.name`` here for prices.
"""

from __future__ import annotations

import base64
import logging
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

import httpx

from ..config import Config
from .base import CardRecord, PriceQuote, ProviderError

log = logging.getLogger("pokeflip.ebay")

PRODUCTION = "https://api.ebay.com"
SANDBOX = "https://api.sandbox.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# Sold-data window normalised for analytics. eBay returns 90 days; the trailing
# 30-day slice is what gets stored as ``sales_count``.
SALES_WINDOW_DAYS = 30

# Grading terms mapped to the variant they are tracked under.
GRADE_PATTERNS = [
    (re.compile(r"\bpsa\s*10\b", re.I), "psa10"),
    (re.compile(r"\bpsa\s*9\b", re.I), "psa9"),
    (re.compile(r"\b(bgs|beckett)\s*9\.5\b", re.I), "bgs95"),
    (re.compile(r"\bcgc\s*10\b", re.I), "cgc10"),
]


class EbayProvider:
    name = "ebay"
    provides_catalog = False

    def __init__(self, config: Config,
                 catalog: Callable[[str], CardRecord | None] | None = None):
        self.config = config
        self.settings = config.provider
        self._catalog = catalog
        self._token: str | None = None
        self._token_expires = 0.0
        base = SANDBOX if self.settings.ebay_sandbox else PRODUCTION
        self._client = httpx.Client(
            base_url=base,
            timeout=self.settings.timeout_seconds,
            headers={"User-Agent": "pokeflip/1.0"},
        )
        self._last_call = 0.0
        # Flips to False the first time eBay refuses the Insights call, so a
        # missing approval costs one request rather than one per card.
        self._sold_available = self.settings.ebay_use_sold_data

    # --- auth -----------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        if not (self.settings.ebay_client_id and self.settings.ebay_client_secret):
            raise ProviderError(
                "eBay needs provider.ebay_client_id and provider.ebay_client_secret; "
                "create an application key at https://developer.ebay.com"
            )
        credentials = base64.b64encode(
            f"{self.settings.ebay_client_id}:{self.settings.ebay_client_secret}".encode()
        ).decode()
        try:
            response = self._client.post(
                "/identity/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": SCOPE},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"eBay token request failed: {exc}") from exc

        self._token = payload.get("access_token")
        if not self._token:
            raise ProviderError("eBay returned no access token")
        self._token_expires = time.time() + float(payload.get("expires_in", 7200))
        return self._token

    # --- HTTP -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        delay = self.settings.request_delay
        elapsed = time.monotonic() - self._last_call
        if delay > 0 and elapsed < delay:
            time.sleep(delay - elapsed)

        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace,
        }
        last_error: Exception | None = None
        for attempt in range(max(1, self.settings.max_retries)):
            try:
                response = self._client.get(path, params=params, headers=headers)
                self._last_call = time.monotonic()
                if response.status_code == 429:
                    time.sleep(2 ** attempt)
                    last_error = ProviderError("rate limited by eBay")
                    continue
                if response.status_code in (401, 403):
                    # Token may simply have expired; a fresh one is cheap.
                    self._token = None
                    if attempt == 0:
                        continue
                    raise ProviderError(
                        f"eBay refused {path} ({response.status_code}). For sold "
                        "data, Marketplace Insights access must be granted to your "
                        "application by eBay."
                    )
                response.raise_for_status()
                return response.json()
            except ProviderError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < max(1, self.settings.max_retries):
                    time.sleep(2 ** attempt)
        raise ProviderError(f"eBay GET {path} failed: {last_error}")

    # --- catalog (not supported) ----------------------------------------

    def _unsupported(self, what: str):
        raise ProviderError(
            f"eBay cannot {what}: it is a marketplace, not a card catalog. "
            "Leave provider.catalog_name as 'pokemontcg' and use eBay for prices."
        )

    def search_cards(self, query: str, limit: int = 50) -> list[CardRecord]:
        self._unsupported("search the card catalog")
        return []

    def cards_in_set(self, set_id: str) -> list[CardRecord]:
        self._unsupported("list a set")
        return []

    def list_sets(self) -> list[dict[str, Any]]:
        self._unsupported("list sets")
        return []

    # --- prices ---------------------------------------------------------

    def fetch_quotes(self, card_ids: Iterable[str]
                     ) -> tuple[list[CardRecord], list[PriceQuote]]:
        if self._catalog is None:
            raise ProviderError(
                "the eBay provider needs the local catalog to know what to search "
                "for; sync cards with the pokemontcg provider first"
            )
        quotes: list[PriceQuote] = []
        for card_id in dict.fromkeys(card_ids):
            card = self._catalog(card_id)
            if card is None:
                log.debug("no catalog entry for %s, skipping", card_id)
                continue
            try:
                quotes.extend(self._quotes_for(card))
            except ProviderError as exc:
                log.warning("eBay lookup failed for %s: %s", card_id, exc)
        # eBay never refreshes catalog metadata, so nothing is returned here.
        return [], quotes

    def _quotes_for(self, card: CardRecord) -> list[PriceQuote]:
        query = build_query(card)
        listings = self._active_listings(query)
        sold = self._sold_items(query) if self._sold_available else []

        groups: dict[str, dict[str, list[Any]]] = {}
        for item in listings:
            variant = classify_variant(item.get("title", ""),
                                       self.settings.ebay_track_graded)
            if variant is None:
                continue
            groups.setdefault(variant, {"listings": [], "sold": []})["listings"].append(item)
        for item in sold:
            variant = classify_variant(item.get("title", ""),
                                       self.settings.ebay_track_graded)
            if variant is None:
                continue
            groups.setdefault(variant, {"listings": [], "sold": []})["sold"].append(item)

        out: list[PriceQuote] = []
        for variant, bucket in groups.items():
            quote = self._build_quote(card.id, variant, bucket["listings"],
                                      bucket["sold"])
            if quote and quote.usable:
                out.append(quote)
        return out

    def _build_quote(self, card_id: str, variant: str, listings: Sequence[dict[str, Any]],
                     sold: Sequence[dict[str, Any]]) -> PriceQuote | None:
        ask_prices = sorted(p for p in (_price_of(i) for i in listings) if p)
        sold_prices = [p for p in (_price_of(i) for i in sold) if p]

        if sold_prices:
            # What copies actually changed hands for.
            market = statistics.median(sold_prices)
            recent = _recent_sales(sold, SALES_WINDOW_DAYS)
            sales_count: int | None = len(recent)
        elif ask_prices:
            # Asking prices are not sale prices. The median ask overstates the
            # market, so the lower quartile is used and the absence of sold data
            # is recorded rather than hidden.
            market = _quantile(ask_prices, 0.25)
            sales_count = None
        else:
            return None

        return PriceQuote(
            card_id=card_id,
            source="ebay",
            variant=variant,
            currency="USD",
            market=round(market, 2),
            low=round(ask_prices[0], 2) if ask_prices else None,
            mid=round(statistics.median(ask_prices), 2) if ask_prices else None,
            high=round(ask_prices[-1], 2) if ask_prices else None,
            direct_low=None,
            sales_count=sales_count,
            listing_count=len(ask_prices) or None,
            extra={
                "sold_sample": len(sold_prices),
                "price_basis": "sold_median" if sold_prices else "listing_p25",
            },
        )

    def _active_listings(self, query: str) -> list[dict[str, Any]]:
        params = {
            "q": query,
            "limit": min(200, self.settings.ebay_max_results),
            "filter": ",".join([
                "buyingOptions:{FIXED_PRICE}",
                "itemLocationCountry:US",
            ]),
        }
        if self.settings.ebay_category_id:
            params["category_ids"] = self.settings.ebay_category_id
        payload = self._get("/buy/browse/v1/item_summary/search", params)
        return payload.get("itemSummaries") or []

    def _sold_items(self, query: str) -> list[dict[str, Any]]:
        params = {
            "q": query,
            "limit": min(200, self.settings.ebay_max_results),
            "filter": "buyingOptions:{FIXED_PRICE}",
        }
        if self.settings.ebay_category_id:
            params["category_ids"] = self.settings.ebay_category_id
        try:
            payload = self._get(
                "/buy/marketplace_insights/v1_beta/item_sales/search", params)
        except ProviderError as exc:
            # One refusal is enough: stop asking for the rest of the run.
            log.warning("eBay sold data unavailable, falling back to listings: %s", exc)
            self._sold_available = False
            return []
        return payload.get("itemSales") or []

    def close(self) -> None:
        self._client.close()


# --- helpers ------------------------------------------------------------


def build_query(card: CardRecord) -> str:
    """Search text specific enough to find the card and little else."""
    parts = [card.name]
    if card.number:
        parts.append(card.number.split("/")[0])
    if card.set_name:
        parts.append(card.set_name)
    parts.append("pokemon")
    return " ".join(part for part in parts if part)


def classify_variant(title: str, track_graded: bool) -> str | None:
    """Work out what a listing is actually selling.

    Returns ``None`` for anything that is not a single raw card - lots,
    bundles, proxies - so a 100-card lot never sets the price of one card.
    """
    lowered = title.lower()
    for term in ("lot of", "bundle", "proxy", "custom", "fake", "orica",
                 "playset", "bulk", "repack", "mystery"):
        if term in lowered:
            return None

    for pattern, variant in GRADE_PATTERNS:
        if pattern.search(title):
            return variant if track_graded else None
    # Any other graded card is excluded: a slab is a different asset and its
    # price would badly distort a raw-card series.
    if re.search(r"\b(psa|bgs|cgc|sgc|beckett)\s*\d", lowered):
        return None

    if "reverse holo" in lowered or "reverse foil" in lowered:
        return "reverseHolofoil"
    if "holo" in lowered or "foil" in lowered:
        return "holofoil"
    return "normal"


def _price_of(item: dict[str, Any]) -> float | None:
    for key in ("price", "lastSoldPrice", "currentBidPrice"):
        block = item.get(key)
        if isinstance(block, dict):
            try:
                value = float(block.get("value"))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def _recent_sales(sold: Sequence[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    """Sales inside the trailing window, so velocity is comparable day to day."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for item in sold:
        stamp = item.get("lastSoldDate") or item.get("soldDate")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            out.append(item)
    return out


def _quantile(ordered: Sequence[float], q: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)
