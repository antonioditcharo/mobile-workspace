"""Watchlist management and alerting.

Alerts are the interrupt-driven half of the app: price targets you set, sharp
moves you did not, and data going stale underneath you. They are deduplicated
so a card sitting below your buy price does not page you every six hours.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from typing import Any, Iterable

from .analytics import load_metrics
from .config import Config
from .db import Database, iso, utcnow
from .portfolio import open_positions, realistic_net_each
from .signals import SignalRun

# A given alert will not be re-raised inside this window.
DEDUPE_HOURS = 20
# Day-over-day move that counts as news on its own.
SPIKE_PCT = 0.15
# Days without a fresh quote before the data itself is the problem.
STALE_DAYS = 5


@dataclass
class Alert:
    kind: str
    title: str
    body: str = ""
    severity: str = "info"       # info | warn | urgent
    card_id: str | None = None
    variant: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- watchlist ----------------------------------------------------------


def add_watch(db: Database, card_id: str, variant: str = "any",
              max_buy: float | None = None, target_sell: float | None = None,
              note: str = "") -> int:
    if not db.get_card(card_id):
        raise ValueError(f"unknown card {card_id!r}; search for it first")
    return db.execute(
        """
        INSERT INTO watchlist (card_id, variant, max_buy, target_sell, note, active, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(card_id, variant) DO UPDATE SET
            max_buy=excluded.max_buy, target_sell=excluded.target_sell,
            note=excluded.note, active=1
        """,
        (card_id, variant, max_buy, target_sell, note, iso()),
    )


def remove_watch(db: Database, card_id: str, variant: str = "any") -> bool:
    return db.execute(
        "DELETE FROM watchlist WHERE card_id = ? AND variant = ?", (card_id, variant)
    ) > 0


def list_watch(db: Database, config: Config, today: date | None = None
               ) -> list[dict[str, Any]]:
    source = config.provider.preferred_source
    out: list[dict[str, Any]] = []
    rows = db.query(
        """
        SELECT w.*, c.name, c.set_name, c.number, c.rarity, c.image_small
        FROM watchlist w LEFT JOIN cards c ON c.id = w.card_id
        WHERE w.active = 1 ORDER BY c.name
        """
    )
    for row in rows:
        variant = row["variant"]
        if variant == "any":
            variant = db.best_variant(row["card_id"], source) or "normal"
        metrics = load_metrics(db, row["card_id"], variant, source, today=today)
        entry = dict(row)
        entry["resolved_variant"] = variant
        entry["metrics"] = metrics.to_dict()
        entry["buy_ready"] = bool(
            row["max_buy"] and metrics.low is not None and metrics.low <= row["max_buy"]
        )
        entry["sell_ready"] = bool(
            row["target_sell"] and metrics.price is not None
            and metrics.price >= row["target_sell"]
        )
        out.append(entry)
    return out


# --- alert generation ---------------------------------------------------


def evaluate_alerts(db: Database, config: Config, run: SignalRun | None = None,
                    today: date | None = None) -> list[Alert]:
    """Work out what deserves an interruption right now."""
    alerts: list[Alert] = []
    source = config.provider.preferred_source

    for entry in list_watch(db, config, today=today):
        metrics = entry["metrics"]
        name = entry.get("name") or entry["card_id"]
        card_id = entry["card_id"]
        variant = entry["resolved_variant"]

        if entry["buy_ready"]:
            alerts.append(Alert(
                kind="watch_buy",
                severity="urgent",
                card_id=card_id,
                variant=variant,
                title=f"{name} is at your buy price",
                body=(
                    f"Cheapest listing ${metrics['low']:,.2f} is at or under your "
                    f"${entry['max_buy']:,.2f} limit (market ${metrics['price']:,.2f})."
                ),
                payload={"watch": {k: entry[k] for k in ("max_buy", "target_sell", "note")},
                         "metrics": metrics},
                dedupe_key=f"watch_buy:{card_id}:{variant}",
            ))

        if entry["sell_ready"]:
            alerts.append(Alert(
                kind="watch_sell",
                severity="urgent",
                card_id=card_id,
                variant=variant,
                title=f"{name} hit your sell target",
                body=(
                    f"Market ${metrics['price']:,.2f} is at or above your "
                    f"${entry['target_sell']:,.2f} target."
                ),
                payload={"metrics": metrics},
                dedupe_key=f"watch_sell:{card_id}:{variant}",
            ))

        change = metrics.get("change_1d")
        if change is not None and abs(change) >= SPIKE_PCT:
            direction = "jumped" if change > 0 else "dropped"
            alerts.append(Alert(
                kind="price_spike",
                severity="warn",
                card_id=card_id,
                variant=variant,
                title=f"{name} {direction} {abs(change) * 100:.0f}% in a day",
                body=(
                    f"Now ${metrics['price']:,.2f}, was "
                    f"${metrics['price'] / (1 + change):,.2f}. Something moved - check "
                    "for reprint news, a tournament result, or a listing error."
                ),
                payload={"metrics": metrics},
                dedupe_key=f"spike:{card_id}:{variant}:{'up' if change > 0 else 'down'}",
            ))

        stale = metrics.get("stale_days")
        if stale is not None and stale >= STALE_DAYS:
            alerts.append(Alert(
                kind="stale_data",
                severity="warn",
                card_id=card_id,
                variant=variant,
                title=f"No fresh price for {name} in {stale} days",
                body="Decisions on this card are running on old data.",
                payload={"metrics": metrics},
                dedupe_key=f"stale:{card_id}:{variant}",
            ))

    # Positions that have quietly become large losses deserve a look even if
    # the sell engine did not rank them highly today.
    for position in open_positions(db):
        metrics = load_metrics(db, position.card_id, position.variant, source, today=today)
        if metrics.price is None or position.total_cost <= 0:
            continue
        net = realistic_net_each(config, metrics.price) * position.quantity
        roi = (net - position.total_cost) / position.total_cost
        if roi <= -config.sell.stop_loss_pct:
            alerts.append(Alert(
                kind="position_loss",
                severity="warn",
                card_id=position.card_id,
                variant=position.variant,
                title=f"{position.card_name} is down {abs(roi) * 100:.0f}%",
                body=(
                    f"{position.quantity}x at ${position.cost_each:,.2f} cost; "
                    f"net now ${net:,.2f} against ${position.total_cost:,.2f} in."
                ),
                payload={"position": position.to_dict(), "roi": roi},
                dedupe_key=f"loss:{position.card_id}:{position.variant}",
            ))

    # A listing that has gone stale is capital you already decided to release,
    # sitting still. That is worth an interruption.
    from . import orders as orders_mod  # imported here to avoid a circular import

    for listing in orders_mod.listing_health(db, config, today=today)["needs_action"]:
        severity = "warn" if listing["verdict"] != "raise" else "info"
        suggested = listing.get("suggested_price")
        alerts.append(Alert(
            kind=f"listing_{listing['verdict']}",
            severity=severity,
            card_id=listing["card_id"],
            variant=listing["variant"],
            title=(
                f"{listing['card_name']}: {listing['verdict']} your listing"
                + (f" to ${suggested:,.2f}" if suggested else "")
            ),
            body=listing.get("reason", ""),
            payload={"listing": listing},
            dedupe_key=f"listing:{listing['id']}:{listing['verdict']}",
        ))

    for warning in _concentration_warnings(db, config):
        alerts.append(Alert(
            kind="concentration",
            severity="warn",
            title=f"Concentration: {warning['subject']}",
            body=warning["message"],
            payload=warning,
            dedupe_key=f"concentration:{warning['kind']}:{warning['subject']}",
        ))

    if run:
        for signal in run.buys[:5]:
            if signal.score >= 75:
                alerts.append(Alert(
                    kind="strong_buy",
                    severity="urgent",
                    card_id=signal.card_id,
                    variant=signal.variant,
                    title=f"Strong buy: {signal.card_name} ({signal.score:.0f})",
                    body=" ".join(signal.reasons[:2]),
                    payload=signal.to_dict(),
                    dedupe_key=f"strong_buy:{signal.card_id}:{signal.variant}",
                ))
        for signal in run.sells[:5]:
            if signal.score >= 75:
                alerts.append(Alert(
                    kind="strong_sell",
                    severity="urgent",
                    card_id=signal.card_id,
                    variant=signal.variant,
                    title=f"Sell now: {signal.card_name} ({signal.score:.0f})",
                    body=" ".join(signal.reasons[:2]),
                    payload=signal.to_dict(),
                    dedupe_key=f"strong_sell:{signal.card_id}:{signal.variant}",
                ))

    return alerts


def _concentration_warnings(db: Database, config: Config) -> list[dict[str, Any]]:
    from .portfolio import concentration

    return concentration(db, config).get("warnings", [])


def snooze(db: Database, dedupe_key: str, days: int = 30) -> str:
    """Stop raising this exact alert until the snooze expires."""
    until = iso(utcnow() + timedelta(days=max(1, days)))
    db.execute(
        "INSERT INTO alert_snoozes (dedupe_key, until, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(dedupe_key) DO UPDATE SET until = excluded.until",
        (dedupe_key, until, iso()),
    )
    return until


def unsnooze(db: Database, dedupe_key: str) -> bool:
    return db.execute("DELETE FROM alert_snoozes WHERE dedupe_key = ?",
                      (dedupe_key,)) > 0


def snoozed_keys(db: Database) -> set[str]:
    now = iso()
    return {
        row["dedupe_key"]
        for row in db.query("SELECT dedupe_key FROM alert_snoozes WHERE until > ?",
                            (now,))
    }


def list_snoozes(db: Database) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM alert_snoozes ORDER BY until DESC")]


def store_alerts(db: Database, alerts: Iterable[Alert]) -> list[dict[str, Any]]:
    """Persist alerts, dropping any snoozed or raised recently for the same reason."""
    cutoff = (utcnow() - timedelta(hours=DEDUPE_HOURS)).isoformat()
    muted = snoozed_keys(db)
    stored: list[dict[str, Any]] = []
    for alert in alerts:
        if alert.dedupe_key and alert.dedupe_key in muted:
            continue
        if alert.dedupe_key:
            recent = db.one(
                "SELECT 1 FROM alerts WHERE dedupe_key = ? AND created_at >= ? LIMIT 1",
                (alert.dedupe_key, cutoff),
            )
            if recent:
                continue
        alert_id = db.execute(
            """
            INSERT INTO alerts (kind, severity, card_id, variant, title, body,
                                payload, dedupe_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (alert.kind, alert.severity, alert.card_id, alert.variant, alert.title,
             alert.body, json.dumps(alert.payload), alert.dedupe_key, iso()),
        )
        entry = alert.to_dict()
        entry["id"] = alert_id
        stored.append(entry)
    return stored


def recent_alerts(db: Database, limit: int = 50, unacknowledged_only: bool = False
                  ) -> list[dict[str, Any]]:
    sql = "SELECT * FROM alerts"
    if unacknowledged_only:
        sql += " WHERE acknowledged_at IS NULL"
    sql += " ORDER BY id DESC LIMIT ?"
    out = []
    for row in db.query(sql, (limit,)):
        entry = dict(row)
        try:
            entry["payload"] = json.loads(entry["payload"] or "{}")
        except (TypeError, ValueError):
            entry["payload"] = {}
        out.append(entry)
    return out


def acknowledge(db: Database, alert_id: int | None = None) -> int:
    if alert_id is None:
        return db.execute(
            "UPDATE alerts SET acknowledged_at = ? WHERE acknowledged_at IS NULL", (iso(),)
        )
    return db.execute(
        "UPDATE alerts SET acknowledged_at = ? WHERE id = ?", (iso(), alert_id)
    )


def mark_delivered(db: Database, alert_ids: Iterable[int]) -> None:
    ids = list(alert_ids)
    if not ids:
        return
    with db.tx() as conn:
        conn.executemany(
            "UPDATE alerts SET delivered_at = ? WHERE id = ?",
            [(iso(), aid) for aid in ids],
        )
