"""Command line interface.

Everything the scheduler does on its own, you can do on demand:

    pokeflip demo                      seed offline data and try it out
    pokeflip refresh                   pull fresh prices now
    pokeflip scan                      what to buy and sell right now
    pokeflip digest --save             the full report
    pokeflip bulk estimate --count 5000 --ask 120
    pokeflip serve                     dashboard + API + scheduler
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import alerts as alerts_mod
from . import backtest as backtest_mod
from . import bulk as bulk_mod
from . import digest as digest_mod
from . import grading as grading_mod
from . import ingest, notify, orders as orders_mod, portfolio
from . import scheduler as scheduler_mod, signals
from .analytics import load_metrics
from .config import Config
from .db import Database
from .providers import ProviderError

# --- output helpers -----------------------------------------------------

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"
_COLOR = sys.stdout.isatty()


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if _COLOR else text


def money(value: Any, currency: str = "USD") -> str:
    if value is None:
        return "-"
    symbol = {"USD": "$", "EUR": "€"}.get(currency, "")
    return f"{symbol}{float(value):,.2f}"


def pct(value: Any, places: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:+.{places}f}%"


def plain_pct(value: Any, places: int = 0) -> str:
    """Percentage without a forced sign - for shares and rates."""
    if value is None:
        return "-"
    return f"{float(value) * 100:.{places}f}%"


VERDICT_COLORS = {"buy": GREEN, "thin": YELLOW, "pass": RED,
                  "insufficient_data": YELLOW, "free": GREEN}


def signed(value: Any) -> str:
    if value is None:
        return "-"
    text = money(value)
    return _c(text, GREEN if float(value) >= 0 else RED)


def table(rows: Sequence[Sequence[Any]], headers: Sequence[str],
          aligns: Sequence[str] | None = None) -> None:
    if not rows:
        print(_c("  (nothing)", DIM))
        return
    cells = [[str(c) for c in row] for row in rows]
    aligns = aligns or ["l"] * len(headers)
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(_strip(cell)))

    def line(values: Sequence[str], bold: bool = False) -> str:
        out = []
        for i, value in enumerate(values):
            pad = widths[i] - len(_strip(value))
            out.append(" " * pad + value if aligns[i] == "r" else value + " " * pad)
        text = "  ".join(out).rstrip()
        return _c(text, BOLD) if bold else text

    print(line(list(headers), bold=True))
    print(_c("  ".join("-" * w for w in widths), DIM))
    for row in cells:
        print(line(row))


def _strip(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            while i < len(text) and text[i] != "m":
                i += 1
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def heading(text: str) -> None:
    print()
    print(_c(text, BOLD + CYAN if _COLOR else ""))
    print()


def emit(args: argparse.Namespace, payload: Any, renderer: Callable[[], None]) -> None:
    """Print JSON when asked, otherwise the human rendering."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    else:
        renderer()


# --- commands -----------------------------------------------------------

def cmd_init(args: argparse.Namespace, db: Database, config: Config) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        print(f"{path} already exists; pass --force to overwrite")
        return 1
    config.save(path)
    print(f"Wrote {path}")
    print("Edit it to set your marketplace fees, thresholds and notification channels.")
    return 0


def cmd_demo(args: argparse.Namespace, db: Database, config: Config) -> int:
    stats = ingest.seed_demo(db, config, days=args.days,
                             include_portfolio=not args.no_portfolio)
    emit(args, stats, lambda: (
        print(f"Seeded {stats['cards']} cards with {stats['days']} days of history "
              f"({stats['price_rows']} price points) and "
              f"{stats['positions']} demo positions."),
        print(),
        print("Try:  pokeflip scan      pokeflip portfolio      pokeflip digest"),
    ))
    return 0


