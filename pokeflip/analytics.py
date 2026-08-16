"""Trend math over stored price history.

Everything here is pure: rows in, numbers out. The signal engine, the API and
the digest all read the same :class:`TrendMetrics` so a number shown in the UI
is the same number a decision was made on.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .db import Database


@dataclass
class PricePoint:
    on: date
    market: float
    low: float | None = None
    mid: float | None = None
    high: float | None = None
    direct_low: float | None = None
    sales_count: int | None = None
    listing_count: int | None = None


@dataclass
class TrendMetrics:
    """Everything known about how one printing has been behaving."""

    card_id: str
    variant: str
    source: str
    currency: str = "USD"
    points: int = 0
    last_date: str | None = None
    stale_days: int | None = None

    price: float | None = None          # current market price
    low: float | None = None            # cheapest live listing
    high: float | None = None
    direct_low: float | None = None

    sma7: float | None = None
    sma30: float | None = None
    sma90: float | None = None

    change_1d: float | None = None
    change_7d: float | None = None
    change_30d: float | None = None
    change_90d: float | None = None

    zscore_90: float | None = None
    volatility: float | None = None     # stdev of daily returns
    dispersion: float | None = None     # stdev/mean over the window

    peak_30: float | None = None
    peak_90: float | None = None
    trough_30: float | None = None
    trough_90: float | None = None
    drawdown_30: float | None = None    # how far below the 30d peak we are
    runup_30: float | None = None       # how far above the 30d trough we are

    momentum: float | None = None       # sma7 vs sma30
    trend_slope: float | None = None    # fitted %/day over 30d
    spread_pct: float | None = None     # (market - low) / market
    direction: str = "unknown"          # rising | falling | flat | unknown

    # Only sources with real transaction data fill these in. ``None`` means the
    # source does not report it, which is not the same as "nothing sold".
    sales_per_week: float | None = None
    listing_count: int | None = None
    liquidity: float | None = None      # 0-1, higher is easier to sell
    liquidity_basis: str = "none"       # observed | inferred | none

    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, include_history: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_history:
            data.pop("history", None)
        return data

    @property
    def has_signal_grade_history(self) -> bool:
        return self.points >= 5 and self.price is not None


# --- series helpers -----------------------------------------------------


def rows_to_points(rows: Iterable[Any]) -> list[PricePoint]:
    """Convert stored price rows into a clean, date-ordered series.

    Rows with no usable price are dropped rather than zero-filled: a missing
    day is missing data, and pretending it was $0 would poison every average.
    """
    points: list[PricePoint] = []
    for row in rows:
        market = row["market"] or row["mid"] or row["low"]
        if market is None or market <= 0:
            continue
        points.append(
            PricePoint(
                on=date.fromisoformat(row["captured_on"]),
                market=float(market),
                low=_pos(row["low"]),
                mid=_pos(row["mid"]),
                high=_pos(row["high"]),
                direct_low=_pos(row["direct_low"]),
                sales_count=_int(_get(row, "sales_count")),
                listing_count=_int(_get(row, "listing_count")),
            )
        )
    points.sort(key=lambda p: p.on)
    return points


def _get(row: Any, key: str) -> Any:
    """Read a column that older databases may not have."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pos(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def sma(values: Sequence[float], window: int) -> float | None:
    """Simple mean of the last ``window`` values.

    Returns a partial average when history is shorter than the window - a
    12-day-old card should still get a usable 30d reference - but callers
    gate on ``points`` before trusting it.
    """
    if not values:
        return None
    slice_ = values[-window:]
    return sum(slice_) / len(slice_)


def value_days_ago(points: Sequence[PricePoint], days: int) -> float | None:
    """Price as of ``days`` before the latest observation.

    Uses the most recent observation at or before the target date so gaps in
    collection do not silently shift the comparison window.
    """
    if not points:
        return None
    target = points[-1].on - timedelta(days=days)
    candidate = None
    for point in points:
        if point.on <= target:
            candidate = point
        else:
            break
    return candidate.market if candidate else None


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous


def daily_returns(points: Sequence[PricePoint]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(points, points[1:]):
        if prev.market > 0:
            span = max(1, (cur.on - prev.on).days)
            # Normalise to a per-day figure so gaps do not look like huge moves.
            out.append(((cur.market - prev.market) / prev.market) / span)
    return out


def linear_slope(points: Sequence[PricePoint]) -> float | None:
    """Least-squares slope as a fraction of mean price per day."""
    if len(points) < 3:
        return None
    base = points[0].on.toordinal()
    xs = [p.on.toordinal() - base for p in points]
    ys = [p.market for p in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0 or mean_y <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    return slope / mean_y


def window(points: Sequence[PricePoint], days: int) -> list[PricePoint]:
    if not points:
        return []
    cutoff = points[-1].on - timedelta(days=days)
    return [p for p in points if p.on >= cutoff]


# --- metric computation -------------------------------------------------


def compute_metrics(
    card_id: str,
    variant: str,
    source: str,
    rows: Iterable[Any],
    currency: str = "USD",
    today: date | None = None,
    include_history: bool = False,
) -> TrendMetrics:
    points = rows_to_points(rows)
    metrics = TrendMetrics(card_id=card_id, variant=variant, source=source,
                           currency=currency, points=len(points))
    if not points:
        return metrics

    latest = points[-1]
    metrics.last_date = latest.on.isoformat()
    metrics.stale_days = (
        (today or datetime.now(timezone.utc).date()) - latest.on
    ).days
    metrics.price = round(latest.market, 4)
    metrics.low = latest.low
    metrics.high = latest.high
    metrics.direct_low = latest.direct_low

    w30 = window(points, 30)
    w90 = window(points, 90)

    metrics.sma7 = _round(sma([p.market for p in window(points, 7)], 7))
    metrics.sma30 = _round(sma([p.market for p in w30], 30))
    metrics.sma90 = _round(sma([p.market for p in w90], 90))

    metrics.change_1d = pct_change(latest.market, value_days_ago(points, 1))
    metrics.change_7d = pct_change(latest.market, value_days_ago(points, 7))
    metrics.change_30d = pct_change(latest.market, value_days_ago(points, 30))
    metrics.change_90d = pct_change(latest.market, value_days_ago(points, 90))

    w90_prices = [p.market for p in w90]
    if len(w90_prices) >= 5:
        mean90 = statistics.fmean(w90_prices)
        stdev90 = statistics.pstdev(w90_prices)
        if stdev90 > 0:
            metrics.zscore_90 = (latest.market - mean90) / stdev90
        if mean90 > 0:
            metrics.dispersion = stdev90 / mean90

    returns = daily_returns(window(points, 60))
    if len(returns) >= 4:
        metrics.volatility = statistics.pstdev(returns)

    if w30:
        w30_prices = [p.market for p in w30]
        metrics.peak_30 = max(w30_prices)
        metrics.trough_30 = min(w30_prices)
        if metrics.peak_30 > 0:
            metrics.drawdown_30 = (metrics.peak_30 - latest.market) / metrics.peak_30
        if metrics.trough_30 > 0:
            metrics.runup_30 = (latest.market - metrics.trough_30) / metrics.trough_30
    if w90_prices:
        metrics.peak_90 = max(w90_prices)
        metrics.trough_90 = min(w90_prices)

    if metrics.sma7 and metrics.sma30 and metrics.sma30 > 0:
        metrics.momentum = (metrics.sma7 - metrics.sma30) / metrics.sma30
    metrics.trend_slope = linear_slope(w30)

    if latest.low and latest.market > 0:
        metrics.spread_pct = (latest.market - latest.low) / latest.market

    metrics.direction = classify_direction(metrics)
    _apply_liquidity(metrics, points)

    if include_history:
        metrics.history = [
            {
                "on": p.on.isoformat(),
                "market": round(p.market, 4),
                "low": p.low,
                "high": p.high,
            }
            for p in points
        ]
    return metrics


# Weekly sales that count as fully liquid. Above this, more volume does not
# make a card meaningfully easier to move.
LIQUID_SALES_PER_WEEK = 12.0
# ``sales_count`` is a *trailing window total*, not sales since the last
# snapshot. Providers normalise to this window so consecutive days can be
# compared and never summed - summing rolling totals would count the same sale
# thirty times.
SALES_WINDOW_DAYS = 30


def _apply_liquidity(metrics: TrendMetrics, points: Sequence[PricePoint]) -> None:
    """Score how easy the card is to actually sell.

    Two regimes. If the source reports real sales counts, liquidity is
    *observed* and comes straight from the sale rate. If it does not - which is
    the case for marketplace-aggregate sources - liquidity is *inferred* from
    price stability and a tight bid/ask spread, and is a much weaker claim. The
    basis is reported alongside the number so nobody mistakes one for the other.
    """
    recent = window(points, 30)
    observed = [p for p in recent if p.sales_count is not None]

    if observed:
        # Latest reading only: each is already a trailing-window total.
        latest_count = observed[-1].sales_count or 0
        metrics.sales_per_week = round(latest_count / SALES_WINDOW_DAYS * 7, 2)
        metrics.liquidity = round(
            min(1.0, metrics.sales_per_week / LIQUID_SALES_PER_WEEK), 3)
        metrics.liquidity_basis = "observed"
    else:
        # No transaction data: a card whose price barely moves and whose
        # cheapest listing sits close to market is probably trading.
        stability = None
        if metrics.dispersion is not None:
            stability = max(0.0, 1 - metrics.dispersion / 0.35)
        tightness = None
        if metrics.spread_pct is not None:
            tightness = max(0.0, 1 - metrics.spread_pct / 0.40)
        parts = [v for v in (stability, tightness) if v is not None]
        if parts:
            metrics.liquidity = round(sum(parts) / len(parts), 3)
            metrics.liquidity_basis = "inferred"

    listings = [p.listing_count for p in recent if p.listing_count is not None]
    if listings:
        metrics.listing_count = listings[-1]


# Observations needed before a trend label means anything. With one point the
# moving averages are equal to it by construction, so momentum computes to
# exactly zero and the card is confidently reported as "flat" on no evidence.
MIN_POINTS_FOR_DIRECTION = 3


def classify_direction(metrics: TrendMetrics) -> str:
    """Label the trend from momentum and slope together.

    Requiring both to agree keeps a single noisy day from flipping the label,
    and a minimum number of observations keeps the label from being invented.
    """
    if metrics.points < MIN_POINTS_FOR_DIRECTION:
        return "unknown"
    momentum = metrics.momentum
    slope = metrics.trend_slope
    if momentum is None and slope is None:
        return "unknown"
    votes = 0
    if momentum is not None:
        votes += 1 if momentum > 0.02 else -1 if momentum < -0.02 else 0
    if slope is not None:
        votes += 1 if slope > 0.001 else -1 if slope < -0.001 else 0
    if votes > 0:
        return "rising"
    if votes < 0:
        return "falling"
    return "flat"


def load_metrics(
    db: Database,
    card_id: str,
    variant: str,
    source: str,
    days: int = 180,
    include_history: bool = False,
    today: date | None = None,
    as_of: date | None = None,
) -> TrendMetrics:
    """Metrics for one printing.

    ``as_of`` restricts the series to what was knowable on that date, so a
    replay cannot see prices that had not happened yet.
    """
    rows = db.price_series(card_id, variant, source, days=days,
                           as_of=as_of.isoformat() if as_of else None)
    currency = rows[-1]["currency"] if rows else "USD"
    return compute_metrics(card_id, variant, source, rows, currency=currency,
                           today=today or as_of, include_history=include_history)


def load_many(
    db: Database,
    pairs: Iterable[tuple[str, str]],
    source: str,
    days: int = 180,
    today: date | None = None,
) -> list[TrendMetrics]:
    return [load_metrics(db, cid, var, source, days=days, today=today)
            for cid, var in pairs]


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def fmt_pct(value: float | None, places: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value * 100:+.{places}f}%"


def fmt_money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "-"
    symbol = {"USD": "$", "EUR": "€"}.get(currency, "")
    return f"{symbol}{value:,.2f}"


def spark_series(metrics: "TrendMetrics", points: int = 30) -> list[float]:
    """A short price series for an inline sparkline.

    Downsampled to a fixed length so a row's chart costs the same whether the
    card has 30 days of history or 700.
    """
    history = [h["market"] for h in metrics.history if h.get("market")]
    if len(history) <= points:
        return [round(v, 4) for v in history]
    step = len(history) / points
    return [round(history[min(len(history) - 1, int(i * step))], 4)
            for i in range(points)]


def annualized_volatility(volatility: float | None) -> float | None:
    """Daily stdev scaled to a yearly figure, for comparing across cards."""
    if volatility is None:
        return None
    return volatility * math.sqrt(365)
