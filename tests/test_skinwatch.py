"""Offline tests — nothing here touches the network."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skinwatch import cli
from skinwatch.analyze import Filters, build_candidate, rank, shortlist
from skinwatch.steam import MarketItem, Overview, parse_int, parse_money
from skinwatch.storage import Baseline, PriceStore

DAY = 86400


class ParsingTests(unittest.TestCase):
    def test_price_formats(self):
        cases = {
            "$1.23": 1.23,
            "1,23€": 1.23,
            "123,45 pуб.": 123.45,
            "1 234,56 pуб.": 1234.56,
            "$1,234.50": 1234.5,
            "0,5€": 0.5,
            "12": 12.0,
        }
        for text, expected in cases.items():
            self.assertAlmostEqual(parse_money(text), expected, msg=text)

    def test_missing_values(self):
        self.assertIsNone(parse_money(None))
        self.assertIsNone(parse_money(""))
        self.assertIsNone(parse_money("—"))

    def test_volume_is_an_integer(self):
        self.assertEqual(parse_int("1,234"), 1234)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = PriceStore(":memory:")
        self.now = int(time.time())

    def tearDown(self):
        self.store.close()

    def write(self, price_cents, ts):
        self.store.record([(730, "AK-47 | Redline", price_cents, 50)], currency=1, ts=ts)

    def test_baseline_is_a_median_of_past_snapshots(self):
        for offset, cents in ((3 * DAY, 1000), (2 * DAY, 1100), (DAY, 1200)):
            self.write(cents, self.now - offset)
        self.write(700, self.now)  # today's crash

        baselines = self.store.baselines(
            730, ["AK-47 | Redline"], 1, window_days=7, before=self.now, now=self.now
        )
        baseline = baselines["AK-47 | Redline"]
        self.assertEqual(baseline.points, 3)
        self.assertAlmostEqual(baseline.price, 11.0)
        self.assertAlmostEqual(baseline.high, 12.0)

    def test_window_excludes_old_points(self):
        self.write(1000, self.now - 30 * DAY)
        self.assertEqual(
            self.store.baselines(730, ["AK-47 | Redline"], 1, window_days=7, now=self.now), {}
        )

    def test_series_and_prune(self):
        self.write(1000, self.now - 10 * DAY)
        self.write(900, self.now)
        self.assertEqual(len(self.store.series(730, "AK-47 | Redline", 1)), 2)
        self.assertEqual(self.store.prune(5, now=self.now), 1)
        self.assertEqual(len(self.store.series(730, "AK-47 | Redline", 1)), 1)

    def test_currencies_do_not_mix(self):
        self.store.record([(730, "Glock", 1000, 5)], currency=1, ts=self.now - DAY)
        self.store.record([(730, "Glock", 90000, 5)], currency=5, ts=self.now - DAY)
        usd = self.store.baselines(730, ["Glock"], 1, 7, now=self.now)["Glock"]
        self.assertAlmostEqual(usd.price, 10.0)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.filters = Filters()

    def test_reference_takes_the_lower_signal(self):
        item = MarketItem(730, "Skin", price=7.0, listings=100)
        candidate = build_candidate(
            item,
            baseline=Baseline(price=12.0, high=13.0, points=4, first_ts=0),
            overview=Overview(lowest=7.0, median=10.0, volume=80),
        )
        self.assertEqual(candidate.reference, 10.0)
        self.assertEqual(candidate.sources, ["median24h", "history"])
        self.assertAlmostEqual(candidate.drop_pct, 30.0)
        self.assertAlmostEqual(candidate.net_resale(0.15), 8.5)
        self.assertAlmostEqual(candidate.profit(0.15), 1.5)

    def test_no_signal_means_no_candidate(self):
        self.assertIsNone(build_candidate(MarketItem(730, "Skin", 7.0, 100)))

    def test_filters_drop_the_noise(self):
        items = [
            MarketItem(730, "cheap", 0.05, 500),  # below min price
            MarketItem(730, "illiquid", 20.0, 2),  # too few listings
            MarketItem(730, "flat", 10.0, 100),  # barely moved
            MarketItem(730, "dead", 5.0, 100),  # dropped but nobody trades it
            MarketItem(730, "good", 6.0, 100),  # the one we want
        ]
        overviews = {
            "cheap": Overview(0.05, 0.5, 900),
            "illiquid": Overview(20.0, 30.0, 40),
            "flat": Overview(10.0, 10.2, 40),
            "dead": Overview(5.0, 12.0, 1),
            "good": Overview(6.0, 9.0, 60),
        }
        found = rank(items, {}, overviews, self.filters, limit=10)
        self.assertEqual([c.name for c in found], ["good"])

    def test_ranking_prefers_margin_backed_by_volume(self):
        items = [MarketItem(730, "liquid", 6.0, 200), MarketItem(730, "thin", 6.0, 200)]
        overviews = {
            "liquid": Overview(6.0, 9.0, 300),
            "thin": Overview(6.0, 9.0, 6),
        }
        found = rank(items, {}, overviews, self.filters, limit=2)
        self.assertEqual([c.name for c in found], ["liquid", "thin"])
        self.assertGreater(found[0].score(0.15), found[1].score(0.15))

    def test_fee_eats_a_small_drop(self):
        items = [MarketItem(730, "smalldrop", 9.0, 100)]
        overviews = {"smalldrop": Overview(9.0, 10.0, 50)}
        self.assertEqual(rank(items, {}, overviews, self.filters, 10), [])

    def test_shortlist_puts_the_fallers_first(self):
        items = [
            MarketItem(730, "faller", 5.0, 50),
            MarketItem(730, "popular", 5.0, 5000),
            MarketItem(730, "tiny", 0.01, 5000),
        ]
        baselines = {"faller": Baseline(price=10.0, high=10.0, points=3, first_ts=0)}
        picks = shortlist(items, baselines, self.filters, size=2)
        self.assertEqual([item.name for item in picks], ["faller", "popular"])


class FakeMarket:
    """Stands in for SteamMarket in the end-to-end CLI test."""

    requests = 3

    def __init__(self, *_args, **_kwargs):
        pass

    def search(self, app_id, pages=5, on_page=None, **_kwargs):
        items = [
            MarketItem(app_id, "AK-47 | Redline (Field-Tested)", 6.0, 400),
            MarketItem(app_id, "Glock-18 | Fade (Factory New)", 500.0, 40),
            MarketItem(app_id, "Sticker | Boring", 1.0, 300),
        ]
        if on_page:
            on_page(1, len(items), len(items))
        return items

    def price_overview(self, _app_id, name):
        return {
            "AK-47 | Redline (Field-Tested)": Overview(6.0, 9.5, 900),
            "Glock-18 | Fade (Factory New)": Overview(500.0, 505.0, 3),
            "Sticker | Boring": Overview(1.0, 1.02, 200),
        }[name]

    def market_url(self, app_id, name):
        return "https://steamcommunity.com/market/listings/%d/%s" % (app_id, name)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "prices.db")
        self._real_market = cli.SteamMarket
        cli.SteamMarket = FakeMarket

    def tearDown(self):
        cli.SteamMarket = self._real_market

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["scan", "--db", self.db, "--quiet", *argv])
        return code, buffer.getvalue()

    def test_json_scan_reports_only_the_real_drop(self):
        code, output = self.run_cli("--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(len(payload["results"]), 1)
        best = payload["results"][0]
        self.assertEqual(best["name"], "AK-47 | Redline (Field-Tested)")
        self.assertAlmostEqual(best["drop_pct"], 36.84, places=2)
        self.assertAlmostEqual(best["profit"], 2.07, places=2)
        self.assertIn("steamcommunity.com", best["url"])

    def test_scan_writes_history(self):
        self.run_cli("--json")
        with PriceStore(self.db) as store:
            rows, names, _first, _last = store.stats()
            self.assertEqual((rows, names), (3, 3))

    def test_no_save_keeps_the_journal_empty(self):
        self.run_cli("--json", "--no-save")
        with PriceStore(self.db) as store:
            self.assertEqual(store.stats()[0], 0)

    def test_table_output_has_a_row_per_hit(self):
        code, output = self.run_cli("--top", "5")
        self.assertEqual(code, 0)
        self.assertIn("AK-47 | Redline", output)
        self.assertIn("-36.8%", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
