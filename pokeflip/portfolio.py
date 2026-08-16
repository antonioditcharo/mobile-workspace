"""Inventory, cost basis and profit-and-loss.

A holding is one lot bought at one price. Selling part of a lot splits it so
the remaining cost basis stays exact, which is what keeps ROI honest after a
few partial sales.

Value is reported two ways: **market value** (what the cards are worth) and
**net liquidation** (what you would actually bank after fees and shipping).
The second number is the real one.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .analytics import load_metrics, spark_series
from .config import Config
from .db import Database, iso, parse_ts, utcnow


@dataclass
class Position:
    """All open lots of one printing, aggregated."""

    card_id: str
    variant: str
    quantity: int
    cost_each: float            # quantity-weighted average
    total_cost: float
    acquired_at: str            # earliest acquisition in the position
    condition: str = "NM"
    card_name: str = ""
    set_name: str = ""
    number: str = ""
    rarity: str = ""
    image: str = ""
    lot_ids: list[int] = field(default_factory=list)

    def hold_days(self, today: date | None = None) -> int | None:
        try:
            acquired = parse_ts(self.acquired_at).date()
        except (ValueError, TypeError):
            return None
        return ((today or datetime.now(timezone.utc).date()) - acquired).days

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValuedPosition:
    position: Position
    price: float | None
    market_value: float | None
    net_value: float | None      # after fees, if liquidated now
    unrealized: float | None
    roi: float | None
    change_30d: float | None
    direction: str
    stale_days: int | None
    spark: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = self.position.to_dict()
        data.update({
            "price": self.price,
            "market_value": self.market_value,
            "net_value": self.net_value,
            "unrealized": self.unrealized,
            "roi": self.roi,
            "change_30d": self.change_30d,
            "direction": self.direction,
            "stale_days": self.stale_days,
            "spark": self.spark,
            "hold_days": self.position.hold_days(),
        })
        return data


def condition_price(config: Config, price: float, condition: str | None) -> float:
    """Quoted prices are near-mint prices; discount them for what you hold."""
    return price * config.conditions.multiplier(condition)


def realistic_net_each(config: Config, price: float,
                       condition: str | None = None) -> float:
    """What one card is actually worth to you, net.

    Selling a $1 card as its own order costs more in fees and postage than the
    card is worth, which would make a box of commons a negative asset. Nobody
    ships those individually - they go out by the hundred - so the value floor
    is the bulk price, and never below zero.
    """
    price = condition_price(config, price, condition)
    single = config.fees.net_proceeds(price)
    rules = config.bulk
    if price < rules.filler_price_ceiling:
        bulk = rules.filler_value_each
    else:
        commission = config.fees.commission_pct + config.fees.payment_pct
        bulk = price * (1 - rules.bulk_discount) * (1 - commission)
    return max(0.0, single, bulk)


# --- mutations ----------------------------------------------------------


def add_holding(
    db: Database,
    card_id: str,
    variant: str = "normal",
    quantity: int = 1,
    cost_each: float = 0.0,
    condition: str = "NM",
    acquired_at: str | None = None,
    acquired_from: str = "",
    notes: str = "",
    lot_id: int | None = None,
) -> int:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not db.get_card(card_id):
        raise ValueError(f"unknown card {card_id!r}; sync or search for it first")
    return db.execute(
        """
        INSERT INTO holdings (card_id, variant, condition, quantity, cost_each,
                              acquired_at, acquired_from, notes, status, lot_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (card_id, variant, condition, quantity, cost_each,
         acquired_at or iso(), acquired_from, notes, lot_id),
    )


