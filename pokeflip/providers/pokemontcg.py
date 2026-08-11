"""Pokemon TCG API v2 provider.

Reads https://api.pokemontcg.io/v2, which carries both TCGplayer (USD) and
Cardmarket (EUR) price blocks on each card. An API key is optional but raises
the rate limit considerably - set ``provider.api_key`` if you have one.

TCGplayer publishes prices per printing (``holofoil``, ``reverseHolofoil``, ...)
so each printing becomes its own tracked variant. Cardmarket publishes a single
aggregate block per card, stored under the ``default`` variant in EUR.
"""

from __future__ import annotations

import time
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from ..config import Config
from .base import CardRecord, PriceQuote, ProviderError

# TCGplayer keys that are prices rather than metadata.
_TCG_FIELDS = ("low", "mid", "high", "market", "directLow")


class PokemonTcgProvider:
    name = "pokemontcg"

    provides_catalog = True

    def __init__(self, config: Config, catalog: Any = None):
        self.config = config
        self.settings = config.provider
        headers = {"User-Agent": "pokeflip/1.0"}
        if self.settings.api_key:
            headers["X-Api-Key"] = self.settings.api_key
        self._client = httpx.Client(
            base_url=self.settings.base_url.rstrip("/"),
            headers=headers,
            timeout=self.settings.timeout_seconds,
        )
        self._last_call = 0.0

    # --- HTTP -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = self.settings.request_delay
        elapsed = time.monotonic() - self._last_call
        if delay > 0 and elapsed < delay:
            time.sleep(delay - elapsed)

        last_error: Exception | None = None
        for attempt in range(max(1, self.settings.max_retries)):
            try:
                response = self._client.get(path, params=params)
                self._last_call = time.monotonic()
                if response.status_code == 429:
                    # Rate limited: back off and try again rather than losing the page.
                    time.sleep(2 ** attempt)
                    last_error = ProviderError("rate limited by pokemontcg.io")
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < max(1, self.settings.max_retries):
                    time.sleep(2 ** attempt)
        raise ProviderError(f"GET {path} failed: {last_error}") from last_error

    def _paged(self, path: str, params: dict[str, Any], cap: int | None = None
               ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        page_size = self.settings.page_size
        while True:
            payload = self._get(path, {**params, "page": page, "pageSize": page_size})
            batch = payload.get("data") or []
            out.extend(batch)
            total = payload.get("totalCount")
            if cap is not None and len(out) >= cap:
                return out[:cap]
            if len(batch) < page_size:
                return out
            if isinstance(total, int) and len(out) >= total:
                return out
            page += 1

    # --- catalog --------------------------------------------------------

    def search_cards(self, query: str, limit: int = 50) -> list[CardRecord]:
        term = query.strip()
        if not term:
            return []
        # An exact id lookup ("sv3pt5-25") should not be treated as a name search.
        if "-" in term and " " not in term:
            try:
                payload = self._get(f"/cards/{quote(term)}")
                data = payload.get("data")
                if data:
                    return [_to_card(data)]
            except ProviderError:
                pass
        raw = self._paged("/cards", {"q": _name_query(term), "orderBy": "-set.releaseDate"},
                          cap=limit)
        return [_to_card(item) for item in raw]

    def cards_in_set(self, set_id: str) -> list[CardRecord]:
        raw = self._paged("/cards", {"q": f"set.id:{set_id}", "orderBy": "number"})
        return [_to_card(item) for item in raw]

    def list_sets(self) -> list[dict[str, Any]]:
        raw = self._paged("/sets", {"orderBy": "-releaseDate"})
        return [_to_set(item) for item in raw]

    # --- prices ---------------------------------------------------------

    def fetch_quotes(self, card_ids: Iterable[str]
                     ) -> tuple[list[CardRecord], list[PriceQuote]]:
        ids = [cid for cid in dict.fromkeys(card_ids) if cid]
        cards: list[CardRecord] = []
        quotes: list[PriceQuote] = []
        # The API takes an OR query, so ids are batched rather than fetched one by one.
        for chunk in _chunks(ids, 40):
            query = " OR ".join(f'id:"{cid}"' for cid in chunk)
            raw = self._paged("/cards", {"q": query}, cap=len(chunk))
            for item in raw:
                cards.append(_to_card(item))
                quotes.extend(extract_quotes(item, self.settings.preferred_source))
        return cards, quotes

    def close(self) -> None:
        self._client.close()


# --- parsing ------------------------------------------------------------


def _name_query(term: str) -> str:
    """Build a Lucene-ish query the API understands.

    A bare word becomes a prefix name match; ``name:x set.id:y`` style input is
    passed through so power users keep the full query language.
    """
    if ":" in term:
        return term
    escaped = term.replace('"', '\\"')
    return f'name:"{escaped}*"'


def _to_card(item: dict[str, Any]) -> CardRecord:
    card_set = item.get("set") or {}
    images = item.get("images") or {}
    subtypes = item.get("subtypes") or []
    return CardRecord(
        id=item.get("id", ""),
        name=item.get("name", ""),
        set_id=card_set.get("id", ""),
        set_name=card_set.get("name", ""),
        number=str(item.get("number", "")),
        rarity=item.get("rarity") or "",
        supertype=item.get("supertype") or "",
        subtypes=", ".join(subtypes),
        artist=item.get("artist") or "",
        image_small=images.get("small", ""),
        image_large=images.get("large", ""),
        tcgplayer_url=(item.get("tcgplayer") or {}).get("url", ""),
        cardmarket_url=(item.get("cardmarket") or {}).get("url", ""),
    )


def _to_set(item: dict[str, Any]) -> dict[str, Any]:
    images = item.get("images") or {}
    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "series": item.get("series", ""),
        "printed_total": item.get("printedTotal"),
        "total": item.get("total"),
        "release_date": item.get("releaseDate", ""),
        "symbol_url": images.get("symbol", ""),
        "logo_url": images.get("logo", ""),
    }