def cmd_search(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.remote:
        rows = ingest.search_and_store(db, config, args.query, limit=args.limit)
    else:
        rows = [dict(r) for r in db.search_cards(args.query, limit=args.limit)]
        if not rows:
            print(_c("Nothing local; searching the provider...", DIM))
            rows = ingest.search_and_store(db, config, args.query, limit=args.limit)

    def render() -> None:
        heading(f"Cards matching {args.query!r}")
        table(
            [[r["id"], r.get("name", ""), r.get("set_name", ""), r.get("number", ""),
              r.get("rarity", "")] for r in rows],
            ["ID", "NAME", "SET", "NO.", "RARITY"],
        )
    emit(args, rows, render)
    return 0


def cmd_sync(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.set:
        result = ingest.sync_set_cards(db, config, args.set)
    elif args.query:
        cards = ingest.search_and_store(db, config, args.query, limit=args.limit)
        result = ingest.capture_prices(db, config, [c["id"] for c in cards])
        result["cards_synced"] = len(cards)
    else:
        print("Pass --set SET_ID or --query TEXT")
        return 1
    emit(args, result, lambda: print(
        f"Synced {result.get('cards_synced', 0)} cards, "
        f"stored {result.get('quotes', 0)} price points."
    ))
    return 0


def cmd_refresh(args: argparse.Namespace, db: Database, config: Config) -> int:
    result = scheduler_mod.run_refresh_cycle(db, config, deliver=args.deliver)

    def render() -> None:
        prices = result["prices"]
        print(f"Prices: {prices.get('quotes', 0)} points across "
              f"{prices.get('targets', 0)} tracked cards")
        if prices.get("errors"):
            print(_c(f"  {len(prices['errors'])} fetch error(s): "
                     f"{prices['errors'][0]}", YELLOW))
        print(f"Signals: {result['buys']} buys, {result['sells']} sells "
              f"from {result['scanned']} printings")
        print(f"Alerts: {result['alerts']} new")
    emit(args, result, render)
    return 0


def cmd_scan(args: argparse.Namespace, db: Database, config: Config) -> int:
    run = signals.generate(db, config)

    def render() -> None:
        heading(f"SELL  ({len(run.sells)} candidates)")
        table(
            [[
                f"{s.card_name}",
                f"{s.set_name} {s.number}",
                s.quantity,
                money(s.price),
                money(s.net_proceeds),
                money(s.total_cost),
                _c(pct(s.roi, 0), GREEN if s.roi >= 0 else RED),
                signed(s.total_net_profit),
                f"{s.score:.0f}",
                s.reasons[0] if s.reasons else "",
            ] for s in run.sells],
            ["CARD", "SET", "QTY", "MARKET", "NET EA", "COST", "ROI", "TOTAL", "SCORE", "WHY"],
            ["l", "l", "r", "r", "r", "r", "r", "r", "r", "l"],
        )

        heading(f"BUY  ({len(run.buys)} candidates)")
        table(
            [[
                f"{s.card_name}",
                f"{s.set_name} {s.number}",
                money(s.entry_price),
                money(s.price),
                signed(s.net_profit),
                pct(s.roi, 0),
                f"{s.score:.0f}",
                s.reasons[0] if s.reasons else "",
            ] for s in run.buys],
            ["CARD", "SET", "BUY AT", "MARKET", "NET", "ROI", "SCORE", "WHY"],
            ["l", "l", "r", "r", "r", "r", "r", "l"],
        )
        print()
        print(_c(f"Scanned {run.scanned} printings. "
                 f"Prices are estimates - check live listings before trading.", DIM))
    emit(args, run.to_dict(), render)
    return 0


def cmd_digest(args: argparse.Namespace, db: Database, config: Config) -> int:
    built = digest_mod.build(db, config, kind=args.kind)
    paths = digest_mod.save(db, config, built) if args.save else None
    delivery = notify.deliver_digest(config, built, paths) if args.deliver else []

    if args.format == "json" or getattr(args, "json", False):
        print(json.dumps(built.to_dict(), indent=2, default=str))
    elif args.format == "html":
        print(digest_mod.render_html(built))
    else:
        print(digest_mod.render_markdown(built))

    if paths and args.format != "json":
        print()
        for label, path in paths.items():
            print(_c(f"saved {label}: {path}", DIM))
    for entry in delivery:
        status = "ok" if entry.get("ok") else f"failed: {entry.get('error')}"
        print(_c(f"delivery {entry['channel']}: {status}", DIM))
    return 0


def cmd_portfolio(args: argparse.Namespace, db: Database, config: Config) -> int:
    data = portfolio.summary(db, config)

    def render() -> None:
        heading("Portfolio")
        fee_note = _c(f"(fees take {money(data['fee_drag'])})", DIM)
        roi_note = (pct(data["unrealized_roi"], 1)
                    if data["unrealized_roi"] is not None else "")
        print(f"  Positions        {data['positions']} ({data['cards']} cards)")
        print(f"  Cost basis       {money(data['cost_basis'])}")
        print(f"  Market value     {money(data['market_value'])}")
        print(f"  Net if sold now  {money(data['net_liquidation'])}  {fee_note}")
        print(f"  Unrealised       {signed(data['unrealized_pnl'])}  {roi_note}")
        realized = data["realized"]
        if realized["sales"]:
            print(f"  Realised         {signed(realized['realized_pnl'])} "
                  f"over {realized['sales']} sales "
                  f"({pct(realized['roi'], 0)}, win rate {pct(realized['win_rate'], 0)})")
        if data["unpriced_positions"]:
            print(_c(f"  {data['unpriced_positions']} position(s) have no price data "
                     f"- run pokeflip refresh", YELLOW))

        heading("Positions")
        table(
            [[
                p["card_name"], f"{p['set_name']} {p['number']}", p["quantity"],
                money(p["cost_each"]), money(p["price"]), money(p["net_value"]),
                signed(p["unrealized"]), pct(p["roi"], 0), p["direction"],
                p.get("hold_days") if p.get("hold_days") is not None else "-",
            ] for p in data["positions_detail"]],
            ["CARD", "SET", "QTY", "COST EA", "MARKET", "NET", "P&L", "ROI",
             "TREND", "DAYS"],
            ["l", "l", "r", "r", "r", "r", "r", "r", "l", "r"],
        )

        if data["by_set"]:
            heading("By set")
            table(
                [[b["set_name"], b["cards"], money(b["cost"]), money(b["net_value"]),
                  signed(b["pnl"])] for b in data["by_set"]],
                ["SET", "CARDS", "COST", "NET", "P&L"],
                ["l", "r", "r", "r", "r"],
            )
    emit(args, data, render)
    return 0


def cmd_hold(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.hold_action == "add":
        try:
            holding_id = portfolio.add_holding(
                db, args.card_id, args.variant, args.quantity, args.cost,
                args.condition, args.acquired, args.source, args.note,
            )
        except ValueError as exc:
            print(_c(str(exc), RED))
            return 1
        print(f"Added holding {holding_id}: {args.quantity}x {args.card_id} "
              f"at {money(args.cost)} each")
        return 0

    if args.hold_action == "sell":
        try:
            result = portfolio.sell_holding(db, args.holding_id, args.quantity,
                                            args.price, config)
        except ValueError as exc:
            print(_c(str(exc), RED))
            return 1
        emit(args, result, lambda: (
            print(f"Sold {result['quantity']}x {result['card_id']} at "
                  f"{money(result['price_each'])}"),
            print(f"  Fees            {money(result['fees_each'])} each"),
            print(f"  Net proceeds    {money(result['net_proceeds'])}"),
            print(f"  Cost basis      {money(result['cost_basis'])}"),
            print(f"  Realised P&L    {signed(result['realized_pnl'])} "
                  f"({pct(result['roi'], 0)})"),
        ))
        return 0

    if args.hold_action == "remove":
        ok = portfolio.remove_holding(db, args.holding_id)
        print(f"Removed holding {args.holding_id}" if ok
              else f"No holding {args.holding_id}")
        return 0 if ok else 1

    lots = portfolio.open_lots(db, args.card_id)

    def render() -> None:
        heading("Open lots")
        table(
            [[l["id"], l.get("card_name") or l["card_id"], l["variant"], l["condition"],
              l["quantity"], money(l["cost_each"]), l["acquired_at"][:10]] for l in lots],
            ["ID", "CARD", "VARIANT", "COND", "QTY", "COST EA", "ACQUIRED"],
            ["r", "l", "l", "l", "r", "r", "l"],
        )
    emit(args, lots, render)
    return 0


def cmd_watch(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.watch_action == "add":
        try:
            alerts_mod.add_watch(db, args.card_id, args.variant, args.max_buy,
                                 args.target_sell, args.note)
        except ValueError as exc:
            print(_c(str(exc), RED))
            return 1
        print(f"Watching {args.card_id}"
              + (f", buy under {money(args.max_buy)}" if args.max_buy else "")
              + (f", sell over {money(args.target_sell)}" if args.target_sell else ""))
        return 0

    if args.watch_action == "remove":
        ok = alerts_mod.remove_watch(db, args.card_id, args.variant)
        print(f"Removed {args.card_id}" if ok else f"{args.card_id} was not watched")
        return 0 if ok else 1

    rows = alerts_mod.list_watch(db, config)

    def render() -> None:
        heading("Watchlist")
        table(
            [[
                r["card_id"], r.get("name") or "", r.get("set_name") or "",
                money(r["metrics"]["price"]), money(r["metrics"]["low"]),
                money(r["max_buy"]), money(r["target_sell"]),
                pct(r["metrics"]["change_7d"]),
                _c("BUY", GREEN) if r["buy_ready"] else
                _c("SELL", YELLOW) if r["sell_ready"] else "",
            ] for r in rows],
            ["ID", "NAME", "SET", "MARKET", "LOW", "MAX BUY", "TARGET", "7D", "STATUS"],
            ["l", "l", "l", "r", "r", "r", "r", "r", "l"],
        )
    emit(args, rows, render)
    return 0


def cmd_card(args: argparse.Namespace, db: Database, config: Config) -> int:
    card = db.get_card(args.card_id)
    if card is None:
        print(_c(f"Unknown card {args.card_id}. Try: pokeflip search {args.card_id}", RED))
        return 1
    source = config.provider.preferred_source
    variants = db.query(
        "SELECT DISTINCT variant FROM price_history WHERE card_id = ? AND source = ?",
        (args.card_id, source),
    )
    metrics = [load_metrics(db, args.card_id, r["variant"], source, days=args.days)
               for r in variants]

    def render() -> None:
        heading(f"{card['name']} - {card['set_name']} {card['number']} ({card['rarity']})")
        for m in metrics:
            print(_c(f"  {m.variant} ({m.source}, {m.points} points)", BOLD))
            print(f"    Market {money(m.price)}   Low {money(m.low)}   "
                  f"High {money(m.high)}")
            print(f"    SMA  7d {money(m.sma7)}   30d {money(m.sma30)}   "
                  f"90d {money(m.sma90)}")
            print(f"    Change  1d {pct(m.change_1d)}   7d {pct(m.change_7d)}   "
                  f"30d {pct(m.change_30d)}   90d {pct(m.change_90d)}")
            print(f"    Range   30d peak {money(m.peak_30)} / trough "
                  f"{money(m.trough_30)}   drawdown {pct(m.drawdown_30)}")
            z_note = "" if m.zscore_90 is None else f"   z-score {m.zscore_90:+.2f}"
            print(f"    Signal  trend {m.direction}   "
                  f"momentum {pct(m.momentum)}{z_note}")
            if m.spread_pct is not None:
                print(f"    Spread  listing floor is {pct(m.spread_pct)} under market")
            print()
        lots = portfolio.open_lots(db, args.card_id)
        if lots:
            print(_c("  Your lots", BOLD))
            table(
                [[l["id"], l["quantity"], money(l["cost_each"]), l["acquired_at"][:10]]
                 for l in lots],
                ["ID", "QTY", "COST EA", "ACQUIRED"], ["r", "r", "r", "l"],
            )
    emit(args, {"card": dict(card), "variants": [m.to_dict() for m in metrics]}, render)
    return 0


def cmd_alerts(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.ack:
        count = alerts_mod.acknowledge(db, args.alert_id)
        print(f"Acknowledged {count} alert(s)")
        return 0
    rows = alerts_mod.recent_alerts(db, limit=args.limit, unacknowledged_only=args.unread)

    def render() -> None:
        heading("Alerts")
        colors = {"urgent": RED, "warn": YELLOW, "info": CYAN}
        table(
            [[
                r["id"], r["created_at"][:16].replace("T", " "),
                _c(r["severity"].upper(), colors.get(r["severity"], "")),
                r["kind"], r["title"],
            ] for r in rows],
            ["ID", "WHEN", "SEV", "KIND", "WHAT"],
            ["r", "l", "l", "l", "l"],
        )
    emit(args, rows, render)
    return 0


def cmd_bulk(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.bulk_action == "value":
        items = _read_items(args)
        if not items:
            print("Provide --file CSV or repeated --item card_id:variant:qty")
            return 1
        valuation = bulk_mod.value_lot(db, config, items, args.ask, args.shipping)
        emit(args, valuation.to_dict(), lambda: _render_lot(valuation))
        return 0

    if args.bulk_action == "estimate":
        result = bulk_mod.estimate_by_count(db, config, args.count, args.set,
                                            None, args.ask, args.shipping)

        def render() -> None:
            heading(f"Unsorted lot: {args.count:,} cards")
            print(f"  Sticker value    {money(result['market_value'])}")
            print(f"  Realistic net    {money(result['realizable_value'])} "
                  f"({money(result['value_per_card'])}/card)")
            print(f"  Maximum bid      {_c(money(result['max_bid']), GREEN)} "
                  f"({money(result['max_bid_per_card'])}/card)")
            if "verdict" in result:
                verdict = result["verdict"]
                color = VERDICT_COLORS.get(verdict, "")
                print(f"  At {money(result['ask_price'])} ask: "
                      f"{_c(verdict.replace('_', ' ').upper(), color)} "
                      f"({pct(result['margin_at_ask'], 0)} margin, "
                      f"{signed(result['profit_at_ask'])})")
            print(f"  Data confidence  {result['confidence'] * 100:.0f}%")
            heading("Assumed composition")
            table(
                [[b["rarity"], f"{b['cards']:.0f}", plain_pct(b["assumed_share"]),
                  money(b["reference_price"]), money(b["realizable"]), b["samples"]]
                 for b in result["breakdown"]],
                ["RARITY", "CARDS", "SHARE", "REF PRICE", "NET", "SAMPLES"],
                ["l", "r", "r", "r", "r", "r"],
            )
            print()
            for note in result["caveats"]:
                print(_c(f"  - {note}", DIM))
        emit(args, result, render)
        return 0

    if args.bulk_action == "plan":
        plan = bulk_mod.sell_plan(db, config)

        def render() -> None:
            heading("Sell individually")
            table(
                [[e["card_name"], e["set_name"], e["quantity"], money(e["price"]),
                  money(e["net_if_single"]), money(e["quantity_value_single"])]
                 for e in plan["sell_individually"][:25]],
                ["CARD", "SET", "QTY", "MARKET", "NET EA", "TOTAL"],
                ["l", "l", "r", "r", "r", "r"],
            )
            heading("Move as bulk")
            table(
                [[e["card_name"], e["set_name"], e["quantity"], money(e["price"]),
                  money(e["net_if_bulk"]), e["reason"]]
                 for e in plan["sell_as_bulk"][:25]],
                ["CARD", "SET", "QTY", "MARKET", "NET EA", "WHY"],
                ["l", "l", "r", "r", "r", "l"],
            )
            print()
            print(f"  Singles net      {money(plan['singles_net_total'])}")
            print(f"  Bulk net         {money(plan['bulk_net_total'])} "
                  f"across {plan['bulk_card_count']} cards")
            print(f"  Suggested ask    {_c(money(plan['suggested_bulk_ask']), GREEN)}")
            print(_c(f"  {plan['note']}", DIM))
        emit(args, plan, render)
        return 0

    if args.bulk_action == "set":
        data = bulk_mod.set_value_concentration(db, config, args.set_id, top_n=args.top)

        def render() -> None:
            heading(f"Value concentration: {data['set_name']}")
            print(f"  Cards priced     {data['cards_priced']}")
            print(f"  Total value      {money(data['total_value'])}")
            print(f"  Median card      {money(data['median_card'])}")
            print(f"  Top {len(data['top_cards'])} share      "
                  f"{pct(data['top_share'], 0)}")
            heading("Chase cards")
            table(
                [[c["card_name"], c["number"], c["rarity"], money(c["price"]),
                  pct(c["change_30d"]), c["direction"]] for c in data["top_cards"]],
                ["CARD", "NO.", "RARITY", "PRICE", "30D", "TREND"],
                ["l", "l", "l", "r", "r", "l"],
            )
            print()
            print(_c(f"  {data['note']}", DIM))
        emit(args, data, render)
        return 0

    print("Unknown bulk action")
    return 1


def _render_lot(valuation: bulk_mod.LotValuation) -> None:
    heading(f"Lot valuation - {valuation.cards} cards")
    print(f"  Sticker value    {money(valuation.market_value)}")
    print(f"  Singles net      {money(valuation.singles_value)}")
    print(f"  Bulk net         {money(valuation.bulk_value)}")
    print(f"  Filler net       {money(valuation.filler_value)}")
    print(f"  Realistic net    {_c(money(valuation.realizable_value), BOLD)}")
    print(f"  Maximum bid      {_c(money(valuation.max_bid), GREEN)}")
    if valuation.ask_price is not None:
        color = VERDICT_COLORS.get(valuation.verdict, "")
        print(f"  At {money(valuation.ask_price)} ask: "
              f"{_c(valuation.verdict.upper(), color)} "
              f"({pct(valuation.margin_at_ask, 0)} margin, "
              f"{signed(valuation.profit_at_ask)})")
    if valuation.top_cards:
        heading("Where the value is")
        table(
            [[c["card_name"], c["set_name"], c["quantity"], money(c["price"]),
              money(c["market_value"]), c["treatment"]] for c in valuation.top_cards],
            ["CARD", "SET", "QTY", "PRICE", "VALUE", "PLAN"],
            ["l", "l", "r", "r", "r", "l"],
        )
    if valuation.notes:
        print()
        for note in valuation.notes:
            print(_c(f"  - {note}", DIM))


def _read_items(args: argparse.Namespace) -> list[tuple[str, str, int]]:
    items: list[tuple[str, str, int]] = []
    if getattr(args, "file", None):
        with open(args.file, newline="") as fh:
            for row in csv.DictReader(fh):
                card_id = (row.get("card_id") or row.get("id") or "").strip()
                if not card_id:
                    continue
                items.append((
                    card_id,
                    (row.get("variant") or "normal").strip(),
                    int(row.get("quantity") or row.get("qty") or 1),
                ))
    for raw in getattr(args, "item", None) or []:
        parts = raw.split(":")
        card_id = parts[0]
        variant = parts[1] if len(parts) > 1 and parts[1] else "normal"
        qty = int(parts[2]) if len(parts) > 2 else 1
        items.append((card_id, variant, qty))
    return items


def cmd_order(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.order_action == "new":
        try:
            order_id = orders_mod.create_order(
                db, config, args.kind, args.card_id, args.quantity, args.price,
                args.variant, args.condition, args.marketplace, notes=args.note,
            )
        except ValueError as exc:
            print(_c(str(exc), RED))
            return 1
        verb = "Bidding on" if args.kind == "buy" else "Listing"
        print(f"Order {order_id}: {verb} {args.quantity}x {args.card_id} "
              f"at {money(args.price)}")
        return 0

    if args.order_action == "take":
        run = signals.generate(db, config, persist=False)
        pool = run.buys if args.kind == "buy" else run.sells
        match = next((s for s in pool if s.card_id == args.card_id), None)
        if match is None:
            print(_c(f"No current {args.kind} signal for {args.card_id}. "
                     f"Run: pokeflip scan", RED))
            return 1
        order_id = orders_mod.create_from_signal(db, config, match.to_dict(),
                                                 args.quantity)
        price = match.entry_price if args.kind == "buy" else match.price
        print(f"Order {order_id} from {match.action}: "
              f"{args.kind} {match.card_name} at {money(price)}")
        print(_c(f"  {match.reasons[0] if match.reasons else ''}", DIM))
        return 0

    if args.order_action == "fill":
        try:
            result = orders_mod.fill_order(db, config, args.order_id, args.price,
                                           args.quantity)
        except ValueError as exc:
            print(_c(str(exc), RED))
            return 1

        def render() -> None:
            if result["kind"] == "buy":
                print(f"Bought {result['quantity']}x {result['card_id']} at "
                      f"{money(result['price_each'])} "
                      f"(holding {result['holding_id']})")
            else:
                print(f"Sold {result['quantity']}x {result['card_id']} at "
                      f"{money(result['price_each'])}")
                print(f"  Net proceeds   {money(result['net_proceeds'])}")
                print(f"  Cost basis     {money(result['cost_basis'])}")
                print(f"  Realised P&L   {signed(result['realized_pnl'])} "
                      f"({pct(result['roi'], 0)})")
            if result.get("remaining_quantity"):
                print(_c(f"  {result['remaining_quantity']} still open as order "
                         f"{result['remaining_order_id']}", DIM))
        emit(args, result, render)
        return 0

    if args.order_action == "cancel":
        ok = orders_mod.cancel_order(db, args.order_id)
        print(f"Cancelled order {args.order_id}" if ok
              else f"No open order {args.order_id}")
        return 0 if ok else 1

    if args.order_action == "reprice":
        try:
            result = orders_mod.reprice(db, args.order_id, args.price, args.reason)
        except ValueError as exc:
            print(_c(str(exc), RED))
            return 1
        print(f"Order {args.order_id}: {money(result['from'])} -> "
              f"{money(result['to'])}")
        return 0

    rows = orders_mod.list_orders(db, args.kind, None if args.all else "open")

    def render() -> None:
        heading("Orders")
        table(
            [[
                o.id, o.kind, o.status, o.card_name, f"{o.set_name} {o.number}",
                o.quantity, money(o.limit_price), o.condition,
                (o.days_open() if o.days_open() is not None else "-"),
                o.signal_action or "",
            ] for o in rows],
            ["ID", "KIND", "STATUS", "CARD", "SET", "QTY", "PRICE", "COND",
             "DAYS", "SIGNAL"],
            ["r", "l", "l", "l", "l", "r", "r", "l", "r", "l"],
        )
        book = orders_mod.summary(db, config)
        print()
        print(f"  {book['open_buys']} open buys ({money(book['open_buy_value'])}), "
              f"{book['open_listings']} listings ({money(book['listed_value'])})")
    emit(args, [o.to_dict() for o in rows], render)
    return 0


def cmd_listings(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.apply:
        applied = orders_mod.apply_suggestions(db, config, tuple(args.verdicts))
        emit(args, applied, lambda: (
            print(f"Re-priced {len(applied)} listing(s)"),
            *[print(f"  order {a['order_id']}: {money(a['from'])} -> {money(a['to'])}")
              for a in applied],
        ))
        return 0

    health = orders_mod.listing_health(db, config)

    def render() -> None:
        heading("Live listings")
        colors = {"cut": YELLOW, "raise": GREEN, "pull": RED, "hold": ""}
        table(
            [[
                l["id"], l["card_name"], f"{l['set_name']} {l['number']}",
                l["quantity"], money(l["limit_price"]), money(l["market_price"]),
                pct(l["ask_vs_market"], 0), l["days_on_market"],
                _c(l["verdict"].upper(), colors.get(l["verdict"], "")),
                money(l["suggested_price"]),
            ] for l in health["listings"]],
            ["ID", "CARD", "SET", "QTY", "ASK", "MARKET", "VS MKT", "DAYS",
             "VERDICT", "SUGGEST"],
            ["r", "l", "l", "r", "r", "r", "r", "r", "l", "r"],
        )
        print()
        print(f"  {health['count']} listed, {money(health['listed_value'])} at ask, "
              f"{health['stale_count']} stale, "
              f"{len(health['needs_action'])} need a decision")
        for entry in health["needs_action"]:
            print(_c(f"  - {entry['card_name']}: {entry['reason']}", DIM))
    emit(args, health, render)
    return 0


def cmd_sell(args: argparse.Namespace, db: Database, config: Config) -> int:
    try:
        result = portfolio.sell_position(
            db, config, args.card_id, args.quantity, args.price,
            args.variant, args.condition, args.method,
        )
    except ValueError as exc:
        print(_c(str(exc), RED))
        return 1

    def render() -> None:
        print(f"Sold {result['quantity']}x {result['card_id']} at "
              f"{money(result['price_each'])} using {result['method']} "
              f"across {result['lots_used']} lot(s)")
        print(f"  Cost basis     {money(result['cost_basis'])}")
        print(f"  Net proceeds   {money(result['net_proceeds'])}")
        print(f"  Realised P&L   {signed(result['realized_pnl'])}")
    emit(args, result, render)
    return 0


def cmd_grade(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.grade_action == "comp":
        try:
            grading_mod.record_comp(db, args.card_id, args.grade, args.price,
                                    args.variant, args.service)
        except ValueError as exc:
            print(_c(str(exc), RED))
            return 1
        print(f"Recorded {args.service} {args.grade} at {money(args.price)} "
              f"for {args.card_id}")
        return 0

    if args.grade_action == "card":
        verdict = grading_mod.evaluate(db, config, args.card_id, args.variant,
                                       args.condition)

        def render() -> None:
            heading(f"Grade {verdict.card_name}? ({verdict.set_name})")
            colors = {"grade": GREEN, "marginal": YELLOW, "sell_raw": RED}
            print(f"  Raw price        {money(verdict.raw_price)}")
            print(f"  Net if sold raw  {money(verdict.raw_net)}")
            print(f"  Grading cost     {money(verdict.grading_cost)} each")
            print(f"  Expected net     {money(verdict.expected_net)}")
            print(f"  Expected gain    {signed(verdict.expected_profit)}"
                  + (f"  ({pct(verdict.expected_roi, 0)})"
                     if verdict.expected_roi is not None else ""))
            print(f"  Capital tied up  {verdict.turnaround_days} days")
            print(f"  Verdict          "
                  f"{_c(verdict.verdict.replace('_', ' ').upper(), colors.get(verdict.verdict, ''))}"
                  f"  {_c(f'(basis: {verdict.basis})', DIM)}")
            heading("Outcome by grade")
            table(
                [[o.grade, plain_pct(o.probability), money(o.price),
                  money(o.net_proceeds), o.source] for o in verdict.outcomes],
                ["GRADE", "ODDS", "PRICE", "NET", "SOURCE"],
                ["l", "r", "r", "r", "l"],
            )
            print()
            for reason in verdict.reasons:
                print(_c(f"  - {reason}", DIM))
        emit(args, verdict.to_dict(), render)
        return 0

    scan = grading_mod.scan_portfolio(db, config, limit=args.limit)

    def render() -> None:
        heading(f"Grading candidates ({scan['service']})")
        colors = {"grade": GREEN, "marginal": YELLOW, "sell_raw": RED}
        table(
            [[
                c["card_name"], c["set_name"], money(c["raw_price"]),
                money(c["expected_net"]), signed(c["expected_profit"]),
                _c(c["verdict"].replace("_", " ").upper(),
                   colors.get(c["verdict"], "")),
                c["basis"],
            ] for c in scan["candidates"]],
            ["CARD", "SET", "RAW", "EXP NET", "GAIN", "VERDICT", "BASIS"],
            ["l", "l", "r", "r", "r", "l", "l"],
        )
        print()
        print(f"  {len(scan['recommended'])} worth submitting, "
              f"{money(scan['submission_cost'])} in fees for "
              f"{money(scan['total_expected_profit'])} of expected upside")
        print(_c(f"  {scan['note']}", DIM))
    emit(args, scan, render)
    return 0


def cmd_backtest(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.stored:
        report = backtest_mod.latest(db)
        if report is None:
            print("No stored backtest. Run: pokeflip backtest")
            return 1
        stats = report["stats"]
        generated = report["generated_at"]
        notes: list[str] = []
    else:
        result = backtest_mod.run(db, config, lookback_days=args.days,
                                  horizons=args.horizon or None)
        stats = [s.to_dict() for s in result.stats]
        generated = result.generated_at
        notes = result.notes
        report = result.to_dict()

    def render() -> None:
        heading("Signal scorecard")
        colors = {"reliable": GREEN, "positive": GREEN, "mixed": YELLOW,
                  "unreliable": RED, "insufficient_data": DIM}
        shown = [s for s in stats
                 if args.horizon is None or s["horizon_days"] in args.horizon]
        table(
            [[
                s["kind"], s["action"], f"{s['horizon_days']}d", s["signals"],
                s["resolved"], plain_pct(s["win_rate"]),
                pct(s["median_roi"], 1), pct(s["median_return"], 1),
                pct(s["avg_max_adverse"], 1),
                _c(s["verdict"].replace("_", " "), colors.get(s["verdict"], "")),
            ] for s in shown],
            ["KIND", "REASON", "HORIZON", "N", "GRADED", "WIN RATE", "MED ROI",
             "MED MOVE", "AVG DRAWDOWN", "VERDICT"],
            ["l", "l", "r", "r", "r", "r", "r", "r", "r", "l"],
        )
        print()
        print(_c(f"  Generated {generated}", DIM))
        for note in notes:
            print(_c(f"  - {note}", DIM))
    emit(args, report, render)
    return 0


def cmd_risk(args: argparse.Namespace, db: Database, config: Config) -> int:
    data = portfolio.concentration(db, config)

    def render() -> None:
        heading("Capital allocation")
        print(f"  Cost basis          {money(data['total_cost'])}")
        print(f"  Open buy orders     {money(data['open_buy_commitments'])}")
        if data["bankroll"]:
            print(f"  Bankroll            {money(data['bankroll'])}")
            print(f"  Free capital        {money(data['free_capital'])}")
        else:
            print(_c("  Set capital.bankroll to track free capital", DIM))
        heading("Largest positions")
        table(
            [[p["card_name"], p["set_name"], p["condition"], money(p["cost"]),
              plain_pct(p["share"], 1)] for p in data["top_positions"]],
            ["CARD", "SET", "COND", "COST", "SHARE"],
            ["l", "l", "l", "r", "r"],
        )
        heading("By set")
        table(
            [[s["set_name"], money(s["cost"]), plain_pct(s["share"], 1)]
             for s in data["by_set"]],
            ["SET", "COST", "SHARE"], ["l", "r", "r"],
        )
        print()
        if data["warnings"]:
            for warning in data["warnings"]:
                print(_c(f"  ! {warning['message']}", YELLOW))
        else:
            print(_c("  No concentration limits exceeded.", DIM))
    emit(args, data, render)
    return 0


def cmd_export(args: argparse.Namespace, db: Database, config: Config) -> int:
    report = portfolio.tax_report(db, args.year)
    if not report["lines"]:
        print("No completed sales to export"
              + (f" for {args.year}" if args.year else ""))
        return 1

    columns = ["card_id", "card_name", "set_name", "variant", "condition",
               "quantity", "acquired", "sold", "held_days", "term",
               "cost_basis", "gross_proceeds", "fees", "net_proceeds", "gain"]
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows({k: line[k] for k in columns}
                             for line in report["lines"])
        print(f"Wrote {len(report['lines'])} rows to {path}")
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=columns)
        writer.writeheader()
        writer.writerows({k: line[k] for k in columns} for line in report["lines"])

    totals = report["totals"]
    print(_c(
        f"\n{totals['sales']} sales, {money(totals['cost_basis'])} basis, "
        f"{money(totals['net_proceeds'])} net, gain {money(totals['gain'])} "
        f"(short {money(totals['short_term_gain'])}, "
        f"long {money(totals['long_term_gain'])})", DIM), file=sys.stderr)
    print(_c(report["note"], DIM), file=sys.stderr)
    return 0


def cmd_notify(args: argparse.Namespace, db: Database, config: Config) -> int:
    if args.notify_action == "test":
        alert = {
            "id": None,
            "kind": "test",
            "severity": args.severity,
            "title": "pokeflip test alert",
            "body": "If you can see this, delivery works.",
            "payload": {},
            "dedupe_key": "",
        }
        results = notify.deliver_alerts(config, [alert], db=db)
        if not results:
            print(_c("No channels configured, or realtime_alerts is off.", YELLOW))
            return 1

        def render() -> None:
            for entry in results:
                status = (_c("ok", GREEN) if entry.get("ok")
                          else _c(f"failed: {entry.get('error')}", RED))
                extra = ""
                if entry.get("sent") == 0 and entry.get("note"):
                    extra = _c(f"  ({entry['note']})", DIM)
                print(f"  {entry['channel']:<10} {status}{extra}")
            if not config.server.public_base_url:
                print()
                print(_c("  No server.public_base_url set, so notifications carry "
                         "no action buttons.", DIM))
        emit(args, results, render)
        return 0 if all(r.get("ok") for r in results) else 1

    if args.notify_action == "snooze":
        if args.clear:
            ok = alerts_mod.unsnooze(db, args.key)
            print(f"Cleared snooze for {args.key}" if ok
                  else f"{args.key} was not snoozed")
            return 0 if ok else 1
        until = alerts_mod.snooze(db, args.key, args.days)
        print(f"Muted {args.key} until {until}")
        return 0

    rows = alerts_mod.list_snoozes(db)

    def render() -> None:
        heading("Snoozed alerts")
        table(
            [[r["dedupe_key"], r["until"][:16].replace("T", " ")] for r in rows],
            ["KEY", "UNTIL"], ["l", "l"],
        )
    emit(args, rows, render)
    return 0


def cmd_bot(args: argparse.Namespace, db: Database, config: Config) -> int:
    from .bot import run_forever

    return run_forever(db, config)


def cmd_runs(args: argparse.Namespace, db: Database, config: Config) -> int:
    rows = [dict(r) for r in db.recent_runs(args.limit)]

    def render() -> None:
        heading("Recent jobs")
        table(
            [[r["id"], r["kind"], r["status"], r["started_at"][:16].replace("T", " "),
              (r["stats"] or "")[:60], (r["error"] or "")[:40]] for r in rows],
            ["ID", "KIND", "STATUS", "STARTED", "STATS", "ERROR"],
            ["r", "l", "l", "l", "l", "l"],
        )
    emit(args, rows, render)
    return 0


def cmd_run(args: argparse.Namespace, db: Database, config: Config) -> int:
    import time

    sched = scheduler_mod.Scheduler(db, config)
    sched.start()
    if not sched.running:
        print("Scheduler is disabled in config (schedule.enabled = false)")
        return 1
    print("Scheduler running. Ctrl-C to stop.")
    for job in sched.jobs():
        print(f"  {job['id']:<14} next {job['next_run']}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        sched.shutdown(wait=False)
    return 0


def cmd_serve(args: argparse.Namespace, db: Database, config: Config) -> int:
    import uvicorn

    from .api import create_app

    host = args.host or config.host
    port = args.port or config.port
    print(f"pokeflip on http://{host}:{port}  (API docs at /docs)")
    uvicorn.run(create_app(config, start_scheduler=not args.no_scheduler),
                host=host, port=port, log_level=args.log_level)
    return 0


def cmd_config(args: argparse.Namespace, db: Database, config: Config) -> int:
    print(json.dumps(config.redacted(), indent=2))
    return 0


# --- parser -------------------------------------------------------------

def _global_flags(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """Flags accepted both before and after the subcommand.

    The copies attached to subparsers suppress their defaults, otherwise
    ``pokeflip --json scan`` would have its value overwritten by the
    subparser's unset default.
    """
    kwargs: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress else {}
    parser.add_argument("--config", help="path to config.json",
                        **({} if suppress else {"default": None}), **kwargs)
    parser.add_argument("--db", help="override the database path",
                        **({} if suppress else {"default": None}), **kwargs)
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output",
                        **({} if suppress else {"default": False}), **kwargs)
    parser.add_argument("--verbose", action="store_true",
                        help="log provider activity",
                        **({} if suppress else {"default": False}), **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pokeflip",
        description="Price, trend and signal tracking for Pokemon card flippers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    _global_flags(parser, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    _global_flags(common, suppress=True)

    sub = parser.add_subparsers(dest="command", required=True)

    def cmd(group: Any, name: str, **kwargs) -> argparse.ArgumentParser:
        """Register a subcommand that also accepts the global flags."""
        return group.add_parser(name, parents=[common], **kwargs)

    p = cmd(sub, "init", help="write a starter config.json")
    p.add_argument("--path", default="config.json")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = cmd(sub, "demo", help="seed offline sample data with real-looking history")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--no-portfolio", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = cmd(sub, "search", help="find cards")
    p.add_argument("query")
    p.add_argument("--remote", action="store_true", help="skip the local catalog")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_search)

    p = cmd(sub, "sync", help="pull cards into the catalog and price them")
    p.add_argument("--set", help="set id, e.g. sv3pt5")
    p.add_argument("--query", help="search text")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_sync)

    p = cmd(sub, "refresh", help="fetch fresh prices, re-score, raise alerts")
    p.add_argument("--deliver", action="store_true", help="push alerts to your channels")
    p.set_defaults(func=cmd_refresh)

    p = cmd(sub, "scan", help="what to buy and sell right now")
    p.set_defaults(func=cmd_scan)

    p = cmd(sub, "digest", help="build the full report")
    p.add_argument("--kind", default="daily", choices=["daily", "weekly"])
    p.add_argument("--format", default="markdown", choices=["markdown", "html", "json"])
    p.add_argument("--save", action="store_true", help="write files to the report dir")
    p.add_argument("--deliver", action="store_true", help="send to your channels")
    p.set_defaults(func=cmd_digest)

    p = cmd(sub, "portfolio", help="holdings, value and P&L")
    p.set_defaults(func=cmd_portfolio)

    p = cmd(sub, "card", help="full detail for one card")
    p.add_argument("card_id")
    p.add_argument("--days", type=int, default=180)
    p.set_defaults(func=cmd_card)

    p = cmd(sub, "hold", help="manage inventory")
    hold = p.add_subparsers(dest="hold_action", required=True)
    a = cmd(hold, "add")
    a.add_argument("card_id")
    a.add_argument("--variant", default="normal")
    a.add_argument("--quantity", type=int, default=1)
    a.add_argument("--cost", type=float, default=0.0, help="cost per card")
    a.add_argument("--condition", default="NM")
    a.add_argument("--acquired", help="ISO date acquired")
    a.add_argument("--source", default="", help="where you bought it")
    a.add_argument("--note", default="")
    b = cmd(hold, "sell")
    b.add_argument("holding_id", type=int)
    b.add_argument("--quantity", type=int, default=1)
    b.add_argument("--price", type=float, required=True, help="sale price per card")
    c = cmd(hold, "remove")
    c.add_argument("holding_id", type=int)
    d = cmd(hold, "list")
    d.add_argument("--card-id")
    p.set_defaults(func=cmd_hold, card_id=None)

    p = cmd(sub, "watch", help="manage the watchlist")
    watch = p.add_subparsers(dest="watch_action", required=True)
    a = cmd(watch, "add")
    a.add_argument("card_id")
    a.add_argument("--variant", default="any")
    a.add_argument("--max-buy", type=float, help="alert when a listing drops to this")
    a.add_argument("--target-sell", type=float, help="alert when market reaches this")
    a.add_argument("--note", default="")
    b = cmd(watch, "remove")
    b.add_argument("card_id")
    b.add_argument("--variant", default="any")
    cmd(watch, "list")
    p.set_defaults(func=cmd_watch)

    p = cmd(sub, "alerts", help="recent alerts")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--unread", action="store_true")
    p.add_argument("--ack", action="store_true", help="mark as read")
    p.add_argument("--alert-id", type=int, help="acknowledge just this one")
    p.set_defaults(func=cmd_alerts)

    p = cmd(sub, "bulk", help="value lots you are buying or selling")
    bulk = p.add_subparsers(dest="bulk_action", required=True)
    a = cmd(bulk, "value", help="value an itemised lot")
    a.add_argument("--file", help="CSV with card_id,variant,quantity")
    a.add_argument("--item", action="append", help="card_id:variant:qty (repeatable)")
    a.add_argument("--ask", type=float, help="the seller's asking price")
    a.add_argument("--shipping", type=float, default=0.0)
    b = cmd(bulk, "estimate", help="value an unsorted lot by card count")
    b.add_argument("--count", type=int, required=True)
    b.add_argument("--set", action="append", default=[], help="restrict to set id(s)")
    b.add_argument("--ask", type=float)
    b.add_argument("--shipping", type=float, default=0.0)
    cmd(bulk, "plan", help="split your inventory into singles vs bulk")
    c = cmd(bulk, "set", help="where the value sits in a set")
    c.add_argument("set_id")
    c.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_bulk)

    p = cmd(sub, "order", help="open buy orders and live listings")
    order = p.add_subparsers(dest="order_action", required=True)
    a = cmd(order, "new", help="record a bid or a listing")
    a.add_argument("kind", choices=["buy", "sell"])
    a.add_argument("card_id")
    a.add_argument("--quantity", type=int, default=1)
    a.add_argument("--price", type=float, required=True,
                   help="your max bid, or your asking price")
    a.add_argument("--variant", default="normal")
    a.add_argument("--condition", default="NM")
    a.add_argument("--marketplace")
    a.add_argument("--note", default="")
    b = cmd(order, "take", help="turn today's recommendation into an order")
    b.add_argument("card_id")
    b.add_argument("--kind", choices=["buy", "sell"], default="buy")
    b.add_argument("--quantity", type=int)
    c = cmd(order, "fill", help="it went through - update the portfolio")
    c.add_argument("order_id", type=int)
    c.add_argument("--price", type=float, help="actual price; defaults to the order's")
    c.add_argument("--quantity", type=int, help="partial fills are fine")
    d = cmd(order, "reprice", help="change a listing's ask")
    d.add_argument("order_id", type=int)
    d.add_argument("--price", type=float, required=True)
    d.add_argument("--reason", default="")
    e = cmd(order, "cancel")
    e.add_argument("order_id", type=int)
    f = cmd(order, "list")
    f.add_argument("--kind", choices=["buy", "sell"])
    f.add_argument("--all", action="store_true", help="include closed orders")
    p.set_defaults(func=cmd_order, kind=None, all=False)

    p = cmd(sub, "listings", help="how your live listings are doing")
    p.add_argument("--apply", action="store_true",
                   help="actually re-price the ones flagged for a cut")
    p.add_argument("--verdicts", nargs="+", default=["cut"],
                   choices=["cut", "raise", "pull"],
                   help="which verdicts --apply should act on")
    p.set_defaults(func=cmd_listings)

    p = cmd(sub, "sell", help="sell from a position, picking lots automatically")
    p.add_argument("card_id")
    p.add_argument("--quantity", type=int, default=1)
    p.add_argument("--price", type=float, required=True, help="sale price per card")
    p.add_argument("--variant", default="normal")
    p.add_argument("--condition")
    p.add_argument("--method", choices=list(portfolio.LOT_SELECTION),
                   help="which lot to sell first")
    p.set_defaults(func=cmd_sell)

    p = cmd(sub, "grade", help="is it worth sending cards away to be graded")
    grade = p.add_subparsers(dest="grade_action", required=True)
    a = cmd(grade, "card", help="evaluate one card")
    a.add_argument("card_id")
    a.add_argument("--variant", default="normal")
    a.add_argument("--condition", default="NM")
    b = cmd(grade, "scan", help="check everything you hold")
    b.add_argument("--limit", type=int, default=25)
    c = cmd(grade, "comp", help="record an observed graded sale price")
    c.add_argument("card_id")
    c.add_argument("grade")
    c.add_argument("--price", type=float, required=True)
    c.add_argument("--variant", default="normal")
    c.add_argument("--service", default="PSA")
    p.set_defaults(func=cmd_grade, limit=25)

    p = cmd(sub, "backtest", help="grade the signal rules against history")
    p.add_argument("--days", type=int, help="how far back to replay")
    p.add_argument("--horizon", type=int, action="append",
                   help="forward window in days (repeatable)")
    p.add_argument("--stored", action="store_true",
                   help="show the last result instead of re-running")
    p.set_defaults(func=cmd_backtest)

    p = cmd(sub, "risk", help="capital allocation and concentration")
    p.set_defaults(func=cmd_risk)

    p = cmd(sub, "export", help="tax-ready CSV of completed sales")
    p.add_argument("--year", type=int)
    p.add_argument("--output", help="write here instead of stdout")
    p.set_defaults(func=cmd_export)

    p = cmd(sub, "notify", help="test delivery and manage snoozed alerts")
    nfy = p.add_subparsers(dest="notify_action", required=True)
    a = cmd(nfy, "test", help="send a test alert to every configured channel")
    a.add_argument("--severity", default="urgent",
                   choices=["info", "warn", "urgent"],
                   help="urgent also bypasses quiet hours")
    b = cmd(nfy, "snooze", help="stop an alert recurring")
    b.add_argument("key", help="the alert's dedupe key, e.g. spike:sv3pt5-25:holofoil")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--clear", action="store_true", help="un-snooze it instead")
    cmd(nfy, "list", help="what is currently muted")
    p.set_defaults(func=cmd_notify, severity="urgent")

    p = cmd(sub, "bot", help="run the Telegram bot in the foreground")
    p.set_defaults(func=cmd_bot)

    p = cmd(sub, "runs", help="recent job history")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_runs)

    p = cmd(sub, "run", help="run the scheduler in the foreground")
    p.set_defaults(func=cmd_run)

    p = cmd(sub, "serve", help="dashboard, API and scheduler")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-scheduler", action="store_true")
    p.add_argument("--log-level", default="info")
    p.set_defaults(func=cmd_serve)

    p = cmd(sub, "config", help="show the resolved configuration")
    p.set_defaults(func=cmd_config)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = Config.load(args.config)
    if args.db:
        config.database = args.db
    db = Database(config.database)

    try:
        return args.func(args, db, config)
    except ProviderError as exc:
        print(_c(f"Price source unavailable: {exc}", RED), file=sys.stderr)
        print("Set provider.name to 'fixture' to work offline, or check your network.",
              file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