def sell_holding(
    db: Database,
    holding_id: int,
    quantity: int,
    price_each: float,
    config: Config,
    sold_at: str | None = None,
) -> dict[str, Any]:
    """Record a sale, splitting the lot when only part of it is sold."""
    row = db.one("SELECT * FROM holdings WHERE id = ? AND status = 'open'", (holding_id,))
    if row is None:
        raise ValueError(f"no open holding with id {holding_id}")
    held = int(row["quantity"])
    if quantity <= 0 or quantity > held:
        raise ValueError(f"can sell between 1 and {held} of holding {holding_id}")

    net_each = config.fees.net_proceeds(price_each)
    when = sold_at or iso()

    with db.tx() as conn:
        if quantity < held:
            # Leave the unsold remainder as an open lot at the same basis.
            conn.execute(
                "UPDATE holdings SET quantity = ? WHERE id = ?",
                (held - quantity, holding_id),
            )
            conn.execute(
                """
                INSERT INTO holdings (card_id, variant, condition, quantity, cost_each,
                                      acquired_at, acquired_from, notes, status,
                                      sold_at, sold_price_each, sold_net_each, lot_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sold', ?, ?, ?, ?)
                """,
                (row["card_id"], row["variant"], row["condition"], quantity,
                 row["cost_each"], row["acquired_at"], row["acquired_from"],
                 row["notes"], when, price_each, net_each, row["lot_id"]),
            )
        else:
            conn.execute(
                """
                UPDATE holdings SET status='sold', sold_at=?, sold_price_each=?,
                                    sold_net_each=? WHERE id=?
                """,
                (when, price_each, net_each, holding_id),
            )

    cost = float(row["cost_each"]) * quantity
    proceeds = net_each * quantity
    return {
        "holding_id": holding_id,
        "card_id": row["card_id"],
        "variant": row["variant"],
        "quantity": quantity,
        "price_each": round(price_each, 2),
        "net_each": round(net_each, 2),
        "fees_each": round(price_each - net_each, 2),
        "cost_basis": round(cost, 2),
        "net_proceeds": round(proceeds, 2),
        "realized_pnl": round(proceeds - cost, 2),
        "roi": round((proceeds - cost) / cost, 4) if cost > 0 else None,
        "sold_at": when,
    }


def remove_holding(db: Database, holding_id: int) -> bool:
    return db.execute("DELETE FROM holdings WHERE id = ?", (holding_id,)) > 0


# Which lot to sell first, as (sort key, reverse).
LOT_SELECTION: dict[str, tuple[Any, bool]] = {
    # Oldest first: the default, and what most tax treatments assume.
    "fifo": (lambda lot: lot["acquired_at"] or "", False),
    "lifo": (lambda lot: lot["acquired_at"] or "", True),
    # Realise the smallest gain now.
    "highest_cost": (lambda lot: float(lot["cost_each"] or 0), True),
    # Realise the largest gain now.
    "lowest_cost": (lambda lot: float(lot["cost_each"] or 0), False),
}


def sell_position(
    db: Database,
    config: Config,
    card_id: str,
    quantity: int,
    price_each: float,
    variant: str = "normal",
    condition: str | None = None,
    method: str | None = None,
    sold_at: str | None = None,
) -> dict[str, Any]:
    """Sell from a position without naming a lot.

    Which lot goes first changes the realised gain, so the choice is explicit
    and configurable rather than incidental to row order.
    """
    method = (method or config.capital.lot_selection or "fifo").lower()
    if method not in LOT_SELECTION:
        raise ValueError(
            f"unknown lot selection {method!r}; use one of {', '.join(LOT_SELECTION)}")

    lots = [
        lot for lot in open_lots(db, card_id)
        if lot["variant"] == variant
        and (condition is None or lot["condition"] == condition)
    ]
    key, reverse = LOT_SELECTION[method]
    lots.sort(key=key, reverse=reverse)
    available = sum(int(lot["quantity"]) for lot in lots)
    if quantity <= 0 or quantity > available:
        raise ValueError(
            f"can sell between 1 and {available} of {card_id} ({variant})")

    sales: list[dict[str, Any]] = []
    remaining = quantity
    for lot in lots:
        if remaining <= 0:
            break
        take = min(remaining, int(lot["quantity"]))
        sales.append(sell_holding(db, int(lot["id"]), take, price_each, config, sold_at))
        remaining -= take

    return {
        "card_id": card_id,
        "variant": variant,
        "condition": condition,
        "method": method,
        "quantity": quantity,
        "price_each": round(price_each, 2),
        "lots_used": len(sales),
        "cost_basis": round(sum(s["cost_basis"] for s in sales), 2),
        "net_proceeds": round(sum(s["net_proceeds"] for s in sales), 2),
        "realized_pnl": round(sum(s["realized_pnl"] for s in sales), 2),
        "sales": sales,
    }


