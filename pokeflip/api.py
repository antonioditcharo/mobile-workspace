"""REST API and dashboard host.

``create_app`` wires the database, the scheduler and the routes together.
Running ``pokeflip serve`` gives you the dashboard at ``/`` and everything the
UI uses under ``/api`` - the UI has no privileged access, so anything it can do
you can do from a script.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import alerts as alerts_mod
from . import backtest as backtest_mod
from . import bulk as bulk_mod
from . import digest as digest_mod
from . import grading as grading_mod
from . import ingest, orders as orders_mod, portfolio, scheduler as scheduler_mod, signals
from .analytics import load_metrics
from .config import Config
from .db import Database
from .providers import ProviderError

log = logging.getLogger("pokeflip.api")
WEB_DIR = Path(__file__).with_name("web")


# --- request bodies -----------------------------------------------------

class WatchBody(BaseModel):
    card_id: str
    variant: str = "any"
    max_buy: float | None = None
    target_sell: float | None = None
    note: str = ""


class HoldingBody(BaseModel):
    card_id: str
    variant: str = "normal"
    quantity: int = Field(1, ge=1)
    cost_each: float = Field(0.0, ge=0)
    condition: str = "NM"
    acquired_at: str | None = None
    acquired_from: str = ""
    notes: str = ""


class SellBody(BaseModel):
    quantity: int = Field(1, ge=1)
    price_each: float = Field(..., ge=0)
    sold_at: str | None = None


class SyncBody(BaseModel):
    query: str | None = None
    set_id: str | None = None
    limit: int = Field(25, ge=1, le=250)


class LotItem(BaseModel):
    card_id: str
    variant: str = "normal"
    quantity: int = Field(1, ge=1)


class LotBody(BaseModel):
    name: str = "Untitled lot"
    description: str = ""
    ask_price: float | None = None
    shipping: float = 0.0
    items: list[LotItem] = Field(default_factory=list)


class LotValueBody(BaseModel):
    items: list[LotItem]
    ask_price: float | None = None
    shipping: float = 0.0


class OrderBody(BaseModel):
    kind: str = Field(..., pattern="^(buy|sell)$")
    card_id: str
    variant: str = "normal"
    condition: str = "NM"
    quantity: int = Field(1, ge=1)
    limit_price: float | None = None
    marketplace: str | None = None
    holding_id: int | None = None
    notes: str = ""


class FromSignalBody(BaseModel):
    signal: dict[str, Any]
    quantity: int | None = Field(None, ge=1)


class FillBody(BaseModel):
    price: float | None = Field(None, ge=0)
    quantity: int | None = Field(None, ge=1)
    when: str | None = None


class RepriceBody(BaseModel):
    price: float = Field(..., gt=0)
    reason: str = ""


class SellPositionBody(BaseModel):
    card_id: str
    variant: str = "normal"
    condition: str | None = None
    quantity: int = Field(1, ge=1)
    price_each: float = Field(..., ge=0)
    method: str | None = None


class CompBody(BaseModel):
    card_id: str
    grade: str
    price: float = Field(..., gt=0)
    variant: str = "normal"
    service: str = "PSA"
    source: str = "manual"


class EstimateBody(BaseModel):
    card_count: int = Field(..., ge=1)
    set_ids: list[str] = Field(default_factory=list)
    mix: dict[str, float] | None = None
    ask_price: float | None = None
    shipping: float = 0.0


# --- app ----------------------------------------------------------------

def create_app(config: Config | None = None, start_scheduler: bool = True) -> FastAPI:
    config = config or Config.load()
    db = Database(config.database)
    sched = scheduler_mod.Scheduler(db, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler and config.schedule.enabled:
            sched.start()
        try:
            yield
        finally:
            sched.shutdown()

    app = FastAPI(title="pokeflip", version="1.0.0", lifespan=lifespan)
    app.state.db = db
    app.state.config = config
    app.state.scheduler = sched

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # --- pages ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> Any:
        index = WEB_DIR / "index.html"
        if not index.is_file():
            return HTMLResponse("<h1>pokeflip</h1><p>Dashboard assets missing.</p>")
        return FileResponse(index)

    # --- meta -----------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        counts = db.one(
            """
            SELECT (SELECT COUNT(*) FROM cards) AS cards,
                   (SELECT COUNT(*) FROM price_history) AS prices,
                   (SELECT COUNT(*) FROM holdings WHERE status='open') AS holdings,
                   (SELECT COUNT(*) FROM watchlist WHERE active=1) AS watchlist
            """
        )
        return {
            "status": "ok",
            "provider": config.provider.name,
            "source": config.provider.preferred_source,
            "scheduler_running": sched.running,
            "counts": dict(counts) if counts else {},
        }

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return config.redacted()

    @app.get("/api/scheduler")
    def scheduler_state() -> dict[str, Any]:
        return {"running": sched.running, "enabled": config.schedule.enabled,
                "jobs": sched.jobs()}

    @app.get("/api/runs")
    def runs(limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
        out = []
        for row in db.recent_runs(limit):
            entry = dict(row)
            try:
                entry["stats"] = json.loads(entry["stats"] or "{}")
            except (TypeError, ValueError):
                entry["stats"] = {}
            out.append(entry)
        return out

    # --- cards ----------------------------------------------------------

    @app.get("/api/cards/search")
    def search(q: str = Query(..., min_length=1), remote: bool = False,
               limit: int = Query(25, ge=1, le=250)) -> list[dict[str, Any]]:
        if remote:
            try:
                return ingest.search_and_store(db, config, q, limit=limit)
            except ProviderError as exc:
                raise HTTPException(502, f"provider error: {exc}") from exc
        return [dict(r) for r in db.search_cards(q, limit=limit)]

    @app.post("/api/cards/sync")
    def sync(body: SyncBody) -> dict[str, Any]:
        try:
            if body.set_id:
                return ingest.sync_set_cards(db, config, body.set_id)
            if body.query:
                cards = ingest.search_and_store(db, config, body.query, limit=body.limit)
                captured = ingest.capture_prices(db, config, [c["id"] for c in cards])
                return {"cards": cards, "prices": captured}
        except ProviderError as exc:
            raise HTTPException(502, f"provider error: {exc}") from exc
        raise HTTPException(400, "provide either query or set_id")

    @app.get("/api/cards/{card_id}")
    def card_detail(card_id: str, days: int = Query(180, ge=7, le=1000)) -> dict[str, Any]:
        card = db.get_card(card_id)
        if card is None:
            raise HTTPException(404, f"unknown card {card_id}")
        source = config.provider.preferred_source
        variants = db.query(
            "SELECT DISTINCT variant FROM price_history WHERE card_id = ? AND source = ?",
            (card_id, source),
        )
        detail = {
            "card": dict(card),
            "source": source,
            "variants": [],
            "holdings": portfolio.open_lots(db, card_id),
        }
        for row in variants:
            metrics = load_metrics(db, card_id, row["variant"], source, days=days,
                                   include_history=True)
            detail["variants"].append(metrics.to_dict(include_history=True))
        return detail

    # --- signals --------------------------------------------------------

    @app.get("/api/signals")
    def get_signals(kind: str | None = None,
                    limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
        return signals.latest_signals(db, kind=kind, limit=limit)

    @app.post("/api/signals/run")
    def run_signals() -> dict[str, Any]:
        return signals.generate(db, config).to_dict()

    @app.post("/api/refresh")
    def refresh(deliver: bool = False) -> dict[str, Any]:
        try:
            return scheduler_mod.run_refresh_cycle(db, config, deliver=deliver)
        except ProviderError as exc:
            raise HTTPException(502, f"provider error: {exc}") from exc

    # --- portfolio ------------------------------------------------------

    @app.get("/api/portfolio")
    def get_portfolio() -> dict[str, Any]:
        return portfolio.summary(db, config)

    @app.get("/api/holdings")
    def get_holdings(card_id: str | None = None) -> list[dict[str, Any]]:
        return portfolio.open_lots(db, card_id)

    @app.post("/api/holdings")
    def create_holding(body: HoldingBody) -> dict[str, Any]:
        try:
            holding_id = portfolio.add_holding(
                db, body.card_id, body.variant, body.quantity, body.cost_each,
                body.condition, body.acquired_at, body.acquired_from, body.notes,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": holding_id}

    @app.delete("/api/holdings/{holding_id}")
    def delete_holding(holding_id: int) -> dict[str, Any]:
        if not portfolio.remove_holding(db, holding_id):
            raise HTTPException(404, f"no holding {holding_id}")
        return {"deleted": holding_id}

    @app.post("/api/holdings/{holding_id}/sell")
    def sell(holding_id: int, body: SellBody) -> dict[str, Any]:
        try:
            return portfolio.sell_holding(db, holding_id, body.quantity,
                                          body.price_each, config, body.sold_at)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/positions/sell")
    def sell_from_position(body: SellPositionBody) -> dict[str, Any]:
        try:
            return portfolio.sell_position(
                db, config, body.card_id, body.quantity, body.price_each,
                body.variant, body.condition, body.method,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/portfolio/concentration")
    def get_concentration() -> dict[str, Any]:
        return portfolio.concentration(db, config)

    @app.get("/api/portfolio/tax")
    def get_tax(year: int | None = None) -> dict[str, Any]:
        return portfolio.tax_report(db, year)

    # --- orders and listings --------------------------------------------

    @app.get("/api/orders")
    def get_orders(kind: str | None = None, status: str | None = "open"
                   ) -> list[dict[str, Any]]:
        return [o.to_dict() for o in orders_mod.list_orders(db, kind, status)]

    @app.post("/api/orders")
    def create_order(body: OrderBody) -> dict[str, Any]:
        try:
            order_id = orders_mod.create_order(
                db, config, body.kind, body.card_id, body.quantity, body.limit_price,
                body.variant, body.condition, body.marketplace, body.holding_id,
                notes=body.notes,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": order_id}

    @app.post("/api/orders/from-signal")
    def order_from_signal(body: FromSignalBody) -> dict[str, Any]:
        try:
            order_id = orders_mod.create_from_signal(db, config, body.signal,
                                                     body.quantity)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": order_id}

    @app.post("/api/orders/{order_id}/fill")
    def fill(order_id: int, body: FillBody) -> dict[str, Any]:
        try:
            return orders_mod.fill_order(db, config, order_id, body.price,
                                         body.quantity, body.when)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/orders/{order_id}/reprice")
    def reprice(order_id: int, body: RepriceBody) -> dict[str, Any]:
        try:
            return orders_mod.reprice(db, order_id, body.price, body.reason)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/orders/{order_id}")
    def cancel(order_id: int) -> dict[str, Any]:
        if not orders_mod.cancel_order(db, order_id):
            raise HTTPException(404, f"no open order {order_id}")
        return {"cancelled": order_id}

    @app.get("/api/listings")
    def listings() -> dict[str, Any]:
        return orders_mod.listing_health(db, config)

    @app.post("/api/listings/apply-suggestions")
    def apply_suggestions(verdicts: str = "cut") -> dict[str, Any]:
        wanted = tuple(v.strip() for v in verdicts.split(",") if v.strip())
        return {"applied": orders_mod.apply_suggestions(db, config, wanted)}

    # --- grading --------------------------------------------------------

    @app.get("/api/grading/scan")
    def grading_scan(limit: int = Query(25, ge=1, le=100)) -> dict[str, Any]:
        return grading_mod.scan_portfolio(db, config, limit=limit)

    @app.get("/api/grading/{card_id}")
    def grading_card(card_id: str, variant: str = "normal",
                     condition: str = "NM") -> dict[str, Any]:
        return grading_mod.evaluate(db, config, card_id, variant, condition).to_dict()

    @app.post("/api/grading/comps")
    def add_comp(body: CompBody) -> dict[str, Any]:
        try:
            comp_id = grading_mod.record_comp(db, body.card_id, body.grade, body.price,
                                              body.variant, body.service, body.source)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": comp_id}

    @app.get("/api/grading/comps/list")
    def get_comps(card_id: str | None = None) -> list[dict[str, Any]]:
        return grading_mod.list_comps(db, card_id)

    # --- backtest -------------------------------------------------------

    @app.get("/api/backtest")
    def get_backtest() -> dict[str, Any]:
        stored = backtest_mod.latest(db)
        if stored is None:
            raise HTTPException(
                404, "no backtest has been run yet; POST /api/backtest/run")
        return stored

    @app.post("/api/backtest/run")
    def run_backtest(lookback_days: int | None = None,
                     include_outcomes: bool = False) -> dict[str, Any]:
        report = backtest_mod.run(db, config, lookback_days=lookback_days)
        return report.to_dict(include_outcomes=include_outcomes)

    # --- watchlist ------------------------------------------------------

    @app.get("/api/watchlist")
    def get_watchlist() -> list[dict[str, Any]]:
        return alerts_mod.list_watch(db, config)

    @app.post("/api/watchlist")
    def add_watch(body: WatchBody) -> dict[str, Any]:
        try:
            watch_id = alerts_mod.add_watch(db, body.card_id, body.variant,
                                            body.max_buy, body.target_sell, body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": watch_id}

    @app.delete("/api/watchlist/{card_id}")
    def drop_watch(card_id: str, variant: str = "any") -> dict[str, Any]:
        if not alerts_mod.remove_watch(db, card_id, variant):
            raise HTTPException(404, f"{card_id} is not on the watchlist")
        return {"removed": card_id}

    # --- alerts ---------------------------------------------------------

    @app.get("/api/alerts")
    def get_alerts(limit: int = Query(50, ge=1, le=200),
                   unread: bool = False) -> list[dict[str, Any]]:
        return alerts_mod.recent_alerts(db, limit=limit, unacknowledged_only=unread)

    @app.post("/api/alerts/ack")
    def ack(alert_id: int | None = None) -> dict[str, Any]:
        return {"acknowledged": alerts_mod.acknowledge(db, alert_id)}

    # --- digest ---------------------------------------------------------

    @app.get("/api/digest")
    def get_digest(kind: str = "daily", deliver: bool = False,
                   save: bool = False) -> dict[str, Any]:
        built = digest_mod.build(db, config, kind=kind)
        payload: dict[str, Any] = {"digest": built.to_dict()}
        if save:
            payload["paths"] = digest_mod.save(db, config, built)
        if deliver:
            from . import notify
            payload["delivery"] = notify.deliver_digest(
                config, built, payload.get("paths"))
        return payload

    @app.get("/api/digest/html", response_class=HTMLResponse)
    def digest_html(kind: str = "daily") -> Any:
        return HTMLResponse(digest_mod.render_html(digest_mod.build(db, config, kind=kind)))

    @app.get("/api/digest/markdown", response_class=PlainTextResponse)
    def digest_markdown(kind: str = "daily") -> Any:
        return PlainTextResponse(
            digest_mod.render_markdown(digest_mod.build(db, config, kind=kind))
        )

    @app.get("/api/reports")
    def reports(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
        return [dict(r) for r in db.query(
            "SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,))]

    # --- bulk -----------------------------------------------------------

    @app.get("/api/bulk/lots")
    def lots() -> list[dict[str, Any]]:
        return bulk_mod.list_lots(db)

    @app.post("/api/bulk/lots")
    def create_lot(body: LotBody) -> dict[str, Any]:
        lot_id = bulk_mod.create_lot(db, body.name, "buy", body.description,
                                     body.ask_price, body.shipping)
        if body.items:
            bulk_mod.add_lot_items(
                db, lot_id, [(i.card_id, i.variant, i.quantity) for i in body.items])
        return {"id": lot_id}

    @app.post("/api/bulk/lots/{lot_id}/items")
    def add_items(lot_id: int, items: list[LotItem]) -> dict[str, Any]:
        added = bulk_mod.add_lot_items(
            db, lot_id, [(i.card_id, i.variant, i.quantity) for i in items])
        return {"added": added}

    @app.post("/api/bulk/lots/{lot_id}/evaluate")
    def evaluate_lot(lot_id: int) -> dict[str, Any]:
        try:
            return bulk_mod.evaluate_lot(db, config, lot_id).to_dict()
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/bulk/value")
    def value_lot(body: LotValueBody) -> dict[str, Any]:
        items = [(i.card_id, i.variant, i.quantity) for i in body.items]
        return bulk_mod.value_lot(db, config, items, body.ask_price,
                                  body.shipping).to_dict()

    @app.post("/api/bulk/estimate")
    def estimate(body: EstimateBody) -> dict[str, Any]:
        return bulk_mod.estimate_by_count(db, config, body.card_count, body.set_ids,
                                          body.mix, body.ask_price, body.shipping)

    @app.get("/api/bulk/sell-plan")
    def bulk_sell_plan() -> dict[str, Any]:
        return bulk_mod.sell_plan(db, config)

    @app.get("/api/bulk/sets/{set_id}")
    def set_concentration(set_id: str, top: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
        return bulk_mod.set_value_concentration(db, config, set_id, top_n=top)

    @app.get("/api/sets")
    def sets() -> list[dict[str, Any]]:
        return [dict(r) for r in db.query(
            "SELECT * FROM sets ORDER BY release_date DESC")]

    @app.exception_handler(ProviderError)
    async def provider_error_handler(_request, exc: ProviderError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    return app
