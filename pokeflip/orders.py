"""Orders and listings - the loop between a recommendation and a position.

An order is one intent to trade. A **buy order** is something you have bid on
or are about to; filling it creates the holding, with the signal that prompted
it attached, so later you can ask whether the app's advice actually worked. A
**sell order is a live listing**: same lifecycle, so days on market and price
cuts belong here rather than in a parallel table.

The half most flippers do by feel is the listing that has been sitting for
three weeks. ``listing_health`` puts a number on it: how long it has been up,
where the market has moved since, and what to re-price it to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from typing import Any, Sequence

from .analytics import load_metrics
from .config import Config
from .db import Database, iso, parse_ts, utcnow
from .portfolio import add_holding, condition_price, sell_holding

OPEN = "open"
FILLED = "filled"
CANCELLED = "cancelled"
EXPIRED = "expired"


@dataclass
class Order:
    id: int
    kind: str
    status: str
    card_id: str
    variant: str
    condition: str
    quantity: int
    limit_price: float | None = None
    original_price: float | None = None
    marketplace: str = ""
    holding_id: int | None = None
    signal_action: str | None = None
    signal_score: float | None = None
    reference_price: float | None = None
    filled_price: float | None = None
    filled_quantity: int | None = None
    fees: float | None = None
    price_cuts: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    closed_at: str | None = None
    card_name: str = ""
    set_name: str = ""
    number: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def days_open(self, today: date | None = None) -> int | None:
        return _days_since(self.created_at, today)

    def days_at_price(self, today: date | None = None) -> int | None:
        """Days the *current* ask has been up.

        Staleness is a property of the price, not of the listing. Measuring
        from the listing date would re-flag a card the moment you cut it and
        grind the ask down to the floor one cycle at a time.
        """
        if self.price_cuts:
            last = self.price_cuts[-1].get("at")
            if last:
                return _days_since(last, today)
        return _days_since(self.created_at, today)


def _days_since(stamp: str | None, today: date | None = None) -> int | None:
    if not stamp:
        return None
    try:
        started = parse_ts(stamp).date()
    except (ValueError, TypeError):
        return None
    return ((today or utcnow().date()) - started).days


def _row_to_order(row: Any) -> Order:
    try:
        cuts = json.loads(row["price_cuts"] or "[]")
    except (TypeError, ValueError):
        cuts = []
    return Order(
        id=int(row["id"]),
        kind=row["kind"],
        status=row["status"],
        card_id=row["card_id"],
        variant=row["variant"],
        condition=row["condition"],
        quantity=int(row["quantity"] or 0),
        limit_price=row["limit_price"],
        original_price=row["original_price"],
        marketplace=row["marketplace"] or "",
        holding_id=row["holding_id"],
        signal_action=row["signal_action"],
        signal_score=row["signal_score"],
        reference_price=row["reference_price"],
        filled_price=row["filled_price"],
        filled_quantity=row["filled_quantity"],
        fees=row["fees"],
        price_cuts=cuts,
        notes=row["notes"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
        card_name=_opt(row, "card_name") or row["card_id"],
        set_name=_opt(row, "set_name") or "",
        number=_opt(row, "number") or "",
    )


def _opt(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


_SELECT = """
    SELECT o.*, c.name AS card_name, c.set_name, c.number
    FROM orders o LEFT JOIN cards c ON c.id = o.card_id