# --- reads --------------------------------------------------------------


def open_positions(db: Database) -> list[Position]:
    rows = db.query(
        """
        SELECT h.card_id, h.variant, h.condition,
               SUM(h.quantity)                    AS quantity,
               SUM(h.quantity * h.cost_each)      AS total_cost,
               MIN(h.acquired_at)                 AS acquired_at,
               GROUP_CONCAT(h.id)                 AS lot_ids,
               c.name, c.set_name, c.number, c.rarity, c.image_small
        FROM holdings h LEFT JOIN cards c ON c.id = h.card_id
        WHERE h.status = 'open'
        -- Condition is part of the identity: an LP copy is not the same asset
        -- as an NM one and must not be averaged in with it.
        GROUP BY h.card_id, h.variant, h.condition
        ORDER BY total_cost DESC
        """
    )
    positions: list[Position] = []
    for row in rows:
        qty = int(row["quantity"] or 0)
        if qty <= 0:
            continue
        total_cost = float(row["total_cost"] or 0.0)
        positions.append(
            Position(
                card_id=row["card_id"],
                variant=row["variant"],
                quantity=qty,
                cost_each=round(total_cost / qty, 4),
                total_cost=round(total_cost, 2),
                acquired_at=row["acquired_at"] or iso(),
                condition=row["condition"] or "NM",
                card_name=row["name"] or row["card_id"],
                set_name=row["set_name"] or "",
                number=row["number"] or "",
                rarity=row["rarity"] or "",
                image=row["image_small"] or "",
                lot_ids=[int(x) for x in (row["lot_ids"] or "").split(",") if x],
            )
        )
    return positions


def open_lots(db: Database, card_id: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT h.*, c.name AS card_name, c.set_name FROM holdings h "
        "LEFT JOIN cards c ON c.id = h.card_id WHERE h.status = 'open'"
    )
    params: list[Any] = []
    if card_id:
        sql += " AND h.card_id = ?"
        params.append(card_id)
    sql += " ORDER BY h.acquired_at DESC"
    return [dict(r) for r in db.query(sql, params)]


def value_positions(db: Database, config: Config, today: date | None = None
                    ) -> list[ValuedPosition]:
    source = config.provider.preferred_source
    out: list[ValuedPosition] = []
    for position in open_positions(db):
        metrics = load_metrics(db, position.card_id, position.variant, source,
                               days=60, include_history=True, today=today)
        price = metrics.price
        if price is None:
            out.append(ValuedPosition(position, None, None, None, None, None,
                                      None, "unknown", None))
            continue
        adjusted = condition_price(config, price, position.condition)
        market_value = adjusted * position.quantity
        net_value = realistic_net_each(config, price, position.condition) * position.quantity
        unrealized = net_value - position.total_cost
        roi = unrealized / position.total_cost if position.total_cost > 0 else None
        out.append(
            ValuedPosition(
                position=position,
                price=round(price, 2),
                market_value=round(market_value, 2),
                net_value=round(net_value, 2),
                unrealized=round(unrealized, 2),
                roi=round(roi, 4) if roi is not None else None,
                change_30d=metrics.change_30d,
                direction=metrics.direction,
                stale_days=metrics.stale_days,
                spark=spark_series(metrics),
            )
        )
    return out


def realized_pnl(db: Database, since: str | None = None) -> dict[str, Any]:
    sql = (
        "SELECT quantity, cost_each, sold_price_each, sold_net_each, sold_at, card_id "
        "FROM holdings WHERE status = 'sold'"
    )
    params: list[Any] = []
    if since:
        sql += " AND sold_at >= ?"
        params.append(since)
    rows = db.query(sql, params)

    cost = proceeds = gross = 0.0
    cards_sold = 0
    wins = losses = 0
    for row in rows:
        qty = int(row["quantity"] or 0)
        lot_cost = float(row["cost_each"] or 0) * qty
        lot_net = float(row["sold_net_each"] or 0) * qty
        cost += lot_cost
        proceeds += lot_net
        gross += float(row["sold_price_each"] or 0) * qty
        cards_sold += qty
        if lot_net >= lot_cost:
            wins += 1
        else:
            losses += 1

    pnl = proceeds - cost
    return {
        "sales": len(rows),
        "cards_sold": cards_sold,
        "gross_sales": round(gross, 2),
        "fees_paid": round(gross - proceeds, 2),
        "cost_basis": round(cost, 2),
        "net_proceeds": round(proceeds, 2),
        "realized_pnl": round(pnl, 2),
        "roi": round(pnl / cost, 4) if cost > 0 else None,
        "win_rate": round(wins / (wins + losses), 4) if (wins + losses) else None,
        "wins": wins,
        "losses": losses,
    }


