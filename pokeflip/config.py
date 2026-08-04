"""Configuration for pokeflip.

Settings resolve in this order (later wins):
  1. built-in defaults below
  2. the JSON config file (``--config`` / ``POKEFLIP_CONFIG``, default ``config.json``)
  3. ``POKEFLIP_*`` environment variables

Every number a flipper would want to tune lives here rather than being buried
in the signal code.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field, asdict, fields, is_dataclass
from pathlib import Path
from typing import Any


def _env(name: str) -> str | None:
    return os.environ.get(f"POKEFLIP_{name.upper()}")


@dataclass
class MarketplaceFees:
    """Cost of actually completing a sale on a given venue.

    ``net_proceeds`` answers the only question that matters when deciding
    whether a flip clears: what lands in your pocket.
    """

    name: str = "tcgplayer"
    # Fraction of sale price taken as commission (TCGplayer seller fee).
    commission_pct: float = 0.1025
    # Payment processing on top of commission.
    payment_pct: float = 0.025
    # Flat per-order payment fee.
    payment_flat: float = 0.30
    # What shipping actually costs you per order (stamp + sleeve + toploader).
    shipping_cost: float = 1.10
    # What the buyer pays you toward shipping (0 for free-shipping listings).
    shipping_charged: float = 0.00

    def net_proceeds(self, sale_price: float) -> float:
        """Money kept after fees and shipping on a single-card order."""
        gross = sale_price + self.shipping_charged
        fees = gross * (self.commission_pct + self.payment_pct) + self.payment_flat
        return gross - fees - self.shipping_cost

    def breakeven_sale_price(self, cost: float) -> float:
        """Sale price needed to exactly recover ``cost``."""
        rate = 1.0 - (self.commission_pct + self.payment_pct)
        if rate <= 0:
            return float("inf")
        needed = cost + self.payment_flat + self.shipping_cost - self.shipping_charged
        return needed / rate - self.shipping_charged


@dataclass
class BuyRules:
    """Thresholds that decide when a card is worth buying."""

    # A card must be at least this cheap relative to its own 30d average.
    dip_pct: float = 0.12
    # Statistical cheapness: z-score of today's price vs its 90d distribution.
    zscore_floor: float = -0.9
    # Minimum gap between the cheapest listing and market price to call it a spread play.
    spread_pct: float = 0.20
    # Ignore anything under this price - fees eat single-card flips below it.
    min_price: float = 3.00
    # Ignore anything above this price unless you opt in (capital + risk control).
    max_price: float = 400.00
    # Required net profit per card after fees, in dollars.
    min_net_profit: float = 2.00
    # What it costs you to take delivery of one card (shipping share, sleeve).
    acquisition_overhead: float = 0.75
    # Required net return on capital.
    min_roi: float = 0.25
    # Don't trust signals computed from a thin history.
    min_history_points: int = 8
    # Reject cards whose day-to-day swings exceed this (coefficient of variation).
    max_volatility: float = 0.55
    # Score below which a candidate is not reported at all.
    min_score: float = 45.0
    # How many buys to surface per digest.
    max_results: int = 25


@dataclass
class SellRules:
    """Thresholds that decide when to exit a position you already hold."""

    # Take profit once net ROI clears this.
    target_roi: float = 0.35
    # Sell into strength when price is this far above the 30d average.
    overextended_pct: float = 0.18
    # Bail out after giving back this much of a recent peak.
    peak_fade_pct: float = 0.10
    # Only act on peak-fade if the position is still at least break-even.
    peak_fade_requires_profit: bool = True
    # Cut a loser that is down this much and still trending down.
    stop_loss_pct: float = 0.25
    # Recycle capital: held this long with nothing happening.
    stagnant_days: int = 120
    stagnant_band_pct: float = 0.05
    min_score: float = 45.0
    max_results: int = 25


@dataclass
class BulkRules:
    """Assumptions for valuing lots you buy or sell in bulk.

    Bulk math fails when you value a lot at full market. These haircuts are the
    difference between a good lot and a garage full of cardboard.
    """

    # Fraction of a lot you realistically sell instead of sitting on forever.
    sell_through_rate: float = 0.55
    # Bulk buyers/sellers transact below market; applied to every card's value.
    bulk_discount: float = 0.25
    # Cards under this value are treated as bulk filler, not individual sales.
    filler_price_ceiling: float = 1.50
    # What filler is actually worth to you per card when moved by the box.
    filler_value_each: float = 0.03
    # Margin you require on a lot before bidding.
    target_lot_margin: float = 0.45
    # Per-card handling time cost when selling singles out of a lot.
    handling_cost_each: float = 0.15
    # Cards worth more than this get pulled and sold individually.
    single_out_threshold: float = 4.00


@dataclass
class ScheduleRules:
    """When the app goes and gets fresh data on its own."""

    enabled: bool = True
    # Refresh prices for everything tracked, every N minutes.
    refresh_interval_minutes: int = 360
    # Daily digest time, 24h local clock.
    digest_hour: int = 8
    digest_minute: int = 0
    # Also run a digest weekly with a wider lookback.
    weekly_digest_day: str = "sun"
    weekly_digest_hour: int = 9
    # Refresh the card catalog (sets/new releases) every N days.
    catalog_refresh_days: int = 7
    # Run a refresh + digest immediately when the scheduler starts.
    run_on_start: bool = False


@dataclass
class NotifyRules:
    """Where the digest and alerts get delivered."""

    # Any of: console, file, webhook, slack, discord, email
    channels: list[str] = field(default_factory=lambda: ["console", "file"])
    webhook_url: str = ""
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""
    email_to: str = ""
    email_from: str = "pokeflip@localhost"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    # Directory for written report files.
    report_dir: str = "reports"
    # Send alerts as they fire, not only in the digest.
    realtime_alerts: bool = True


@dataclass
class ProviderRules:
    """Price source selection and politeness."""

    # "pokemontcg" for the live Pokemon TCG API, "fixture" for offline/demo data.
    name: str = "pokemontcg"
    api_key: str = ""
    base_url: str = "https://api.pokemontcg.io/v2"
    # Which price block to treat as the source of truth.
    preferred_source: str = "tcgplayer"  # tcgplayer | cardmarket
    timeout_seconds: float = 30.0
    max_retries: int = 3
    # Seconds between outbound calls; the public API is rate limited.
    request_delay: float = 0.35
    page_size: int = 250
    # EUR->USD used when comparing Cardmarket against TCGplayer.
    eur_to_usd: float = 1.08
    # Directory of JSON fixtures for the offline provider.
    fixture_dir: str = "data/fixtures"


@dataclass
class Config:
    database: str = "data/pokeflip.db"
    # Sets whose whole card list you want priced every cycle, e.g. ["sv3pt5"].
    tracked_sets: list[str] = field(default_factory=list)
    # Cap on how many cards a single refresh will price.
    max_cards_per_refresh: int = 2000
    # Keep this many days of price history; older rows are pruned.
    history_retention_days: int = 730
    timezone: str = "UTC"
    host: str = "127.0.0.1"
    port: int = 8787
    provider: ProviderRules = field(default_factory=ProviderRules)
    fees: MarketplaceFees = field(default_factory=MarketplaceFees)
    buy: BuyRules = field(default_factory=BuyRules)
    sell: SellRules = field(default_factory=SellRules)
    bulk: BulkRules = field(default_factory=BulkRules)
    schedule: ScheduleRules = field(default_factory=ScheduleRules)
    notify: NotifyRules = field(default_factory=NotifyRules)

    # --- loading -------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        path = path or _env("config") or "config.json"
        cfg = cls()
        p = Path(path)
        if p.is_file():
            with p.open() as fh:
                cfg = cfg.merged(json.load(fh))
        cfg.apply_env()
        return cfg

    def merged(self, data: dict[str, Any]) -> "Config":
        """Return a copy with ``data`` overlaid on top, nested dataclasses included."""
        out = copy.deepcopy(self)
        _overlay(out, data)
        return out

    def apply_env(self) -> None:
        """Overlay ``POKEFLIP_*`` environment variables.

        Nested values use a double underscore: ``POKEFLIP_BUY__MIN_ROI=0.4``.
        """
        for key, raw in os.environ.items():
            if not key.startswith("POKEFLIP_"):
                continue
            path = key[len("POKEFLIP_"):].lower().split("__")
            if path == ["config"]:
                continue
            target: Any = self
            for part in path[:-1]:
                target = getattr(target, part, None)
                if target is None:
                    break
            if target is None or not is_dataclass(target):
                continue
            leaf = path[-1]
            if not hasattr(target, leaf):
                continue
            current = getattr(target, leaf)
            try:
                setattr(target, leaf, _coerce(raw, current))
            except (ValueError, json.JSONDecodeError):
                continue

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | os.PathLike) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")

    def redacted(self) -> dict[str, Any]:
        """Config safe to render in the UI or an API response."""
        data = self.to_dict()
        secrets = (
            ("provider", "api_key"),
            ("notify", "smtp_password"),
            ("notify", "webhook_url"),
            ("notify", "slack_webhook_url"),
            ("notify", "discord_webhook_url"),
        )
        for section, key in secrets:
            if data.get(section, {}).get(key):
                data[section][key] = "***"
        return data


def _overlay(obj: Any, data: dict[str, Any]) -> None:
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _overlay(current, value)
        else:
            setattr(obj, key, value)


def _coerce(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        raw = raw.strip()
        if raw.startswith("["):
            return json.loads(raw)
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw
