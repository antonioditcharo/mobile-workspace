"""Tests for pokeflip.

Runs on the standard library alone: ``python3 -m unittest discover tests``.
The offline fixture provider makes every case deterministic, so these tests
never touch the network.
"""

from __future__ import annotations

import contextlib
import json
import os
import logging
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokeflip import (  # noqa: E402
    actions, backtest, bot, bulk, grading, ingest, notify, orders, portfolio,
    providers, signals,
)
from pokeflip import alerts as alerts_mod  # noqa: E402
from pokeflip import desknotify, desktop, paths  # noqa: E402
from pokeflip import setup as pfsetup  # noqa: E402
from pokeflip.alerts import add_watch, evaluate_alerts, store_alerts  # noqa: E402
from pokeflip.analytics import (  # noqa: E402
    compute_metrics, linear_slope, load_metrics, pct_change, sma, value_days_ago,
)
from pokeflip.config import Config, MarketplaceFees  # noqa: E402
from pokeflip.db import Database  # noqa: E402
from pokeflip.digest import build as build_digest, render_html, render_markdown  # noqa: E402
from pokeflip.providers import ebay  # noqa: E402
from pokeflip.providers.base import CardRecord, ProviderError  # noqa: E402
from pokeflip.providers.fixture import FixtureProvider  # noqa: E402
from pokeflip.providers.pokemontcg import extract_quotes  # noqa: E402


@contextlib.contextmanager
def quiet(logger_name: str):
    """Silence a logger while a test exercises a deliberate failure path."""
    logger = logging.getLogger(logger_name)
    previous = logger.disabled
    logger.disabled = True
    try:
        yield
    finally:
        logger.disabled = previous


def make_rows(prices, start=None, variant="normal", card_id="test-1"):
    """Build price_history-shaped rows from a list of daily prices."""
    start = start or date(2025, 1, 1)
    rows = []
    for i, price in enumerate(prices):
        on = (start + timedelta(days=i)).isoformat()
        rows.append({
            "card_id": card_id, "source": "tcgplayer", "variant": variant,
            "captured_on": on, "captured_at": f"{on}T12:00:00+00:00",
            "currency": "USD", "market": price, "low": price * 0.85,
            "mid": price * 1.02, "high": price * 1.4, "direct_low": price * 0.87,
        })
    return rows


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config = Config()
        self.config.database = str(root / "test.db")
        self.config.provider.name = "fixture"
        self.config.notify.report_dir = str(root / "reports")
        self.db = Database(self.config.database)

    def tearDown(self):
        self._tmp.cleanup()


# --- fees ---------------------------------------------------------------

class FeeTests(unittest.TestCase):
    def test_net_proceeds_removes_commission_payment_and_shipping(self):
        fees = MarketplaceFees(commission_pct=0.10, payment_pct=0.02,
                               payment_flat=0.30, shipping_cost=1.00,
                               shipping_charged=0.0)
        # 100 - 12% - 0.30 - 1.00
        self.assertAlmostEqual(fees.net_proceeds(100.0), 86.70, places=2)

    def test_breakeven_round_trips_through_net_proceeds(self):
        fees = MarketplaceFees()
        price = fees.breakeven_sale_price(25.0)
        self.assertAlmostEqual(fees.net_proceeds(price), 25.0, places=6)

    def test_cheap_cards_have_negative_single_order_economics(self):
        fees = MarketplaceFees()
        self.assertLess(fees.net_proceeds(1.00), 0)


# --- analytics ----------------------------------------------------------

class AnalyticsTests(unittest.TestCase):
    def test_sma_averages_the_trailing_window(self):
        self.assertEqual(sma([1, 2, 3, 4, 5], 3), 4.0)

    def test_sma_is_partial_when_history_is_short(self):
        self.assertEqual(sma([2, 4], 10), 3.0)

    def test_pct_change_guards_zero_and_none(self):
        self.assertIsNone(pct_change(10, 0))
        self.assertIsNone(pct_change(None, 5))
        self.assertAlmostEqual(pct_change(110, 100), 0.10)

    def test_value_days_ago_uses_the_last_point_at_or_before_target(self):
        from pokeflip.analytics import rows_to_points

        points = rows_to_points(make_rows([10.0, 11.0, 12.0, 13.0, 14.0, 15.0]))
        # Latest is day 6 (15.0); three days back lands on day 3 (12.0).
        self.assertAlmostEqual(value_days_ago(points, 3), 12.0)
        # Beyond the start of the series there is nothing to compare against.
        self.assertIsNone(value_days_ago(points, 30))
        self.assertIsNone(value_days_ago([], 1))

    def test_value_days_ago_skips_gaps_backwards(self):
        from pokeflip.analytics import rows_to_points

        rows = make_rows([10.0, 20.0, 30.0])
        rows[1]["captured_on"] = "2025-01-05"   # a gap in collection
        rows[2]["captured_on"] = "2025-01-09"
        points = rows_to_points(rows)
        # Four days before 01-09 is 01-05, which has an observation.
        self.assertAlmostEqual(value_days_ago(points, 4), 20.0)
        # Two days back has none, so the nearest earlier one is used.
        self.assertAlmostEqual(value_days_ago(points, 2), 20.0)

    def test_metrics_on_a_rising_series(self):
        rows = make_rows([float(100 + i) for i in range(60)])
        m = compute_metrics("test-1", "normal", "tcgplayer", rows,
                            today=date(2025, 3, 1))
        self.assertEqual(m.points, 60)
        self.assertEqual(m.direction, "rising")
        self.assertGreater(m.momentum, 0)
        self.assertGreater(m.trend_slope, 0)
        self.assertAlmostEqual(m.price, 159.0)
        self.assertAlmostEqual(m.drawdown_30, 0.0, places=6)

    def test_metrics_on_a_falling_series(self):
        rows = make_rows([float(200 - i) for i in range(60)])
        m = compute_metrics("test-1", "normal", "tcgplayer", rows,
                            today=date(2025, 3, 1))
        self.assertEqual(m.direction, "falling")
        self.assertLess(m.momentum, 0)
        self.assertGreater(m.drawdown_30, 0)

    def test_flat_series_has_no_direction_and_no_zscore(self):
        rows = make_rows([50.0] * 40)
        m = compute_metrics("test-1", "normal", "tcgplayer", rows,
                            today=date(2025, 2, 9))
        self.assertEqual(m.direction, "flat")
        self.assertIsNone(m.zscore_90)   # zero stdev must not divide by zero

    def test_rows_without_a_usable_price_are_dropped_not_zeroed(self):
        rows = make_rows([10.0, 10.0, 10.0])
        rows[1]["market"] = rows[1]["mid"] = rows[1]["low"] = None
        m = compute_metrics("test-1", "normal", "tcgplayer", rows)
        self.assertEqual(m.points, 2)
        self.assertAlmostEqual(m.price, 10.0)

    def test_empty_history_yields_empty_metrics(self):
        m = compute_metrics("test-1", "normal", "tcgplayer", [])
        self.assertEqual(m.points, 0)
        self.assertIsNone(m.price)
        self.assertEqual(m.direction, "unknown")

    def test_linear_slope_needs_three_points(self):
        self.assertIsNone(linear_slope([]))

    def test_gaps_do_not_inflate_daily_returns(self):
        rows = make_rows([100.0, 110.0])
        rows[1]["captured_on"] = "2025-01-11"   # ten days later, not one
        m = compute_metrics("test-1", "normal", "tcgplayer", rows)
        self.assertIsNone(m.volatility)         # too few points to measure
        self.assertAlmostEqual(m.change_1d, None if m.change_1d is None else m.change_1d)


# --- provider parsing ---------------------------------------------------

class ProviderParsingTests(unittest.TestCase):
    RAW = {
        "id": "sv3pt5-199",
        "name": "Charizard ex",
        "tcgplayer": {"prices": {
            "holofoil": {"low": 300.0, "mid": 420.0, "high": 600.0,
                         "market": 410.5, "directLow": 305.0},
            "reverseHolofoil": {"low": 0, "mid": None, "high": None,
                                "market": None, "directLow": None},
        }},
        "cardmarket": {"prices": {"trendPrice": 380.0, "lowPrice": 320.0,
                                  "averageSellPrice": 375.0, "avg30": 390.0}},
    }

    def test_extracts_tcgplayer_variant_and_cardmarket_aggregate(self):
        quotes = extract_quotes(self.RAW)
        by_key = {(q.source, q.variant): q for q in quotes}
        self.assertIn(("tcgplayer", "holofoil"), by_key)
        self.assertIn(("cardmarket", "default"), by_key)
        self.assertAlmostEqual(by_key[("tcgplayer", "holofoil")].market, 410.5)
        self.assertEqual(by_key[("cardmarket", "default")].currency, "EUR")

    def test_all_zero_price_blocks_are_discarded(self):
        quotes = extract_quotes(self.RAW)
        self.assertNotIn("reverseHolofoil", [q.variant for q in quotes])

    def test_card_without_an_id_yields_nothing(self):
        self.assertEqual(extract_quotes({"tcgplayer": {"prices": {}}}), [])

    def test_fixture_provider_is_deterministic(self):
        provider = FixtureProvider(Config())
        card_id = provider.all_card_ids()[0]
        _, first = provider.quotes_on([card_id], date(2025, 6, 1))
        _, second = provider.quotes_on([card_id], date(2025, 6, 1))
        self.assertEqual(first[0].market, second[0].market)

    def test_fixture_listing_floor_sits_under_market(self):
        provider = FixtureProvider(Config())
        _, quotes = provider.quotes_on(provider.all_card_ids(), date(2025, 6, 1))
        for quote in quotes:
            self.assertLess(quote.low, quote.market)


# --- config -------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_nested_overlay_keeps_untouched_values(self):
        config = Config().merged({"buy": {"min_roi": 0.5}})
        self.assertEqual(config.buy.min_roi, 0.5)
        self.assertEqual(config.buy.min_price, Config().buy.min_price)

    def test_unknown_keys_are_ignored(self):
        config = Config().merged({"nope": 1, "buy": {"nope": 2}})
        self.assertFalse(hasattr(config.buy, "nope"))

    def test_secrets_are_redacted(self):
        config = Config()
        config.provider.api_key = "secret"
        self.assertEqual(config.redacted()["provider"]["api_key"], "***")

    def test_round_trip_through_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.buy.min_roi = 0.42
            config.save(path)
            self.assertEqual(Config.load(path).buy.min_roi, 0.42)


# --- database -----------------------------------------------------------

class DatabaseTests(TempDbCase):
    def test_same_day_capture_updates_rather_than_duplicating(self):
        self.db.upsert_cards([{"id": "c1", "name": "Test", "set_id": None,
                               "set_name": "S", "number": "1", "rarity": "",
                               "supertype": "", "subtypes": "", "artist": "",
                               "image_small": "", "image_large": "",
                               "tcgplayer_url": "", "cardmarket_url": ""}])
        rows = make_rows([10.0], card_id="c1")
        self.db.record_prices(rows)
        rows[0]["market"] = 12.0
        self.db.record_prices(rows)
        stored = self.db.price_series("c1", "normal", "tcgplayer", days=3650)
        self.assertEqual(len(stored), 1)
        self.assertAlmostEqual(stored[0]["market"], 12.0)

    def test_prune_drops_only_old_rows(self):
        ingest.seed_demo(self.db, self.config, days=40, include_portfolio=False)
        before = self.db.one("SELECT COUNT(*) AS n FROM price_history")["n"]
        self.db.prune_history(retention_days=10)
        after = self.db.one("SELECT COUNT(*) AS n FROM price_history")["n"]
        self.assertLess(after, before)
        self.assertGreater(after, 0)


# --- buy signals --------------------------------------------------------

class BuySignalTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.card = {"name": "Test Card", "set_name": "Set", "number": "1",
                     "rarity": "Rare", "image_small": ""}

    def metrics_for(self, prices, **overrides):
        rows = make_rows(prices)
        for key, value in overrides.items():
            for row in rows:
                row[key] = value
        return compute_metrics("test-1", "normal", "tcgplayer", rows,
                               today=date(2025, 1, 1) + timedelta(days=len(prices) - 1))

    def test_a_dip_below_the_average_is_a_buy(self):
        prices = [100.0] * 40 + [70.0]
        signal = signals.evaluate_buy(self.metrics_for(prices), self.card, self.config)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.kind, "buy")
        self.assertIn(signal.action, {"buy_dip", "buy_undervalued", "buy_spread"})
        self.assertTrue(signal.reasons)

    def test_thin_history_is_never_a_buy(self):
        signal = signals.evaluate_buy(self.metrics_for([100.0, 60.0]),
                                      self.card, self.config)
        self.assertIsNone(signal)

    def test_penny_cards_are_excluded(self):
        prices = [1.0] * 40 + [0.5]
        self.assertIsNone(
            signals.evaluate_buy(self.metrics_for(prices), self.card, self.config))

    def test_cards_above_the_price_ceiling_are_excluded(self):
        prices = [10_000.0] * 40 + [7_000.0]
        self.assertIsNone(
            signals.evaluate_buy(self.metrics_for(prices), self.card, self.config))

    def test_a_collapsing_card_is_not_a_dip(self):
        # Down 60% and still falling: a falling knife, not a discount.
        prices = [float(200 - i * 2.8) for i in range(45)]
        self.assertIsNone(
            signals.evaluate_buy(self.metrics_for(prices), self.card, self.config))

    def test_stale_quotes_are_rejected(self):
        rows = make_rows([100.0] * 40 + [70.0])
        metrics = compute_metrics("test-1", "normal", "tcgplayer", rows,
                                  today=date(2026, 1, 1))
        self.assertIsNone(signals.evaluate_buy(metrics, self.card, self.config))

    def test_a_flat_card_at_its_average_is_not_a_buy(self):
        self.assertIsNone(
            signals.evaluate_buy(self.metrics_for([50.0] * 60), self.card, self.config))

    def test_profit_is_computed_after_fees_and_acquisition_cost(self):
        prices = [100.0] * 40 + [70.0]
        signal = signals.evaluate_buy(self.metrics_for(prices), self.card, self.config)
        expected_cost = signal.entry_price + self.config.buy.acquisition_overhead
        self.assertAlmostEqual(signal.total_cost, round(expected_cost, 2), places=2)
        self.assertAlmostEqual(
            signal.net_profit,
            round(self.config.fees.net_proceeds(signal.price) - expected_cost, 2),
            places=2,
        )

    def test_raising_the_roi_floor_filters_marginal_buys(self):
        prices = [100.0] * 40 + [88.0]
        loose = Config()
        loose.buy.min_roi = 0.01
        loose.buy.min_net_profit = 0.01
        loose.buy.min_score = 1
        strict = Config()
        strict.buy.min_roi = 5.0
        strict.buy.min_net_profit = 500.0
        metrics = self.metrics_for(prices)
        self.assertIsNotNone(signals.evaluate_buy(metrics, self.card, loose))
        self.assertIsNone(signals.evaluate_buy(metrics, self.card, strict))