def summary(db: Database, config: Config, today: date | None = None) -> dict[str, Any]:
    """Portfolio-level view: what you hold, what it is worth, how it is doing."""
    valued = value_positions(db, config, today=today)
    priced = [v for v in valued if v.net_value is not None]

    cost_basis = sum(v.position.total_cost for v in valued)
    market_value = sum(v.market_value or 0.0 for v in priced)
    net_value = sum(v.net_value or 0.0 for v in priced)
    unrealized = net_value - sum(v.position.total_cost for v in priced)

    by_set: dict[str, dict[str, float]] = {}
    for v in priced:
        bucket = by_set.setdefault(
            v.position.set_name or "Unknown",
            {"cost": 0.0, "net_value": 0.0, "cards": 0},
        )
        bucket["cost"] += v.position.total_cost
        bucket["net_value"] += v.net_value or 0.0
        bucket["cards"] += v.position.quantity

    movers = sorted(
        (v for v in priced if v.change_30d is not None),
        key=lambda v: v.change_30d or 0.0,
    )

    return {
        "as_of": iso(),
        "positions": len(valued),
        "unpriced_positions": len(valued) - len(priced),
        "cards": sum(v.position.quantity for v in valued),
        "cost_basis": round(cost_basis, 2),
        "market_value": round(market_value, 2),
        "net_liquidation": round(net_value, 2),
        "fee_drag": round(market_value - net_value, 2),
        "unrealized_pnl": round(unrealized, 2),
        "unrealized_roi": round(
            unrealized / sum(v.position.total_cost for v in priced), 4
        ) if priced and sum(v.position.total_cost for v in priced) > 0 else None,
        "realized": realized_pnl(db),
        "by_set": [
            {
                "set_name": name,
                "cost": round(vals["cost"], 2),
                "net_value": round(vals["net_value"], 2),
                "cards": int(vals["cards"]),
                "pnl": round(vals["net_value"] - vals["cost"], 2),
            }
            for name, vals in sorted(
                by_set.items(), key=lambda kv: kv[1]["net_value"], reverse=True
            )
        ],
        "top_gainers": [v.to_dict() for v in reversed(movers[-5:])],
        "top_losers": [v.to_dict() for v in movers[:5]],
        "positions_detail": [v.to_dict() for v in valued],
        "concentration": concentration(db, config, valued),
    }


