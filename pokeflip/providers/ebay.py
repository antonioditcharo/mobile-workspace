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
from dataclasses import dataclass
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

# --- listing classification --------------------------------------------
#
# A stored quote is meant to represent *one raw, English, near-mint copy of
# this exact printing*, because everything downstream assumes it: condition
# multipliers discount from a near-mint baseline, and the signal engine
# compares a card against its own history. Anything else in the series is not
# noise, it is a different asset, and it moves the number in ways no amount of
# statistics will recover from.
#
# Every pattern below is word-boundary anchored. Pokemon names are full of
# traps - "Lotad" contains "lot", "Slowking" contains "slow" - and substring
# matching silently discards real listings.

_GRADERS = r"psa|bgs|beckett|cgc|sgc|ace|tag|ags|gma|hga"

# Grading terms mapped to the variant they are tracked under. Only the grades
# with a liquid market get their own series; everything else slabbed is simply
# excluded from the raw price.
GRADE_PATTERNS = [
    (re.compile(r"\bpsa\s*\.?\s*10\b", re.I), "psa10"),
    (re.compile(r"\bpsa\s*\.?\s*9(?!\.)\b", re.I), "psa9"),
    (re.compile(r"\b(?:bgs|beckett)\s*\.?\s*9\.5\b", re.I), "bgs95"),
    (re.compile(r"\b(?:bgs|beckett)\s*\.?\s*10\b", re.I), "bgs10"),
    (re.compile(r"\bcgc\s*\.?\s*10\b", re.I), "cgc10"),
    (re.compile(r"\bcgc\s*\.?\s*9\.5\b", re.I), "cgc95"),
]

# Phrases that mention grading while selling a *raw* card. Without these,
# "PSA 10 READY" - one of the most common raw-card marketing phrases there is -
# gets thrown away as if it were a slab.
_RAW_MARKETING = re.compile(
    r"\b(?:"
    r"ready|worthy|candidate|potential|contender|hopeful|bound|quality|grade[sd]?\s+well|"
    r"would\s+grade|for\s+grading|pre[\s-]?grade|ungraded|un[\s-]?slabbed|raw"
    r")\b",
    re.I,
)
_UNGRADED = re.compile(r"\b(?:ungraded|not\s+graded|no\s+grade|raw)\b", re.I)

# Online redemption codes. They sell for pennies and share every keyword with
# the card they depict, so they are the single most destructive thing that can
# get into a price series.
_CODE_CARD = re.compile(
    r"\b(?:code\s*cards?|online\s*codes?|ptcgo|ptcgl|redeem(?:able)?\s*codes?|"
    r"digital\s*cards?|qr\s*codes?|unused\s*codes?)\b", re.I)

# Sealed product is a different market from singles.
_SEALED = re.compile(
    r"\b(?:sealed|booster\s*(?:box|pack|bundle)?|boosters|elite\s*trainer|\betb\b|"
    r"blister|display\s*box|case|tin|collection\s*box|premium\s*collection|"
    r"build\s*(?:&|and)\s*battle|theme\s*deck|starter\s*deck)\b", re.I)
# "Pack fresh" and friends describe a raw single, not a sealed pack.
_SEALED_EXEMPT = re.compile(r"\b(?:pack\s*fresh|fresh\s*(?:out\s*of\s*)?pack|"
                            r"straight\s*from\s*(?:the\s*)?pack)\b", re.I)

# More than one card.
_MULTIPLE = re.compile(
    r"\b(?:"
    r"lots?|job\s*lot|bundle[sd]?|bulk|playset|repack|mystery|grab\s*bag|"
    r"binder|collection|complete\s*set|master\s*set|full\s*set|"
    r"pick\s*(?:your|a|any)|choose\s*(?:your|any)|you\s*pick|your\s*choice|"
    r"random|assorted|mixed"
    r")\b", re.I)
# Quantities of two or more: "4x", "x4", "(4)", "5 cards". A leading "1x" is a
# single card and must not be caught.
_QUANTITY = re.compile(
    r"(?:\b(?:[2-9]|\d{2,})\s*x\b)"          # 4x, 10 x
    r"|(?:\bx\s*(?:[2-9]|\d{2,})\b)"          # x4, x10
    r"|(?:\b(?:[2-9]|\d{2,})\s+cards?\b)"     # 5 cards
    r"|(?:\((?:[2-9]|\d{2,})\)\s*(?:cards?|pcs)?)",
    re.I)

