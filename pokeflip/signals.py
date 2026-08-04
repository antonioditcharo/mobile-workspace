"""Buy and sell signal engine.

The rule set encodes how a flipper actually decides:

* **Buy** when a card is cheap *relative to its own history* (not relative to
  some absolute idea of value), when the cheapest listing sits well under what
  the card sells for, or when a rising card has not yet run away - and only when
  the trade clears fees with room to spare.
* **Sell** when a position has hit its target, has run hot, has started giving
  back a peak, has gone dead, or has broken down far enough to cut.

Every signal carries the arithmetic that produced it, so a recommendation can
be argued with rather than merely trusted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Any, Sequence

from .analytics import TrendMetrics, load_metrics
from .config import Config
from .db import Database, iso
from .portfolio import Position, open_positions

# A quote older than this is not a basis for a trade.
MAX_STALE_DAYS = 14

# How far one reason alone justifies acting, before any corroboration.
# Reasons to trade are alternatives, not evidence that has to pile up: a
# position that has hit its profit target is a sell whether or not anything
# else agrees, so the strongest reason sets the score and a second one only
# adds to it.
BUY_CONVICTION = {
    "buy_dip": 0.80,
    "buy_spread": 0.78,
    "buy_undervalued": 0.75,
    "buy_momentum": 0.60,
}
SELL_CONVICTION = {
    "sell_target": 0.90,
    "sell_stop_loss": 0.85,
    "sell_peak_fade": 0.72,
    "sell_strength": 0.72,
    "sell_trend_break": 0.60,
    "sell_stagnant": 0.55,
}
# Weight given to the second-strongest reason, as a share of the headroom left.
CORROBORATION = 0.35


def score_actions(actions: Sequence[tuple[float, str]],
                  conviction: dict[str, float]) -> float:
    """Blend triggered reasons into a 0-100 score."""
    strengths = sorted(
        (conviction.get(name, 0.5) * max(0.0, min(1.0, value)) for value, name in actions),
        reverse=True,
    )
    if not strengths:
        return 0.0
    best = strengths[0]
    second = strengths[1] if len(strengths) > 1 else 0.0
    return round(100 * min(1.0, best + CORROBORATION * (1 - best) * second), 1)


@dataclass
class Signal:
    kind: str                  # buy | sell
    action: str                # buy_dip, sell_target, ...
    card_id: str
    variant: str
    source: str
    card_name: str = ""
    set_name: str = ""
    number: str = ""
    rarity: str = ""
    image: str = ""
    currency: str = "USD"

    score: float = 0.0
    price: float = 0.0         # current market price
    entry_price: float = 0.0   # what you would pay per card
    exit_price: float = 0.0    # what you would realistically sell at
    total_cost: float = 0.0    # entry incl. acquisition overhead
    net_proceeds: float = 0.0  # after marketplace fees and shipping
    net_profit: float = 0.0
    roi: float = 0.0
    target_price: float | None = None   # mean-reversion / thesis price
    target_profit: float | None = None
    target_roi: float | None = None
    quantity: int = 1
    hold_days: int | None = None
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def total_net_profit(self) -> float:
        return self.net_profit * max(1, self.quantity)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_net_profit"] = self.total_net_profit
        return data


# --- buy side -----------------------------------------------------------


def evaluate_buy(metrics: TrendMetrics, card: dict[str, Any], config: Config
                 ) -> Signal | None:
    """Score one printing as a purchase candidate. ``None`` means 'not a buy'."""
    rules = config.buy
    price = metrics.price
    if price is None or metrics.points < rules.min_history_points:
        return None
    if metrics.stale_days is not None and metrics.stale_days > MAX_STALE_DAYS:
        return None
    if price < rules.min_price or price > rules.max_price:
        return None
    if metrics.volatility is not None and metrics.volatility > rules.max_volatility:
        return None

    # You buy at the cheapest listing, not at the market average.
    entry = metrics.low if metrics.low and metrics.low > 0 else price
    entry = min(entry, price)
    total_cost = entry + rules.acquisition_overhead
    net_now = config.fees.net_proceeds(price)
    net_profit = net_now - total_cost
    roi = net_profit / total_cost if total_cost > 0 else 0.0

    # Mean reversion thesis: a dip buy pays off when the card returns to its
    # own 30-day average, so that gets scored separately from the instant flip.
    fair_value = metrics.sma30 or price
    target_price = max(fair_value, price)
    target_net = config.fees.net_proceeds(target_price)
    target_profit = target_net - total_cost
    target_roi = target_profit / total_cost if total_cost > 0 else 0.0

    components: list[tuple[str, float, float]] = []  # (name, weight, value 0..1)
    reasons: list[str] = []
    actions: list[tuple[float, str]] = []

    # 1. Discount to its own 30-day average.
    below_sma = 0.0
    if metrics.sma30 and metrics.sma30 > 0:
        below_sma = (metrics.sma30 - price) / metrics.sma30
    dip_value = _ratio(below_sma, rules.dip_pct)
    components.append(("dip", 0.26, dip_value))
    if below_sma >= rules.dip_pct:
        reasons.append(
            f"Trading {below_sma * 100:.1f}% under its 30-day average "
            f"({_money(metrics.sma30, metrics.currency)})"
        )
        actions.append((dip_value, "buy_dip"))

    # 2. Statistically cheap against its own 90-day distribution.
    z_value = 0.0
    if metrics.zscore_90 is not None:
        z_value = _ratio(-metrics.zscore_90, abs(rules.zscore_floor) or 1.0)
        if metrics.zscore_90 <= rules.zscore_floor:
            reasons.append(
                f"Bottom of its 90-day range (z-score {metrics.zscore_90:+.2f})"
            )
            actions.append((z_value, "buy_undervalued"))
    components.append(("value", 0.20, z_value))

    # 3. Listing floor well under market - buy the floor, sell at market.
    spread_value = 0.0
    if metrics.spread_pct is not None:
        spread_value = _ratio(metrics.spread_pct, rules.spread_pct)
        if metrics.spread_pct >= rules.spread_pct:
            reasons.append(
                f"Cheapest listing {_money(metrics.low, metrics.currency)} is "
                f"{metrics.spread_pct * 100:.0f}% below market "
                f"{_money(price, metrics.currency)}"
            )
            actions.append((spread_value, "buy_spread"))
    components.append(("spread", 0.22, spread_value))

    # 4. Early momentum: turning up but not yet extended past the recent peak.
    momentum_value = 0.0
    if metrics.momentum is not None and metrics.momentum > 0:
        headroom = metrics.drawdown_30 or 0.0
        momentum_value = min(1.0, metrics.momentum / 0.10) * (0.5 + min(0.5, headroom * 3))
        if metrics.momentum > 0.03 and metrics.direction == "rising":
            reasons.append(
                f"Trend turning up ({metrics.momentum * 100:+.1f}% 7d vs 30d average)"
            )
            actions.append((momentum_value, "buy_momentum"))
    components.append(("momentum", 0.12, momentum_value))

    # 5. The trade has to clear fees.
    roi_value = _ratio(roi, rules.min_roi)
    components.append(("roi", 0.14, roi_value))

    # 6. Prefer cards that behave predictably.
    stability = 1.0
    if metrics.volatility is not None and rules.max_volatility > 0:
        stability = max(0.0, 1 - metrics.volatility / rules.max_volatility)
    components.append(("stability", 0.06, stability))

    score = score_actions(actions, BUY_CONVICTION)

    # Hard gates: profit and return must both clear, on either thesis.
    clears_now = net_profit >= rules.min_net_profit and roi >= rules.min_roi
    clears_target = (target_profit >= rules.min_net_profit
                     and target_roi >= rules.min_roi)
    if not (clears_now or clears_target):
        return None
    if not actions or score < rules.min_score:
        return None

    # Falling knife guard: a steep downtrend is not a dip.
    if metrics.change_30d is not None and metrics.change_30d < -0.35 \
            and metrics.direction == "falling":
        return None

    actions.sort(reverse=True)
    if metrics.direction == "falling":
        reasons.append("Still trending down - size the position accordingly")

    return Signal(
        kind="buy",
        action=actions[0][1],
        card_id=metrics.card_id,
        variant=metrics.variant,
        source=metrics.source,
        card_name=card.get("name", ""),
        set_name=card.get("set_name", ""),
        number=card.get("number", ""),
        rarity=card.get("rarity", ""),
        image=card.get("image_small", ""),
        currency=metrics.currency,
        score=score,
        price=round(price, 2),
        entry_price=round(entry, 2),
        exit_price=round(price, 2),
        total_cost=round(total_cost, 2),
        net_proceeds=round(net_now, 2),
        net_profit=round(net_profit, 2),
        roi=round(roi, 4),
        target_price=round(target_price, 2),
        target_profit=round(target_profit, 2),
        target_roi=round(target_roi, 4),
        reasons=reasons,
        metrics=_metric_summary(metrics, components),
    )


# --- sell side ----------------------------------------------------------


def evaluate_sell(metrics: TrendMetrics, position: Position, config: Config,
                  today: date | None = None) -> Signal | None:
    """Score an open position as a sale candidate."""
    rules = config.sell
    price = metrics.price
    if price is None:
        return None
    if metrics.stale_days is not None and metrics.stale_days > MAX_STALE_DAYS:
        return None
    if price < config.bulk.filler_price_ceiling:
        # Too cheap to list on its own; the bulk plan handles these.
        return None

    net_each = config.fees.net_proceeds(price)
    cost_each = position.cost_each
    profit_each = net_each - cost_each
    roi = profit_each / cost_each if cost_each > 0 else (1.0 if profit_each > 0 else 0.0)
    hold_days = position.hold_days(today)

    components: list[tuple[str, float, float]] = []
    reasons: list[str] = []
    actions: list[tuple[float, str]] = []

    # 1. Target reached - the boring, most common reason to sell.
    target_value = _ratio(roi, rules.target_roi)
    components.append(("target", 0.30, target_value))
    if roi >= rules.target_roi:
        reasons.append(
            f"Target hit: {_money(net_each, metrics.currency)} net vs "
            f"{_money(cost_each, metrics.currency)} cost ({roi * 100:+.0f}%)"
        )
        actions.append((target_value, "sell_target"))

    # 2. Running hot above its own average.
    over_value = 0.0
    if metrics.sma30 and metrics.sma30 > 0:
        over = (price - metrics.sma30) / metrics.sma30
        over_value = _ratio(over, rules.overextended_pct)
        if over >= rules.overextended_pct and profit_each > 0:
            reasons.append(
                f"{over * 100:.0f}% above its 30-day average - selling into strength"
            )
            actions.append((over_value, "sell_strength"))
    components.append(("overextended", 0.20, over_value))

    # 3. Giving back a peak: exit before the rest of the move is gone.
    fade_value = 0.0
    if metrics.drawdown_30 is not None:
        fade_value = _ratio(metrics.drawdown_30, rules.peak_fade_pct)
        profitable_enough = profit_each > 0 or not rules.peak_fade_requires_profit
        if (metrics.drawdown_30 >= rules.peak_fade_pct
                and metrics.direction != "rising" and profitable_enough):
            reasons.append(
                f"Down {metrics.drawdown_30 * 100:.0f}% from its 30-day peak of "
                f"{_money(metrics.peak_30, metrics.currency)}"
            )
            actions.append((fade_value, "sell_peak_fade"))
    components.append(("peak_fade", 0.18, fade_value))

    # 4. Dead money - the capital is worth more somewhere else.
    stagnant_value = 0.0
    if hold_days is not None and hold_days >= rules.stagnant_days:
        drift = abs(metrics.change_90d or 0.0)
        if drift <= rules.stagnant_band_pct:
            stagnant_value = 1.0
            reasons.append(
                f"Held {hold_days} days and flat ({drift * 100:.1f}% over 90d) - "
                "recycle the capital"
            )
            actions.append((stagnant_value, "sell_stagnant"))
    components.append(("stagnant", 0.10, stagnant_value))

    # 5. Cut the loser.
    loss_value = 0.0
    if roi <= -rules.stop_loss_pct and metrics.direction == "falling":
        loss_value = 1.0
        reasons.append(
            f"Down {roi * 100:.0f}% and still falling - stop-loss level reached"
        )
        actions.append((loss_value, "sell_stop_loss"))
    components.append(("stop_loss", 0.12, loss_value))

    # 6. Trend break while in profit.
    break_value = 0.0
    if metrics.momentum is not None and metrics.momentum < -0.03 and profit_each > 0:
        break_value = min(1.0, abs(metrics.momentum) / 0.10)
        reasons.append("7-day average has crossed below the 30-day while in profit")
        actions.append((break_value, "sell_trend_break"))
    components.append(("trend_break", 0.10, break_value))

    score = score_actions(actions, SELL_CONVICTION)
    if not actions or score < rules.min_score:
        return None

    actions.sort(reverse=True)
    breakeven = config.fees.breakeven_sale_price(cost_each)
    if price < breakeven:
        reasons.append(
            f"Note: break-even needs {_money(breakeven, metrics.currency)} at current fees"
        )

    return Signal(
        kind="sell",
        action=actions[0][1],
        card_id=metrics.card_id,
        variant=metrics.variant,
        source=metrics.source,
        card_name=position.card_name or metrics.card_id,
        set_name=position.set_name,
        number=position.number,
        rarity=position.rarity,
        image=position.image,
        currency=metrics.currency,
        score=score,
        price=round(price, 2),
        entry_price=round(cost_each, 2),
        exit_price=round(price, 2),
        total_cost=round(cost_each, 2),
        net_proceeds=round(net_each, 2),
        net_profit=round(profit_each, 2),
        roi=round(roi, 4),
        quantity=position.quantity,
        hold_days=hold_days,
        reasons=reasons,
        metrics=_metric_summary(metrics, components),
    )


# --- orchestration ------------------------------------------------------


@dataclass
class SignalRun:
    buys: list[Signal] = field(default_factory=list)
    sells: list[Signal] = field(default_factory=list)
    scanned: int = 0
    run_id: int | None = None
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "scanned": self.scanned,
            "buys": [s.to_dict() for s in self.buys],
            "sells": [s.to_dict() for s in self.sells],
        }


def buy_universe(db: Database, config: Config) -> list[tuple[str, str]]:
    """Everything worth scoring as a purchase.

    Watchlist entries and tracked sets always qualify; anything else with
    enough stored history is scanned too, so opportunities are not limited to
    what you already thought to watch.
    """
    source = config.provider.preferred_source
    pairs: dict[tuple[str, str], None] = {}

    for row in db.query(
        """
        SELECT DISTINCT ph.card_id, ph.variant
        FROM price_history ph
        JOIN watchlist w ON w.card_id = ph.card_id
        WHERE ph.source = ? AND w.active = 1
          AND (w.variant = 'any' OR w.variant = ph.variant)
        """,
        (source,),
    ):
        pairs[(row["card_id"], row["variant"])] = None

    if config.tracked_sets:
        placeholders = ",".join("?" * len(config.tracked_sets))
        for row in db.query(
            f"""
            SELECT DISTINCT ph.card_id, ph.variant
            FROM price_history ph JOIN cards c ON c.id = ph.card_id
            WHERE ph.source = ? AND c.set_id IN ({placeholders})
            """,
            (source, *config.tracked_sets),
        ):
            pairs[(row["card_id"], row["variant"])] = None

    for row in db.query(
        """
        SELECT card_id, variant, COUNT(*) AS n
        FROM price_history WHERE source = ?
        GROUP BY card_id, variant
        HAVING n >= ?
        """,
        (source, config.buy.min_history_points),
    ):
        pairs[(row["card_id"], row["variant"])] = None

    return list(pairs)


def generate(db: Database, config: Config, today: date | None = None,
             persist: bool = True) -> SignalRun:
    """Score the whole universe and return ranked buys and sells."""
    source = config.provider.preferred_source
    run = SignalRun(generated_at=iso())

    if persist:
        run.run_id = db.start_run("signals")

    try:
        cards = {row["id"]: dict(row) for row in db.query("SELECT * FROM cards")}

        for card_id, variant in buy_universe(db, config):
            run.scanned += 1
            metrics = load_metrics(db, card_id, variant, source, today=today)
            signal = evaluate_buy(metrics, cards.get(card_id, {}), config)
            if signal:
                run.buys.append(signal)

        for position in open_positions(db):
            metrics = load_metrics(db, position.card_id, position.variant, source,
                                   today=today)
            if metrics.price is None:
                continue
            run.scanned += 1
            signal = evaluate_sell(metrics, position, config, today=today)
            if signal:
                run.sells.append(signal)

        run.buys.sort(key=lambda s: (-s.score, -s.net_profit))
        run.sells.sort(key=lambda s: (-s.score, -s.total_net_profit))
        run.buys = run.buys[: config.buy.max_results]
        run.sells = run.sells[: config.sell.max_results]

        if persist:
            _persist(db, run)
            db.finish_run(run.run_id, "ok", {
                "scanned": run.scanned,
                "buys": len(run.buys),
                "sells": len(run.sells),
            })
    except Exception as exc:  # keep a failed scan visible in run history
        if persist and run.run_id:
            db.finish_run(run.run_id, "error", error=str(exc))
        raise
    return run


def _persist(db: Database, run: SignalRun) -> None:
    rows = [
        (
            run.run_id, s.kind, s.card_id, s.variant, s.score, s.action, s.price,
            json.dumps(s.reasons), json.dumps(s.to_dict()), run.generated_at,
        )
        for s in (*run.buys, *run.sells)
    ]
    if not rows:
        return
    with db.tx() as conn:
        conn.executemany(
            """
            INSERT INTO signals (run_id, kind, card_id, variant, score, action, price,
                                 reasons, metrics, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def latest_signals(db: Database, kind: str | None = None, limit: int = 50
                   ) -> list[dict[str, Any]]:
    """Most recent stored signal set, newest run first."""
    row = db.one("SELECT MAX(run_id) AS run_id FROM signals")
    if not row or row["run_id"] is None:
        return []
    sql = "SELECT * FROM signals WHERE run_id = ?"
    params: list[Any] = [row["run_id"]]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY score DESC LIMIT ?"
    params.append(limit)
    return [json.loads(r["metrics"]) for r in db.query(sql, params)]


# --- helpers ------------------------------------------------------------


def _ratio(value: float | None, threshold: float) -> float:
    """Progress toward a threshold, clamped to 0..1."""
    if value is None or threshold <= 0:
        return 0.0
    return max(0.0, min(1.0, value / threshold))


def _money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "n/a"
    symbol = {"USD": "$", "EUR": "€"}.get(currency, "")
    return f"{symbol}{value:,.2f}"


def _metric_summary(metrics: TrendMetrics, components: Sequence[tuple[str, float, float]]
                    ) -> dict[str, Any]:
    return {
        "price": metrics.price,
        "low": metrics.low,
        "sma7": metrics.sma7,
        "sma30": metrics.sma30,
        "sma90": metrics.sma90,
        "change_7d": metrics.change_7d,
        "change_30d": metrics.change_30d,
        "change_90d": metrics.change_90d,
        "zscore_90": metrics.zscore_90,
        "volatility": metrics.volatility,
        "spread_pct": metrics.spread_pct,
        "drawdown_30": metrics.drawdown_30,
        "momentum": metrics.momentum,
        "direction": metrics.direction,
        "points": metrics.points,
        "stale_days": metrics.stale_days,
        "components": {name: round(value, 3) for name, _, value in components},
    }