def value_history(db: Database, config: Config, days: int = 90,
                  today: date | None = None) -> dict[str, Any]:
    """What the book has been worth, day by day.

    Reconstructed rather than recorded: for each day it values the lots that
    were open on that day at that day's price. That means the curve is correct
    from the moment you enter a holding, instead of only starting the day you
    first ran the app.

    Prices are carried forward across gaps in collection - a day with no quote
    is missing data, not a card that briefly became worthless.
    """
    source = config.provider.preferred_source
    end = today or utcnow().date()
    start = end - timedelta(days=days)

    lots = db.query(
        """
        SELECT card_id, variant, condition, quantity, cost_each,
               acquired_at, status, sold_at
        FROM holdings
        """
    )
    if not lots:
        return {"days": [], "start": start.isoformat(), "end": end.isoformat()}

    # One pass over the price table, then everything else is in memory.
    prices: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for key, rows in db.full_series(source).items():
        series = [
            (date.fromisoformat(r["captured_on"]), float(r["market"]))
            for r in rows if r["market"]
        ]
        if series:
            prices[key] = series

    def price_on(key: tuple[str, str], when: date) -> float | None:
        series = prices.get(key)
        if not series:
            return None
        latest = None
        for observed_on, value in series:
            if observed_on > when:
                break
            latest = value
        return latest

    parsed = []
    for lot in lots:
        try:
            acquired = parse_ts(lot["acquired_at"]).date()
        except (ValueError, TypeError):
            continue
        sold = None
        if lot["sold_at"]:
            try:
                sold = parse_ts(lot["sold_at"]).date()
            except (ValueError, TypeError):
                sold = None
        parsed.append((lot, acquired, sold))

    out: list[dict[str, Any]] = []
    for offset in range(days + 1):
        when = start + timedelta(days=offset)
        cost = market = net = 0.0
        held = 0
        for lot, acquired, sold in parsed:
            if acquired > when:
                continue
            if sold is not None and sold <= when:
                continue
            quantity = int(lot["quantity"] or 0)
            cost += float(lot["cost_each"] or 0) * quantity
            held += quantity
            price = price_on((lot["card_id"], lot["variant"]), when)
            if price is None:
                continue
            market += condition_price(config, price, lot["condition"]) * quantity
            net += realistic_net_each(config, price, lot["condition"]) * quantity

        out.append({
            "on": when.isoformat(),
            "cards": held,
            "cost_basis": round(cost, 2),
            "market_value": round(market, 2),
            "net_value": round(net, 2),
            "unrealized": round(net - cost, 2),
        })

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": out,
        "note": (
            "Reconstructed from stored prices and your acquisition dates. Days "
            "before a card was first priced show its cost but no value."
        ),
    }


# --- capital allocation -------------------------------------------------


def concentration(db: Database, config: Config,
                  valued: list[ValuedPosition] | None = None) -> dict[str, Any]:
    """Where the capital is bunched up, and whether that is a problem.

    One card going wrong should cost you a slice of the bankroll, not the
    bankroll. These are warnings rather than blocks - some flippers do want a
    concentrated book - but they should be a decision, not a surprise.
    """
    rules = config.capital
    valued = valued if valued is not None else value_positions(db, config)
    total_cost = sum(v.position.total_cost for v in valued)

    positions = sorted(
        (
            {
                "card_id": v.position.card_id,
                "card_name": v.position.card_name,
                "set_name": v.position.set_name,
                "condition": v.position.condition,
                "cost": v.position.total_cost,
                "share": (v.position.total_cost / total_cost) if total_cost > 0 else 0.0,
            }
            for v in valued
        ),
        key=lambda e: e["cost"],
        reverse=True,
    )

    by_set: dict[str, float] = {}
    for v in valued:
        by_set[v.position.set_name or "Unknown"] = (
            by_set.get(v.position.set_name or "Unknown", 0.0) + v.position.total_cost)
    sets = sorted(
        (
            {"set_name": name, "cost": round(cost, 2),
             "share": (cost / total_cost) if total_cost > 0 else 0.0}
            for name, cost in by_set.items()
        ),
        key=lambda e: e["cost"],
        reverse=True,
    )

    committed = float((db.one(
        "SELECT COALESCE(SUM(quantity * limit_price), 0) AS c FROM orders "
        "WHERE kind = 'buy' AND status = 'open'"
    ) or {"c": 0.0})["c"] or 0.0)

    warnings: list[dict[str, Any]] = []
    for entry in positions:
        if entry["share"] > rules.max_position_pct:
            warnings.append({
                "kind": "position",
                "subject": entry["card_name"] or entry["card_id"],
                "share": round(entry["share"], 4),
                "limit": rules.max_position_pct,
                "message": (
                    f"{entry['card_name'] or entry['card_id']} is "
                    f"{entry['share'] * 100:.0f}% of your cost basis "
                    f"(limit {rules.max_position_pct * 100:.0f}%)"
                ),
            })
    for entry in sets:
        if entry["share"] > rules.max_set_pct:
            warnings.append({
                "kind": "set",
                "subject": entry["set_name"],
                "share": round(entry["share"], 4),
                "limit": rules.max_set_pct,
                "message": (
                    f"{entry['set_name']} is {entry['share'] * 100:.0f}% of your "
                    f"cost basis (limit {rules.max_set_pct * 100:.0f}%)"
                ),
            })
    if rules.bankroll > 0:
        committed_share = (total_cost + committed) / rules.bankroll
        if committed_share > rules.max_committed_pct:
            warnings.append({
                "kind": "committed",
                "subject": "bankroll",
                "share": round(committed_share, 4),
                "limit": rules.max_committed_pct,
                "message": (
                    f"{committed_share * 100:.0f}% of your ${rules.bankroll:,.0f} "
                    f"bankroll is tied up in inventory and open orders "
                    f"(limit {rules.max_committed_pct * 100:.0f}%)"
                ),
            })

    return {
        "total_cost": round(total_cost, 2),
        "open_buy_commitments": round(committed, 2),
        "bankroll": rules.bankroll,
        "free_capital": (
            round(rules.bankroll - total_cost - committed, 2)
            if rules.bankroll > 0 else None
        ),
        "largest_position_share": round(positions[0]["share"], 4) if positions else None,
        "top_positions": [
            {**e, "cost": round(e["cost"], 2), "share": round(e["share"], 4)}
            for e in positions[:5]
        ],
        "by_set": [{**e, "share": round(e["share"], 4)} for e in sets[:8]],
        "warnings": warnings,
    }