_NOT_GENUINE = re.compile(
    r"\b(?:proxy|proxies|custom|fake|counterfeit|orica|replica|reprint\s*card|"
    r"art\s*card|fan\s*made|not\s*(?:a\s*)?real|novelty|sticker)\b", re.I)

# A Japanese Charizard is not an English Charizard, and neither is its price.
_FOREIGN = re.compile(
    r"\b(?:japanese|japan|jpn|jp\s*ver|korean|korea|chinese|china|"
    r"german|french|italian|spanish|portuguese|dutch|thai|indonesian|russian|"
    r"traditional\s*chinese|simplified\s*chinese)\b", re.I)
_ENGLISH = re.compile(r"\benglish\b", re.I)
# "Ships from Japan" describes the seller, not the card. Plenty of English
# singles are posted from Japan, and dropping them loses real listings.
_ORIGIN_BEFORE = re.compile(
    r"\b(?:from|ships?|shipped|shipping|posted|sent|seller|located|based|"
    r"direct|import(?:ed)?)\b[\s\w]{0,12}$", re.I)
# The origin word can also follow the country: "Japan seller", "Japan post".
_ORIGIN_AFTER = re.compile(
    r"^[\s,\-]{0,3}\b(?:seller|sellers|post|postage|shipping|ships?|import|"
    r"based|direct|warehouse)\b", re.I)

# Quotes are a near-mint baseline; a beaten copy in the series makes every
# condition multiplier downstream wrong.
_DAMAGED = re.compile(
    r"\b(?:damaged?|dmg|heavily\s*played|moderately\s*played|poor|played\s*condition|"
    r"crease[sd]?|creasing|bent|water\s*damage|scratched|whitening|"
    r"as\s*is|for\s*parts)\b", re.I)
_MISPRINT = re.compile(r"\b(?:misprint|error\s*card|miscut|mis[\s-]?cut)\b", re.I)

# Printing variants. 1st edition and shadowless are separate assets that can
# trade at many multiples of the unlimited print.
_FIRST_EDITION = re.compile(r"\b(?:1st|first)\s*\.?\s*(?:ed|edition)\b", re.I)
_SHADOWLESS = re.compile(r"\bshadowless\b", re.I)
_REVERSE = re.compile(r"\b(?:reverse|rev)\s*\.?\s*(?:holo(?:foil)?|foil)?\b", re.I)
_NON_HOLO = re.compile(r"\bnon[\s-]?holo(?:foil)?\b", re.I)
_HOLO = re.compile(r"\bholo(?:foil|graphic)?\b|\bfoil\b", re.I)

# Rarities and card types that are foil whether or not the seller says so.
# Without this, "Snorlax 143/165 Illustration Rare" lands in a phantom
# "normal" series that never lines up with the same card's real one.
_IMPLIES_HOLO = re.compile(
    r"\b(?:"
    r"illustration\s*rare|special\s*illustration|secret\s*rare|ultra\s*rare|"
    r"hyper\s*rare|double\s*rare|rainbow\s*rare|shiny\s*rare|radiant|amazing\s*rare|"
    r"trainer\s*gallery|ace\s*spec|alt(?:ernate)?\s*art|full\s*art|gold\s*(?:card|rare)|"
    r"\bsir\b|\bir\b|\bvmax\b|\bvstar\b|\bv-?union\b|\bgx\b|\bex\b|\bv\b|"
    r"break|legend|prime|lv\.?\s*x|star\b"
    r")\b", re.I)

# The same idea from the catalog side, which is authoritative when the title
# says nothing.
_RARITY_IMPLIES_HOLO = re.compile(
    r"holo|ultra|secret|rainbow|illustration|shiny|hyper|double|radiant|"
    r"amazing|break|legend|prime|gold|ace\s*spec|star|promo", re.I)


def variant_for_rarity(rarity: str | None) -> str:
    """The printing a rarity implies, for when the listing title is silent."""
    if rarity and _RARITY_IMPLIES_HOLO.search(rarity):
        return "holofoil"
    return "normal"


