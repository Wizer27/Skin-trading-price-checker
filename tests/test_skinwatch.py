"""Offline tests — nothing here touches the network."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skinwatch import cli
from skinwatch.analyze import Filters, build_candidate, eligible, rank
from skinwatch.steam import MarketItem, Overview, SteamMarket, parse_int, parse_money


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

    def test_no_weighted_accept_language(self):
        """Steam 429s /market/priceoverview when this header carries q-values."""
        self.assertNotIn("Accept-Language", SteamMarket()._headers())


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.filters = Filters()

    def test_median_is_the_reference(self):
        candidate = build_candidate(
            MarketItem(730, "Skin", price=7.0, listings=100),
            Overview(lowest=7.0, median=10.0, volume=80),
        )
        self.assertEqual(candidate.reference, 10.0)
        self.assertAlmostEqual(candidate.drop_pct, 30.0)
        self.assertAlmostEqual(candidate.net_resale(0.15), 8.5)
        self.assertAlmostEqual(candidate.profit(0.15), 1.5)
        self.assertAlmostEqual(candidate.margin_pct(0.15), 21.43, places=2)

    def test_without_a_median_there_is_no_candidate(self):
        item = MarketItem(730, "Skin", 7.0, 100)
        self.assertIsNone(build_candidate(item, None))
        self.assertIsNone(build_candidate(item, Overview(lowest=7.0, median=None, volume=80)))

    def test_filters_drop_the_noise(self):
        items = [
            MarketItem(730, "flat", 10.0, 100),  # barely moved
            MarketItem(730, "dead", 5.0, 100),  # dropped but nobody trades it
            MarketItem(730, "smalldrop", 9.0, 100),  # drop eaten by the fee
            MarketItem(730, "good", 6.0, 100),  # the one we want
        ]
        overviews = {
            "flat": Overview(10.0, 10.2, 40),
            "dead": Overview(5.0, 12.0, 1),
            "smalldrop": Overview(9.0, 10.0, 50),
            "good": Overview(6.0, 9.0, 60),
        }
        self.assertEqual([c.name for c in rank(items, overviews, self.filters, 10)], ["good"])

    def test_ranking_prefers_margin_backed_by_volume(self):
        items = [MarketItem(730, "liquid", 6.0, 200), MarketItem(730, "thin", 6.0, 200)]
        overviews = {"liquid": Overview(6.0, 9.0, 300), "thin": Overview(6.0, 9.0, 6)}
        found = rank(items, overviews, self.filters, limit=2)
        self.assertEqual([c.name for c in found], ["liquid", "thin"])
        self.assertGreater(found[0].score(0.15), found[1].score(0.15))

    def test_eligible_skips_pennies_and_illiquid_and_sorts_by_lots(self):
        items = [
            MarketItem(730, "cheap", 0.05, 5000),
            MarketItem(730, "illiquid", 20.0, 2),
            MarketItem(730, "popular", 5.0, 900),
            MarketItem(730, "ok", 5.0, 50),
        ]
        self.assertEqual([i.name for i in eligible(items, self.filters)], ["popular", "ok"])


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
        self._real_market = cli.SteamMarket
        cli.SteamMarket = FakeMarket

    def tearDown(self):
        cli.SteamMarket = self._real_market

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["scan", "--quiet", *argv])
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

    def test_table_output_has_a_row_per_hit(self):
        code, output = self.run_cli("--top", "5")
        self.assertEqual(code, 0)
        self.assertIn("AK-47 | Redline", output)
        self.assertIn("-36.8%", output)

    def test_nothing_matched_is_not_an_error(self):
        code, output = self.run_cli("--min-drop", "90")
        self.assertEqual(code, 0)
        self.assertIn("Nothing matched", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
