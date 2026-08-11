"""The digest: one document that answers 'what do I do today?'.

Built from the same signal run, portfolio valuation and alert set the API and
UI use, then rendered to JSON, Markdown and HTML. The top of every digest is an
action list - buys to place, sells to list - because that is the only part most
people will read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import grading as grading_mod
from . import orders as orders_mod
from .alerts import evaluate_alerts, store_alerts
from .analytics import load_metrics
from .bulk import sell_plan
from .config import Config
from .db import Database, iso, utcnow
from .portfolio import summary as portfolio_summary
from .signals import SignalRun, generate

TEMPLATE_DIR = Path(__file__).with_name("templates")


@dataclass
class Digest:
    kind: str = "daily"
    generated_at: str = ""
    portfolio: dict[str, Any] = field(default_factory=dict)
    buys: list[dict[str, Any]] = field(default_factory=list)
    sells: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    movers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    bulk: dict[str, Any] = field(default_factory=dict)
    listings: dict[str, Any] = field(default_factory=dict)
    orders: dict[str, Any] = field(default_factory=dict)
    grading: dict[str, Any] = field(default_factory=dict)
    concentration: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def headline(self) -> str:
        buys = len(self.buys)
        sells = len(self.sells)
        pnl = self.portfolio.get("unrealized_pnl")
        parts = [f"{buys} buy{'' if buys == 1 else 's'}",
                 f"{sells} sell{'' if sells == 1 else 's'}"]
        if pnl is not None:
            parts.append(f"portfolio {pnl:+,.2f} unrealised")
        return " | ".join(parts)


def build(db: Database, config: Config, kind: str = "daily",
          today: date | None = None, run: SignalRun | None = None,
          persist_alerts: bool = True) -> Digest:
    """Assemble a digest. Runs the signal engine unless one is supplied."""
    run = run or generate(db, config, today=today)
    digest = Digest(kind=kind, generated_at=iso())

    digest.portfolio = portfolio_summary(db, config, today=today)
    digest.buys = [s.to_dict() for s in run.buys]
    digest.sells = [s.to_dict() for s in run.sells]

    found = evaluate_alerts(db, config, run=run, today=today)
    digest.alerts = store_alerts(db, found) if persist_alerts else [a.to_dict() for a in found]

    digest.movers = _movers(db, config, today=today)

    # Live listings that need a decision, and the order book behind them.
    digest.listings = orders_mod.listing_health(db, config, today=today)
    digest.orders = orders_mod.summary(db, config)
    digest.concentration = digest.portfolio.get("concentration", {})

    grading_scan = grading_mod.scan_portfolio(db, config, today=today, limit=5)
    digest.grading = {
        "recommended": grading_scan["recommended"][:3],
        "total_expected_profit": grading_scan["total_expected_profit"],
        "submission_cost": grading_scan["submission_cost"],
        "service": grading_scan["service"],
    }

    plan = sell_plan(db, config, today=today)
    digest.bulk = {
        "bulk_card_count": plan["bulk_card_count"],
        "bulk_net_total": plan["bulk_net_total"],
        "singles_net_total": plan["singles_net_total"],
        "suggested_bulk_ask": plan["suggested_bulk_ask"],
        "top_bulk": plan["sell_as_bulk"][:5],
    }
    digest.actions = _actions(digest)
    digest.stats = {
        "scanned": run.scanned,
        "signal_run_id": run.run_id,
        "tracked_cards": (db.one("SELECT COUNT(DISTINCT card_id) AS n FROM price_history")
                          or {"n": 0})["n"],
        "price_points": (db.one("SELECT COUNT(*) AS n FROM price_history")
                         or {"n": 0})["n"],
    }
    return digest


def _movers(db: Database, config: Config, today: date | None = None, limit: int = 8
            ) -> dict[str, list[dict[str, Any]]]:
    """Biggest 7-day moves across everything tracked - the market's own news."""
    source = config.provider.preferred_source
    cards = {row["id"]: dict(row) for row in db.query("SELECT * FROM cards")}
    entries: list[dict[str, Any]] = []

    pairs = db.query(
        """
        SELECT card_id, variant, COUNT(*) AS n FROM price_history
        WHERE source = ? GROUP BY card_id, variant HAVING n >= 8
        """,
        (source,),
    )
    for row in pairs:
        metrics = load_metrics(db, row["card_id"], row["variant"], source, today=today)
        if metrics.change_7d is None or metrics.price is None:
            continue
        # Sub-dollar cards produce enormous percentage moves that mean nothing.
        if metrics.price < config.buy.min_price:
            continue
        card = cards.get(row["card_id"], {})
        entries.append({
            "card_id": row["card_id"],
            "variant": row["variant"],
            "card_name": card.get("name", row["card_id"]),
            "set_name": card.get("set_name", ""),
            "price": metrics.price,
            "change_7d": metrics.change_7d,
            "change_30d": metrics.change_30d,
            "direction": metrics.direction,
        })

    entries.sort(key=lambda e: e["change_7d"])
    return {
        "losers": entries[:limit],
        "gainers": list(reversed(entries[-limit:])),
    }