# --- sell signals -------------------------------------------------------

class SellSignalTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def position(self, cost_each=10.0, quantity=1, days_held=60):
        acquired = (date(2025, 3, 1) - timedelta(days=days_held)).isoformat()
        return portfolio.Position(
            card_id="test-1", variant="normal", quantity=quantity,
            cost_each=cost_each, total_cost=cost_each * quantity,
            acquired_at=f"{acquired}T12:00:00+00:00", card_name="Test Card",
        )

    def metrics_for(self, prices):
        rows = make_rows(prices, start=date(2025, 1, 1))
        return compute_metrics("test-1", "normal", "tcgplayer", rows,
                               today=date(2025, 1, 1) + timedelta(days=len(prices) - 1))

    def test_hitting_the_profit_target_triggers_a_sell(self):
        metrics = self.metrics_for([100.0] * 40)
        signal = signals.evaluate_sell(metrics, self.position(cost_each=20.0),
                                       self.config, today=date(2025, 2, 9))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "sell_target")
        self.assertGreater(signal.roi, self.config.sell.target_roi)

    def test_a_small_loss_in_a_flat_market_is_not_a_sell(self):
        metrics = self.metrics_for([100.0] * 40)
        signal = signals.evaluate_sell(metrics, self.position(cost_each=95.0),
                                       self.config, today=date(2025, 2, 9))
        self.assertIsNone(signal)

    def test_a_deep_loss_in_a_downtrend_triggers_a_stop(self):
        metrics = self.metrics_for([float(200 - i * 2) for i in range(45)])
        signal = signals.evaluate_sell(metrics, self.position(cost_each=190.0),
                                       self.config, today=date(2025, 2, 14))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "sell_stop_loss")

    def test_giving_back_a_peak_triggers_a_fade_sell(self):
        prices = [100.0] * 30 + [140.0, 138.0, 130.0, 124.0, 121.0]
        metrics = self.metrics_for(prices)
        signal = signals.evaluate_sell(metrics, self.position(cost_each=50.0),
                                       self.config, today=date(2025, 2, 4))
        self.assertIsNotNone(signal)
        self.assertIn(signal.action, {"sell_peak_fade", "sell_target", "sell_strength"})

    def test_quantity_scales_the_total_but_not_the_per_card_figures(self):
        metrics = self.metrics_for([100.0] * 40)
        one = signals.evaluate_sell(metrics, self.position(cost_each=20.0, quantity=1),
                                    self.config, today=date(2025, 2, 9))
        five = signals.evaluate_sell(metrics, self.position(cost_each=20.0, quantity=5),
                                     self.config, today=date(2025, 2, 9))
        self.assertAlmostEqual(one.net_profit, five.net_profit)
        self.assertAlmostEqual(five.total_net_profit, one.net_profit * 5, places=6)

    def test_bulk_priced_cards_are_left_to_the_bulk_plan(self):
        metrics = self.metrics_for([1.10] * 40)
        self.assertIsNone(
            signals.evaluate_sell(metrics, self.position(cost_each=0.10),
                                  self.config, today=date(2025, 2, 9)))


# --- portfolio ----------------------------------------------------------

class PortfolioTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=60, include_portfolio=False)
        self.card_id = FixtureProvider(self.config).all_card_ids()[0]

    def test_add_then_value_a_position(self):
        portfolio.add_holding(self.db, self.card_id, "holofoil", 3, 100.0)
        summary = portfolio.summary(self.db, self.config)
        self.assertEqual(summary["cards"], 3)
        self.assertAlmostEqual(summary["cost_basis"], 300.0)
        self.assertGreater(summary["net_liquidation"], 0)

    def test_unknown_card_is_rejected(self):
        with self.assertRaises(ValueError):
            portfolio.add_holding(self.db, "not-a-card", quantity=1)

    def test_partial_sale_splits_the_lot_and_preserves_basis(self):
        holding_id = portfolio.add_holding(self.db, self.card_id, "holofoil", 10, 20.0)
        result = portfolio.sell_holding(self.db, holding_id, 4, 50.0, self.config)
        self.assertEqual(result["quantity"], 4)
        remaining = portfolio.open_lots(self.db, self.card_id)
        self.assertEqual(sum(lot["quantity"] for lot in remaining), 6)
        self.assertAlmostEqual(remaining[0]["cost_each"], 20.0)

    def test_selling_more_than_held_is_refused(self):
        holding_id = portfolio.add_holding(self.db, self.card_id, "holofoil", 2, 10.0)
        with self.assertRaises(ValueError):
            portfolio.sell_holding(self.db, holding_id, 3, 50.0, self.config)

    def test_realized_pnl_accounts_for_fees(self):
        holding_id = portfolio.add_holding(self.db, self.card_id, "holofoil", 1, 10.0)
        portfolio.sell_holding(self.db, holding_id, 1, 100.0, self.config)
        realized = portfolio.realized_pnl(self.db)
        self.assertEqual(realized["sales"], 1)
        self.assertGreater(realized["fees_paid"], 0)
        self.assertAlmostEqual(
            realized["realized_pnl"],
            round(self.config.fees.net_proceeds(100.0) - 10.0, 2), places=2)

    def test_weighted_average_cost_across_lots(self):
        portfolio.add_holding(self.db, self.card_id, "holofoil", 1, 10.0)
        portfolio.add_holding(self.db, self.card_id, "holofoil", 3, 20.0)
        position = next(p for p in portfolio.open_positions(self.db)
                        if p.card_id == self.card_id)
        self.assertEqual(position.quantity, 4)
        self.assertAlmostEqual(position.cost_each, 17.5)

    def test_bulk_priced_cards_never_value_below_zero(self):
        self.assertGreaterEqual(portfolio.realistic_net_each(self.config, 0.50), 0.0)
        self.assertGreater(portfolio.realistic_net_each(self.config, 0.50), 0.0)

    def test_valuable_cards_use_single_order_economics(self):
        self.assertAlmostEqual(
            portfolio.realistic_net_each(self.config, 500.0),
            self.config.fees.net_proceeds(500.0), places=6)


# --- bulk ---------------------------------------------------------------

class BulkTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=60, include_portfolio=False)
        provider = FixtureProvider(self.config)
        self.ids = provider.all_card_ids()

    def test_realizable_value_is_always_below_sticker_value(self):
        items = [(cid, "holofoil", 1) for cid in self.ids[:8]]
        valuation = bulk.value_lot(self.db, self.config, items)
        self.assertGreater(valuation.market_value, 0)
        self.assertLess(valuation.realizable_value, valuation.market_value)

    def test_max_bid_leaves_the_target_margin(self):
        items = [(cid, "holofoil", 1) for cid in self.ids[:8]]
        valuation = bulk.value_lot(self.db, self.config, items)
        margin = ((valuation.realizable_value - valuation.max_bid)
                  / valuation.max_bid)
        self.assertAlmostEqual(margin, self.config.bulk.target_lot_margin, places=3)

    def test_verdict_flips_from_buy_to_pass_as_the_ask_rises(self):
        items = [(cid, "holofoil", 1) for cid in self.ids[:8]]
        cheap = bulk.value_lot(self.db, self.config, items, ask_price=1.0)
        dear = bulk.value_lot(self.db, self.config, items, ask_price=1_000_000.0)
        self.assertEqual(cheap.verdict, "buy")
        self.assertEqual(dear.verdict, "pass")

    def test_unknown_cards_count_as_filler_not_as_nothing(self):
        valuation = bulk.value_lot(self.db, self.config, [("ghost-1", "normal", 50)])
        self.assertEqual(valuation.cards, 50)
        self.assertEqual(valuation.unpriced_cards, 50)
        self.assertGreater(valuation.realizable_value, 0)
        self.assertTrue(any("no price history" in note for note in valuation.notes))

    def test_by_count_estimate_withholds_a_verdict_on_thin_data(self):
        result = bulk.estimate_by_count(self.db, self.config, 5000, ask_price=100.0)
        self.assertEqual(result["verdict"], "insufficient_data")
        self.assertLess(result["confidence"], 1.0)

    def test_sell_plan_splits_singles_from_bulk(self):
        expensive = self.ids[0]
        cheap = next(cid for cid in self.ids
                     if (self.db.latest_price(cid, "normal", "tcgplayer") or {})
                     and (self.db.latest_price(cid, "normal", "tcgplayer")["market"] < 1.5))
        portfolio.add_holding(self.db, expensive, "holofoil", 1, 10.0)
        portfolio.add_holding(self.db, cheap, "normal", 100, 0.05)
        plan = bulk.sell_plan(self.db, self.config)
        self.assertTrue(plan["sell_individually"])
        self.assertTrue(plan["sell_as_bulk"])
        self.assertGreaterEqual(plan["bulk_card_count"], 100)

    def test_set_concentration_reports_a_share(self):
        data = bulk.set_value_concentration(self.db, self.config, "sv3pt5", top_n=3)
        self.assertGreater(data["cards_priced"], 0)
        self.assertGreater(data["top_share"], 0)
        self.assertLessEqual(data["top_share"], 1.0)

    def test_lot_persistence_round_trip(self):
        lot_id = bulk.create_lot(self.db, "Test lot", ask_price=50.0)
        bulk.add_lot_items(self.db, lot_id, [(self.ids[0], "holofoil", 2)])
        valuation = bulk.evaluate_lot(self.db, self.config, lot_id)
        self.assertEqual(valuation.cards, 2)
        stored = bulk.list_lots(self.db)[0]
        self.assertEqual(stored["item_count"], 2)
        self.assertIsNotNone(stored["valuation"])


# --- end to end ---------------------------------------------------------

class EndToEndTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.stats = ingest.seed_demo(self.db, self.config, days=150)

    def test_demo_seed_produces_history_and_positions(self):
        self.assertGreater(self.stats["price_rows"], 1000)
        self.assertGreater(self.stats["positions"], 0)

    def test_signal_run_persists_and_is_readable(self):
        run = signals.generate(self.db, self.config)
        self.assertGreater(run.scanned, 0)
        self.assertIsNotNone(run.run_id)
        stored = signals.latest_signals(self.db)
        self.assertEqual(len(stored), len(run.buys) + len(run.sells))

    def test_signals_respect_the_result_caps(self):
        self.config.buy.max_results = 1
        self.config.buy.min_score = 0
        self.config.buy.min_roi = -1
        self.config.buy.min_net_profit = -100
        run = signals.generate(self.db, self.config)
        self.assertLessEqual(len(run.buys), 1)

    def test_alerts_are_deduplicated(self):
        card_id = FixtureProvider(self.config).all_card_ids()[0]
        add_watch(self.db, card_id, "any", max_buy=1_000_000.0)
        first = store_alerts(self.db, evaluate_alerts(self.db, self.config))
        second = store_alerts(self.db, evaluate_alerts(self.db, self.config))
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_watchlist_requires_a_known_card(self):
        with self.assertRaises(ValueError):
            add_watch(self.db, "ghost-9")

    def test_digest_builds_and_renders_every_format(self):
        digest = build_digest(self.db, self.config)
        self.assertIn("unrealised", digest.headline)
        markdown = render_markdown(digest)
        self.assertIn("# pokeflip", markdown)
        html = render_html(digest)
        self.assertIn("<!doctype html>", html.lower())
        payload = json.loads(json.dumps(digest.to_dict(), default=str))
        self.assertIn("portfolio", payload)

    def test_digest_actions_are_ordered_by_priority_then_value(self):
        digest = build_digest(self.db, self.config)
        priorities = [a["priority"] for a in digest.actions]
        self.assertEqual(priorities, sorted(priorities))

    def test_refresh_records_a_run(self):
        stats = ingest.refresh_prices(self.db, self.config)
        self.assertIn("quotes", stats)
        latest = self.db.recent_runs(1)[0]
        self.assertEqual(latest["kind"], "refresh")
        self.assertIn(latest["status"], {"ok", "partial"})

    def test_tracking_universe_prioritises_holdings_and_watchlist(self):
        universe = ingest.tracking_universe(self.db, self.config)
        held = {row["card_id"] for row in
                self.db.query("SELECT card_id FROM holdings WHERE status='open'")}
        self.assertTrue(held.issubset(set(universe)))


# --- condition ----------------------------------------------------------

class ConditionTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def test_played_copies_are_worth_less_than_near_mint(self):
        self.assertEqual(portfolio.condition_price(self.config, 100.0, "NM"), 100.0)
        self.assertEqual(portfolio.condition_price(self.config, 100.0, "LP"), 85.0)
        self.assertEqual(portfolio.condition_price(self.config, 100.0, "DMG"), 35.0)

    def test_unknown_condition_falls_back_to_the_default(self):
        self.assertEqual(portfolio.condition_price(self.config, 100.0, "graded?"), 100.0)
        self.assertEqual(portfolio.condition_price(self.config, 100.0, None), 100.0)

    def test_condition_is_case_insensitive(self):
        self.assertEqual(portfolio.condition_price(self.config, 100.0, "lp"), 85.0)

    def test_net_proceeds_apply_the_condition_discount(self):
        nm = portfolio.realistic_net_each(self.config, 100.0, "NM")
        lp = portfolio.realistic_net_each(self.config, 100.0, "LP")
        self.assertGreater(nm, lp)
        self.assertAlmostEqual(lp, self.config.fees.net_proceeds(85.0), places=6)


# --- lot selection ------------------------------------------------------

class LotSelectionTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=40, include_portfolio=False)
        self.card_id = FixtureProvider(self.config).all_card_ids()[0]
        # Two lots: an old cheap one and a recent expensive one.
        portfolio.add_holding(self.db, self.card_id, "holofoil", 2, 10.0,
                              acquired_at="2025-01-01T00:00:00+00:00")
        portfolio.add_holding(self.db, self.card_id, "holofoil", 2, 40.0,
                              acquired_at="2025-06-01T00:00:00+00:00")

    def remaining_costs(self):
        return sorted(lot["cost_each"]
                      for lot in portfolio.open_lots(self.db, self.card_id))

    def test_fifo_sells_the_oldest_lot_first(self):
        result = portfolio.sell_position(self.db, self.config, self.card_id, 2, 60.0,
                                         "holofoil", method="fifo")
        self.assertAlmostEqual(result["cost_basis"], 20.0)
        self.assertEqual(self.remaining_costs(), [40.0])

    def test_lifo_sells_the_newest_lot_first(self):
        result = portfolio.sell_position(self.db, self.config, self.card_id, 2, 60.0,
                                         "holofoil", method="lifo")
        self.assertAlmostEqual(result["cost_basis"], 80.0)
        self.assertEqual(self.remaining_costs(), [10.0])

    def test_highest_cost_realises_the_smallest_gain(self):
        result = portfolio.sell_position(self.db, self.config, self.card_id, 2, 60.0,
                                         "holofoil", method="highest_cost")
        self.assertAlmostEqual(result["cost_basis"], 80.0)

    def test_a_sale_can_span_several_lots(self):
        result = portfolio.sell_position(self.db, self.config, self.card_id, 3, 60.0,
                                         "holofoil", method="fifo")
        self.assertEqual(result["lots_used"], 2)
        self.assertAlmostEqual(result["cost_basis"], 60.0)  # 2x10 + 1x40

    def test_selling_more_than_held_is_refused(self):
        with self.assertRaises(ValueError):
            portfolio.sell_position(self.db, self.config, self.card_id, 99, 60.0,
                                    "holofoil")

    def test_unknown_method_is_refused(self):
        with self.assertRaises(ValueError):
            portfolio.sell_position(self.db, self.config, self.card_id, 1, 60.0,
                                    "holofoil", method="vibes")


