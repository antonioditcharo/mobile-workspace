"""Bulk lot valuation - buying boxes and selling the tail.

Bulk is where flippers lose money, and they lose it the same way every time: by
valuing a lot at full market. Three haircuts separate a good lot from a garage
full of cardboard, and all three are applied here.

* **Sell-through** - you will not sell every card. Ever.
* **Bulk discount** - moving volume means pricing under market.
* **Fees and handling** - a hundred $2 cards is a hundred orders' worth of fees.

The output is a maximum bid, not an appraisal: the number you can pay and still
make your target margin.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Any, Iterable, Sequence

from .analytics import load_metrics
from .config import Config
from .db import Database, iso
from .portfolio import open_positions

# Rough composition of an unsorted modern bulk box, used when a lot is described
# by card count rather than by an itemised list.
# Tracked cards per rarity before a by-count estimate is worth believing.
MIN_RARITY_SAMPLES = 12
# Applied to a rarity priced off a handful of observations.
THIN_SAMPLE_HAIRCUT = 0.5
# Sample coverage below this yields no buy/pass verdict at all.
MIN_CONFIDENCE = 0.5

DEFAULT_MIX = {
    "Common": 0.55,
    "Uncommon": 0.28,
    "Rare": 0.11,
    "Rare Holo": 0.035,
    "Ultra Rare": 0.02,
    "Secret Rare": 0.005,
}


@dataclass
class LotLine:
    """One card line inside a lot, valued."""

    card_id: str
    variant: str
    quantity: int
    card_name: str = ""
    set_name: str = ""
    rarity: str = ""
    price: float | None = None
    market_value: float = 0.0
    realizable: float = 0.0
    treatment: str = "filler"     # single | bulk | filler
    direction: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LotValuation:
    lines: list[LotLine] = field(default_factory=list)
    cards: int = 0
    priced_cards: int = 0
    unpriced_cards: int = 0
    market_value: float = 0.0        # full retail, before any haircut
    realizable_value: float = 0.0    # what you should actually expect to bank
    singles_value: float = 0.0
    bulk_value: float = 0.0
    filler_value: float = 0.0
    fees_estimate: float = 0.0
    handling_estimate: float = 0.0
    max_bid: float = 0.0
    ask_price: float | None = None
    shipping: float = 0.0
    margin_at_ask: float | None = None
    profit_at_ask: float | None = None
    verdict: str = "unknown"
    notes: list[str] = field(default_factory=list)
    top_cards: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["lines"] = [line.to_dict() if isinstance(line, LotLine) else line
                         for line in self.lines]
        return data


# --- buying a lot -------------------------------------------------------

def value_lot(
    db: Database,
    config: Config,
    items: Sequence[tuple[str, str, int]],
    ask_price: float | None = None,
    shipping: float = 0.0,
    today: date | None = None,
    include_lines: bool = True,
) -> LotValuation:
    """Value an itemised lot and produce a maximum bid.

    ``items`` is ``(card_id, variant, quantity)``. Cards with no price history
    are counted as filler rather than skipped - an unknown card is worth
    approximately nothing to a flipper, and pretending otherwise inflates the bid.
    """
    rules = config.bulk
    source = config.provider.preferred_source
    valuation = LotValuation(ask_price=ask_price, shipping=shipping)

    cards = {row["id"]: dict(row) for row in db.query("SELECT * FROM cards")}
    singles_orders = 0

    for card_id, variant, quantity in items:
        quantity = max(0, int(quantity))
        if quantity == 0:
            continue
        valuation.cards += quantity
        card = cards.get(card_id, {})
        metrics = load_metrics(db, card_id, variant, source, today=today)
        price = metrics.price

        line = LotLine(
            card_id=card_id,
            variant=variant,
            quantity=quantity,
            card_name=card.get("name", card_id),
            set_name=card.get("set_name", ""),
            rarity=card.get("rarity", ""),
            price=price,
            direction=metrics.direction,
        )

        if price is None:
            valuation.unpriced_cards += quantity
            line.treatment = "filler"
            line.realizable = rules.filler_value_each * quantity
            valuation.filler_value += line.realizable
        else:
            valuation.priced_cards += quantity
            line.market_value = round(price * quantity, 2)
            valuation.market_value += line.market_value

            if price < rules.filler_price_ceiling:
                # Not worth listing individually; it is worth what bulk buyers pay.
                line.treatment = "filler"
                line.realizable = rules.filler_value_each * quantity
                valuation.filler_value += line.realizable
            elif price >= rules.single_out_threshold:
                # Pull it and sell it as a single: fees and handling per order.
                line.treatment = "single"
                sold = quantity * rules.sell_through_rate
                singles_orders += sold
                gross_each = price * (1 - rules.bulk_discount)
                net_each = config.fees.net_proceeds(gross_each) - rules.handling_cost_each
                line.realizable = max(0.0, net_each * sold)
                valuation.singles_value += line.realizable
            else:
                # Mid-value: realistically moved in small lots at a discount.
                line.treatment = "bulk"
                sold = quantity * rules.sell_through_rate
                gross_each = price * (1 - rules.bulk_discount)
                # Bundled sales avoid per-card order fees but keep commission.
                commission = config.fees.commission_pct + config.fees.payment_pct
                line.realizable = max(0.0, gross_each * (1 - commission) * sold)
                valuation.bulk_value += line.realizable

        line.realizable = round(line.realizable, 2)
        if include_lines:
            valuation.lines.append(line)

    valuation.realizable_value = round(
        valuation.singles_value + valuation.bulk_value + valuation.filler_value, 2
    )
    valuation.singles_value = round(valuation.singles_value, 2)
    valuation.bulk_value = round(valuation.bulk_value, 2)
    valuation.filler_value = round(valuation.filler_value, 2)
    valuation.market_value = round(valuation.market_value, 2)
    valuation.fees_estimate = round(
        max(0.0, valuation.market_value * rules.sell_through_rate
            * (config.fees.commission_pct + config.fees.payment_pct)), 2
    )
    valuation.handling_estimate = round(singles_orders * rules.handling_cost_each, 2)

    # Bid low enough that the target margin survives the haircuts.
    denominator = 1 + rules.target_lot_margin
    valuation.max_bid = round(max(0.0, valuation.realizable_value / denominator - shipping), 2)

    if ask_price is not None:
        total_outlay = ask_price + shipping
        profit = valuation.realizable_value - total_outlay
        valuation.profit_at_ask = round(profit, 2)
        valuation.margin_at_ask = round(profit / total_outlay, 4) if total_outlay > 0 else None
        if valuation.margin_at_ask is None:
            valuation.verdict = "free"
        elif valuation.margin_at_ask >= rules.target_lot_margin:
            valuation.verdict = "buy"
        elif valuation.margin_at_ask > 0:
            valuation.verdict = "thin"
        else:
            valuation.verdict = "pass"
    else:
        valuation.verdict = "no_ask"

    valuation.top_cards = [
        line.to_dict() for line in
        sorted((l for l in valuation.lines if l.price),
               key=lambda l: l.market_value, reverse=True)[:10]
    ]
    valuation.notes = _lot_notes(valuation, rules)
    return valuation


def _lot_notes(valuation: LotValuation, rules) -> list[str]:
    notes: list[str] = []
    if valuation.market_value > 0:
        keep = valuation.realizable_value / valuation.market_value
        notes.append(
            f"Expect to realise about {keep * 100:.0f}% of the "
            f"${valuation.market_value:,.2f} sticker value after "
            f"{rules.sell_through_rate * 100:.0f}% sell-through, "
            f"{rules.bulk_discount * 100:.0f}% bulk discount and fees."
        )
    if valuation.unpriced_cards:
        notes.append(
            f"{valuation.unpriced_cards} card(s) have no price history and were "
            "counted as filler - sync them for a sharper number."
        )
    concentration = 0.0
    if valuation.market_value > 0 and valuation.top_cards:
        concentration = sum(c["market_value"] for c in valuation.top_cards) / valuation.market_value
        notes.append(
            f"Top {len(valuation.top_cards)} cards carry {concentration * 100:.0f}% of the "
            "lot value - verify those exist and are in the stated condition before bidding."
        )
    if concentration > 0.75:
        notes.append(
            "Value is highly concentrated: if the headline cards are misgraded or "
            "missing, the rest of the lot will not cover the purchase."
        )
    if valuation.verdict == "thin":
        notes.append("Margin is positive but under target - only worth it if you can flip it fast.")
    if valuation.verdict == "pass":
        notes.append("At this ask the lot loses money on realistic assumptions.")
    return notes


# --- lots described by count -------------------------------------------

def rarity_value_table(db: Database, config: Config, set_ids: Sequence[str] | None = None
                       ) -> dict[str, dict[str, float]]:
    """Median value per rarity, computed from your own tracked prices."""
    source = config.provider.preferred_source
    sql = """
        SELECT c.rarity AS rarity, ph.market AS market
        FROM cards c
        JOIN price_history ph ON ph.card_id = c.id
        WHERE ph.source = ? AND ph.market IS NOT NULL
          AND ph.captured_on = (
              SELECT MAX(captured_on) FROM price_history
              WHERE card_id = c.id AND source = ph.source AND variant = ph.variant
          )
    """
    params: list[Any] = [source]
    if set_ids:
        sql += f" AND c.set_id IN ({','.join('?' * len(set_ids))})"
        params.extend(set_ids)

    buckets: dict[str, list[float]] = {}
    for row in db.query(sql, params):
        rarity = row["rarity"] or "Unknown"
        buckets.setdefault(rarity, []).append(float(row["market"]))

    return {
        rarity: {
            "median": round(statistics.median(values), 2),
            "mean": round(statistics.fmean(values), 2),
            # Bulk skews cheap: the holos in a box are the ones nobody pulled
            # for singles, so the low quartile is the honest reference.
            "p25": round(_percentile(values, 0.25), 2),
            "samples": len(values),
        }
        for rarity, values in buckets.items() if values
    }


def _percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile; stable for tiny samples."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def estimate_by_count(
    db: Database,
    config: Config,
    card_count: int,
    set_ids: Sequence[str] | None = None,
    mix: dict[str, float] | None = None,
    ask_price: float | None = None,
    shipping: float = 0.0,
) -> dict[str, Any]:
    """Value an unsorted lot described only by card count.

    Uses the median price per rarity from your own tracked data and an assumed
    rarity mix. Far rougher than an itemised valuation - use it to decide
    whether a listing is worth asking questions about, not to place a bid.
    """
    rules = config.bulk
    table = rarity_value_table(db, config, set_ids)
    mix = mix or DEFAULT_MIX
    total = sum(mix.values()) or 1.0

    breakdown: list[dict[str, Any]] = []
    gross = 0.0
    realizable = 0.0
    weighted_samples = 0.0
    thin: list[str] = []

    for rarity, share in mix.items():
        count = card_count * (share / total)
        stats = table.get(rarity)
        samples = stats["samples"] if stats else 0
        if samples >= MIN_RARITY_SAMPLES:
            reference = stats["p25"]
        elif samples > 0:
            # Too few observations to trust; use them but haircut hard and say so.
            reference = stats["p25"] * THIN_SAMPLE_HAIRCUT
            thin.append(rarity)
        else:
            reference = rules.filler_value_each
            thin.append(rarity)

        value = reference * count
        gross += value
        weighted_samples += (share / total) * min(samples, MIN_RARITY_SAMPLES)

        if reference < rules.filler_price_ceiling:
            take = rules.filler_value_each * count
        else:
            gross_each = reference * (1 - rules.bulk_discount)
            commission = config.fees.commission_pct + config.fees.payment_pct
            take = gross_each * (1 - commission) * count * rules.sell_through_rate
        realizable += take
        breakdown.append({
            "rarity": rarity,
            "assumed_share": round(share / total, 4),
            "cards": round(count, 1),
            "reference_price": round(reference, 2),
            "median_price": round(stats["median"], 2) if stats else None,
            "market_value": round(value, 2),
            "realizable": round(take, 2),
            "samples": samples,
        })

    confidence = round(weighted_samples / MIN_RARITY_SAMPLES, 3)
    max_bid = max(0.0, realizable / (1 + rules.target_lot_margin) - shipping)
    caveats = [
        "Rarity mix is assumed, not observed. Treat this as a screening number; "
        "itemise the lot before committing real money.",
        "Prices use the 25th percentile of your tracked cards per rarity - bulk "
        "holos are the ones nobody pulled for singles.",
    ]
    if thin:
        caveats.append(
            f"Thin or missing price data for: {', '.join(sorted(set(thin)))}. "
            "Sync more cards from these sets before trusting the number."
        )

    out: dict[str, Any] = {
        "card_count": card_count,
        "set_ids": list(set_ids or []),
        "market_value": round(gross, 2),
        "realizable_value": round(realizable, 2),
        "value_per_card": round(realizable / card_count, 4) if card_count else 0.0,
        "max_bid": round(max_bid, 2),
        "max_bid_per_card": round(max_bid / card_count, 4) if card_count else 0.0,
        "breakdown": breakdown,
        "confidence": confidence,
        "assumptions": {
            "sell_through_rate": rules.sell_through_rate,
            "bulk_discount": rules.bulk_discount,
            "target_lot_margin": rules.target_lot_margin,
            "price_basis": "25th percentile per rarity",
        },
        "caveats": caveats,
        "caveat": caveats[0],
    }

    if ask_price is not None:
        outlay = ask_price + shipping
        margin = (realizable - outlay) / outlay if outlay > 0 else None
        out["ask_price"] = ask_price
        out["profit_at_ask"] = round(realizable - outlay, 2)
        out["margin_at_ask"] = round(margin, 4) if margin is not None else None
        if confidence < MIN_CONFIDENCE:
            # Refusing to call it is the honest answer when the inputs are guesses.
            out["verdict"] = "insufficient_data"
            caveats.append(
                "Not enough tracked prices to give a verdict on this lot."
            )
        elif margin is None:
            out["verdict"] = "free"
        elif margin >= rules.target_lot_margin:
            out["verdict"] = "buy"
        elif margin > 0:
            out["verdict"] = "thin"
        else:
            out["verdict"] = "pass"
    return out


# --- selling in bulk ----------------------------------------------------

def sell_plan(db: Database, config: Config, today: date | None = None) -> dict[str, Any]:
    """Split your inventory into 'sell individually' and 'move as bulk'.

    A card is worth listing on its own only when the net after fees, shipping
    and handling beats what a bulk buyer would pay for it.
    """
    rules = config.bulk
    source = config.provider.preferred_source
    singles: list[dict[str, Any]] = []
    bulk: list[dict[str, Any]] = []
    unpriced: list[dict[str, Any]] = []

    for position in open_positions(db):
        metrics = load_metrics(db, position.card_id, position.variant, source, today=today)
        price = metrics.price
        entry = {
            "card_id": position.card_id,
            "variant": position.variant,
            "card_name": position.card_name,
            "set_name": position.set_name,
            "quantity": position.quantity,
            "cost_each": position.cost_each,
            "price": price,
            "direction": metrics.direction,
        }
        if price is None:
            unpriced.append(entry)
            continue

        net_single = config.fees.net_proceeds(price) - rules.handling_cost_each
        bulk_each = (rules.filler_value_each if price < rules.filler_price_ceiling
                     else price * (1 - rules.bulk_discount)
                     * (1 - config.fees.commission_pct - config.fees.payment_pct))
        entry["net_if_single"] = round(net_single, 2)
        entry["net_if_bulk"] = round(bulk_each, 2)
        entry["quantity_value_single"] = round(max(0.0, net_single) * position.quantity, 2)
        entry["quantity_value_bulk"] = round(max(0.0, bulk_each) * position.quantity, 2)

        if net_single <= 0:
            entry["reason"] = "Fees exceed the sale price - listing it alone loses money"
            bulk.append(entry)
        elif net_single > bulk_each:
            entry["reason"] = (
                f"Nets {net_single - bulk_each:+.2f} more per card sold individually"
            )
            singles.append(entry)
        else:
            entry["reason"] = "Bulk pricing beats the fee drag on a single listing"
            bulk.append(entry)

    singles.sort(key=lambda e: e["quantity_value_single"], reverse=True)
    bulk.sort(key=lambda e: e["quantity_value_bulk"], reverse=True)

    singles_total = sum(e["quantity_value_single"] for e in singles)
    bulk_total = sum(e["quantity_value_bulk"] for e in bulk)
    return {
        "as_of": iso(),
        "sell_individually": singles,
        "sell_as_bulk": bulk,
        "unpriced": unpriced,
        "singles_net_total": round(singles_total, 2),
        "bulk_net_total": round(bulk_total, 2),
        "total_net": round(singles_total + bulk_total, 2),
        "bulk_card_count": sum(e["quantity"] for e in bulk),
        "suggested_bulk_ask": round(bulk_total * (1 + rules.target_lot_margin), 2),
        "note": (
            "Suggested bulk ask includes your target margin, so there is room to "
            "negotiate down to the net figure."
        ),
    }


def set_value_concentration(db: Database, config: Config, set_id: str, top_n: int = 10
                            ) -> dict[str, Any]:
    """Where the money sits in a set - the cards that make a box worth opening."""
    source = config.provider.preferred_source
    rows = db.query(
        """
        SELECT DISTINCT ph.card_id, ph.variant FROM price_history ph
        JOIN cards c ON c.id = ph.card_id
        WHERE c.set_id = ? AND ph.source = ?
        """,
        (set_id, source),
    )
    cards = {row["id"]: dict(row) for row in db.query(
        "SELECT * FROM cards WHERE set_id = ?", (set_id,))}

    entries: list[dict[str, Any]] = []
    for row in rows:
        metrics = load_metrics(db, row["card_id"], row["variant"], source)
        if metrics.price is None:
            continue
        card = cards.get(row["card_id"], {})
        entries.append({
            "card_id": row["card_id"],
            "variant": row["variant"],
            "card_name": card.get("name", row["card_id"]),
            "number": card.get("number", ""),
            "rarity": card.get("rarity", ""),
            "price": metrics.price,
            "change_30d": metrics.change_30d,
            "direction": metrics.direction,
        })

    entries.sort(key=lambda e: e["price"], reverse=True)
    total = sum(e["price"] for e in entries)
    top = entries[:top_n]
    top_value = sum(e["price"] for e in top)
    return {
        "set_id": set_id,
        "set_name": next(iter(cards.values()), {}).get("set_name", set_id) if cards else set_id,
        "cards_priced": len(entries),
        "total_value": round(total, 2),
        "median_card": round(statistics.median([e["price"] for e in entries]), 2) if entries else 0,
        "top_cards": top,
        "top_share": round(top_value / total, 4) if total > 0 else None,
        "note": (
            "A high top-share means the set's value is concentrated in a few "
            "chase cards - bulk from it is worth less than the total suggests."
        ),
    }


# --- lot persistence ----------------------------------------------------

def create_lot(db: Database, name: str, kind: str = "buy", description: str = "",
               ask_price: float | None = None, shipping: float = 0.0) -> int:
    return db.execute(
        """
        INSERT INTO bulk_lots (name, kind, description, ask_price, shipping, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'evaluating', ?)
        """,
        (name, kind, description, ask_price, shipping, iso()),
    )


def add_lot_items(db: Database, lot_id: int, items: Iterable[tuple[str, str, int]]) -> int:
    rows = [(lot_id, cid, variant, int(qty)) for cid, variant, qty in items]
    if not rows:
        return 0
    with db.tx() as conn:
        conn.executemany(
            """
            INSERT INTO bulk_lot_items (lot_id, card_id, variant, quantity)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(lot_id, card_id, variant)
            DO UPDATE SET quantity = quantity + excluded.quantity
            """,
            rows,
        )
    return len(rows)


def lot_items(db: Database, lot_id: int) -> list[tuple[str, str, int]]:
    return [
        (r["card_id"], r["variant"], int(r["quantity"]))
        for r in db.query(
            "SELECT card_id, variant, quantity FROM bulk_lot_items WHERE lot_id = ?",
            (lot_id,),
        )
    ]


def evaluate_lot(db: Database, config: Config, lot_id: int, today: date | None = None
                 ) -> LotValuation:
    lot = db.one("SELECT * FROM bulk_lots WHERE id = ?", (lot_id,))
    if lot is None:
        raise ValueError(f"no lot with id {lot_id}")
    valuation = value_lot(
        db, config, lot_items(db, lot_id),
        ask_price=lot["ask_price"], shipping=float(lot["shipping"] or 0.0), today=today,
    )
    db.execute(
        "UPDATE bulk_lots SET evaluated_at = ?, valuation = ? WHERE id = ?",
        (iso(), json.dumps(valuation.to_dict()), lot_id),
    )
    return valuation


def list_lots(db: Database) -> list[dict[str, Any]]:
    out = []
    for row in db.query("SELECT * FROM bulk_lots ORDER BY id DESC"):
        entry = dict(row)
        entry["item_count"] = (db.one(
            "SELECT COALESCE(SUM(quantity), 0) AS n FROM bulk_lot_items WHERE lot_id = ?",
            (row["id"],),
        ) or {"n": 0})["n"]
        if entry.get("valuation"):
            try:
                entry["valuation"] = json.loads(entry["valuation"])
            except (TypeError, ValueError):
                entry["valuation"] = None
        out.append(entry)
    return out