def _actions(digest: Digest) -> list[dict[str, Any]]:
    """Flatten the digest into an ordered do-this list."""
    actions: list[dict[str, Any]] = []

    for signal in digest.sells[:8]:
        actions.append({
            "priority": 1 if signal["score"] >= 70 else 2,
            "type": "sell",
            "card": f"{signal['card_name']} ({signal['set_name']} {signal['number']})",
            "detail": (
                f"List {signal['quantity']}x at ~${signal['price']:,.2f} - "
                f"${signal['net_proceeds']:,.2f} net each, "
                f"{signal['roi'] * 100:+.0f}% on ${signal['total_cost']:,.2f} cost"
            ),
            "why": signal["reasons"][0] if signal["reasons"] else "",
            "value": signal["total_net_profit"],
            "card_id": signal["card_id"],
        })

    for signal in digest.buys[:8]:
        actions.append({
            "priority": 1 if signal["score"] >= 70 else 3,
            "type": "buy",
            "card": f"{signal['card_name']} ({signal['set_name']} {signal['number']})",
            "detail": (
                f"Bid up to ${signal['entry_price']:,.2f} - "
                f"${signal['net_profit']:,.2f} net at today's ${signal['price']:,.2f} "
                f"market ({signal['roi'] * 100:+.0f}%)"
            ),
            "why": signal["reasons"][0] if signal["reasons"] else "",
            "value": signal["net_profit"],
            "card_id": signal["card_id"],
        })

    # A listing sitting unsold is money you have already decided to release;
    # it deserves the same prominence as a new trade.
    for listing in (digest.listings or {}).get("needs_action", [])[:8]:
        verb = {"cut": "Re-price", "raise": "Raise", "pull": "Pull"}.get(
            listing["verdict"], "Review")
        suggested = listing.get("suggested_price")
        actions.append({
            "priority": 2 if listing["verdict"] != "raise" else 1,
            "type": "listing",
            "card": f"{listing['card_name']} ({listing['set_name']} {listing['number']})",
            "detail": (
                f"{verb} {listing['quantity']}x from "
                f"${listing['limit_price']:,.2f}"
                + (f" to ${suggested:,.2f}" if suggested else "")
                + f" - {listing['days_on_market']} days listed"
            ),
            "why": listing.get("reason", ""),
            "value": abs((listing.get("limit_price") or 0) - (suggested or 0))
                     * listing["quantity"],
            "card_id": listing["card_id"],
        })

    for candidate in (digest.grading or {}).get("recommended", [])[:3]:
        actions.append({
            "priority": 3,
            "type": "grade",
            "card": f"{candidate['card_name']} ({candidate['set_name']})",
            "detail": (
                f"Grading looks worth about "
                f"${candidate['expected_profit']:,.2f} more than selling raw at "
                f"${candidate['raw_price']:,.2f}"
            ),
            "why": (candidate.get("reasons") or [""])[0],
            "value": candidate["expected_profit"],
            "card_id": candidate["card_id"],
        })

    for warning in (digest.concentration or {}).get("warnings", []):
        actions.append({
            "priority": 2,
            "type": "risk",
            "card": warning["subject"],
            "detail": warning["message"],
            "why": "capital concentration limit",
            "value": 0.0,
            "card_id": None,
        })

    for alert in digest.alerts:
        if alert.get("severity") == "urgent" and alert.get("kind", "").startswith("watch"):
            actions.append({
                "priority": 1,
                "type": "alert",
                "card": alert.get("title", ""),
                "detail": alert.get("body", ""),
                "why": "watchlist trigger",
                "value": 0.0,
                "card_id": alert.get("card_id"),
            })

    bulk = digest.bulk or {}
    if bulk.get("bulk_card_count", 0) >= 50:
        actions.append({
            "priority": 3,
            "type": "bulk",
            "card": f"{bulk['bulk_card_count']} cards in the bulk tail",
            "detail": (
                f"Worth ~${bulk['bulk_net_total']:,.2f} net as a lot; "
                f"list at ${bulk['suggested_bulk_ask']:,.2f} to leave negotiating room"
            ),
            "why": "fees make these unprofitable to list individually",
            "value": bulk["bulk_net_total"],
            "card_id": None,
        })

    actions.sort(key=lambda a: (a["priority"], -a["value"]))
    return actions