"""


# --- creating -----------------------------------------------------------


def create_order(
    db: Database,
    config: Config,
    kind: str,
    card_id: str,
    quantity: int = 1,
    limit_price: float | None = None,
    variant: str = "normal",
    condition: str = "NM",
    marketplace: str | None = None,
    holding_id: int | None = None,
    signal_action: str | None = None,
    signal_score: float | None = None,
    reference_price: float | None = None,
    notes: str = "",
) -> int:
    if kind not in {"buy", "sell"}:
        raise ValueError("kind must be 'buy' or 'sell'")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not db.get_card(card_id):
        raise ValueError(f"unknown card {card_id!r}; search for it first")

    if kind == "sell":
        holding_id = _resolve_sell_lot(db, card_id, variant, condition, quantity,
                                       holding_id)

    now = iso()
    return db.execute(
        """
        INSERT INTO orders (kind, status, card_id, variant, condition, quantity,
                            limit_price, original_price, marketplace, holding_id,
                            signal_action, signal_score, reference_price,
                            price_cuts, notes, created_at, updated_at)
        VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)
        """,
        (kind, card_id, variant, condition, quantity, limit_price, limit_price,
         marketplace or config.orders.default_marketplace, holding_id,
         signal_action, signal_score, reference_price, notes, now, now),
    )


def _resolve_sell_lot(db: Database, card_id: str, variant: str, condition: str,
                      quantity: int, holding_id: int | None) -> int | None:
    """Make sure a listing is backed by cards you actually hold and have not
    already listed elsewhere."""
    if holding_id is not None:
        row = db.one("SELECT * FROM holdings WHERE id = ? AND status = 'open'",
                     (holding_id,))
        if row is None:
            raise ValueError(f"no open holding with id {holding_id}")
        if int(row["quantity"]) < quantity:
            raise ValueError(
                f"holding {holding_id} has {row['quantity']}, cannot list {quantity}")
        return holding_id

    held = db.one(
        """
        SELECT COALESCE(SUM(quantity), 0) AS n FROM holdings
        WHERE card_id = ? AND variant = ? AND condition = ? AND status = 'open'
        """,
        (card_id, variant, condition),
    )
    listed = db.one(
        """
        SELECT COALESCE(SUM(quantity), 0) AS n FROM orders
        WHERE card_id = ? AND variant = ? AND condition = ?
          AND kind = 'sell' AND status = 'open'
        """,
        (card_id, variant, condition),
    )
    available = int(held["n"] or 0) - int(listed["n"] or 0)
    if quantity > available:
        raise ValueError(
            f"only {available} unlisted copies of {card_id} ({variant}, {condition}) "
            f"are available to list"
        )
    return None


def create_from_signal(db: Database, config: Config, signal: dict[str, Any],
                       quantity: int | None = None) -> int:
    """Turn a recommendation into an order without retyping its numbers.

    A buy is placed at the price the signal said to bid; a sell is listed at
    the market price it was scored against.
    """
    kind = signal.get("kind")
    if kind not in {"buy", "sell"}:
        raise ValueError("signal must have kind 'buy' or 'sell'")
    price = (signal.get("entry_price") if kind == "buy" else signal.get("price"))
    return create_order(
        db, config, kind,
        card_id=signal["card_id"],
        quantity=quantity or (1 if kind == "buy" else int(signal.get("quantity") or 1)),
        limit_price=price,
        variant=signal.get("variant", "normal"),
        condition=signal.get("condition", "NM"),
        signal_action=signal.get("action"),
        signal_score=signal.get("score"),
        reference_price=signal.get("price"),
        notes=(signal.get("reasons") or [""])[0],
    )


# --- transitions --------------------------------------------------------


def fill_order(db: Database, config: Config, order_id: int,
               price: float | None = None, quantity: int | None = None,
               when: str | None = None) -> dict[str, Any]:
    """Record that an order went through.

    A filled buy becomes a holding; a filled listing closes out the lot behind
    it and books the realised profit. Either way the paperwork stops being your
    problem.
    """
    order = get_order(db, order_id)
    if order is None:
        raise ValueError(f"no order with id {order_id}")
    if order.status != OPEN:
        raise ValueError(f"order {order_id} is already {order.status}")

    quantity = int(quantity or order.quantity)
    if quantity <= 0 or quantity > order.quantity:
        raise ValueError(f"fill quantity must be between 1 and {order.quantity}")
    price = float(price if price is not None else (order.limit_price or 0.0))
    when = when or iso()

    result: dict[str, Any] = {
        "order_id": order_id, "kind": order.kind, "card_id": order.card_id,
        "quantity": quantity, "price_each": round(price, 2), "filled_at": when,
    }

    if order.kind == "buy":
        holding_id = add_holding(
            db, order.card_id, order.variant, quantity, price, order.condition,
            acquired_at=when, acquired_from=order.marketplace,
            notes=f"order {order_id}" + (f" ({order.signal_action})"
                                         if order.signal_action else ""),
        )
        result["holding_id"] = holding_id
        result["cost_basis"] = round(price * quantity, 2)
    else:
        lot_id = order.holding_id or _pick_lot_for_sale(db, order)
        if lot_id is None:
            raise ValueError(
                f"no open lot of {order.card_id} left to settle order {order_id}")
        sale = sell_holding(db, lot_id, quantity, price, config, sold_at=when)
        db.execute("UPDATE holdings SET sale_order_id = ? WHERE id = ?",
                   (order_id, lot_id))
        result.update({
            "net_proceeds": sale["net_proceeds"],
            "cost_basis": sale["cost_basis"],
            "realized_pnl": sale["realized_pnl"],
            "roi": sale["roi"],
        })

    # A partial fill leaves the rest of the order live.
    remaining = order.quantity - quantity
    if remaining > 0:
        db.execute(
            "UPDATE orders SET quantity = ?, updated_at = ? WHERE id = ?",
            (remaining, when, order_id),
        )
        new_id = db.execute(
            """
            INSERT INTO orders (kind, status, card_id, variant, condition, quantity,
                                limit_price, original_price, marketplace, holding_id,
                                signal_action, signal_score, reference_price,
                                filled_price, filled_quantity, price_cuts, notes,
                                created_at, updated_at, closed_at)
            VALUES (?, 'filled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order.kind, order.card_id, order.variant, order.condition, quantity,
             order.limit_price, order.original_price, order.marketplace,
             order.holding_id, order.signal_action, order.signal_score,
             order.reference_price, price, quantity, json.dumps(order.price_cuts),
             order.notes, order.created_at, when, when),
        )
        result["order_id"] = new_id
        result["remaining_order_id"] = order_id
        result["remaining_quantity"] = remaining
    else:
        db.execute(
            """
            UPDATE orders SET status='filled', filled_price=?, filled_quantity=?,
                              updated_at=?, closed_at=? WHERE id=?
            """,
            (price, quantity, when, when, order_id),
        )
    return result