def extract_quotes(item: dict[str, Any], preferred: str = "tcgplayer") -> list[PriceQuote]:
    """Pull every price block off a raw API card into normalised quotes.

    Both marketplaces are always captured. ``preferred`` only decides which one
    the signal engine treats as authoritative; having the other on file is what
    makes cross-market comparison possible later.
    """
    card_id = item.get("id", "")
    if not card_id:
        return []
    quotes: list[PriceQuote] = []

    tcg_prices = ((item.get("tcgplayer") or {}).get("prices") or {})
    for variant, block in tcg_prices.items():
        if not isinstance(block, dict):
            continue
        quote_obj = PriceQuote(
            card_id=card_id,
            source="tcgplayer",
            variant=variant,
            currency="USD",
            market=_num(block.get("market")),
            low=_num(block.get("low")),
            mid=_num(block.get("mid")),
            high=_num(block.get("high")),
            direct_low=_num(block.get("directLow")),
            extra={k: block.get(k) for k in _TCG_FIELDS if k in block},
        )
        if quote_obj.usable:
            quotes.append(quote_obj)

    cm = (item.get("cardmarket") or {}).get("prices") or {}
    if isinstance(cm, dict) and cm:
        # Cardmarket reports one aggregate block per card, not per printing.
        quote_obj = PriceQuote(
            card_id=card_id,
            source="cardmarket",
            variant="default",
            currency="EUR",
            market=_num(cm.get("trendPrice")) or _num(cm.get("averageSellPrice")),
            low=_num(cm.get("lowPrice")),
            mid=_num(cm.get("averageSellPrice")),
            high=_num(cm.get("avg30")),
            direct_low=_num(cm.get("lowPriceExPlus")),
            extra={k: cm.get(k) for k in ("avg1", "avg7", "avg30") if k in cm},
        )
        if quote_obj.usable:
            quotes.append(quote_obj)

    if preferred not in {"tcgplayer", "cardmarket"}:
        return quotes
    return quotes


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