# --- tax reporting ------------------------------------------------------

# Days held before a sale counts as long-term in most jurisdictions. Check
# your own rules; this is a label on a report, not tax advice.
LONG_TERM_DAYS = 365


def tax_report(db: Database, year: int | None = None) -> dict[str, Any]:
    """Closed positions with cost basis, proceeds, fees and holding period."""
    sql = (
        "SELECT h.*, c.name AS card_name, c.set_name FROM holdings h "
        "LEFT JOIN cards c ON c.id = h.card_id "
        "WHERE h.status = 'sold' AND h.sold_at IS NOT NULL"
    )
    params: list[Any] = []
    if year:
        sql += " AND h.sold_at >= ? AND h.sold_at < ?"
        params += [f"{year}-01-01", f"{year + 1}-01-01"]

    lines: list[dict[str, Any]] = []
    for row in db.query(sql + " ORDER BY h.sold_at", params):
        qty = int(row["quantity"] or 0)
        cost = float(row["cost_each"] or 0) * qty
        gross = float(row["sold_price_each"] or 0) * qty
        net = float(row["sold_net_each"] or 0) * qty
        try:
            held = (parse_ts(row["sold_at"]).date()
                    - parse_ts(row["acquired_at"]).date()).days
        except (ValueError, TypeError):
            held = None
        lines.append({
            "card_id": row["card_id"],
            "card_name": row["card_name"] or row["card_id"],
            "set_name": row["set_name"] or "",
            "variant": row["variant"],
            "condition": row["condition"],
            "quantity": qty,
            "acquired": (row["acquired_at"] or "")[:10],
            "sold": (row["sold_at"] or "")[:10],
            "held_days": held,
            "term": ("long" if held is not None and held >= LONG_TERM_DAYS
                     else "short" if held is not None else "unknown"),
            "cost_basis": round(cost, 2),
            "gross_proceeds": round(gross, 2),
            "fees": round(gross - net, 2),
            "net_proceeds": round(net, 2),
            "gain": round(net - cost, 2),
        })

    return {
        "year": year,
        "lines": lines,
        "totals": {
            "sales": len(lines),
            "cards": sum(line["quantity"] for line in lines),
            "cost_basis": round(sum(line["cost_basis"] for line in lines), 2),
            "gross_proceeds": round(sum(line["gross_proceeds"] for line in lines), 2),
            "fees": round(sum(line["fees"] for line in lines), 2),
            "net_proceeds": round(sum(line["net_proceeds"] for line in lines), 2),
            "gain": round(sum(line["gain"] for line in lines), 2),
            "short_term_gain": round(
                sum(line["gain"] for line in lines if line["term"] == "short"), 2),
            "long_term_gain": round(
                sum(line["gain"] for line in lines if line["term"] == "long"), 2),
        },
        "note": (
            "Figures come from what you recorded. Confirm them against your "
            "marketplace statements before filing anything."
        ),
    }