def _pick_lot_for_sale(db: Database, order: Order) -> int | None:
    row = db.one(
        """
        SELECT id FROM holdings
        WHERE card_id = ? AND variant = ? AND condition = ? AND status = 'open'
        ORDER BY acquired_at LIMIT 1
        """,
        (order.card_id, order.variant, order.condition),
    )
    return int(row["id"]) if row else None


def cancel_order(db: Database, order_id: int, status: str = CANCELLED) -> bool:
    return db.execute(
        "UPDATE orders SET status=?, updated_at=?, closed_at=? WHERE id=? AND status='open'",
        (status, iso(), iso(), order_id),
    ) > 0


def reprice(db: Database, order_id: int, new_price: float, reason: str = "") -> dict[str, Any]:
    """Change a listing's ask, keeping the history of what it has been."""
    order = get_order(db, order_id)
    if order is None:
        raise ValueError(f"no order with id {order_id}")
    if order.status != OPEN:
        raise ValueError(f"order {order_id} is {order.status}, not open")
    if new_price <= 0:
        raise ValueError("price must be positive")

    cuts = list(order.price_cuts)
    cuts.append({
        "at": iso(),
        "from": order.limit_price,
        "to": round(new_price, 2),
        "reason": reason,
    })
    db.execute(
        "UPDATE orders SET limit_price=?, price_cuts=?, updated_at=? WHERE id=?",
        (new_price, json.dumps(cuts), iso(), order_id),
    )
    return {"order_id": order_id, "from": order.limit_price,
            "to": round(new_price, 2), "cuts": len(cuts)}


def expire_stale(db: Database, config: Config, today: date | None = None) -> list[int]:
    """Close out buy orders that were never going to fill."""
    cutoff = (utcnow() - timedelta(days=config.orders.buy_order_expiry_days)).isoformat()
    rows = db.query(
        "SELECT id FROM orders WHERE kind='buy' AND status='open' AND created_at < ?",
        (cutoff,),
    )
    ids = [int(r["id"]) for r in rows]
    for order_id in ids:
        cancel_order(db, order_id, EXPIRED)
    return ids


# --- reading ------------------------------------------------------------


def get_order(db: Database, order_id: int) -> Order | None:
    row = db.one(_SELECT + " WHERE o.id = ?", (order_id,))
    return _row_to_order(row) if row else None


def list_orders(db: Database, kind: str | None = None, status: str | None = OPEN,
                limit: int = 200) -> list[Order]:
    sql = _SELECT + " WHERE 1=1"
    params: list[Any] = []
    if kind:
        sql += " AND o.kind = ?"
        params.append(kind)
    if status:
        sql += " AND o.status = ?"
        params.append(status)
    sql += " ORDER BY o.created_at DESC LIMIT ?"
    params.append(limit)
    return [_row_to_order(r) for r in db.query(sql, params)]


# --- listing health -----------------------------------------------------


@dataclass
class ListingReview:
    order: Order
    days_on_market: int | None
    days_at_price: int | None
    market_price: float | None
    ask_vs_market: float | None      # how far your ask sits above market
    market_drift: float | None       # market move since you listed
    suggested_price: float | None
    verdict: str                     # hold | cut | raise | pull | unknown
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = self.order.to_dict()
        data.update({
            "days_on_market": self.days_on_market,
            "days_at_price": self.days_at_price,
            "market_price": self.market_price,
            "ask_vs_market": self.ask_vs_market,
            "market_drift": self.market_drift,
            "suggested_price": self.suggested_price,
            "verdict": self.verdict,
            "reason": self.reason,
        })
        return data


