"""Replay the signal engine against history and grade what it said.

For every day in the lookback window the engine is re-run using **only** the
prices that existed on that day, and the recommendation is then checked against
what actually happened over the following weeks. The result is a scorecard per
reason:

    buy_dip      42 signals   64% profitable at 30d   median +11.3%
    buy_momentum 18 signals   39% profitable at 30d   median  -2.1%

That is what turns threshold tuning from taste into evidence, and it is the
only honest way to find out which of these rules you should stop trusting.

Two things to be clear about:

* **No look-ahead.** Metrics on day D are computed from a series truncated at
  D. The forward window is only ever read to score the outcome.
* **Sell signals need a cost basis you did not have.** A synthetic one is used:
  the card's own market price ``sell_entry_lookback`` days before the signal,
  as if you had bought it then. Sells are therefore graded on whether the
  *timing* was right - did the price stall or fall afterwards - not on a
  profit you never actually made.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from .analytics import PricePoint, compute_metrics, rows_to_points
from .config import Config
from .db import Database, iso
from .portfolio import Position
from .signals import evaluate_buy, evaluate_sell


@dataclass
class Outcome:
    """One replayed signal, measured over one forward window."""

    kind: str
    action: str
    card_id: str
    variant: str
    card_name: str
    signal_date: str
    score: float
    signal_price: float
    entry_price: float
    horizon_days: int
    exit_price: float | None = None
    forward_return: float | None = None   # market move over the window
    net_profit: float | None = None       # buys: after fees, per card
    roi: float | None = None
    max_favorable: float | None = None    # best move available in the window
    max_adverse: float | None = None      # worst drawdown suffered in the window
    outcome: str = "open"                 # win | loss | flat | unresolved

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionStats:
    action: str
    kind: str
    horizon_days: int
    signals: int = 0
    resolved: int = 0
    win_rate: float | None = None
    median_return: float | None = None
    mean_return: float | None = None
    median_roi: float | None = None
    total_net_profit: float | None = None
    best: float | None = None
    worst: float | None = None
    avg_max_adverse: float | None = None
    verdict: str = "insufficient_data"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestReport:
    generated_at: str = ""
    run_id: int | None = None
    start: str = ""
    end: str = ""
    horizons: list[int] = field(default_factory=list)
    days_replayed: int = 0
    signals: int = 0
    stats: list[ActionStats] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self, include_outcomes: bool = False) -> dict[str, Any]:
        data = {
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "start": self.start,
            "end": self.end,
            "horizons": self.horizons,
            "days_replayed": self.days_replayed,
            "signals": self.signals,
            "stats": [s.to_dict() for s in self.stats],
            "notes": self.notes,
        }
        if include_outcomes:
            data["outcomes"] = [o.to_dict() for o in self.outcomes]
        return data

    def for_action(self, action: str, horizon: int) -> ActionStats | None:
        for entry in self.stats:
            if entry.action == action and entry.horizon_days == horizon:
                return entry
        return None


# A win needs to beat this, so a signal that moved nothing is not scored as a
# success just because it rounded up.
FLAT_BAND = 0.02
# Fewest graded signals before a verdict is offered at all.
MIN_SAMPLE = 8


def run(db: Database, config: Config, lookback_days: int | None = None,
        horizons: Sequence[int] | None = None, persist: bool = True,
        end: date | None = None) -> BacktestReport:
    """Replay the window and return the scorecard."""
    rules = config.backtest
    lookback = lookback_days or rules.lookback_days
    horizons = sorted(horizons or rules.horizons)
    source = config.provider.preferred_source

    series = db.full_series(source)
    cards = {row["id"]: dict(row) for row in db.query("SELECT * FROM cards")}
    points_by_key = {key: rows_to_points(rows) for key, rows in series.items()}
    points_by_key = {k: v for k, v in points_by_key.items() if len(v) >= 10}

    report = BacktestReport(generated_at=iso(), horizons=list(horizons))
    if not points_by_key:
        report.notes.append("No price history to replay yet.")
        return report

    last_day = end or max(p[-1].on for p in points_by_key.values())
    first_day = max(
        min(p[0].on for p in points_by_key.values()),
        last_day - timedelta(days=lookback),
    )
    # A signal needs a full forward window to be graded, so replaying past this
    # point would only produce unresolved rows.
    final_decision_day = last_day - timedelta(days=min(horizons))
    if final_decision_day <= first_day:
        report.notes.append(
            f"Not enough history: need more than {min(horizons)} days beyond the "
            "start of the series to grade anything."
        )
        return report

    report.start = first_day.isoformat()
    report.end = final_decision_day.isoformat()
    if persist:
        report.run_id = db.start_run("backtest")

    try:
        last_signal_on: dict[tuple[str, str, str], date] = {}
        day_cursor = first_day
        step = max(1, rules.step_days)

        while day_cursor <= final_decision_day:
            report.days_replayed += 1
            for (card_id, variant), points in points_by_key.items():
                past = [p for p in points if p.on <= day_cursor]
                if len(past) < config.buy.min_history_points:
                    continue
                future = [p for p in points if p.on > day_cursor]
                if not future:
                    continue

                metrics = compute_metrics(
                    card_id, variant, source, _as_rows(past), today=day_cursor)
                card = cards.get(card_id, {})

                buy = evaluate_buy(metrics, card, config)
                if buy and _fresh(last_signal_on, card_id, variant, buy.action,
                                  day_cursor, rules.dedupe_days):
                    report.outcomes.extend(
                        _grade_buy(buy, metrics, past, future, config, horizons,
                                   day_cursor, card))

                sell = _evaluate_synthetic_sell(
                    metrics, past, config, day_cursor, card, rules.sell_entry_lookback)
                if sell and _fresh(last_signal_on, card_id, variant, sell.action,
                                   day_cursor, rules.dedupe_days):
                    report.outcomes.extend(
                        _grade_sell(sell, metrics, future, horizons, day_cursor, card))

            day_cursor += timedelta(days=step)

        report.signals = len({
            (o.card_id, o.variant, o.signal_date, o.action) for o in report.outcomes
        })
        report.stats = summarise(report.outcomes)
        report.notes.extend(_report_notes(report, config))

        if persist and report.run_id:
            _persist(db, report)
            db.finish_run(report.run_id, "ok", {
                "days": report.days_replayed,
                "signals": report.signals,
                "outcomes": len(report.outcomes),
            })
    except Exception as exc:
        if persist and report.run_id:
            db.finish_run(report.run_id, "error", error=str(exc))
        raise
    return report


# --- grading ------------------------------------------------------------


def _fresh(seen: dict[tuple[str, str, str], date], card_id: str, variant: str,
           action: str, on: date, dedupe_days: int) -> bool:
    """A card sitting cheap for a month is one call, not thirty."""
    key = (card_id, variant, action)
    previous = seen.get(key)
    if previous is not None and (on - previous).days < dedupe_days:
        return False
    seen[key] = on
    return True


def _grade_buy(signal, metrics, past: Sequence[PricePoint], future: Sequence[PricePoint],
               config: Config, horizons: Sequence[int], on: date,
               card: dict[str, Any]) -> list[Outcome]:
    out: list[Outcome] = []
    for horizon in horizons:
        window = [p for p in future if (p.on - on).days <= horizon]
        outcome = Outcome(
            kind="buy",
            action=signal.action,
            card_id=signal.card_id,
            variant=signal.variant,
            card_name=card.get("name", signal.card_id),
            signal_date=on.isoformat(),
            score=signal.score,
            signal_price=signal.price,
            entry_price=signal.entry_price,
            horizon_days=horizon,
        )
        if not window:
            outcome.outcome = "unresolved"
            out.append(outcome)
            continue

        exit_point = window[-1]
        outcome.exit_price = round(exit_point.market, 2)
        outcome.forward_return = round(
            (exit_point.market - signal.price) / signal.price, 4)
        # The real question for a buy: bought at the signal's entry price and
        # sold at market on the horizon date, what lands in your pocket?
        net = config.fees.net_proceeds(exit_point.market)
        outcome.net_profit = round(net - signal.total_cost, 2)
        outcome.roi = (round(outcome.net_profit / signal.total_cost, 4)
                       if signal.total_cost > 0 else None)
        prices = [p.market for p in window]
        outcome.max_favorable = round((max(prices) - signal.price) / signal.price, 4)
        outcome.max_adverse = round((min(prices) - signal.price) / signal.price, 4)
        outcome.outcome = _label(outcome.roi)
        out.append(outcome)
    return out


def _grade_sell(signal, metrics, future: Sequence[PricePoint],
                horizons: Sequence[int], on: date, card: dict[str, Any]
                ) -> list[Outcome]:
    """A sell is right when the price does not run away after you exit."""
    out: list[Outcome] = []
    for horizon in horizons:
        window = [p for p in future if (p.on - on).days <= horizon]
        outcome = Outcome(
            kind="sell",
            action=signal.action,
            card_id=signal.card_id,
            variant=signal.variant,
            card_name=card.get("name", signal.card_id),
            signal_date=on.isoformat(),
            score=signal.score,
            signal_price=signal.price,
            entry_price=signal.entry_price,
            horizon_days=horizon,
        )
        if not window:
            outcome.outcome = "unresolved"
            out.append(outcome)
            continue

        exit_point = window[-1]
        move = (exit_point.market - signal.price) / signal.price
        outcome.exit_price = round(exit_point.market, 2)
        outcome.forward_return = round(move, 4)
        # Selling avoided the subsequent move; a fall is money kept.
        outcome.net_profit = round((signal.price - exit_point.market)
                                   * max(1, signal.quantity), 2)
        outcome.roi = round(-move, 4)
        prices = [p.market for p in window]
        outcome.max_favorable = round((signal.price - min(prices)) / signal.price, 4)
        outcome.max_adverse = round((max(prices) - signal.price) / signal.price, 4)
        outcome.outcome = _label(-move)
        out.append(outcome)
    return out


def _label(value: float | None) -> str:
    if value is None:
        return "unresolved"
    if value > FLAT_BAND:
        return "win"
    if value < -FLAT_BAND:
        return "loss"
    return "flat"


def _evaluate_synthetic_sell(metrics, past: Sequence[PricePoint], config: Config,
                             on: date, card: dict[str, Any], entry_lookback: int):
    """Run the sell rules against a plausible historical position.

    Without a cost basis, rules like 'target reached' can never fire, so the
    scorecard would only ever grade the price-only exits. The synthetic basis
    is the card's own market price ``entry_lookback`` days earlier.
    """
    target = on - timedelta(days=entry_lookback)
    basis_point = None
    for point in past:
        if point.on <= target:
            basis_point = point
        else:
            break
    if basis_point is None or basis_point.market <= 0:
        return None

    position = Position(
        card_id=metrics.card_id,
        variant=metrics.variant,
        quantity=1,
        cost_each=basis_point.market,
        total_cost=basis_point.market,
        acquired_at=f"{basis_point.on.isoformat()}T12:00:00+00:00",
        card_name=card.get("name", metrics.card_id),
        set_name=card.get("set_name", ""),
    )
    return evaluate_sell(metrics, position, config, today=on)


def _as_rows(points: Sequence[PricePoint]) -> list[dict[str, Any]]:
    """Turn points back into the row shape ``compute_metrics`` expects."""
    return [
        {
            "captured_on": p.on.isoformat(),
            "market": p.market,
            "low": p.low,
            "mid": p.mid,
            "high": p.high,
            "direct_low": p.direct_low,
            "sales_count": p.sales_count,
            "listing_count": p.listing_count,
        }
        for p in points
    ]


# --- aggregation --------------------------------------------------------


def summarise(outcomes: Iterable[Outcome]) -> list[ActionStats]:
    """Roll individual outcomes up into a per-reason scorecard."""
    buckets: dict[tuple[str, str, int], list[Outcome]] = {}
    for outcome in outcomes:
        buckets.setdefault(
            (outcome.kind, outcome.action, outcome.horizon_days), []).append(outcome)

    stats: list[ActionStats] = []
    for (kind, action, horizon), group in sorted(buckets.items()):
        resolved = [o for o in group if o.outcome != "unresolved" and o.roi is not None]
        entry = ActionStats(action=action, kind=kind, horizon_days=horizon,
                            signals=len(group), resolved=len(resolved))
        if resolved:
            rois = [o.roi for o in resolved if o.roi is not None]
            returns = [o.forward_return for o in resolved
                       if o.forward_return is not None]
            wins = sum(1 for o in resolved if o.outcome == "win")
            entry.win_rate = round(wins / len(resolved), 4)
            entry.median_roi = round(statistics.median(rois), 4) if rois else None
            if returns:
                entry.median_return = round(statistics.median(returns), 4)
                entry.mean_return = round(statistics.fmean(returns), 4)
                entry.best = round(max(returns), 4)
                entry.worst = round(min(returns), 4)
            profits = [o.net_profit for o in resolved if o.net_profit is not None]
            if profits:
                entry.total_net_profit = round(sum(profits), 2)
            adverse = [o.max_adverse for o in resolved if o.max_adverse is not None]
            if adverse:
                entry.avg_max_adverse = round(statistics.fmean(adverse), 4)
            entry.verdict = _verdict(entry)
        stats.append(entry)

    stats.sort(key=lambda s: (s.kind, s.horizon_days, -(s.win_rate or 0)))
    return stats


def _verdict(entry: ActionStats) -> str:
    if entry.resolved < MIN_SAMPLE:
        return "insufficient_data"
    win_rate = entry.win_rate or 0.0
    median = entry.median_roi or 0.0
    if win_rate >= 0.6 and median > 0:
        return "reliable"
    if win_rate >= 0.5 and median > 0:
        return "positive"
    if median <= 0 and win_rate < 0.45:
        return "unreliable"
    return "mixed"


def _report_notes(report: BacktestReport, config: Config) -> list[str]:
    notes = [
        "Signals were scored using only the prices available on each replay "
        "date; forward windows were read solely to grade the result.",
        f"Sell signals use a synthetic cost basis - the card's own price "
        f"{config.backtest.sell_entry_lookback} days before the signal - so they "
        "measure exit timing, not a profit you actually made.",
    ]
    thin = [s for s in report.stats if s.verdict == "insufficient_data"]
    if thin:
        actions = sorted({s.action for s in thin})
        notes.append(
            f"Too few graded signals to judge: {', '.join(actions)}. "
            f"Track more cards or replay a longer window."
        )
    unreliable = sorted({s.action for s in report.stats if s.verdict == "unreliable"})
    if unreliable:
        notes.append(
            f"Consider tightening or disabling: {', '.join(unreliable)} - "
            "they lost money more often than not over the window tested."
        )
    notes.append(
        "Past behaviour of a rule is not a promise. A window that contained one "
        "big market move can flatter or damn any rule."
    )
    return notes


def _persist(db: Database, report: BacktestReport) -> None:
    rows = [
        (
            report.run_id, o.kind, o.action, o.card_id, o.variant, o.signal_date,
            o.score, o.entry_price, o.signal_price, o.horizon_days, o.exit_price,
            o.forward_return, o.net_profit, o.roi, o.max_favorable, o.max_adverse,
            o.outcome, report.generated_at,
        )
        for o in report.outcomes
    ]
    if not rows:
        return
    with db.tx() as conn:
        conn.executemany(
            """
            INSERT INTO backtest_results
                (run_id, kind, action, card_id, variant, signal_date, score,
                 entry_price, signal_price, horizon_days, exit_price, forward_return,
                 net_profit, roi, max_favorable, max_adverse, outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def latest(db: Database) -> dict[str, Any] | None:
    """The most recent stored scorecard, without re-running the replay."""
    row = db.one("SELECT MAX(run_id) AS run_id FROM backtest_results")
    if not row or row["run_id"] is None:
        return None
    run_id = row["run_id"]
    outcomes = [
        Outcome(
            kind=r["kind"], action=r["action"], card_id=r["card_id"],
            variant=r["variant"], card_name=r["card_id"],
            signal_date=r["signal_date"], score=r["score"] or 0.0,
            signal_price=r["signal_price"] or 0.0, entry_price=r["entry_price"] or 0.0,
            horizon_days=int(r["horizon_days"]), exit_price=r["exit_price"],
            forward_return=r["forward_return"], net_profit=r["net_profit"],
            roi=r["roi"], max_favorable=r["max_favorable"],
            max_adverse=r["max_adverse"], outcome=r["outcome"],
        )
        for r in db.query("SELECT * FROM backtest_results WHERE run_id = ?", (run_id,))
    ]
    run_row = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    stats = summarise(outcomes)
    return {
        "run_id": run_id,
        "generated_at": run_row["finished_at"] if run_row else None,
        "stats": [s.to_dict() for s in stats],
        "signals": len({(o.card_id, o.variant, o.signal_date, o.action)
                        for o in outcomes}),
        "run_stats": json.loads((run_row["stats"] if run_row else None) or "{}"),
    }