# --- rendering ----------------------------------------------------------


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = lambda v, c="USD": (
        "-" if v is None else f"{'$' if c == 'USD' else '€'}{v:,.2f}"
    )
    env.filters["pct"] = lambda v, p=1: "-" if v is None else f"{v * 100:+.{p}f}%"
    return env


def render_html(digest: Digest) -> str:
    return _env().get_template("digest.html").render(
        d=digest, generated=digest.generated_at, headline=digest.headline
    )


def render_markdown(digest: Digest) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# pokeflip {digest.kind} digest")
    add("")
    add(f"_{digest.generated_at}_ - {digest.headline}")
    add("")

    p = digest.portfolio
    if p:
        add("## Portfolio")
        add("")
        add(f"- Cards held: **{p.get('cards', 0)}** across {p.get('positions', 0)} positions")
        add(f"- Cost basis: **{_m(p.get('cost_basis'))}**")
        add(f"- Market value: **{_m(p.get('market_value'))}**")
        add(f"- Net if liquidated today: **{_m(p.get('net_liquidation'))}** "
            f"(fees would take {_m(p.get('fee_drag'))})")
        roi = p.get("unrealized_roi")
        add(f"- Unrealised: **{_m(p.get('unrealized_pnl'))}**"
            + (f" ({roi * 100:+.1f}%)" if roi is not None else ""))
        realized = p.get("realized") or {}
        if realized.get("sales"):
            add(f"- Realised to date: **{_m(realized.get('realized_pnl'))}** "
                f"over {realized['sales']} sales")
        add("")

    if digest.actions:
        add("## Do this")
        add("")
        for i, action in enumerate(digest.actions, 1):
            add(f"{i}. **{action['type'].upper()}** - {action['card']}")
            add(f"   - {action['detail']}")
            if action.get("why"):
                add(f"   - _{action['why']}_")
        add("")

    if digest.sells:
        add("## Sell candidates")
        add("")
        add("| Card | Qty | Market | Net each | Cost | ROI | Score | Why |")
        add("|---|---:|---:|---:|---:|---:|---:|---|")
        for s in digest.sells:
            add(f"| {s['card_name']} ({s['set_name']} {s['number']}) | {s['quantity']} "
                f"| {_m(s['price'])} | {_m(s['net_proceeds'])} | {_m(s['total_cost'])} "
                f"| {s['roi'] * 100:+.0f}% | {s['score']:.0f} "
                f"| {s['reasons'][0] if s['reasons'] else ''} |")
        add("")

    if digest.buys:
        add("## Buy candidates")
        add("")
        add("| Card | Buy at | Market | Net profit | ROI | Score | Why |")
        add("|---|---:|---:|---:|---:|---:|---|")
        for s in digest.buys:
            add(f"| {s['card_name']} ({s['set_name']} {s['number']}) | {_m(s['entry_price'])} "
                f"| {_m(s['price'])} | {_m(s['net_profit'])} | {s['roi'] * 100:+.0f}% "
                f"| {s['score']:.0f} | {s['reasons'][0] if s['reasons'] else ''} |")
        add("")

    movers = digest.movers or {}
    if movers.get("gainers") or movers.get("losers"):
        add("## Movers (7 days)")
        add("")
        for label, key in (("Up", "gainers"), ("Down", "losers")):
            rows = movers.get(key) or []
            if not rows:
                continue
            add(f"**{label}**")
            add("")
            for m in rows:
                add(f"- {m['card_name']} ({m['set_name']}): {_m(m['price'])} "
                    f"{m['change_7d'] * 100:+.1f}%")
            add("")

    listings = digest.listings or {}
    if listings.get("count"):
        add("## Listings")
        add("")
        add(f"- {listings['count']} live, {_m(listings['listed_value'])} at ask, "
            f"{listings['stale_count']} stale")
        for listing in listings.get("needs_action", []):
            suggested = listing.get("suggested_price")
            add(f"- **{listing['verdict'].upper()}** {listing['card_name']}: "
                f"{_m(listing['limit_price'])}"
                + (f" -> {_m(suggested)}" if suggested else "")
                + f" ({listing['days_on_market']}d) - {listing.get('reason', '')}")
        add("")

    grading = digest.grading or {}
    if grading.get("recommended"):
        add("## Grading")
        add("")
        for candidate in grading["recommended"]:
            add(f"- {candidate['card_name']}: raw {_m(candidate['raw_price'])}, "
                f"expected {_m(candidate['expected_profit'])} extra from "
                f"{grading.get('service', 'PSA')} ({candidate['verdict']})")
        add(f"- Submission cost {_m(grading.get('submission_cost'))} for "
            f"{_m(grading.get('total_expected_profit'))} of expected upside")
        add("")

    warnings = (digest.concentration or {}).get("warnings", [])
    if warnings:
        add("## Risk")
        add("")
        for warning in warnings:
            add(f"- {warning['message']}")
        add("")

    if digest.alerts:
        add("## Alerts")
        add("")
        for alert in digest.alerts:
            add(f"- **[{alert.get('severity', 'info').upper()}]** {alert.get('title', '')}"
                + (f" - {alert['body']}" if alert.get("body") else ""))
        add("")

    bulk = digest.bulk or {}
    if bulk.get("bulk_card_count"):
        add("## Bulk")
        add("")
        add(f"- {bulk['bulk_card_count']} cards are better moved as bulk: "
            f"~{_m(bulk['bulk_net_total'])} net, list around "
            f"{_m(bulk['suggested_bulk_ask'])}")
        add(f"- Singles worth listing individually: {_m(bulk['singles_net_total'])} net")
        add("")

    stats = digest.stats or {}
    add("---")
    add(f"_Scanned {stats.get('scanned', 0)} printings across "
        f"{stats.get('tracked_cards', 0)} tracked cards, "
        f"{stats.get('price_points', 0)} stored price points._")
    return "\n".join(lines)


def _m(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def save(db: Database, config: Config, digest: Digest) -> dict[str, str]:
    """Write the digest to disk and record it, returning the paths written."""
    report_dir = Path(config.notify.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    base = report_dir / f"digest-{digest.kind}-{stamp}"

    paths = {
        "html": str(base.with_suffix(".html")),
        "markdown": str(base.with_suffix(".md")),
        "json": str(base.with_suffix(".json")),
    }
    Path(paths["html"]).write_text(render_html(digest))
    Path(paths["markdown"]).write_text(render_markdown(digest))
    Path(paths["json"]).write_text(json.dumps(digest.to_dict(), indent=2, default=str))

    db.execute(
        "INSERT INTO reports (kind, created_at, path, summary, payload) VALUES (?, ?, ?, ?, ?)",
        (digest.kind, digest.generated_at, paths["html"], digest.headline,
         json.dumps(paths)),
    )
    return paths
