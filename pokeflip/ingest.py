"""Catalog sync and price capture.

``refresh_prices`` is the job the scheduler runs: it works out what you care
about (watchlist, holdings, lots under evaluation, tracked sets), asks the
provider for current prices, and writes one snapshot per card per day.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from .config import Config
from .db import Database, day, iso, utcnow
from .providers import (
    CardRecord, PriceProvider, ProviderError, build_catalog_provider, build_provider,
)
from .providers.fixture import FixtureProvider

log = logging.getLogger("pokeflip.ingest")


def catalog_lookup(db: Database):
    """Card metadata by id, for price sources that search by name.

    eBay has no card database, so it needs to be told what "sv3pt5-199" is
    before it can look up a price for it.
    """
    def lookup(card_id: str) -> CardRecord | None:
        row = db.get_card(card_id)
        if row is None:
            return None
        return CardRecord(
            id=row["id"], name=row["name"] or "", set_id=row["set_id"] or "",
            set_name=row["set_name"] or "", number=row["number"] or "",
            rarity=row["rarity"] or "", supertype=row["supertype"] or "",
            subtypes=row["subtypes"] or "", artist=row["artist"] or "",
            image_small=row["image_small"] or "", image_large=row["image_large"] or "",
            tcgplayer_url=row["tcgplayer_url"] or "",
            cardmarket_url=row["cardmarket_url"] or "",
        )
    return lookup


def tracking_universe(db: Database, config: Config) -> list[str]:
    """Card ids worth spending API calls on, most important first."""
    ordered: dict[str, None] = {}

    def add(rows: Iterable[Any], key: str = "card_id") -> None:
        for row in rows:
            value = row[key]
            if value:
                ordered[value] = None

    add(db.query("SELECT DISTINCT card_id FROM holdings WHERE status = 'open'"))
    add(db.query("SELECT DISTINCT card_id FROM watchlist WHERE active = 1"))
    add(db.query(
        """
        SELECT DISTINCT i.card_id FROM bulk_lot_items i
        JOIN bulk_lots l ON l.id = i.lot_id
        WHERE l.status IN ('evaluating', 'open')
        """
    ))
    if config.tracked_sets:
        placeholders = ",".join("?" * len(config.tracked_sets))
        add(db.query(
            f"SELECT id AS card_id FROM cards WHERE set_id IN ({placeholders})",
            config.tracked_sets,
        ))
    # Anything already being tracked keeps its series continuous.
    add(db.query("SELECT DISTINCT card_id FROM price_history"))

    return list(ordered)[: config.max_cards_per_refresh]


def sync_sets(db: Database, provider: PriceProvider) -> int:
    count = 0
    for entry in provider.list_sets():
        if not entry.get("id"):
            continue
        db.upsert_set(entry)
        count += 1
    return count


def sync_set_cards(db: Database, config: Config, set_id: str,
                   provider: PriceProvider | None = None) -> dict[str, Any]:
    """Pull a whole set into the catalog and price it immediately."""
    owned = provider is None
    provider = provider or build_catalog_provider(config)
    try:
        cards = provider.cards_in_set(set_id)
        db.upsert_cards(c.as_row() for c in cards)
    finally:
        if owned:
            provider.close()

    # Prices come from the price source, which is not always the catalog source.
    result = capture_prices(db, config, [c.id for c in cards])
    result["set_id"] = set_id
    result["cards_synced"] = len(cards)
    return result


def search_and_store(db: Database, config: Config, query: str, limit: int = 25,
                     provider: PriceProvider | None = None) -> list[dict[str, Any]]:
    """Search the provider, store what comes back, return catalog rows."""
    owned = provider is None
    provider = provider or build_catalog_provider(config)
    try:
        cards = provider.search_cards(query, limit=limit)
        db.upsert_cards(c.as_row() for c in cards)
        return [c.as_row() for c in cards]
    finally:
        if owned:
            provider.close()


def capture_prices(db: Database, config: Config, card_ids: Sequence[str],
                   provider: PriceProvider | None = None) -> dict[str, Any]:
    """Fetch and store one price snapshot for the given cards."""
    if not card_ids:
        return {"cards": 0, "quotes": 0, "errors": []}

    owned = provider is None
    provider = provider or build_provider(config, catalog_lookup(db))
    captured_at = iso()
    captured_on = day()
    errors: list[str] = []
    total_quotes = 0
    total_cards = 0

    try:
        # Chunked so one bad batch does not lose an entire refresh.
        for chunk in _chunks(list(card_ids), 40):
            try:
                cards, quotes = provider.fetch_quotes(chunk)
            except ProviderError as exc:
                log.warning("price fetch failed for %d cards: %s", len(chunk), exc)
                errors.append(str(exc))
                continue
            if cards:
                db.upsert_cards(c.as_row() for c in cards)
                total_cards += len(cards)
            rows = [q.as_row(captured_at, captured_on) for q in quotes if q.usable]
            total_quotes += db.record_prices(rows)
    finally:
        if owned:
            provider.close()

    return {"cards": total_cards, "quotes": total_quotes, "errors": errors}


def refresh_prices(db: Database, config: Config, card_ids: Sequence[str] | None = None
                   ) -> dict[str, Any]:
    """The scheduled refresh. Records a run so failures are visible later."""
    run_id = db.start_run("refresh")
    try:
        targets = list(card_ids) if card_ids else tracking_universe(db, config)
        if not targets:
            stats = {"cards": 0, "quotes": 0, "note": "nothing tracked yet"}
            db.finish_run(run_id, "ok", stats)
            return stats

        stats = capture_prices(db, config, targets)
        stats["targets"] = len(targets)
        if config.history_retention_days > 0:
            stats["pruned"] = db.prune_history(config.history_retention_days)

        status = "ok" if not stats["errors"] else "partial"
        db.finish_run(run_id, status, stats)
        return stats
    except Exception as exc:
        db.finish_run(run_id, "error", error=str(exc))
        raise


def seed_demo(db: Database, config: Config, days: int = 180,
              include_portfolio: bool = True) -> dict[str, Any]:
    """Backfill a realistic history from the offline provider.

    Gives a brand-new install months of trend data instantly so the signal
    engine has something to chew on. Uses the fixture provider regardless of
    the configured one - this is demo data by definition, never live prices.
    """
    provider = FixtureProvider(config)
    card_ids = provider.all_card_ids()

    for entry in provider.list_sets():
        db.upsert_set(entry)

    today = utcnow().date()
    rows: list[dict[str, Any]] = []
    for offset in range(days, -1, -1):
        on = today - timedelta(days=offset)
        cards, quotes = provider.quotes_on(card_ids, on)
        if offset == days:
            db.upsert_cards(c.as_row() for c in cards)
        stamp = f"{on.isoformat()}T12:00:00+00:00"
        rows.extend(q.as_row(stamp, on.isoformat()) for q in quotes if q.usable)

    written = db.record_prices(rows)

    seeded_positions = 0
    if include_portfolio and not db.one("SELECT 1 FROM holdings LIMIT 1"):
        seeded_positions = _seed_portfolio(db, provider, today)

    if not db.one("SELECT 1 FROM watchlist LIMIT 1"):
        for card_id in card_ids[:6]:
            db.execute(
                "INSERT OR IGNORE INTO watchlist (card_id, variant, note, active, created_at) "
                "VALUES (?, 'any', 'seeded by demo', 1, ?)",
                (card_id, iso()),
            )

    return {
        "cards": len(card_ids),
        "days": days,
        "price_rows": written,
        "positions": seeded_positions,
    }


def _seed_portfolio(db: Database, provider: FixtureProvider, today: date) -> int:
    """A starter inventory bought at historical prices, so P&L is non-trivial."""
    picks = [
        ("Umbreon VMAX", 1, 150, 0.72),
        ("Charizard ex", 2, 95, 0.88),
        ("Pikachu", 8, 120, 0.65),
        ("Gengar VMAX", 3, 60, 1.15),
        ("Iono", 2, 45, 0.80),
        ("Snorlax", 12, 30, 0.90),
        ("Bulbasaur", 40, 100, 0.55),
    ]
    by_name = {entry["name"]: entry for entry in provider._catalog.values()}
    seeded = 0
    for name, qty, days_ago, cost_ratio in picks:
        entry = by_name.get(name)
        if not entry:
            continue
        bought_on = today - timedelta(days=days_ago)
        _, quotes = provider.quotes_on([entry["id"]], bought_on)
        if not quotes or quotes[0].market is None:
            continue
        cost = round(quotes[0].market * cost_ratio, 2)
        db.execute(
            """
            INSERT INTO holdings (card_id, variant, condition, quantity, cost_each,
                                  acquired_at, acquired_from, notes, status)
            VALUES (?, ?, 'NM', ?, ?, ?, 'demo seed', 'seeded by demo', 'open')
            """,
            (entry["id"], entry.get("variant", "normal"), qty, cost,
             f"{bought_on.isoformat()}T12:00:00+00:00"),
        )
        seeded += 1
    return seeded


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