@dataclass
class ListingVerdict:
    """What a listing is, or why it was thrown out.

    The reason matters as much as the decision: it is what makes the filters
    tunable against real listings rather than a black box.
    """

    variant: str | None
    reason: str = ""

    @property
    def kept(self) -> bool:
        return self.variant is not None


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
        except httpx.HTTPError as exc:
            # Never reached eBay at all - a proxy, DNS or offline problem.
            raise ProviderError(f"could not reach {self._client.base_url}: {exc}"
                                ) from exc

        if response.status_code in (400, 401):
            # eBay's own rejection: the keyset is wrong for this environment.
            detail = _error_detail(response)
            raise ProviderError(f"eBay rejected the credentials ({detail})")
        if response.status_code == 403:
            raise ProviderError(
                "403 from the token endpoint. eBay normally answers 400 or 401 "
                "for a bad keyset, so this is more likely a proxy or firewall "
                "between you and api.ebay.com"
            )
        try:
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

    def _classify(self, title: str, card: CardRecord) -> ListingVerdict:
        """Classify one listing in the context of the card being priced."""
        return classify(
            title,
            track_graded=self.settings.ebay_track_graded,
            english_only=self.settings.ebay_english_only,
            exclude_damaged=self.settings.ebay_exclude_damaged,
            extra_exclusions=self.settings.ebay_exclude_terms,
            # The catalog knows the rarity, so a foil card stays foil even when
            # the seller never types "holo".
            default_printing=variant_for_rarity(card.rarity),
        )

    def _quotes_for(self, card: CardRecord) -> list[PriceQuote]:
        query = build_query(card)
        listings = self._active_listings(query)
        sold = self._sold_items(query) if self._sold_available else []

        groups: dict[str, dict[str, list[Any]]] = {}
        for item in listings:
            verdict = self._classify(item.get("title", ""), card)
            if not verdict.kept:
                continue
            groups.setdefault(verdict.variant,
                              {"listings": [], "sold": []})["listings"].append(item)
        for item in sold:
            verdict = self._classify(item.get("title", ""), card)
            if not verdict.kept:
                continue
            groups.setdefault(verdict.variant,
                              {"listings": [], "sold": []})["sold"].append(item)

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

    # --- diagnostics ----------------------------------------------------

    def check_credentials(self) -> dict[str, Any]:
        """Prove the credentials work, and say precisely what does not.

        eBay's failures look alike from the outside - a bad secret, an app
        without the right scope, and a missing Marketplace Insights grant all
        surface as a 4xx. This separates them so the fix is obvious.
        """
        report: dict[str, Any] = {
            "marketplace": self.settings.ebay_marketplace,
            "environment": "sandbox" if self.settings.ebay_sandbox else "production",
            "credentials": False,
            "browse": False,
            "sold_data": False,
            "problems": [],
            "notes": [],
        }

        if not (self.settings.ebay_client_id and self.settings.ebay_client_secret):
            report["problems"].append(
                "No credentials. Set provider.ebay_client_id and "
                "provider.ebay_client_secret from https://developer.ebay.com "
                "(Application Keys -> the Production keyset)."
            )
            return report

        try:
            self._access_token()
            report["credentials"] = True
        except ProviderError as exc:
            message = str(exc)
            if "rejected the credentials" in message:
                fix = ("Check the client id and secret, and that you copied the "
                       "Production keyset rather than the Sandbox one "
                       "(or set provider.ebay_sandbox).")
            elif "could not reach" in message or "proxy or firewall" in message:
                fix = ("This looks like a network problem rather than a bad key. "
                       "Confirm this machine can reach api.ebay.com.")
            else:
                fix = "Retry; if it persists, check developer.ebay.com for outages."
            report["problems"].append(f"{message}. {fix}")
            return report

        probe = "charizard pokemon card"
        try:
            listings = self._active_listings(probe)
            report["browse"] = True
            report["listings_seen"] = len(listings)
            if not listings:
                report["notes"].append(
                    "Browse works but returned nothing for a broad query, which "
                    "usually means the category filter is wrong for this "
                    "marketplace. Try clearing provider.ebay_category_id."
                )
        except ProviderError as exc:
            report["problems"].append(
                f"Browse API refused: {exc}. The keyset is valid but the app may "
                "not be subscribed to the Browse API."
            )
            return report

        if not self.settings.ebay_use_sold_data:
            report["notes"].append(
                "Sold data is switched off (provider.ebay_use_sold_data), so "
                "prices come from asking prices and velocity is unavailable."
            )
            return report

        try:
            sold = self._sold_items(probe)
            # _sold_items swallows the refusal and flips the flag, so read that
            # rather than trusting an empty list.
            if self._sold_available:
                report["sold_data"] = True
                report["sold_seen"] = len(sold)
            else:
                report["problems"].append(
                    "Marketplace Insights refused. That API needs a separate "
                    "grant from eBay - apply at developer.ebay.com. Until then "
                    "prices come from the lower quartile of asking prices and "
                    "velocity is reported as unknown."
                )
        except ProviderError as exc:
            report["problems"].append(f"Marketplace Insights failed: {exc}")

        return report

    def probe(self, card: CardRecord) -> dict[str, Any]:
        """Show what eBay returns for one card, and how it was classified.

        The search query and the lot/grade filters are heuristics that will need
        tuning against real listings. This makes that tuning possible instead of
        guesswork.
        """
        query = build_query(card)
        listings = self._active_listings(query)
        sold = self._sold_items(query) if self._sold_available else []

        def sort_items(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
            buckets: dict[str, list[dict[str, Any]]] = {}
            # Grouped by reason, because "38 rejected" tells you nothing and
            # "31 rejected as lots, 7 as graded" tells you what to tune.
            rejected: dict[str, list[str]] = {}
            for item in items:
                title = item.get("title", "")
                verdict = self._classify(title, card)
                if not verdict.kept:
                    rejected.setdefault(verdict.reason, []).append(title)
                    continue
                buckets.setdefault(verdict.variant, []).append(
                    {"title": title, "price": _price_of(item)})
            return {
                "kept": buckets,
                "rejected": rejected,
                "rejected_count": sum(len(v) for v in rejected.values()),
            }

        classified_listings = sort_items(listings)
        classified_sold = sort_items(sold)
        quotes = self._quotes_for(card)

        return {
            "card_id": card.id,
            "card_name": card.name,
            "rarity": card.rarity,
            "assumed_printing": variant_for_rarity(card.rarity),
            "query": query,
            "listings_found": len(listings),
            "sold_found": len(sold),
            "sold_available": self._sold_available,
            "listings": classified_listings,
            "sold": classified_sold,
            "quotes": [
                {
                    "variant": q.variant,
                    "market": q.market,
                    "low": q.low,
                    "high": q.high,
                    "sales_count": q.sales_count,
                    "listing_count": q.listing_count,
                    "basis": q.extra.get("price_basis"),
                }
                for q in quotes
            ],
        }

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


def classify(title: str, track_graded: bool = False, english_only: bool = True,
             exclude_damaged: bool = True, extra_exclusions: Sequence[str] = (),
             default_printing: str = "normal") -> ListingVerdict:
    """Decide what a listing is selling, and say why when it is rejected.

    Rejections are ordered cheapest-and-most-certain first, so the reported
    reason is the most specific one that applies.
    """
    if not title.strip():
        return ListingVerdict(None, "empty title")

    for term in extra_exclusions:
        # User-supplied terms, matched whole-word so a configured "lot" cannot
        # take out every Lotad listing.
        if term and re.search(rf"\b{re.escape(term)}\b", title, re.I):
            return ListingVerdict(None, f"excluded term: {term}")

    if _CODE_CARD.search(title):
        return ListingVerdict(None, "online code card")
    if _NOT_GENUINE.search(title):
        return ListingVerdict(None, "proxy or custom")
    if _SEALED.search(title) and not _SEALED_EXEMPT.search(title):
        return ListingVerdict(None, "sealed product")
    if _MULTIPLE.search(title):
        return ListingVerdict(None, "multiple cards")
    if _QUANTITY.search(title):
        return ListingVerdict(None, "quantity above one")
    if _MISPRINT.search(title):
        return ListingVerdict(None, "misprint or error card")
    if english_only and _is_foreign_printing(title):
        return ListingVerdict(None, "not an English printing")

    graded = _grade_of(title)
    if graded is not None:
        if not track_graded:
            return ListingVerdict(None, f"graded ({graded})")
        return ListingVerdict(graded)

    if exclude_damaged and _DAMAGED.search(title):
        # Quotes are a near-mint baseline; condition is applied downstream.
        return ListingVerdict(None, "below near-mint condition")

    return ListingVerdict(_printing_of(title, default_printing))


def _is_foreign_printing(title: str) -> bool:
    """Is the card itself foreign, or is that just where it ships from?

    "Japanese Charizard" is a different asset. "Charizard, free shipping from
    Japan" is the card you wanted, posted by someone abroad.
    """
    if _ENGLISH.search(title):
        return False
    matches = list(_FOREIGN.finditer(title))
    if not matches:
        return False
    # Foreign only if at least one mention describes the card rather than the
    # postage. The origin word can sit on either side: "from Japan",
    # "Japan seller".
    def is_origin(match: re.Match) -> bool:
        return bool(_ORIGIN_BEFORE.search(title[:match.start()])
                    or _ORIGIN_AFTER.search(title[match.end():]))

    return any(not is_origin(match) for match in matches)


def _grade_of(title: str) -> str | None:
    """The slab grade, or ``None`` for a raw card.

    Grading words appear constantly on raw listings - "PSA 10 READY",
    "gem mint candidate" - so a mention is not enough on its own.
    """
    if _UNGRADED.search(title):
        return None

    for pattern, variant in GRADE_PATTERNS:
        match = pattern.search(title)
        if match and not _looks_like_marketing(title, match):
            return variant

    # Slabbed by someone, at a grade with no market of its own.
    other = re.search(rf"\b(?:{_GRADERS})\s*\.?\s*(?:10|[1-9](?:\.5)?)\b",
                      title, re.I)
    if other and not _looks_like_marketing(title, other):
        return "graded_other"

    generic = re.search(r"\b(?:graded|slab(?:bed)?|encapsulated|"
                        r"gem\s*(?:mt|mint)\s*10)\b", title, re.I)
    if generic and not _looks_like_marketing(title, generic):
        return "graded_other"
    return None


def _looks_like_marketing(title: str, match: re.Match) -> bool:
    """Is this grade mention selling a raw card rather than a slab?

    Checked in a window around the mention: "PSA 10 READY" is raw, but
    "PSA 10 - ready to ship" is a slab, and only proximity tells them apart.
    """
    window = title[max(0, match.start() - 30):match.end() + 30]
    if re.search(r"\bready\s+to\s+(?:ship|post|send)\b", window, re.I):
        return False
    return bool(_RAW_MARKETING.search(window))


def _printing_of(title: str, default: str = "normal") -> str:
    """Which printing of the card this is.

    Naming follows TCGplayer's variants so an eBay series and a pokemontcg
    series for the same printing line up - which only works if a card that is
    foil by definition is recognised as foil even when the title never says so.

    ``default`` is what the card's own rarity implies, used when the title
    gives no signal at all.
    """
    if _REVERSE.search(title):
        return "reverseHolofoil"

    if _NON_HOLO.search(title):
        holo = False                      # the seller said so explicitly
    elif _HOLO.search(title) or _IMPLIES_HOLO.search(title):
        holo = True
    else:
        holo = default.startswith(("holo", "1stEditionHolo", "shadowlessHolo"))

    if _FIRST_EDITION.search(title):
        return "1stEditionHolofoil" if holo else "1stEditionNormal"
    if _SHADOWLESS.search(title):
        return "shadowlessHolofoil" if holo else "shadowlessNormal"
    return "holofoil" if holo else "normal"


def classify_variant(title: str, track_graded: bool = False) -> str | None:
    """Backwards-compatible shim returning just the variant."""
    return classify(title, track_graded).variant


def _error_detail(response: httpx.Response) -> str:
    """eBay puts the useful part in the body, not the status line."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    for key in ("error_description", "error", "message"):
        if payload.get(key):
            return f"HTTP {response.status_code}: {payload[key]}"
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        return (f"HTTP {response.status_code}: "
                f"{first.get('message') or first.get('longMessage') or first}")
    return f"HTTP {response.status_code}"


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