# --- orders and listings ------------------------------------------------

class OrderTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=90, include_portfolio=False)
        provider = FixtureProvider(self.config)
        self.card_id = provider.all_card_ids()[0]
        self.variant = provider._catalog[self.card_id]["variant"]

    def test_filling_a_buy_creates_a_holding(self):
        order_id = orders.create_order(self.db, self.config, "buy", self.card_id,
                                       2, 50.0, self.variant)
        result = orders.fill_order(self.db, self.config, order_id, 48.0)
        self.assertIn("holding_id", result)
        lots = portfolio.open_lots(self.db, self.card_id)
        self.assertEqual(sum(lot["quantity"] for lot in lots), 2)
        self.assertAlmostEqual(lots[0]["cost_each"], 48.0)
        self.assertEqual(orders.get_order(self.db, order_id).status, "filled")

    def test_filling_a_listing_books_the_realised_profit(self):
        portfolio.add_holding(self.db, self.card_id, self.variant, 1, 20.0)
        order_id = orders.create_order(self.db, self.config, "sell", self.card_id,
                                       1, 100.0, self.variant)
        result = orders.fill_order(self.db, self.config, order_id, 100.0)
        self.assertAlmostEqual(result["cost_basis"], 20.0)
        self.assertAlmostEqual(
            result["realized_pnl"],
            round(self.config.fees.net_proceeds(100.0) - 20.0, 2), places=2)
        self.assertEqual(portfolio.open_lots(self.db, self.card_id), [])

    def test_a_partial_fill_leaves_the_rest_open(self):
        order_id = orders.create_order(self.db, self.config, "buy", self.card_id,
                                       5, 50.0, self.variant)
        result = orders.fill_order(self.db, self.config, order_id, 50.0, quantity=2)
        self.assertEqual(result["remaining_quantity"], 3)
        self.assertEqual(orders.get_order(self.db, order_id).quantity, 3)
        self.assertEqual(orders.get_order(self.db, order_id).status, "open")
        self.assertEqual(sum(l["quantity"]
                             for l in portfolio.open_lots(self.db, self.card_id)), 2)

    def test_cannot_list_more_than_you_hold(self):
        portfolio.add_holding(self.db, self.card_id, self.variant, 2, 20.0)
        orders.create_order(self.db, self.config, "sell", self.card_id, 2, 100.0,
                            self.variant)
        with self.assertRaises(ValueError):
            orders.create_order(self.db, self.config, "sell", self.card_id, 1, 100.0,
                                self.variant)

    def test_cannot_fill_the_same_order_twice(self):
        order_id = orders.create_order(self.db, self.config, "buy", self.card_id,
                                       1, 50.0, self.variant)
        orders.fill_order(self.db, self.config, order_id, 50.0)
        with self.assertRaises(ValueError):
            orders.fill_order(self.db, self.config, order_id, 50.0)

    def signal_dict(self, **overrides):
        signal = {
            "kind": "buy", "action": "buy_dip", "card_id": self.card_id,
            "variant": self.variant, "condition": "NM", "price": 60.0,
            "entry_price": 48.0, "quantity": 1, "score": 82.0,
            "reasons": ["Trading 20% under its 30-day average"],
        }
        signal.update(overrides)
        return signal

    def test_a_buy_order_from_a_signal_bids_the_recommended_entry_price(self):
        order_id = orders.create_from_signal(self.db, self.config, self.signal_dict())
        order = orders.get_order(self.db, order_id)
        self.assertEqual(order.kind, "buy")
        self.assertAlmostEqual(order.limit_price, 48.0)   # the entry, not the market
        self.assertAlmostEqual(order.reference_price, 60.0)
        self.assertEqual(order.signal_action, "buy_dip")
        self.assertEqual(order.signal_score, 82.0)
        self.assertIn("30-day average", order.notes)

    def test_a_sell_order_from_a_signal_lists_at_the_market_price(self):
        portfolio.add_holding(self.db, self.card_id, self.variant, 3, 10.0)
        order_id = orders.create_from_signal(
            self.db, self.config,
            self.signal_dict(kind="sell", action="sell_target", quantity=3))
        order = orders.get_order(self.db, order_id)
        self.assertEqual(order.kind, "sell")
        self.assertEqual(order.quantity, 3)
        self.assertAlmostEqual(order.limit_price, 60.0)

    def test_a_signal_without_a_valid_kind_is_refused(self):
        with self.assertRaises(ValueError):
            orders.create_from_signal(self.db, self.config,
                                      self.signal_dict(kind="maybe"))

    def test_generated_signals_can_be_turned_into_orders_unchanged(self):
        # The contract that matters: whatever `scan` emits, `order take` accepts.
        run = signals.generate(self.db, self.config, persist=False)
        for signal in (*run.buys, *run.sells)[:3]:
            payload = signal.to_dict()
            if payload["kind"] == "sell":
                continue  # needs matching inventory, covered above
            order_id = orders.create_from_signal(self.db, self.config, payload)
            self.assertEqual(orders.get_order(self.db, order_id).signal_action,
                             payload["action"])

    def test_repricing_records_the_history(self):
        portfolio.add_holding(self.db, self.card_id, self.variant, 1, 20.0)
        order_id = orders.create_order(self.db, self.config, "sell", self.card_id,
                                       1, 100.0, self.variant)
        orders.reprice(self.db, order_id, 90.0, "test")
        order = orders.get_order(self.db, order_id)
        self.assertAlmostEqual(order.limit_price, 90.0)
        self.assertAlmostEqual(order.original_price, 100.0)
        self.assertEqual(len(order.price_cuts), 1)
        self.assertAlmostEqual(order.price_cuts[0]["from"], 100.0)

    def test_stale_buy_orders_expire(self):
        order_id = orders.create_order(self.db, self.config, "buy", self.card_id,
                                       1, 50.0, self.variant)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        self.db.execute("UPDATE orders SET created_at = ? WHERE id = ?", (old, order_id))
        self.assertEqual(orders.expire_stale(self.db, self.config), [order_id])
        self.assertEqual(orders.get_order(self.db, order_id).status, "expired")

    def test_cancel_only_works_once(self):
        order_id = orders.create_order(self.db, self.config, "buy", self.card_id,
                                       1, 50.0, self.variant)
        self.assertTrue(orders.cancel_order(self.db, order_id))
        self.assertFalse(orders.cancel_order(self.db, order_id))


class ListingReviewTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=90, include_portfolio=False)
        provider = FixtureProvider(self.config)
        self.card_id = provider.all_card_ids()[0]
        self.variant = provider._catalog[self.card_id]["variant"]
        self.market = self.db.latest_price(
            self.card_id, self.variant, "tcgplayer")["market"]
        portfolio.add_holding(self.db, self.card_id, self.variant, 4, 10.0)

    def listing(self, ask: float, age_days: int = 0) -> int:
        order_id = orders.create_order(self.db, self.config, "sell", self.card_id,
                                       1, ask, self.variant)
        if age_days:
            when = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
            self.db.execute("UPDATE orders SET created_at = ? WHERE id = ?",
                            (when, order_id))
        return order_id

    def review(self, order_id: int):
        return orders.review_listing(self.db, self.config,
                                     orders.get_order(self.db, order_id))

    def test_a_fresh_listing_at_market_is_left_alone(self):
        self.assertEqual(self.review(self.listing(self.market)).verdict, "hold")

    def test_an_underpriced_listing_should_be_raised(self):
        review = self.review(self.listing(self.market * 0.7))
        self.assertEqual(review.verdict, "raise")
        self.assertGreater(review.suggested_price, self.market * 0.7)

    def test_a_wildly_overpriced_listing_is_cut_even_when_fresh(self):
        self.assertEqual(self.review(self.listing(self.market * 1.6)).verdict, "cut")

    def test_a_stale_listing_needs_a_decision(self):
        review = self.review(self.listing(self.market * 1.15, age_days=40))
        self.assertIn(review.verdict, {"cut", "pull"})
        self.assertIsNotNone(review.suggested_price)

    def test_a_cut_is_not_immediately_re_cut(self):
        # The ratchet bug: staleness must be measured from the last price
        # change, or an auto-applied cut re-fires every cycle down to the floor.
        order_id = self.listing(self.market * 1.15, age_days=40)
        first = self.review(order_id)
        self.assertEqual(first.verdict, "cut")
        orders.reprice(self.db, order_id, first.suggested_price, "test")
        self.assertEqual(self.review(order_id).verdict, "hold")

    def test_suggested_cuts_never_go_below_the_floor(self):
        review = self.review(self.listing(self.market * 1.05, age_days=40))
        if review.suggested_price:
            floor = self.market * self.config.orders.price_floor_vs_market
            self.assertGreaterEqual(review.suggested_price, round(floor, 2) - 0.01)

    def test_applying_suggestions_only_touches_flagged_listings(self):
        self.listing(self.market)                       # hold
        self.listing(self.market * 1.15, age_days=40)   # cut
        applied = orders.apply_suggestions(self.db, self.config, ("cut",))
        self.assertEqual(len(applied), 1)

    def test_condition_is_accounted_for_in_the_comparison(self):
        # A played copy listed at the near-mint price is overpriced, not fair.
        portfolio.add_holding(self.db, self.card_id, self.variant, 1, 10.0,
                              condition="MP")
        order_id = orders.create_order(self.db, self.config, "sell", self.card_id,
                                       1, self.market, self.variant, condition="MP")
        review = self.review(order_id)
        self.assertGreater(review.ask_vs_market, 0.3)


# --- grading ------------------------------------------------------------

class GradingTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=90, include_portfolio=False)
        provider = FixtureProvider(self.config)
        # Pick something comfortably above the grading price floor.
        self.card_id = next(
            cid for cid, entry in provider._catalog.items()
            if entry["base_price"] > 100
        )
        self.variant = provider._catalog[self.card_id]["variant"]

    def test_it_prices_the_printing_that_actually_trades(self):
        verdict = grading.evaluate(self.db, self.config, self.card_id, "normal")
        self.assertEqual(verdict.variant, self.variant)
        self.assertIsNotNone(verdict.raw_price)

    def test_cheap_cards_are_not_worth_grading(self):
        self.config.grading.min_raw_price = 10_000.0
        verdict = grading.evaluate(self.db, self.config, self.card_id, self.variant)
        self.assertEqual(verdict.verdict, "sell_raw")

    def test_probabilities_sum_to_one(self):
        verdict = grading.evaluate(self.db, self.config, self.card_id, self.variant)
        self.assertAlmostEqual(sum(o.probability for o in verdict.outcomes), 1.0,
                               places=3)

    def test_expected_value_nets_off_fees_and_grading_cost(self):
        verdict = grading.evaluate(self.db, self.config, self.card_id, self.variant)
        gross_expected = sum(o.probability * o.net_proceeds for o in verdict.outcomes)
        self.assertAlmostEqual(
            verdict.expected_net, round(gross_expected - verdict.grading_cost, 2),
            places=1)
        self.assertAlmostEqual(
            verdict.expected_profit,
            round(verdict.expected_net - verdict.raw_net, 2), places=2)

    def test_recorded_comps_replace_the_guessed_multipliers(self):
        before = grading.evaluate(self.db, self.config, self.card_id, self.variant)
        self.assertEqual(before.basis, "multiplier")
        for grade in ("10", "9", "8", "7"):
            grading.record_comp(self.db, self.card_id, grade, 500.0, self.variant)
        after = grading.evaluate(self.db, self.config, self.card_id, self.variant)
        self.assertEqual(after.basis, "comp")
        self.assertTrue(all(o.source == "comp" for o in after.outcomes))
        self.assertTrue(all(o.price == 500.0 for o in after.outcomes))

    def test_the_multiplier_caveat_is_stated_when_there_are_no_comps(self):
        verdict = grading.evaluate(self.db, self.config, self.card_id, self.variant)
        self.assertTrue(any("comps" in reason for reason in verdict.reasons))

    def test_unknown_cards_return_an_unknown_verdict_not_an_error(self):
        verdict = grading.evaluate(self.db, self.config, "ghost-1")
        self.assertEqual(verdict.verdict, "unknown")

    def test_comp_for_an_unknown_card_is_refused(self):
        with self.assertRaises(ValueError):
            grading.record_comp(self.db, "ghost-1", "10", 100.0)

    def test_portfolio_scan_skips_played_copies(self):
        portfolio.add_holding(self.db, self.config and self.card_id, self.variant,
                              1, 50.0, condition="LP")
        scan = grading.scan_portfolio(self.db, self.config)
        self.assertEqual(scan["candidates"], [])
        self.assertGreaterEqual(scan["skipped"], 1)


# --- capital and tax ----------------------------------------------------

class CapitalTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=60, include_portfolio=False)
        self.ids = FixtureProvider(self.config).all_card_ids()

    def test_an_oversized_position_is_flagged(self):
        portfolio.add_holding(self.db, self.ids[0], "holofoil", 1, 900.0)
        portfolio.add_holding(self.db, self.ids[1], "holofoil", 1, 100.0)
        data = portfolio.concentration(self.db, self.config)
        kinds = {w["kind"] for w in data["warnings"]}
        self.assertIn("position", kinds)
        self.assertAlmostEqual(data["largest_position_share"], 0.9, places=2)

    def test_a_balanced_book_raises_nothing(self):
        for card_id in self.ids[:12]:
            portfolio.add_holding(self.db, card_id, "holofoil", 1, 100.0)
        data = portfolio.concentration(self.db, self.config)
        self.assertEqual(
            [w for w in data["warnings"] if w["kind"] == "position"], [])

    def test_open_buy_orders_count_against_the_bankroll(self):
        self.config.capital.bankroll = 1000.0
        portfolio.add_holding(self.db, self.ids[0], "holofoil", 1, 400.0)
        orders.create_order(self.db, self.config, "buy", self.ids[1], 2, 100.0)
        data = portfolio.concentration(self.db, self.config)
        self.assertAlmostEqual(data["open_buy_commitments"], 200.0)
        self.assertAlmostEqual(data["free_capital"], 400.0)
        self.assertIn("committed", {w["kind"] for w in data["warnings"]})

    def test_no_bankroll_means_no_free_capital_figure(self):
        self.assertIsNone(portfolio.concentration(self.db, self.config)["free_capital"])


class TaxReportTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=60, include_portfolio=False)
        self.card_id = FixtureProvider(self.config).all_card_ids()[0]

    def test_holding_period_is_classified(self):
        old = portfolio.add_holding(self.db, self.card_id, "holofoil", 1, 10.0,
                                    acquired_at="2023-01-01T00:00:00+00:00")
        recent = portfolio.add_holding(self.db, self.card_id, "holofoil", 1, 10.0,
                                       acquired_at="2026-01-01T00:00:00+00:00")
        portfolio.sell_holding(self.db, old, 1, 100.0, self.config,
                               sold_at="2026-06-01T00:00:00+00:00")
        portfolio.sell_holding(self.db, recent, 1, 100.0, self.config,
                               sold_at="2026-06-01T00:00:00+00:00")
        terms = {line["term"] for line in portfolio.tax_report(self.db)["lines"]}
        self.assertEqual(terms, {"long", "short"})

    def test_totals_reconcile_with_the_lines(self):
        holding_id = portfolio.add_holding(self.db, self.card_id, "holofoil", 2, 10.0)
        portfolio.sell_holding(self.db, holding_id, 2, 100.0, self.config)
        report = portfolio.tax_report(self.db)
        line = report["lines"][0]
        self.assertAlmostEqual(line["gain"],
                               line["net_proceeds"] - line["cost_basis"], places=2)
        self.assertAlmostEqual(line["fees"],
                               line["gross_proceeds"] - line["net_proceeds"], places=2)
        self.assertAlmostEqual(report["totals"]["gain"], line["gain"], places=2)

    def test_a_year_filter_excludes_other_years(self):
        holding_id = portfolio.add_holding(self.db, self.card_id, "holofoil", 1, 10.0)
        portfolio.sell_holding(self.db, holding_id, 1, 100.0, self.config,
                               sold_at="2024-05-05T00:00:00+00:00")
        self.assertEqual(len(portfolio.tax_report(self.db, 2024)["lines"]), 1)
        self.assertEqual(len(portfolio.tax_report(self.db, 2025)["lines"]), 0)


# --- backtest -----------------------------------------------------------

class BacktestTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=200, include_portfolio=False)

    def test_a_replay_produces_graded_outcomes(self):
        report = backtest.run(self.db, self.config, lookback_days=120,
                              horizons=[30], persist=False)
        self.assertGreater(report.days_replayed, 0)
        self.assertGreater(len(report.outcomes), 0)
        self.assertTrue(all(o.horizon_days == 30 for o in report.outcomes))

    def test_signals_are_never_scored_using_future_prices(self):
        report = backtest.run(self.db, self.config, lookback_days=90,
                              horizons=[7], persist=False)
        source = self.config.provider.preferred_source
        for outcome in report.outcomes[:20]:
            on = date.fromisoformat(outcome.signal_date)
            metrics = load_metrics(self.db, outcome.card_id, outcome.variant,
                                   source, as_of=on)
            # Metrics as of the signal date must match the price it was scored on.
            self.assertAlmostEqual(metrics.price, outcome.signal_price, places=2)

    def test_the_forward_window_never_starts_before_the_signal(self):
        report = backtest.run(self.db, self.config, lookback_days=90,
                              horizons=[30], persist=False)
        for outcome in report.outcomes:
            if outcome.exit_price is None:
                continue
            self.assertLessEqual(
                date.fromisoformat(outcome.signal_date) + timedelta(days=1),
                date.fromisoformat(report.end) + timedelta(days=outcome.horizon_days))

    def test_repeated_signals_on_one_card_are_counted_once_per_window(self):
        self.config.backtest.dedupe_days = 30
        sparse = backtest.run(self.db, self.config, lookback_days=120,
                              horizons=[7], persist=False)
        self.config.backtest.dedupe_days = 1
        dense = backtest.run(self.db, self.config, lookback_days=120,
                             horizons=[7], persist=False)
        self.assertLess(sparse.signals, dense.signals)

    def test_verdicts_need_a_minimum_sample(self):
        outcomes = [
            backtest.Outcome(kind="buy", action="buy_dip", card_id="c", variant="v",
                             card_name="c", signal_date="2026-01-01", score=80,
                             signal_price=10, entry_price=9, horizon_days=30,
                             roi=0.5, forward_return=0.5, outcome="win")
            for _ in range(3)
        ]
        self.assertEqual(backtest.summarise(outcomes)[0].verdict, "insufficient_data")

    def test_a_losing_rule_is_called_unreliable(self):
        outcomes = [
            backtest.Outcome(kind="buy", action="buy_dip", card_id="c", variant="v",
                             card_name="c", signal_date="2026-01-01", score=80,
                             signal_price=10, entry_price=9, horizon_days=30,
                             roi=-0.2, forward_return=-0.2, outcome="loss")
            for _ in range(12)
        ]
        stats = backtest.summarise(outcomes)[0]
        self.assertEqual(stats.verdict, "unreliable")
        self.assertEqual(stats.win_rate, 0.0)

    def test_a_winning_rule_is_called_reliable(self):
        outcomes = [
            backtest.Outcome(kind="buy", action="buy_dip", card_id="c", variant="v",
                             card_name="c", signal_date="2026-01-01", score=80,
                             signal_price=10, entry_price=9, horizon_days=30,
                             roi=0.3, forward_return=0.3, outcome="win")
            for _ in range(12)
        ]
        self.assertEqual(backtest.summarise(outcomes)[0].verdict, "reliable")

    def test_flat_outcomes_do_not_count_as_wins(self):
        self.assertEqual(backtest._label(0.001), "flat")
        self.assertEqual(backtest._label(0.5), "win")
        self.assertEqual(backtest._label(-0.5), "loss")
        self.assertEqual(backtest._label(None), "unresolved")

    def test_results_persist_and_can_be_read_back(self):
        report = backtest.run(self.db, self.config, lookback_days=90, horizons=[30])
        stored = backtest.latest(self.db)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["run_id"], report.run_id)
        self.assertEqual(len(stored["stats"]), len(report.stats))

    def test_an_empty_database_reports_rather_than_crashes(self):
        empty = Database(str(Path(self._tmp.name) / "empty.db"))
        report = backtest.run(empty, self.config, persist=False)
        self.assertEqual(report.signals, 0)
        self.assertTrue(report.notes)


# --- ebay ---------------------------------------------------------------

class EbayListingFilterTests(unittest.TestCase):
    """The filters decide what a price series is made of, so they get the
    scrutiny. A wrongly kept listing poisons the number; a wrongly rejected one
    silently starves it."""

    def reason(self, title, **kwargs):
        return ebay.classify(title, **kwargs).reason

    def assertKept(self, title, variant=None, **kwargs):
        verdict = ebay.classify(title, **kwargs)
        self.assertTrue(verdict.kept,
                        f"wrongly rejected ({verdict.reason}): {title}")
        if variant:
            self.assertEqual(verdict.variant, variant, title)

    def assertDropped(self, title, reason_contains=None, **kwargs):
        verdict = ebay.classify(title, **kwargs)
        self.assertFalse(verdict.kept, f"wrongly kept as {verdict.variant}: {title}")
        if reason_contains:
            self.assertIn(reason_contains, verdict.reason, title)

    # --- multiples ---

    def test_lots_in_all_their_forms_are_excluded(self):
        for title in ("Pokemon Card Lot 50 Cards Charizard",
                      "Job Lot Pokemon Cards Charizard",
                      "Charizard ex Bundle of cards",
                      "Charizard ex x4 Playset",
                      "Bulk Pokemon Cards Charizard",
                      "Charizard ex 199/165 Binder Collection",
                      "Pokemon 151 Complete Master Set",
                      "Pick Your Card Pokemon 151 Singles"):
            self.assertDropped(title, "multiple")

    def test_quantities_above_one_are_excluded(self):
        for title in ("4x Charizard ex 199/165", "Charizard ex x10",
                      "Charizard ex 199/165 (5) cards", "50 cards Charizard"):
            self.assertDropped(title, "quantity")

    def test_a_single_copy_written_as_a_quantity_survives(self):
        # "1x Charizard" is one card and must not be read as a lot.
        self.assertKept("1x Charizard ex 199/165 NM")
        self.assertKept("Charizard ex 199/165 - 1 card only")

    def test_pokemon_names_containing_filter_words_survive(self):
        # The whole reason every pattern is word-boundary anchored.
        self.assertKept("Lotad 43/165 Common 151 NM")
        self.assertKept("Slowking 199/165 Holo")
        self.assertKept("Slowbro 43/132 Holo")

    # --- not a raw single ---

    def test_online_code_cards_never_reach_the_price_series(self):
        for title in ("Charizard ex PTCGO Code Card",
                      "Pokemon Online Code Cards",
                      "Charizard ex PTCGL redeemable code"):
            self.assertDropped(title, "code card")

    def test_sealed_product_is_a_different_market(self):
        for title in ("Pokemon 151 Booster Box Sealed",
                      "Pokemon 151 Elite Trainer Box ETB",
                      "Pokemon 151 Booster Pack",
                      "Charizard Premium Collection Tin"):
            self.assertDropped(title, "sealed")

    def test_pack_fresh_describes_a_raw_single_not_a_pack(self):
        self.assertKept("Charizard ex 199/165 - Pack Fresh NM")
        self.assertKept("Charizard ex straight from the pack")

    def test_proxies_and_customs_are_excluded(self):
        for title in ("Charizard ex Custom Orica Proxy",
                      "Charizard fan made art card",
                      "Charizard ex counterfeit replica"):
            self.assertDropped(title, "proxy")

    def test_foreign_printings_are_a_different_asset(self):
        self.assertDropped("Charizard Japanese 151 SIR", "English")
        self.assertDropped("Charizard Korean 151", "English")
        self.assertDropped("Charizard German Glurak", "English")

    def test_an_explicit_english_marker_wins_over_a_stray_language_word(self):
        self.assertKept("Charizard ex 199/165 English NM Japanese seller")

    def test_shipping_from_a_country_is_not_a_foreign_printing(self):
        # An English single posted from Japan is the card you wanted. The
        # origin word can sit on either side of the country name.
        for title in ("Charizard ex 199/165 - Free Shipping from Japan",
                      "Charizard ex 199/165 - Japan seller, fast post",
                      "Charizard ex 199/165 Japan Post tracked",
                      "Charizard ex 199/165 imported from Japan"):
            self.assertKept(title)

    def test_a_foreign_card_posted_from_abroad_is_still_foreign(self):
        self.assertDropped("Pokemon Japanese Charizard - ships from Japan",
                           "English")

    def test_foreign_filtering_can_be_switched_off(self):
        self.assertKept("Charizard Japanese 151 SIR", english_only=False)

    def test_damaged_copies_do_not_set_a_near_mint_baseline(self):
        for title in ("Charizard 4/102 Heavily Played Creased",
                      "Charizard 4/102 water damage",
                      "Charizard 4/102 as is for parts"):
            self.assertDropped(title, "near-mint")

    def test_damage_filtering_can_be_switched_off(self):
        self.assertKept("Charizard 4/102 Heavily Played", exclude_damaged=False)

    def test_misprints_are_a_separate_market(self):
        self.assertDropped("Charizard ex Misprint Error Card", "misprint")

    # --- graded ---

    def test_slabs_are_excluded_from_the_raw_price(self):
        for title in ("Charizard ex PSA 10 GEM MINT", "Charizard BGS 9.5 Beckett",
                      "Charizard ex CGC 9 Slabbed", "Charizard ex ACE 10 Graded",
                      "Charizard ex SGC 8", "Charizard encapsulated graded"):
            self.assertDropped(title, "graded")

    def test_liquid_grades_get_their_own_variant_when_tracked(self):
        self.assertEqual(ebay.classify("Charizard PSA 10", track_graded=True).variant,
                         "psa10")
        self.assertEqual(ebay.classify("Charizard PSA 9 mint", track_graded=True).variant,
                         "psa9")
        self.assertEqual(ebay.classify("Charizard BGS 9.5", track_graded=True).variant,
                         "bgs95")
        self.assertEqual(ebay.classify("Charizard SGC 8", track_graded=True).variant,
                         "graded_other")

    def test_grade_marketing_on_a_raw_card_is_not_a_slab(self):
        # "PSA 10 READY" is one of the commonest raw-card phrases there is.
        # Treating it as graded throws away real listings.
        for title in ("Charizard ex PSA 10 READY Gem Mint Candidate",
                      "Charizard ex PSA 10 worthy",
                      "Charizard ex would grade PSA 9",
                      "Charizard ex raw ungraded PSA 10 potential"):
            self.assertKept(title)

    def test_a_slab_that_ships_fast_is_still_a_slab(self):
        # "ready to ship" must not be mistaken for "PSA 10 ready".
        self.assertDropped("Charizard ex PSA 10 - ready to ship today", "graded")

    def test_explicitly_ungraded_beats_a_grade_mention(self):
        self.assertKept("Charizard ex 199/165 Ungraded Raw NM")

    # --- printings ---

    def test_reverse_holos_are_their_own_series(self):
        self.assertKept("Pikachu 025/165 Reverse Holo 151 NM", "reverseHolofoil")
        self.assertKept("Bulbasaur 001/165 rev holo", "reverseHolofoil")

    def test_first_edition_and_shadowless_are_separate_assets(self):
        # A 1st edition Base Charizard trades at many multiples of unlimited.
        self.assertKept("Base Set Charizard 4/102 1st Edition Holo",
                        "1stEditionHolofoil")
        self.assertKept("Charizard 4/102 Shadowless Holo", "shadowlessHolofoil")
        self.assertKept("Machamp 8/102 1st Edition Base Set", "1stEditionHolofoil",
                        default_printing="holofoil")

    def test_a_foil_rarity_is_foil_even_when_the_title_never_says_holo(self):
        # Otherwise these land in a phantom "normal" series that never lines up
        # with the same card's real one.
        for title in ("Snorlax 143/165 Illustration Rare 151",
                      "Iono 269/193 Special Illustration Rare",
                      "Umbreon VMAX 215/203 Alt Art",
                      "Mew ex 205/165 Gold Secret Rare",
                      "Charizard ex 199/165 Trading Card"):
            self.assertKept(title, "holofoil", default_printing="holofoil")

    def test_an_explicit_non_holo_beats_the_catalog_default(self):
        self.assertKept("Pikachu 025/165 151 Non-Holo", "normal",
                        default_printing="holofoil")

    def test_plain_cards_stay_normal(self):
        self.assertKept("Bulbasaur 001/165 Common 151 NM", "normal")
        self.assertKept("Lickitung 38/102 Base Set NM", "normal")

    def test_rarity_maps_to_the_expected_default_printing(self):
        for rarity in ("Common", "Uncommon", "Rare"):
            self.assertEqual(ebay.variant_for_rarity(rarity), "normal")
        for rarity in ("Rare Holo", "Illustration Rare", "Ultra Rare",
                       "Special Illustration Rare", "Secret Rare", "Double Rare"):
            self.assertEqual(ebay.variant_for_rarity(rarity), "holofoil")
        self.assertEqual(ebay.variant_for_rarity(None), "normal")

    # --- configuration ---

    def test_extra_exclusions_match_whole_words_only(self):
        # A configured "lot" must not take out every Lotad listing.
        self.assertDropped("Charizard ex signed by artist",
                           "signed", extra_exclusions=["signed"])
        self.assertKept("Lotad 43/165 Common", extra_exclusions=["lot"])

    def test_an_empty_title_is_rejected_rather_than_guessed_at(self):
        self.assertDropped("   ", "empty")


class EbayParsingTests(unittest.TestCase):

    def test_the_query_names_the_card_and_the_game(self):
        card = CardRecord(id="sv3pt5-199", name="Charizard ex", number="199/165",
                          set_name="151")
        query = ebay.build_query(card)
        self.assertIn("Charizard ex", query)
        self.assertIn("199", query)
        self.assertIn("151", query)
        self.assertIn("pokemon", query)

    def test_sold_prices_beat_asking_prices_when_both_exist(self):
        provider = ebay.EbayProvider(Config())
        listings = [{"title": "Charizard holo", "price": {"value": "120.00"}},
                    {"title": "Charizard holo", "price": {"value": "150.00"}}]
        sold = [{"title": "Charizard holo", "lastSoldPrice": {"value": "100.00"},
                 "lastSoldDate": datetime.now(timezone.utc).isoformat()}]
        quote = provider._build_quote("x-1", "holofoil", listings, sold)
        self.assertAlmostEqual(quote.market, 100.00)
        self.assertAlmostEqual(quote.low, 120.00)
        self.assertEqual(quote.extra["price_basis"], "sold_median")
        self.assertEqual(quote.sales_count, 1)

    def test_listings_only_uses_the_low_quartile_and_admits_it(self):
        provider = ebay.EbayProvider(Config())
        listings = [{"title": "Charizard holo", "price": {"value": str(p)}}
                    for p in (100, 120, 140, 200)]
        quote = provider._build_quote("x-1", "holofoil", listings, [])
        self.assertLess(quote.market, 140)
        self.assertIsNone(quote.sales_count)   # unknown, not zero
        self.assertEqual(quote.extra["price_basis"], "listing_p25")

    def test_old_sales_do_not_count_toward_velocity(self):
        recent = datetime.now(timezone.utc).isoformat()
        stale = (datetime.now(timezone.utc) - timedelta(days=80)).isoformat()
        sold = [{"lastSoldDate": recent}, {"lastSoldDate": stale}]
        self.assertEqual(len(ebay._recent_sales(sold, 30)), 1)

    def test_it_refuses_catalog_questions_with_a_useful_message(self):
        provider = ebay.EbayProvider(Config())
        with self.assertRaises(ProviderError) as caught:
            provider.search_cards("charizard")
        self.assertIn("catalog", str(caught.exception))

    def test_credentials_are_required_before_any_call(self):
        provider = ebay.EbayProvider(Config())
        with self.assertRaises(ProviderError) as caught:
            provider._access_token()
        self.assertIn("ebay_client_id", str(caught.exception))

    def test_the_catalog_provider_falls_back_when_prices_come_from_ebay(self):
        config = Config()
        config.provider.name = "ebay"
        self.assertFalse(providers.provides_catalog(config))
        self.assertIsInstance(providers.build_catalog_provider(config),
                              providers.PokemonTcgProvider)


class LiquidityTests(unittest.TestCase):
    def test_reported_sales_give_an_observed_liquidity_score(self):
        rows = make_rows([50.0] * 30)
        for row in rows:
            row["sales_count"] = 12      # trailing 30-day total
        metrics = compute_metrics("c", "v", "ebay", rows, today=date(2025, 1, 30))
        self.assertEqual(metrics.liquidity_basis, "observed")
        self.assertAlmostEqual(metrics.sales_per_week, 2.8, places=1)

    def test_rolling_totals_are_not_summed_across_days(self):
        # Thirty snapshots each reporting the same 30-day total is 12 sales,
        # not 360.
        rows = make_rows([50.0] * 30)
        for row in rows:
            row["sales_count"] = 12
        metrics = compute_metrics("c", "v", "ebay", rows, today=date(2025, 1, 30))
        self.assertLess(metrics.sales_per_week, 5)

    def test_without_sales_data_liquidity_is_only_inferred(self):
        metrics = compute_metrics("c", "v", "tcgplayer", make_rows([50.0] * 30),
                                  today=date(2025, 1, 30))
        self.assertEqual(metrics.liquidity_basis, "inferred")
        self.assertIsNone(metrics.sales_per_week)

    def test_no_history_means_no_liquidity_claim(self):
        metrics = compute_metrics("c", "v", "tcgplayer", [])
        self.assertEqual(metrics.liquidity_basis, "none")
        self.assertIsNone(metrics.liquidity)


class AsOfTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=90, include_portfolio=False)
        self.card_id = FixtureProvider(self.config).all_card_ids()[0]
        self.variant = FixtureProvider(self.config)._catalog[self.card_id]["variant"]

    def test_as_of_hides_everything_after_that_day(self):
        cutoff = date.today() - timedelta(days=30)
        rows = self.db.price_series(self.card_id, self.variant, "tcgplayer",
                                    days=365, as_of=cutoff.isoformat())
        self.assertTrue(rows)
        self.assertLessEqual(
            max(date.fromisoformat(r["captured_on"]) for r in rows), cutoff)

    def test_metrics_as_of_a_past_day_differ_from_today(self):
        past = load_metrics(self.db, self.card_id, self.variant, "tcgplayer",
                            as_of=date.today() - timedelta(days=30))
        now = load_metrics(self.db, self.card_id, self.variant, "tcgplayer")
        self.assertNotEqual(past.last_date, now.last_date)
        self.assertEqual(past.stale_days, 0)


# --- push delivery ------------------------------------------------------

class PushFilterTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.config.notify.push_min_severity = "warn"
        self.config.notify.quiet_hours_start = 22
        self.config.notify.quiet_hours_end = 7

    def alert(self, severity="warn"):
        return {"severity": severity, "title": "t", "body": "b"}

    def test_quiet_hours_wrap_past_midnight(self):
        for hour in (22, 23, 0, 3, 6):
            self.assertTrue(notify.in_quiet_hours(self.config, hour), hour)
        for hour in (7, 12, 21):
            self.assertFalse(notify.in_quiet_hours(self.config, hour), hour)

    def test_a_daytime_quiet_window_does_not_wrap(self):
        self.config.notify.quiet_hours_start = 9
        self.config.notify.quiet_hours_end = 17
        self.assertTrue(notify.in_quiet_hours(self.config, 12))
        self.assertFalse(notify.in_quiet_hours(self.config, 20))

    def test_equal_bounds_disable_quiet_hours(self):
        self.config.notify.quiet_hours_start = 0
        self.config.notify.quiet_hours_end = 0
        self.assertFalse(notify.in_quiet_hours(self.config, 3))

    def test_low_severity_alerts_do_not_reach_a_phone(self):
        self.assertFalse(notify.should_push(self.config, self.alert("info"), hour=12))
        self.assertTrue(notify.should_push(self.config, self.alert("warn"), hour=12))

    def test_only_urgent_alerts_break_quiet_hours(self):
        self.assertFalse(notify.should_push(self.config, self.alert("warn"), hour=3))
        self.assertTrue(notify.should_push(self.config, self.alert("urgent"), hour=3))

    def test_quiet_hours_can_mute_even_urgent_alerts(self):
        self.config.notify.quiet_hours_allow_urgent = False
        self.assertFalse(notify.should_push(self.config, self.alert("urgent"), hour=3))

    def test_telegram_markdown_is_escaped(self):
        escaped = notify._escape_md("Charizard ex (151) - $4.50!")
        for char in "()-.!":
            self.assertIn(f"\\{char}", escaped)


class PushPayloadTests(TempDbCase):
    """Payload shape, verified without touching the network."""

    def setUp(self):
        super().setUp()
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.config.notify.channels = ["ntfy"]
        self.config.notify.ntfy_topic = "test-topic"
        self.config.server.public_base_url = "https://pokeflip.example"

    def capture(self, url, json=None, data=None, headers=None, timeout=None):
        self.sent.append((url, json if json is not None else data))

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "result": {}}

        return _Response()

    def deliver(self, alerts, db=None):
        original = notify.httpx.post
        notify.httpx.post = self.capture
        try:
            return notify.deliver_alerts(self.config, alerts, db=db)
        finally:
            notify.httpx.post = original

    def alert(self, **overrides):
        base = {"id": 1, "kind": "strong_buy", "severity": "urgent",
                "title": "Strong buy: Test", "body": "cheap",
                "card_id": "x-1", "variant": "normal", "dedupe_key": "k",
                "payload": {"card_id": "x-1", "entry_price": 10.0, "price": 20.0}}
        base.update(overrides)
        return base

    def test_each_alert_becomes_its_own_push(self):
        self.deliver([self.alert(id=1), self.alert(id=2, dedupe_key="k2")])
        self.assertEqual(len(self.sent), 2)

    def test_severity_maps_to_priority_and_a_tag(self):
        self.deliver([self.alert(severity="urgent")])
        _, payload = self.sent[0]
        self.assertEqual(payload["priority"], 5)
        self.assertEqual(payload["tags"], ["rotating_light"])
        self.assertEqual(payload["topic"], "test-topic")

    def test_suppressed_alerts_are_reported_not_silently_dropped(self):
        self.config.notify.push_min_severity = "urgent"
        results = self.deliver([self.alert(severity="info")])
        self.assertEqual(self.sent, [])
        self.assertEqual(results[0]["sent"], 0)
        self.assertIn("threshold", results[0]["note"])

    def test_buttons_appear_when_the_server_is_reachable(self):
        self.db.upsert_cards([{"id": "x-1", "name": "Test", "set_id": None,
                               "set_name": "S", "number": "1", "rarity": "",
                               "supertype": "", "subtypes": "", "artist": "",
                               "image_small": "", "image_large": "",
                               "tcgplayer_url": "", "cardmarket_url": ""}])
        self.deliver([self.alert()], db=self.db)
        _, payload = self.sent[0]
        labels = [a["label"] for a in payload["actions"]]
        self.assertIn("Bid placed", labels)
        self.assertLessEqual(len(payload["actions"]), notify.MAX_ACTIONS)
        self.assertTrue(payload["actions"][0]["url"].startswith(
            "https://pokeflip.example/api/act/"))

    def test_no_buttons_without_a_public_url(self):
        self.config.server.public_base_url = ""
        self.deliver([self.alert()], db=self.db)
        self.assertNotIn("actions", self.sent[0][1])

    def test_a_missing_topic_is_reported_as_a_failure(self):
        self.config.notify.ntfy_topic = ""
        with quiet("pokeflip.notify"):
            results = self.deliver([self.alert()])
        self.assertFalse(results[0]["ok"])
        self.assertIn("ntfy_topic", results[0]["error"])


# --- one-tap actions ----------------------------------------------------

class ActionTokenTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=60, include_portfolio=False)
        provider = FixtureProvider(self.config)
        self.card_id = provider.all_card_ids()[0]
        self.variant = provider._catalog[self.card_id]["variant"]
        self.config.server.public_base_url = "https://pokeflip.example"

    def test_no_token_is_minted_without_a_public_url(self):
        self.config.server.public_base_url = ""
        self.assertIsNone(actions.mint(self.db, self.config, "ack", "Dismiss", {}))

    def test_tokens_are_long_and_unguessable(self):
        first = actions.mint(self.db, self.config, "ack", "Dismiss", {})
        second = actions.mint(self.db, self.config, "ack", "Dismiss", {})
        self.assertGreaterEqual(len(first.token), 32)
        self.assertNotEqual(first.token, second.token)

    def test_an_unknown_action_kind_is_refused(self):
        with self.assertRaises(actions.ActionError):
            actions.mint(self.db, self.config, "rm_rf", "Oops", {})

    def test_a_buy_link_records_an_order_once(self):
        link = actions.mint(self.db, self.config, "place_buy", "Bid placed",
                            {"card_id": self.card_id, "variant": self.variant,
                             "price": 12.5, "quantity": 1})
        first = actions.execute(self.db, self.config, link.token)
        self.assertEqual(first["status"], "ok")
        order_id = first["result"]["order_id"]
        self.assertAlmostEqual(
            orders.get_order(self.db, order_id).limit_price, 12.5)

        # A phone prefetch or a double tap must not place a second bid.
        second = actions.execute(self.db, self.config, link.token)
        self.assertEqual(second["status"], "already_done")
        self.assertEqual(second["result"]["order_id"], order_id)
        self.assertEqual(len(orders.list_orders(self.db, kind="buy")), 1)

    def test_an_unknown_token_is_refused(self):
        with self.assertRaises(actions.ActionError):
            actions.execute(self.db, self.config, "not-a-token")

    def test_an_expired_token_is_refused(self):
        link = actions.mint(self.db, self.config, "ack", "Dismiss", {})
        self.db.execute("UPDATE action_tokens SET expires_at = ? WHERE token = ?",
                        ("2020-01-01T00:00:00+00:00", link.token))
        with self.assertRaises(actions.ActionError):
            actions.execute(self.db, self.config, link.token)

    def test_a_listing_link_reprices_the_order(self):
        portfolio.add_holding(self.db, self.card_id, self.variant, 1, 10.0)
        order_id = orders.create_order(self.db, self.config, "sell", self.card_id,
                                       1, 100.0, self.variant)
        link = actions.mint(self.db, self.config, "apply_reprice", "Re-price",
                            {"order_id": order_id, "price": 85.0})
        actions.execute(self.db, self.config, link.token)
        self.assertAlmostEqual(orders.get_order(self.db, order_id).limit_price, 85.0)

    def test_snoozing_stops_the_alert_recurring(self):
        alert = alerts_mod.Alert(kind="price_spike", title="t", dedupe_key="spike:x")
        self.assertEqual(len(alerts_mod.store_alerts(self.db, [alert])), 1)

        link = actions.mint(self.db, self.config, "snooze", "Not interested",
                            {"dedupe_key": "spike:x", "days": 30})
        actions.execute(self.db, self.config, link.token)

        # Dedupe alone would have let it back after DEDUPE_HOURS; a snooze
        # must not.
        self.db.execute("DELETE FROM alerts")
        self.assertEqual(alerts_mod.store_alerts(self.db, [alert]), [])
        self.assertTrue(alerts_mod.unsnooze(self.db, "spike:x"))
        self.assertEqual(len(alerts_mod.store_alerts(self.db, [alert])), 1)

    def test_expired_tokens_are_pruned(self):
        link = actions.mint(self.db, self.config, "ack", "Dismiss", {})
        self.db.execute("UPDATE action_tokens SET expires_at = ? WHERE token = ?",
                        ("2020-01-01T00:00:00+00:00", link.token))
        self.assertGreaterEqual(actions.prune(self.db), 1)
        self.assertIsNone(
            self.db.one("SELECT 1 FROM action_tokens WHERE token = ?", (link.token,)))

    def test_alert_links_stay_within_the_button_limit(self):
        alert = {"id": 5, "kind": "strong_buy", "card_id": self.card_id,
                 "variant": self.variant, "dedupe_key": "k",
                 "payload": {"card_id": self.card_id, "entry_price": 10.0}}
        links = actions.links_for_alert(self.db, self.config, alert)
        self.assertTrue(links)
        self.assertLessEqual(len(links), notify.MAX_ACTIONS)

    def test_no_links_when_actionable_is_off(self):
        self.config.notify.actionable = False
        self.assertEqual(
            actions.links_for_alert(self.db, self.config,
                                    {"id": 1, "kind": "strong_buy"}), [])


