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
from datetime import date, datetime, timezone
from typing import Any

from .analytics import load_metrics
from .config import Config
from .db import Database, iso, parse_ts


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
            "hold_days": self.position.hold_days(),
        })
        return data


def realistic_net_each(config: Config, price: float) -> float:
    """What one card is actually worth to you, net.

    Selling a $1 card as its own order costs more in fees and postage than the
    card is worth, which would make a box of commons a negative asset. Nobody
    ships those individually - they go out by the hundred - so the value floor
    is the bulk price, and never below zero.
    """
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


# --- reads --------------------------------------------------------------


def open_positions(db: Database) -> list[Position]:
    rows = db.query(
        """
        SELECT h.card_id, h.variant,
               SUM(h.quantity)                    AS quantity,
               SUM(h.quantity * h.cost_each)      AS total_cost,
               MIN(h.acquired_at)                 AS acquired_at,
               MIN(h.condition)                   AS condition,
               GROUP_CONCAT(h.id)                 AS lot_ids,
               c.name, c.set_name, c.number, c.rarity, c.image_small
        FROM holdings h LEFT JOIN cards c ON c.id = h.card_id
        WHERE h.status = 'open'
        GROUP BY h.card_id, h.variant
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
        metrics = load_metrics(db, position.card_id, position.variant, source, today=today)
        price = metrics.price
        if price is None:
            out.append(ValuedPosition(position, None, None, None, None, None,
                                      None, "unknown", None))
            continue
        market_value = price * position.quantity
        net_value = realistic_net_each(config, price) * position.quantity
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
    }
