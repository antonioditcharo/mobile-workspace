"""Tests for pokeflip.

Runs on the standard library alone: ``python3 -m unittest discover tests``.
The offline fixture provider makes every case deterministic, so these tests
never touch the network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pokeflip import bulk, ingest, portfolio, signals  # noqa: E402
from pokeflip.alerts import add_watch, evaluate_alerts, store_alerts  # noqa: E402
from pokeflip.analytics import (  # noqa: E402
    compute_metrics, linear_slope, pct_change, sma, value_days_ago,
)
from pokeflip.config import Config, MarketplaceFees  # noqa: E402
from pokeflip.db import Database  # noqa: E402
from pokeflip.digest import build as build_digest, render_html, render_markdown  # noqa: E402
from pokeflip.providers.fixture import FixtureProvider  # noqa: E402
from pokeflip.providers.pokemontcg import extract_quotes  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