# --- HTTP surface -------------------------------------------------------

class ApiAuthTests(TempDbCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from pokeflip.api import create_app

        ingest.seed_demo(self.db, self.config, days=30, include_portfolio=False)
        self.config.server.api_token = "s3cret"
        self.config.server.public_base_url = "https://pokeflip.example"
        self.client = TestClient(create_app(self.config, start_scheduler=False))

    def test_health_is_reachable_without_a_token(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_data_endpoints_require_the_token(self):
        self.assertEqual(self.client.get("/api/portfolio").status_code, 401)

    def test_a_bearer_header_is_accepted(self):
        response = self.client.get("/api/portfolio",
                                   headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(response.status_code, 200)

    def test_a_query_token_is_accepted(self):
        self.assertEqual(
            self.client.get("/api/portfolio?token=s3cret").status_code, 200)

    def test_a_wrong_token_is_rejected(self):
        self.assertEqual(
            self.client.get("/api/portfolio?token=nope").status_code, 401)

    def test_action_links_bypass_the_header_check(self):
        # A phone following a notification button cannot set headers, so the
        # single-use token in the URL has to be sufficient on its own.
        link = actions.mint(self.db, self.config, "ack", "Dismiss", {"alert_id": None})
        response = self.client.post(f"/api/act/{link.token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_following_a_link_in_a_browser_returns_a_page(self):
        link = actions.mint(self.db, self.config, "ack", "Dismiss", {"alert_id": None})
        response = self.client.get(f"/api/act/{link.token}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_a_dead_link_explains_itself_in_the_browser(self):
        response = self.client.get("/api/act/nope")
        self.assertEqual(response.status_code, 410)
        self.assertIn("not valid", response.text)

    def test_get_actions_can_be_disabled(self):
        self.config.server.allow_get_actions = False
        link = actions.mint(self.db, self.config, "ack", "Dismiss", {"alert_id": None})
        self.assertEqual(self.client.get(f"/api/act/{link.token}").status_code, 405)


# --- telegram bot -------------------------------------------------------

class BotTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=90)
        self.config.notify.telegram_bot_token = "token"
        self.config.notify.telegram_chat_id = "4242"
        self.bot = bot.TelegramBot(self.db, self.config)
        self.sent: list[dict[str, Any]] = []
        self.bot._api = lambda method, payload, timeout=30.0: self._api(method, payload)
        self.updates: list[dict[str, Any]] = []

    def _api(self, method: str, payload: dict[str, Any]):
        if method == "sendMessage":
            self.sent.append(payload)
            return {}
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return batch
        return {}

    def update(self, text: str, chat_id: str = "4242", update_id: int = 1):
        return {"update_id": update_id,
                "message": {"chat": {"id": chat_id}, "text": text}}

    def test_help_lists_the_commands(self):
        reply = self.bot.handle("/help", "4242")
        for command in ("/scan", "/portfolio", "/listings"):
            self.assertIn(command, reply)

    def test_an_unknown_command_still_helps(self):
        self.assertIn("Unknown command", self.bot.handle("/frobnicate", "4242"))

    def test_scan_reports_signals(self):
        reply = self.bot.handle("/scan", "4242")
        self.assertTrue(reply)
        self.assertEqual(len(self.sent), 1)

    def test_portfolio_reports_money(self):
        self.assertIn("Cost basis", self.bot.handle("/portfolio", "4242"))

    def test_a_failing_command_replies_instead_of_crashing(self):
        self.bot.commands["boom"] = lambda args: 1 / 0
        with quiet("pokeflip.bot"):
            self.assertIn("failed", self.bot.handle("/boom", "4242"))

    def test_messages_from_other_chats_are_ignored(self):
        self.updates = [self.update("/portfolio", chat_id="9999")]
        with quiet("pokeflip.bot"):
            self.assertEqual(self.bot.poll_once(), 0)
        self.assertEqual(self.sent, [])

    def test_the_offset_advances_past_ignored_messages(self):
        # Otherwise a stranger's message would be replayed on every poll.
        self.updates = [self.update("/portfolio", chat_id="9999", update_id=7)]
        with quiet("pokeflip.bot"):
            self.bot.poll_once()
        self.assertEqual(self.db.get_setting(bot.OFFSET_KEY), 8)

    def test_authorised_messages_are_handled_and_acknowledged(self):
        self.updates = [self.update("/help", update_id=11)]
        self.assertEqual(self.bot.poll_once(), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.db.get_setting(bot.OFFSET_KEY), 12)

    def test_command_suffixes_from_group_chats_are_stripped(self):
        self.assertIn("/scan", self.bot.handle("/help@pokeflip_bot", "4242"))

    def test_it_reports_when_it_is_not_configured(self):
        self.config.notify.telegram_bot_token = ""
        self.assertFalse(bot.TelegramBot(self.db, self.config).configured)


# --- setup and doctor ---------------------------------------------------

class DoctorTests(TempDbCase):
    def statuses(self, **kwargs):
        checks = pfsetup.diagnose(self.db, self.config, check_network=False, **kwargs)
        return {c.name: c.status for c in checks}

    def test_an_empty_install_flags_that_nothing_is_tracked(self):
        result = self.statuses()
        self.assertEqual(result["tracking"], pfsetup.FAIL)
        self.assertEqual(result["price freshness"], pfsetup.WARN)

    def test_a_seeded_install_passes_tracking(self):
        ingest.seed_demo(self.db, self.config, days=30)
        self.assertEqual(self.statuses()["tracking"], pfsetup.PASS)

    def test_zero_fees_are_a_failure_not_a_warning(self):
        # Without fees every flip looks profitable, which is worse than useless.
        self.config.fees.commission_pct = 0.0
        self.config.fees.payment_pct = 0.0
        self.assertEqual(self.statuses()["fees"], pfsetup.FAIL)

    def test_missing_shipping_cost_is_flagged(self):
        self.config.fees.shipping_cost = 0.0
        self.assertEqual(self.statuses()["fees"], pfsetup.WARN)

    def test_publishing_without_a_token_is_a_failure(self):
        self.config.server.public_base_url = "https://pokeflip.example"
        self.config.server.api_token = ""
        self.assertEqual(self.statuses()["api security"], pfsetup.FAIL)

    def test_publishing_over_plain_http_is_flagged(self):
        self.config.server.public_base_url = "http://pokeflip.example"
        self.config.server.api_token = "t"
        self.assertEqual(self.statuses()["api security"], pfsetup.WARN)

    def test_a_secured_published_server_passes(self):
        self.config.server.public_base_url = "https://pokeflip.example"
        self.config.server.api_token = "t"
        self.assertEqual(self.statuses()["api security"], pfsetup.PASS)

    def test_a_channel_missing_its_credentials_is_a_failure(self):
        self.config.notify.channels = ["ntfy"]
        self.config.notify.ntfy_topic = ""
        self.assertEqual(self.statuses()["delivery"], pfsetup.FAIL)

    def test_a_fully_configured_channel_passes(self):
        self.config.notify.channels = ["ntfy"]
        self.config.notify.ntfy_topic = "topic"
        result = self.statuses()
        self.assertEqual(result["delivery"], pfsetup.PASS)
        self.assertEqual(result["phone push"], pfsetup.PASS)

    def test_an_unknown_timezone_is_caught(self):
        self.config.timezone = "Mars/Olympus"
        self.assertEqual(self.statuses()["scheduler"], pfsetup.FAIL)

    def test_every_problem_carries_a_fix(self):
        self.config.notify.channels = ["telegram"]
        self.config.fees.commission_pct = 0.0
        self.config.fees.payment_pct = 0.0
        for check in pfsetup.diagnose(self.db, self.config, check_network=False):
            if check.status in (pfsetup.WARN, pfsetup.FAIL):
                self.assertTrue(check.fix, f"{check.name} has no suggested fix")

    def test_an_unreachable_provider_is_reported_not_raised(self):
        self.config.provider.name = "pokemontcg"
        self.config.provider.base_url = "http://127.0.0.1:1/v2"
        self.config.provider.max_retries = 1
        self.config.provider.timeout_seconds = 0.5
        checks = {c.name: c for c in pfsetup.diagnose(self.db, self.config)}
        self.assertEqual(checks["catalog source"].status, pfsetup.FAIL)
        self.assertIn("unreachable", checks["catalog source"].detail)


class SetupWizardTests(unittest.TestCase):
    def run_wizard(self, answers: dict[str, str], confirms: dict[str, bool]):
        def ask(prompt: str, default: str) -> str:
            for key, value in answers.items():
                if key in prompt:
                    return value
            return default

        def confirm(prompt: str, default: bool) -> bool:
            for key, value in confirms.items():
                if key in prompt:
                    return value
            return default

        return pfsetup.wizard(Config(), ask, confirm)

    def test_the_marketplace_sets_the_fee_model(self):
        config = self.run_wizard({"Where do you sell": "ebay"}, {})
        self.assertEqual(config.fees.name, "ebay")
        self.assertAlmostEqual(config.fees.commission_pct, 0.1325)

    def test_an_unrecognised_marketplace_falls_back_safely(self):
        config = self.run_wizard({"Where do you sell": "carboot"}, {})
        self.assertGreater(config.fees.commission_pct, 0)

    def test_an_api_token_is_generated_not_left_blank(self):
        config = self.run_wizard(
            {"Public URL": "https://pokeflip.example"},
            {"public URL": True},
        )
        self.assertEqual(config.server.public_base_url, "https://pokeflip.example")
        self.assertGreaterEqual(len(config.server.api_token), 32)

    def test_no_token_is_generated_when_not_publishing(self):
        config = self.run_wizard({}, {"public URL": False, "home wifi": False})
        self.assertEqual(config.server.api_token, "")
        self.assertEqual(config.host, "127.0.0.1")

    def test_phone_access_binds_outward_and_always_sets_a_token(self):
        """Leaving localhost without a token is never an option offered."""
        config = self.run_wizard({}, {"home wifi": True})
        self.assertEqual(config.host, "0.0.0.0")
        self.assertGreaterEqual(len(config.server.api_token), 32)

    def test_a_random_ntfy_topic_is_suggested(self):
        config = self.run_wizard({}, {"ntfy": True})
        self.assertTrue(config.notify.ntfy_topic.startswith("pokeflip-"))
        self.assertIn("ntfy", config.notify.channels)

    def test_declining_push_leaves_only_file_delivery(self):
        config = self.run_wizard({}, {"ntfy": False, "reach this server": False})
        self.assertEqual(config.notify.channels, ["file"])

    def test_tracked_sets_are_split_and_trimmed(self):
        config = self.run_wizard({"Sets to track": " sv3pt5 , swsh7 ,"}, {})
        self.assertEqual(config.tracked_sets, ["sv3pt5", "swsh7"])

    def test_the_result_survives_a_round_trip_to_disk(self):
        config = self.run_wizard({"Where do you sell": "ebay",
                                  "Working capital": "1500"}, {})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config.save(path)
            reloaded = Config.load(path)
        self.assertAlmostEqual(reloaded.capital.bankroll, 1500.0)
        self.assertAlmostEqual(reloaded.fees.commission_pct, 0.1325)


# --- ebay diagnostics ---------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ebay.httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None)


class EbayCredentialTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.config.provider.name = "ebay"
        self.config.provider.ebay_client_id = "id"
        self.config.provider.ebay_client_secret = "secret"
        self.config.provider.max_retries = 1
        self.provider = ebay.EbayProvider(self.config)

    def patch_post(self, response):
        self.provider._client.post = lambda *a, **k: response

    def test_missing_credentials_are_named_precisely(self):
        self.config.provider.ebay_client_id = ""
        report = ebay.EbayProvider(self.config).check_credentials()
        self.assertFalse(report["credentials"])
        self.assertIn("ebay_client_id", report["problems"][0])

    def test_a_rejected_keyset_points_at_the_keyset(self):
        self.patch_post(FakeResponse(400, {"error_description": "invalid_client"}))
        report = self.provider.check_credentials()
        self.assertFalse(report["credentials"])
        problem = report["problems"][0]
        self.assertIn("invalid_client", problem)
        self.assertIn("Production keyset", problem)

    def test_a_network_failure_is_not_blamed_on_the_keyset(self):
        # A proxy 403 must not send you hunting for a bad secret.
        def boom(*args, **kwargs):
            raise ebay.httpx.ConnectError("403 Forbidden")

        self.provider._client.post = boom
        report = self.provider.check_credentials()
        problem = report["problems"][0]
        self.assertIn("network problem", problem)
        self.assertNotIn("Production keyset", problem)

    def test_a_403_on_the_token_endpoint_is_called_out_as_unusual(self):
        self.patch_post(FakeResponse(403, {}))
        report = self.provider.check_credentials()
        self.assertIn("proxy or firewall", report["problems"][0])

    def test_working_browse_without_sold_access_is_a_partial_pass(self):
        self.patch_post(FakeResponse(200, {"access_token": "t", "expires_in": 7200}))
        calls = {"n": 0}

        def fake_get(path, params):
            calls["n"] += 1
            if "marketplace_insights" in path:
                raise ebay.ProviderError("eBay refused /item_sales/search (403)")
            return {"itemSummaries": [{"title": "Charizard holo",
                                       "price": {"value": "10"}}]}

        self.provider._get = fake_get
        with quiet("pokeflip.ebay"):
            report = self.provider.check_credentials()
        self.assertTrue(report["credentials"])
        self.assertTrue(report["browse"])
        self.assertFalse(report["sold_data"])
        self.assertIn("Marketplace Insights", report["problems"][0])

    def test_full_access_reports_clean(self):
        self.patch_post(FakeResponse(200, {"access_token": "t", "expires_in": 7200}))
        self.provider._get = lambda path, params: (
            {"itemSales": [{"title": "Charizard holo",
                            "lastSoldPrice": {"value": "10"},
                            "lastSoldDate": datetime.now(timezone.utc).isoformat()}]}
            if "marketplace_insights" in path
            else {"itemSummaries": [{"title": "Charizard holo",
                                     "price": {"value": "12"}}]}
        )
        report = self.provider.check_credentials()
        self.assertTrue(report["sold_data"])
        self.assertEqual(report["problems"], [])

    def test_probe_shows_what_was_kept_and_what_was_filtered(self):
        self.patch_post(FakeResponse(200, {"access_token": "t", "expires_in": 7200}))
        self.provider._get = lambda path, params: (
            {} if "marketplace_insights" in path else
            {"itemSummaries": [
                {"title": "Charizard ex 199 holo", "price": {"value": "400"}},
                {"title": "Pokemon LOT OF 50 cards", "price": {"value": "20"}},
                {"title": "Charizard ex PSA 10", "price": {"value": "1200"}},
            ]}
        )
        card = CardRecord(id="sv3pt5-199", name="Charizard ex", number="199",
                          set_name="151")
        result = self.provider.probe(card)
        self.assertIn("Charizard ex", result["query"])
        self.assertEqual(result["listings_found"], 3)
        self.assertEqual(result["listings"]["rejected_count"], 2)  # lot + slab
        self.assertEqual(set(result["listings"]["rejected"]),
                         {"multiple cards", "graded (psa10)"})
        self.assertIn("holofoil", result["listings"]["kept"])

    def test_error_detail_prefers_the_body_over_the_status_line(self):
        self.assertIn("invalid_client",
                      ebay._error_detail(FakeResponse(400,
                                                      {"error": "invalid_client"})))
        self.assertIn("bad scope", ebay._error_detail(
            FakeResponse(401, {"errors": [{"message": "bad scope"}]})))
        self.assertEqual(ebay._error_detail(FakeResponse(500)), "HTTP 500")


class DoctorProviderTests(TempDbCase):
    """The doctor must check the source it actually prices from."""

    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=30, include_portfolio=False)

    def test_a_catalog_only_setup_checks_one_source(self):
        self.config.provider.name = "fixture"
        names = [c.name for c in pfsetup.diagnose(self.db, self.config)]
        self.assertIn("catalog source", names)
        self.assertNotIn("price source", names)

    def test_ebay_is_checked_separately_from_its_catalog(self):
        # The bug this covers: build_catalog_provider returns pokemontcg for an
        # eBay setup, so checking only that would pass a broken price source.
        self.config.provider.name = "ebay"
        self.config.provider.catalog_name = "fixture"
        self.config.provider.ebay_client_id = ""
        checks = {c.name: c for c in pfsetup.diagnose(self.db, self.config)}
        self.assertIn("price source", checks)
        self.assertEqual(checks["price source"].status, pfsetup.FAIL)
        self.assertIn("ebay_client_id", checks["price source"].detail)

    def test_an_empty_catalog_is_fatal_for_a_name_searching_source(self):
        empty = Database(str(Path(self._tmp.name) / "bare.db"))
        self.config.provider.name = "ebay"
        self.config.provider.catalog_name = "fixture"
        checks = {c.name: c for c in pfsetup.diagnose(empty, self.config)}
        self.assertEqual(checks["catalog contents"].status, pfsetup.FAIL)

    def test_a_populated_catalog_passes(self):
        self.config.provider.name = "ebay"
        self.config.provider.catalog_name = "fixture"
        checks = {c.name: c for c in pfsetup.diagnose(self.db, self.config)}
        self.assertEqual(checks["catalog contents"].status, pfsetup.PASS)


# --- desktop app --------------------------------------------------------

class PathResolutionTests(unittest.TestCase):
    """Where files live. Getting this wrong means the app window and the
    terminal end up on two different databases."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in
                       ("POKEFLIP_CONFIG", "XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        os.environ.pop("POKEFLIP_CONFIG", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "cfg")
        os.environ["XDG_DATA_HOME"] = str(self.root / "dat")

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def test_an_explicit_path_wins(self):
        path, source = paths.resolve_config_path("/tmp/somewhere.json")
        self.assertEqual(path, Path("/tmp/somewhere.json"))
        self.assertEqual(source, "explicit")

    def test_the_environment_beats_the_default(self):
        os.environ["POKEFLIP_CONFIG"] = "/tmp/from-env.json"
        path, source = paths.resolve_config_path()
        self.assertEqual(path, Path("/tmp/from-env.json"))
        self.assertEqual(source, "environment")

    def test_a_project_local_config_is_preferred(self):
        project = self.root / "project"
        project.mkdir()
        (project / "config.json").write_text("{}")
        path, source = paths.resolve_config_path(cwd=project)
        self.assertEqual(path, project / "config.json")
        self.assertEqual(source, "working directory")

    def test_without_one_it_falls_back_to_the_user_directory(self):
        empty = self.root / "empty"
        empty.mkdir()
        path, source = paths.resolve_config_path(cwd=empty)
        self.assertEqual(source, "user directory")
        self.assertTrue(str(path).startswith(str(self.root / "cfg")))

    def test_a_user_config_anchors_data_outside_the_working_directory(self):
        # An app launched from an icon has no meaningful cwd to write beside.
        path, source = paths.resolve_config_path(cwd=self.root / "nope")
        database = paths.default_database_for(path, source)
        self.assertTrue(Path(database).is_absolute())
        self.assertIn("dat", database)

    def test_a_project_config_keeps_its_data_beside_it(self):
        database = paths.default_database_for(Path("config.json"),
                                              "working directory")
        self.assertFalse(Path(database).is_absolute())

    def test_loading_anchors_relative_paths_for_a_user_config(self):
        from pokeflip.cli import load_config

        config = load_config()
        self.assertTrue(Path(config.database).is_absolute())
        self.assertTrue(Path(config.notify.report_dir).is_absolute())

    def test_loading_leaves_a_project_config_relative(self):
        from pokeflip.cli import load_config

        project = self.root / "proj"
        project.mkdir()
        (project / "config.json").write_text('{"database": "data/pokeflip.db"}')
        previous = Path.cwd()
        os.chdir(project)
        try:
            config = load_config()
        finally:
            os.chdir(previous)
        self.assertEqual(config.database, "data/pokeflip.db")


class DesktopServerTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=20, include_portfolio=False)

    def test_a_free_port_is_actually_free(self):
        port = desktop.free_port()
        self.assertGreater(port, 1024)
        self.assertNotEqual(port, desktop.free_port())

    def test_the_server_starts_serves_and_stops(self):
        import httpx as _httpx

        with desktop.running_server(self.config) as server:
            health = _httpx.get(f"{server.url}/api/health", timeout=5).json()
            self.assertEqual(health["status"], "ok")
            page = _httpx.get(f"{server.url}/", timeout=5)
            self.assertEqual(page.status_code, 200)
            self.assertIn("pokeflip", page.text)
        # Once stopped, the port stops answering.
        with self.assertRaises(_httpx.HTTPError):
            _httpx.get(f"{server.url}/api/health", timeout=2)

    def test_it_binds_only_to_loopback(self):
        server = desktop.ServerThread(self.config)
        self.assertEqual(server.host, "127.0.0.1")

    def test_the_window_url_carries_the_api_token(self):
        # The window is a browser and needs the same credential as anything
        # else, or a secured install would open to a 401.
        server = desktop.ServerThread(self.config)
        self.config.server.api_token = ""
        self.assertNotIn("token=", desktop._window_url(server, self.config))
        self.config.server.api_token = "abc"
        self.assertIn("token=abc", desktop._window_url(server, self.config))

    def test_waiting_gives_up_rather_than_hanging(self):
        self.assertFalse(
            desktop.wait_for_server("http://127.0.0.1:1", timeout=0.5))


class DesktopNotifyTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[dict[str, Any]] = []

        def fake_run(command, input=None, text=None, timeout=None, check=None,
                     capture_output=None):
            self.calls.append({"command": list(command), "stdin": input})

            class _Done:
                returncode = 0
            return _Done()

        self._saved_run = desknotify.subprocess.run
        desknotify.subprocess.run = fake_run
        self._saved_platform = desknotify.sys.platform
        self._saved_which = desknotify.shutil.which

    def tearDown(self):
        desknotify.subprocess.run = self._saved_run
        desknotify.sys.platform = self._saved_platform
        desknotify.shutil.which = self._saved_which

    def pretend(self, platform: str, tools: Sequence[str] = ()):
        desknotify.sys.platform = platform
        desknotify.shutil.which = lambda name: name if name in tools else None

    def test_linux_uses_notify_send_with_urgency(self):
        self.pretend("linux", ["notify-send"])
        desknotify.notify("Strong buy", "cheap", "urgent")
        command = self.calls[0]["command"]
        self.assertEqual(command[0], "notify-send")
        self.assertIn("--urgency=critical", command)
        # Urgent alerts should stay on screen until dismissed.
        self.assertIn("--expire-time=0", command)
        self.assertIn("Strong buy", command)

    def test_linux_lower_severities_time_out(self):
        self.pretend("linux", ["notify-send"])
        desknotify.notify("Heads up", "body", "info")
        self.assertIn("--expire-time=12000", self.calls[0]["command"])

    def test_macos_uses_osascript(self):
        self.pretend("darwin", ["osascript"])
        desknotify.notify("Sell now", "target hit", "urgent")
        command = self.calls[0]["command"]
        self.assertEqual(command[0], "osascript")
        self.assertIn("display notification", command[2])
        self.assertIn("sound name", command[2])   # urgent gets a sound

    def test_windows_drives_powershell_over_stdin(self):
        self.pretend("win32", ["powershell"])
        desknotify.notify("Sell now", "target hit", "warn")
        call = self.calls[0]
        self.assertEqual(call["command"][0], "powershell")
        self.assertIn("NotifyIcon", call["stdin"])
        self.assertIn("Warning", call["stdin"])

    def test_a_missing_backend_is_reported_not_swallowed(self):
        self.pretend("linux", [])
        self.assertFalse(desknotify.available())
        with self.assertRaises(desknotify.NotifierUnavailable):
            desknotify.notify("x")

    def test_quotes_in_a_card_name_cannot_break_out_of_applescript(self):
        self.pretend("darwin", ["osascript"])
        desknotify.notify('Charizard "ex"', 'it\'s up 20%', "info")
        script = self.calls[0]["command"][2]
        self.assertIn('\\"ex\\"', script)

    def test_quotes_cannot_break_out_of_powershell(self):
        self.pretend("win32", ["powershell"])
        desknotify.notify("It's here", "don't panic", "info")
        stdin = self.calls[0]["stdin"]
        self.assertIn("It''s here", stdin)

    def test_newlines_are_flattened_so_toasts_stay_readable(self):
        self.pretend("darwin", ["osascript"])
        desknotify.notify("Title", "line one\nline two", "info")
        self.assertNotIn("\n", self.calls[0]["command"][2].split("with title")[0])

    def test_an_action_link_rides_along_in_the_body(self):
        self.pretend("linux", ["notify-send"])
        desknotify.notify("Buy", "cheap", "warn", link="https://x/api/act/tok")
        self.assertIn("https://x/api/act/tok", self.calls[0]["command"][-1])

    def test_long_bodies_are_truncated(self):
        self.pretend("linux", ["notify-send"])
        desknotify.notify("T", "x" * 5000, "info")
        self.assertLessEqual(len(self.calls[0]["command"][-1]),
                             desknotify.MAX_BODY + 1)


class DesktopChannelTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.config.notify.channels = ["desktop"]
        self.sent: list[dict[str, Any]] = []

    def deliver(self, alerts):
        from pokeflip import desknotify as dn

        original = dn.notify
        dn.notify = lambda **kw: self.sent.append(kw)
        try:
            return notify.deliver_alerts(self.config, alerts, db=self.db)
        finally:
            dn.notify = original

    def test_desktop_is_treated_as_a_push_channel(self):
        self.assertIn("desktop", notify.PUSH_CHANNELS)
        self.assertNotIn("desktop", notify.PHONE_CHANNELS)

    def test_alerts_reach_the_desktop_notifier(self):
        results = self.deliver([
            {"severity": "urgent", "title": "Strong buy", "body": "cheap",
             "kind": "strong_buy", "dedupe_key": "k"},
        ])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["title"], "Strong buy")
        self.assertTrue(results[0]["ok"])

    def test_the_severity_floor_applies_to_the_desktop_too(self):
        self.config.notify.push_min_severity = "urgent"
        self.deliver([{"severity": "info", "title": "meh", "dedupe_key": "k"}])
        self.assertEqual(self.sent, [])


# --- dashboard session ---------------------------------------------------

class DashboardAuthTests(TempDbCase):
    """With a token set, the dashboard has to be usable in a browser. It was
    not: the page loaded from ?token= but its own fetch calls carried nothing,
    so every one of them 401'd."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from pokeflip.api import create_app

        ingest.seed_demo(self.db, self.config, days=20, include_portfolio=False)
        self.config.server.api_token = "s3cret"
        self.client = TestClient(create_app(self.config, start_scheduler=False),
                                 follow_redirects=False)

    def test_the_token_hand_off_sets_a_cookie_and_redirects(self):
        response = self.client.get("/?token=s3cret")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertIn("pokeflip_session", response.cookies)

    def test_after_the_hand_off_the_pages_api_calls_succeed(self):
        self.client.get("/?token=s3cret")           # cookie stored on the client
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/portfolio").status_code, 200)
        self.assertEqual(self.client.get("/api/digest").status_code, 200)

    def test_the_cookie_is_http_only_so_scripts_cannot_read_it(self):
        response = self.client.get("/?token=s3cret")
        self.assertIn("HttpOnly", response.headers["set-cookie"])

    def test_a_wrong_token_gets_no_cookie(self):
        response = self.client.get("/?token=nope")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("pokeflip_session", response.cookies)

    def test_a_forged_cookie_is_rejected(self):
        self.client.cookies.set("pokeflip_session", "forged")
        self.assertEqual(self.client.get("/api/portfolio").status_code, 401)

    def test_logging_out_clears_the_session(self):
        self.client.get("/?token=s3cret")
        self.assertEqual(self.client.get("/api/portfolio").status_code, 200)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/portfolio").status_code, 401)

    def test_the_exempt_prefix_cannot_be_walked_into_a_protected_route(self):
        for path in ("/api/act/../portfolio", "/api/health/../portfolio"):
            self.assertNotEqual(self.client.get(path).status_code, 200, path)


class PortfolioHistoryTests(TempDbCase):
    def setUp(self):
        super().setUp()
        ingest.seed_demo(self.db, self.config, days=90, include_portfolio=False)
        provider = FixtureProvider(self.config)
        self.card_id = provider.all_card_ids()[0]
        self.variant = provider._catalog[self.card_id]["variant"]

    def test_an_empty_book_returns_no_days_rather_than_crashing(self):
        self.assertEqual(portfolio.value_history(self.db, self.config)["days"], [])

    def test_value_starts_when_the_card_was_acquired_not_when_tracking_began(self):
        bought = date.today() - timedelta(days=30)
        portfolio.add_holding(self.db, self.card_id, self.variant, 1, 50.0,
                              acquired_at=f"{bought.isoformat()}T12:00:00+00:00")
        days = portfolio.value_history(self.db, self.config, days=60)["days"]
        before = [d for d in days if d["on"] < bought.isoformat()]
        after = [d for d in days if d["on"] >= bought.isoformat()]
        self.assertTrue(all(d["cost_basis"] == 0 for d in before))
        self.assertTrue(all(d["cost_basis"] == 50.0 for d in after))
        self.assertTrue(all(d["cards"] == 1 for d in after))

    def test_a_sold_lot_leaves_the_curve_on_the_day_it_sold(self):
        bought = date.today() - timedelta(days=40)
        holding = portfolio.add_holding(
            self.db, self.card_id, self.variant, 1, 50.0,
            acquired_at=f"{bought.isoformat()}T12:00:00+00:00")
        sold_on = date.today() - timedelta(days=10)
        portfolio.sell_holding(self.db, holding, 1, 90.0, self.config,
                               sold_at=f"{sold_on.isoformat()}T12:00:00+00:00")
        days = {d["on"]: d for d in
                portfolio.value_history(self.db, self.config, days=60)["days"]}
        self.assertEqual(days[(sold_on - timedelta(days=1)).isoformat()]["cards"], 1)
        self.assertEqual(days[sold_on.isoformat()]["cards"], 0)

    def test_unrealised_is_net_value_less_cost_on_every_day(self):
        portfolio.add_holding(self.db, self.card_id, self.variant, 2, 40.0,
                              acquired_at="2020-01-01T00:00:00+00:00")
        for day in portfolio.value_history(self.db, self.config, days=30)["days"]:
            self.assertAlmostEqual(day["unrealized"],
                                   round(day["net_value"] - day["cost_basis"], 2),
                                   places=2)

    def test_condition_is_applied_to_historical_value_too(self):
        portfolio.add_holding(self.db, self.card_id, self.variant, 1, 10.0,
                              condition="MP",
                              acquired_at="2020-01-01T00:00:00+00:00")
        played = portfolio.value_history(self.db, self.config, days=10)["days"][-1]
        self.db.execute("UPDATE holdings SET condition = 'NM'")
        mint = portfolio.value_history(self.db, self.config, days=10)["days"][-1]
        self.assertLess(played["market_value"], mint["market_value"])

    def test_the_endpoint_serves_it(self):
        from fastapi.testclient import TestClient

        from pokeflip.api import create_app

        portfolio.add_holding(self.db, self.card_id, self.variant, 1, 10.0)
        client = TestClient(create_app(self.config, start_scheduler=False))
        payload = client.get("/api/portfolio/history?days=30").json()
        self.assertEqual(len(payload["days"]), 31)


class SparkSeriesTests(unittest.TestCase):
    def metrics_with(self, count):
        from pokeflip.analytics import compute_metrics

        return compute_metrics("c", "v", "s", make_rows([float(i + 1)
                                                         for i in range(count)]),
                               include_history=True, today=date(2030, 1, 1))

    def test_a_short_series_is_passed_through_whole(self):
        from pokeflip.analytics import spark_series

        self.assertEqual(len(spark_series(self.metrics_with(12))), 12)

    def test_a_long_series_is_downsampled_to_a_fixed_length(self):
        from pokeflip.analytics import spark_series

        self.assertEqual(len(spark_series(self.metrics_with(400))), 30)

    def test_no_history_yields_no_spark(self):
        from pokeflip.analytics import compute_metrics, spark_series

        self.assertEqual(spark_series(compute_metrics("c", "v", "s", [])), [])


class TrendConfidenceTests(unittest.TestCase):
    def test_a_trend_is_not_claimed_from_one_observation(self):
        # The moving averages equal the single point by construction, so
        # momentum computes to exactly zero and the card would be reported as
        # confidently "flat" on no evidence at all.
        from pokeflip.analytics import compute_metrics

        for count in (1, 2):
            metrics = compute_metrics("c", "v", "s", make_rows([10.0] * count),
                                      today=date(2025, 1, 2))
            self.assertEqual(metrics.direction, "unknown", f"{count} points")

    def test_enough_observations_do_produce_a_label(self):
        from pokeflip.analytics import compute_metrics

        metrics = compute_metrics("c", "v", "s",
                                  make_rows([float(10 + i) for i in range(20)]),
                                  today=date(2025, 1, 20))
        self.assertEqual(metrics.direction, "rising")


# --- pairing a phone -----------------------------------------------------

class PairingCodeTests(TempDbCase):
    """A code short enough to type is only safe if it is short-lived and
    single-use. These are the properties that make that true."""

    def test_a_minted_code_redeems_once(self):
        from pokeflip import pairing

        code = pairing.mint(self.db)
        self.assertTrue(pairing.redeem(self.db, code.pretty))
        self.assertFalse(pairing.redeem(self.db, code.pretty))

    def test_the_typed_form_is_forgiving_about_case_and_dashes(self):
        from pokeflip import pairing

        code = pairing.mint(self.db)
        typed = f" {code.pretty.lower().replace('-', ' ')} "
        self.assertTrue(pairing.redeem(self.db, typed))

    def test_an_expired_code_is_refused(self):
        from pokeflip import pairing
        from pokeflip.db import iso, utcnow

        code = pairing.mint(self.db)
        self.db.execute("UPDATE pair_codes SET expires_at = ? WHERE code = ?",
                        (iso(utcnow() - timedelta(minutes=1)), code.code))
        self.assertFalse(pairing.redeem(self.db, code.pretty))

    def test_an_unknown_code_is_refused(self):
        from pokeflip import pairing

        self.assertFalse(pairing.redeem(self.db, "ABCD-EFGH"))
        self.assertFalse(pairing.redeem(self.db, ""))
        self.assertFalse(pairing.redeem(self.db, "short"))

    def test_the_alphabet_avoids_characters_that_get_misread(self):
        from pokeflip import pairing

        for banned in "ILOU01":
            self.assertNotIn(banned, pairing.CODE_ALPHABET)

    def test_codes_do_not_repeat(self):
        from pokeflip import pairing

        seen = {pairing.mint(self.db).code for _ in range(50)}
        self.assertEqual(len(seen), 50)

    def test_expired_codes_are_eventually_forgotten(self):
        from pokeflip import pairing
        from pokeflip.db import iso, utcnow

        code = pairing.mint(self.db)
        self.db.execute("UPDATE pair_codes SET expires_at = ? WHERE code = ?",
                        (iso(utcnow() - timedelta(days=3)), code.code))
        pairing.purge(self.db)
        self.assertIsNone(
            self.db.one("SELECT code FROM pair_codes WHERE code = ?", (code.code,)))

    def test_active_codes_exclude_spent_ones(self):
        from pokeflip import pairing

        first = pairing.mint(self.db)
        pairing.mint(self.db)
        pairing.redeem(self.db, first.pretty)
        self.assertEqual(len(pairing.active_codes(self.db)), 1)


class PairingAddressTests(unittest.TestCase):
    def test_loopback_hosts_are_recognised(self):
        from pokeflip import pairing

        for host in ("127.0.0.1", "localhost", "127.0.0.5"):
            config = Config(host=host)
            self.assertTrue(pairing.is_loopback_host(config), host)
        self.assertFalse(pairing.is_loopback_host(Config(host="192.168.1.9")))
        self.assertFalse(pairing.is_loopback_host(Config(host="0.0.0.0")))

    def test_a_loopback_bind_offers_no_address_for_a_phone(self):
        from pokeflip import pairing

        self.assertEqual(pairing.base_urls(Config(host="127.0.0.1")), [])

    def test_a_published_url_wins_over_a_discovered_one(self):
        from pokeflip import pairing

        config = Config(host="0.0.0.0")
        config.server.public_base_url = "https://pokeflip.example/"
        self.assertEqual(pairing.base_urls(config)[0], "https://pokeflip.example")

    def test_an_explicit_lan_bind_is_used_as_given(self):
        from pokeflip import pairing

        config = Config(host="192.168.1.9", port=9000)
        self.assertEqual(pairing.base_urls(config), ["http://192.168.1.9:9000"])

    def test_discovered_addresses_are_never_loopback_or_link_local(self):
        from pokeflip import pairing

        for address in pairing.lan_addresses():
            self.assertFalse(address.startswith("127."), address)
            self.assertFalse(address.startswith("169.254."), address)


class PairingRouteTests(TempDbCase):
    """The phone hand-off, end to end: a code in a URL becomes a session."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from pokeflip.api import create_app

        ingest.seed_demo(self.db, self.config, days=20, include_portfolio=False)
        self.config.server.api_token = "s3cret"
        self.client = TestClient(create_app(self.config, start_scheduler=False),
                                 follow_redirects=False)

    def _code(self) -> str:
        from pokeflip import pairing

        return pairing.mint(self.db).pretty

    def test_a_pairing_link_grants_a_session(self):
        response = self.client.get(f"/p/{self._code()}")
        self.assertEqual(response.status_code, 303)
        self.assertIn("pokeflip_session", response.cookies)
        self.assertEqual(self.client.get("/api/portfolio").status_code, 200)

    def test_the_granted_cookie_is_http_only(self):
        response = self.client.get(f"/p/{self._code()}")
        self.assertIn("HttpOnly", response.headers["set-cookie"])

    def test_a_link_cannot_be_replayed(self):
        code = self._code()
        self.client.get(f"/p/{code}")
        self.client.cookies.clear()
        second = self.client.get(f"/p/{code}")
        self.assertEqual(second.status_code, 401)
        self.assertNotIn("pokeflip_session", second.cookies)

    def test_a_wrong_code_grants_nothing(self):
        response = self.client.get("/p/ZZZZ-ZZZZ")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/api/portfolio").status_code, 401)

    def test_guessing_is_throttled(self):
        from pokeflip import pairing

        for _ in range(pairing.MAX_ATTEMPTS):
            self.client.get("/p/ZZZZ-ZZZZ")
        # A valid code presented after the budget is spent still fails: the
        # throttle is on the client, not on the code.
        blocked = self.client.get(f"/p/{self._code()}")
        self.assertEqual(blocked.status_code, 429)

    def test_the_form_accepts_a_typed_code(self):
        response = self.client.post(
            "/pair", content=f"code={self._code()}",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("pokeflip_session", response.cookies)

    def test_the_pairing_page_is_reachable_without_a_session(self):
        response = self.client.get("/pair")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Pair this device", response.text)

    def test_the_pairing_page_does_not_leak_the_token(self):
        self.assertNotIn("s3cret", self.client.get("/pair").text)

    def test_minting_a_code_needs_an_existing_session(self):
        self.assertEqual(self.client.post("/api/pair").status_code, 401)

    def test_a_paired_session_can_mint_the_next_code(self):
        self.client.get(f"/p/{self._code()}")
        payload = self.client.post("/api/pair").json()
        self.assertIn("-", payload["code"])
        self.assertTrue(payload["needs_token"])

    def test_the_manifest_is_served_for_add_to_home_screen(self):
        response = self.client.get("/static/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["start_url"], "/")

    def test_the_home_screen_icon_is_served(self):
        response = self.client.get("/static/icon-180.png")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\x89PNG"))

    def test_the_pairing_prefix_cannot_be_walked_into_a_protected_route(self):
        for path in ("/p/../api/portfolio", "/pair/../api/portfolio"):
            self.assertEqual(self.client.get(path).status_code, 401, path)


class StandalonePageTests(unittest.TestCase):
    """The pairing and action pages carry their own CSS, so nothing else
    catches a heading that is near-black on a near-black background."""

    def test_every_heading_colour_is_redefined_for_dark_mode(self):
        from pokeflip.api import _action_page, _pair_page

        for page in (_pair_page(), _pair_page("bad code"),
                     _action_page("Done", "Recorded", True),
                     _action_page("Nope", "Expired", False)):
            dark = page.split("prefers-color-scheme: dark")[1]
            for token in ("--tone-ink", "--tone-good", "--tone-bad"):
                self.assertIn(token, dark)
            self.assertIn("var(--tone-", page)

    def test_the_pages_link_an_icon_so_favicon_ico_is_never_requested(self):
        from pokeflip.api import _pair_page

        self.assertIn('rel="icon"', _pair_page())

    def test_the_error_text_is_escaped(self):
        from pokeflip.api import _pair_page

        self.assertNotIn("<script>", _pair_page("<script>alert(1)</script>"))


class PhoneDoctorTests(TempDbCase):
    def test_localhost_with_phone_delivery_is_flagged(self):
        self.config.host = "127.0.0.1"
        self.config.notify.channels = ["file", "ntfy"]
        check = pfsetup._check_phone(self.config)
        self.assertEqual(check.status, "warn")
        self.assertIn("0.0.0.0", check.fix)

    def test_localhost_alone_is_not_a_problem(self):
        self.config.host = "127.0.0.1"
        self.config.notify.channels = ["file"]
        self.assertEqual(pfsetup._check_phone(self.config).status, "pass")

    def test_binding_outward_without_a_token_fails_the_check(self):
        self.config.host = "0.0.0.0"
        self.config.server.api_token = ""
        self.assertEqual(pfsetup._check_phone(self.config).status, "fail")

    def test_binding_outward_with_a_token_passes(self):
        self.config.host = "0.0.0.0"
        self.config.server.api_token = "s3cret"
        self.assertEqual(pfsetup._check_phone(self.config).status, "pass")

    def test_a_stale_lan_address_is_caught(self):
        # The router hands out a new lease and every notification button
        # quietly starts pointing at somebody else's laptop.
        self.config.server.public_base_url = "http://192.168.77.123:8787"
        check = pfsetup._check_phone(self.config)
        self.assertEqual(check.status, "warn")
        self.assertIn("192.168.77.123", check.detail)

    def test_a_public_hostname_is_not_treated_as_stale(self):
        self.config.server.public_base_url = "https://pokeflip.example"
        self.assertNotIn("stale", pfsetup._check_phone(self.config).detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
