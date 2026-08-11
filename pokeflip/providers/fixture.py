"""Offline provider for demos, tests, and air-gapped environments.

It serves a card catalog from ``provider.fixture_dir`` (falling back to a small
built-in catalog) and synthesises prices from a deterministic price model. The
same card on the same day always produces the same quote, so tests are stable
and a seeded database looks like a real one that has been running for months.

This is what ``pokeflip demo`` runs on. Point ``provider.name`` at
``pokemontcg`` for live data.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import Config
from .base import CardRecord, PriceQuote

# name, set_id, set_name, number, rarity, base USD price, annual drift, volatility, variant
_BUILTIN_CATALOG: list[tuple[str, str, str, str, str, float, float, float, str]] = [
    ("Charizard ex", "sv3pt5", "151", "199", "Special Illustration Rare", 310.0, 0.22, 0.05, "holofoil"),
    ("Charizard", "base1", "Base", "4", "Rare Holo", 268.0, 0.14, 0.06, "holofoil"),
    ("Mew ex", "sv3pt5", "151", "205", "Special Illustration Rare", 148.0, -0.10, 0.07, "holofoil"),
    ("Pikachu", "sv3pt5", "151", "025", "Illustration Rare", 21.5, 0.30, 0.09, "holofoil"),
    ("Blastoise ex", "sv3pt5", "151", "184", "Ultra Rare", 34.0, -0.18, 0.06, "holofoil"),
    ("Umbreon VMAX", "swsh7", "Evolving Skies", "215", "Secret Rare", 402.0, 0.35, 0.08, "holofoil"),
    ("Rayquaza VMAX", "swsh7", "Evolving Skies", "218", "Secret Rare", 118.0, 0.05, 0.07, "holofoil"),
    ("Gengar VMAX", "swsh8", "Fusion Strike", "271", "Secret Rare", 62.0, -0.22, 0.08, "holofoil"),
    ("Lugia V", "swsh12", "Silver Tempest", "186", "Secret Rare", 44.0, 0.12, 0.06, "holofoil"),
    ("Giratina V", "swsh11", "Lost Origin", "186", "Secret Rare", 55.0, -0.05, 0.07, "holofoil"),
    ("Iono", "sv2", "Paldea Evolved", "269", "Special Illustration Rare", 78.0, 0.18, 0.06, "holofoil"),
    ("Miriam", "sv1", "Scarlet & Violet", "251", "Special Illustration Rare", 26.0, -0.02, 0.07, "holofoil"),
    ("Roaring Moon ex", "sv4", "Paradox Rift", "251", "Special Illustration Rare", 91.0, 0.09, 0.06, "holofoil"),
    ("Snorlax", "sv3pt5", "151", "143", "Illustration Rare", 9.4, 0.06, 0.08, "reverseHolofoil"),
    ("Bulbasaur", "sv3pt5", "151", "001", "Common", 1.15, 0.03, 0.10, "normal"),
    ("Squirtle", "sv3pt5", "151", "007", "Common", 0.95, -0.04, 0.11, "normal"),
    ("Eevee", "sv3pt5", "151", "133", "Common", 1.85, 0.08, 0.09, "reverseHolofoil"),
    ("Alakazam ex", "sv3pt5", "151", "201", "Ultra Rare", 42.0, 0.16, 0.06, "holofoil"),
    ("Zapdos", "base1", "Base", "16", "Rare Holo", 74.0, 0.07, 0.07, "holofoil"),
    ("Machamp", "base1", "Base", "8", "Rare Holo", 28.5, -0.12, 0.08, "holofoil"),
    ("Venusaur ex", "sv3pt5", "151", "198", "Special Illustration Rare", 132.0, 0.20, 0.06, "holofoil"),
    ("Mimikyu", "sv4pt5", "Paldean Fates", "97", "Illustration Rare", 15.0, 0.25, 0.09, "holofoil"),
    ("Greninja ex", "sv6pt5", "Shrouded Fable", "170", "Special Illustration Rare", 168.0, 0.28, 0.07, "holofoil"),
    ("Pidgeot ex", "sv3pt5", "151", "225", "Ultra Rare", 38.0, -0.15, 0.06, "holofoil"),
    ("Nidoking", "base1", "Base", "11", "Rare Holo", 22.0, 0.02, 0.09, "holofoil"),
]

_SET_META = {
    "sv3pt5": ("151", "Scarlet & Violet", 165, 207, "2023/09/22"),
    "base1": ("Base", "Base", 102, 102, "1999/01/09"),
    "swsh7": ("Evolving Skies", "Sword & Shield", 203, 237, "2021/08/27"),
    "swsh8": ("Fusion Strike", "Sword & Shield", 264, 284, "2021/11/12"),
    "swsh11": ("Lost Origin", "Sword & Shield", 196, 217, "2022/09/09"),
    "swsh12": ("Silver Tempest", "Sword & Shield", 195, 215, "2022/11/11"),
    "sv1": ("Scarlet & Violet", "Scarlet & Violet", 198, 258, "2023/03/31"),
    "sv2": ("Paldea Evolved", "Scarlet & Violet", 193, 279, "2023/06/09"),
    "sv4": ("Paradox Rift", "Scarlet & Violet", 182, 266, "2023/11/03"),
    "sv4pt5": ("Paldean Fates", "Scarlet & Violet", 91, 245, "2024/01/26"),
    "sv6pt5": ("Shrouded Fable", "Scarlet & Violet", 64, 99, "2024/08/02"),
}

_EPOCH = date(2024, 1, 1)


class FixtureProvider:
    """Deterministic offline price source."""

    name = "fixture"

    provides_catalog = True

    def __init__(self, config: Config, catalog: Any = None):
        self.config = config
        self.settings = config.provider
        self._catalog: dict[str, dict[str, Any]] = {}
        self._load_catalog()

    # --- catalog --------------------------------------------------------

    def _load_catalog(self) -> None:
        path = Path(self.settings.fixture_dir) / "cards.json"
        if path.is_file():
            with path.open() as fh:
                for entry in json.load(fh):
                    self._catalog[entry["id"]] = entry
            return
        for name, set_id, set_name, number, rarity, base, drift, vol, variant in _BUILTIN_CATALOG:
            card_id = f"{set_id}-{number.lstrip('0') or '0'}"
            self._catalog[card_id] = {
                "id": card_id,
                "name": name,
                "set_id": set_id,
                "set_name": set_name,
                "number": number,
                "rarity": rarity,
                "supertype": "Trainer" if name in {"Iono", "Miriam"} else "Pokemon",
                "subtypes": "",
                "artist": "",
                "base_price": base,
                "drift": drift,
                "volatility": vol,
                "variant": variant,
            }

    def _record(self, entry: dict[str, Any]) -> CardRecord:
        return CardRecord(
            id=entry["id"],
            name=entry["name"],
            set_id=entry.get("set_id", ""),
            set_name=entry.get("set_name", ""),
            number=entry.get("number", ""),
            rarity=entry.get("rarity", ""),
            supertype=entry.get("supertype", ""),
            subtypes=entry.get("subtypes", ""),
            artist=entry.get("artist", ""),
            image_small=entry.get("image_small", ""),
            image_large=entry.get("image_large", ""),
            tcgplayer_url=entry.get("tcgplayer_url", ""),
            cardmarket_url=entry.get("cardmarket_url", ""),
        )

    def search_cards(self, query: str, limit: int = 50) -> list[CardRecord]:
        term = query.strip().lower()
        if not term:
            return []
        hits = [
            entry for entry in self._catalog.values()
            if term in entry["name"].lower()
            or term in entry["id"].lower()
            or term in entry.get("set_name", "").lower()
        ]
        hits.sort(key=lambda e: (e["name"], e.get("set_name", "")))
        return [self._record(e) for e in hits[:limit]]

    def cards_in_set(self, set_id: str) -> list[CardRecord]:
        hits = [e for e in self._catalog.values() if e.get("set_id") == set_id]
        hits.sort(key=lambda e: e.get("number", ""))
        return [self._record(e) for e in hits]

    def list_sets(self) -> list[dict[str, Any]]:
        out = []
        for set_id, (name, series, printed, total, released) in _SET_META.items():
            out.append({
                "id": set_id,
                "name": name,
                "series": series,
                "printed_total": printed,
                "total": total,
                "release_date": released,
                "symbol_url": "",
                "logo_url": "",
            })
        return out

    def all_card_ids(self) -> list[str]:
        return list(self._catalog)

    # --- prices ---------------------------------------------------------

    def fetch_quotes(self, card_ids: Iterable[str]
                     ) -> tuple[list[CardRecord], list[PriceQuote]]:
        return self.quotes_on(card_ids, datetime.now(timezone.utc).date())

    def quotes_on(self, card_ids: Iterable[str], on: date
                  ) -> tuple[list[CardRecord], list[PriceQuote]]:
        """Quotes as they would have looked on ``on`` - used to backfill history."""
        cards: list[CardRecord] = []
        quotes: list[PriceQuote] = []
        for card_id in dict.fromkeys(card_ids):
            entry = self._catalog.get(card_id)
            if not entry:
                continue
            cards.append(self._record(entry))
            quotes.append(self._quote(entry, on))
        return cards, quotes

    def _quote(self, entry: dict[str, Any], on: date) -> PriceQuote:
        market = _model_price(entry, on)
        # A live listing floor sits below market by a card-specific spread; the
        # occasional deep underpriced listing is what a spread play looks for.
        spread = 0.06 + 0.22 * _noise(entry["id"], on, "spread")
        low = round(market * (1 - spread), 2)
        return PriceQuote(
            card_id=entry["id"],
            source="tcgplayer",
            variant=entry.get("variant", "normal"),
            currency="USD",
            market=round(market, 2),
            low=low,
            mid=round(market * 1.04, 2),
            high=round(market * (1.35 + 0.4 * _noise(entry["id"], on, "high")), 2),
            direct_low=round(low * 1.02, 2),
        )

    def close(self) -> None:  # nothing to release
        return None


# --- price model --------------------------------------------------------


def _hash_unit(*parts: Any) -> float:
    """Deterministic float in [0, 1) from the given parts."""
    raw = "|".join(str(p) for p in parts).encode()
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _noise(card_id: str, on: date, tag: str) -> float:
    return _hash_unit(card_id, on.isoformat(), tag)


def _model_price(entry: dict[str, Any], on: date) -> float:
    """Trend + cycle + shock model.

    Deliberately produces the shapes the signal engine is meant to catch:
    a drifting trend, a slow cycle, day noise, and occasional sharp dips and
    spikes that look like reprint news or hype.
    """
    base = float(entry.get("base_price", 10.0))
    drift = float(entry.get("drift", 0.0))
    vol = float(entry.get("volatility", 0.07))
    t = (on - _EPOCH).days

    trend = (1 + drift) ** (t / 365.0)
    cycle = 1 + 0.09 * math.sin((t / 47.0) + _hash_unit(entry["id"], "phase") * 6.283)
    noise = 1 + vol * (_noise(entry["id"], on, "day") - 0.5) * 2

    shock = 1.0
    # Roughly one event per ~40 days per card, decaying over the following week.
    for back in range(0, 8):
        d = date.fromordinal(on.toordinal() - back)
        roll = _hash_unit(entry["id"], d.isoformat(), "shock")
        if roll > 0.975:
            direction = 1.0 if _hash_unit(entry["id"], d.isoformat(), "dir") > 0.45 else -1.0
            magnitude = 0.12 + 0.25 * _hash_unit(entry["id"], d.isoformat(), "mag")
            shock *= 1 + direction * magnitude * (1 - back / 8.0)

    price = base * trend * cycle * noise * shock
    return max(0.05, price)