def review_listing(db: Database, config: Config, order: Order,
                   today: date | None = None) -> ListingReview:
    """Decide whether a live listing should be left alone, cut, or raised."""
    rules = config.orders
    metrics = load_metrics(db, order.card_id, order.variant,
                           config.provider.preferred_source, today=today)
    days = order.days_open(today)
    days_at_price = order.days_at_price(today)
    ask = order.limit_price

    if metrics.price is None or ask is None:
        return ListingReview(order, days, days_at_price, None, None, None, None, "unknown",
                             "No current price for this card - run a refresh.")

    # Compare like with like: your LP copy is not worth the NM quote.
    market = condition_price(config, metrics.price, order.condition)
    ask_vs_market = (ask - market) / market if market > 0 else None
    drift = None
    if order.reference_price:
        reference = condition_price(config, order.reference_price, order.condition)
        if reference > 0:
            drift = (market - reference) / reference

    floor = market * rules.price_floor_vs_market
    stale = days_at_price is not None and days_at_price >= rules.stale_listing_days

    # Underpriced: the market came to you. Raising beats leaving money behind.
    if ask_vs_market is not None and ask_vs_market < -rules.drift_tolerance:
        return ListingReview(
            order, days, days_at_price, round(market, 2), round(ask_vs_market, 4),
            round(drift, 4) if drift is not None else None,
            round(market * 0.99, 2), "raise",
            f"Your ask is {abs(ask_vs_market) * 100:.0f}% under a market that has "
            f"moved to ${market:,.2f} - you are leaving money on the table.",
        )

    if stale and ask_vs_market is not None and ask_vs_market > rules.drift_tolerance:
        target = max(floor, min(market * 0.99, ask * (1 - rules.stale_cut_pct)))
        return ListingReview(
            order, days, days_at_price, round(market, 2), round(ask_vs_market, 4),
            round(drift, 4) if drift is not None else None, round(target, 2), "cut",
            f"{days_at_price} days at this price and {ask_vs_market * 100:.0f}% over "
            f"the ${market:,.2f} market.",
        )

    if stale:
        target = max(floor, ask * (1 - rules.stale_cut_pct))
        verdict = "cut"
        reason = (f"{days_at_price} days at this price with no sale"
                  + (f" ({days} days listed)" if days != days_at_price else "") + ".")
        if metrics.direction == "falling":
            verdict = "pull"
            reason += " The card is trending down - cut hard or take it back and bulk it."
        return ListingReview(
            order, days, days_at_price, round(market, 2), round(ask_vs_market, 4),
            round(drift, 4) if drift is not None else None, round(target, 2),
            verdict, reason)

    if ask_vs_market is not None and ask_vs_market > rules.drift_tolerance * 2:
        target = max(floor, market * 1.02)
        return ListingReview(
            order, days, days_at_price, round(market, 2), round(ask_vs_market, 4),
            round(drift, 4) if drift is not None else None, round(target, 2), "cut",
            f"Priced {ask_vs_market * 100:.0f}% above the ${market:,.2f} market - "
            "it will not sell there.",
        )

    return ListingReview(
        order, days, days_at_price, round(market, 2),
        round(ask_vs_market, 4) if ask_vs_market is not None else None,
        round(drift, 4) if drift is not None else None, None, "hold",
        "Priced in line with the market.",
    )


def listing_health(db: Database, config: Config, today: date | None = None
                   ) -> dict[str, Any]:
    """Every live listing, with a verdict on each."""
    reviews = [review_listing(db, config, order, today)
               for order in list_orders(db, kind="sell", status=OPEN)]
    needs_action = [r for r in reviews if r.verdict in {"cut", "raise", "pull"}]
    listed_value = sum((r.order.limit_price or 0) * r.order.quantity for r in reviews)
    return {
        "as_of": iso(),
        "listings": [r.to_dict() for r in reviews],
        "needs_action": [r.to_dict() for r in needs_action],
        "count": len(reviews),
        "listed_value": round(listed_value, 2),
        "stale_count": sum(
            1 for r in reviews
            if r.days_on_market is not None
            and r.days_on_market >= config.orders.stale_listing_days
        ),
    }


def apply_suggestions(db: Database, config: Config, only_verdicts: Sequence[str] = ("cut",),
                      today: date | None = None) -> list[dict[str, Any]]:
    """Re-price every listing whose review calls for it.

    Kept explicit rather than automatic: changing your own asking prices is a
    decision, and it should be one you asked for.
    """
    applied: list[dict[str, Any]] = []
    for order in list_orders(db, kind="sell", status=OPEN):
        review = review_listing(db, config, order, today)
        if review.verdict in only_verdicts and review.suggested_price:
            applied.append(reprice(db, order.id, review.suggested_price,
                                   f"auto: {review.verdict} - {review.reason}"))
    return applied


def summary(db: Database, config: Config) -> dict[str, Any]:
    """Counts and money for the order book."""
    buys = list_orders(db, kind="buy", status=OPEN)
    sells = list_orders(db, kind="sell", status=OPEN)
    return {
        "open_buys": len(buys),
        "open_buy_value": round(
            sum((o.limit_price or 0) * o.quantity for o in buys), 2),
        "open_listings": len(sells),
        "listed_value": round(
            sum((o.limit_price or 0) * o.quantity for o in sells), 2),
    }
