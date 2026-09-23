"""Offline tests — nothing here touches the network."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dipscan import cli, crypto, stocks
from dipscan.analyze import Filters, measure, rank
from dipscan.models import Asset


def flat(value: float, points: int = 40):
    return [value] * points


def asset(symbol, price, prices, volumes=None, turnover=50_000_000.0, kind="stock"):
    return Asset(
        kind=kind,
        symbol=symbol,
        name=symbol,
        price=price,
        currency="USD",
        prices=prices,
        volumes=volumes or [],
        money_volume=turnover,
        url="http://example.test/%s" % symbol,
    )


class MeasureTests(unittest.TestCase):
    def test_drawdown_and_z_of_a_real_dip(self):
        prices = flat(100.0, 30) + [90.0]
        dip = measure(asset("DIP", 90.0, prices))
        self.assertAlmostEqual(dip.drawdown, 10.0)
        self.assertLess(dip.z, -1.0)
        self.assertAlmostEqual(dip.to_median, 11.11, places=2)

    def test_short_history_is_refused(self):
        self.assertIsNone(measure(asset("NEW", 10.0, [10.0, 9.0])))

    def test_a_retraced_pump_is_not_a_dip(self):
        """Halving after a double leaves you on the mean: deep drawdown, zero abnormality."""
        prices = flat(50.0, 20) + flat(150.0, 20) + [75.0]
        dip = measure(asset("PUMP", 75.0, prices))
        self.assertGreater(dip.drawdown, 40.0)
        self.assertLess(dip.abnormality, 0.35)
        quiet = measure(asset("CALM", 90.0, flat(100.0, 30) + [90.0]))
        self.assertGreater(quiet.score, dip.score)

    def test_volume_ratio_uses_the_window_median(self):
        prices = flat(100.0, 20) + [90.0]
        volumes = flat(1000.0, 20) + [5000.0]
        dip = measure(asset("VOL", 90.0, prices, volumes))
        self.assertAlmostEqual(dip.vol_ratio, 5.0)
        without = measure(asset("NOVOL", 90.0, prices))
        self.assertGreater(dip.score, without.score)  # volume adds a bonus, capped at 15%


class RankTests(unittest.TestCase):
    def setUp(self):
        self.filters = Filters()

    def test_filters(self):
        assets = [
            asset("THIN", 90.0, flat(100.0, 30) + [90.0], turnover=1000.0),  # illiquid
            asset("CALM", 99.5, flat(100.0, 30) + [99.5]),  # drop too small
            asset("ABOVE", 120.0, flat(100.0, 30) + [120.0]),  # not below its mean at all
            asset("GOOD", 85.0, flat(100.0, 30) + [85.0]),
        ]
        self.assertEqual([dip.asset.symbol for dip in rank(assets, self.filters, 10)], ["GOOD"])

    def test_deeper_abnormal_dip_ranks_first(self):
        assets = [
            asset("SMALL", 92.0, flat(100.0, 30) + [92.0]),
            asset("BIG", 80.0, flat(100.0, 30) + [80.0]),
        ]
        self.assertEqual([dip.asset.symbol for dip in rank(assets, self.filters, 2)], ["BIG", "SMALL"])


class SourceParsingTests(unittest.TestCase):
    def test_coingecko_rows_become_assets(self):
        class FakeClient:
            requests = 1

            def get_json(self, _url, _params=None):
                return [
                    {
                        "id": "bitcoin",
                        "symbol": "btc",
                        "name": "Bitcoin",
                        "current_price": 84225,
                        "total_volume": 45953797589,
                        "price_change_percentage_24h_in_currency": -2.6,
                        "price_change_percentage_7d_in_currency": 11.3,
                        "sparkline_in_7d": {"price": [90000.0, 88000.0, 84225.0]},
                    }
                ]

        coins = crypto.fetch(FakeClient(), top=1)
        self.assertEqual(coins[0].symbol, "BTC")
        self.assertEqual(coins[0].prices[-1], 84225.0)
        self.assertEqual(coins[0].money_volume, 45953797589.0)

    def test_yahoo_nulls_are_skipped(self):
        class FakeClient:
            def get_json(self, _url, _params=None):
                return {
                    "chart": {
                        "result": [
                            {
                                "meta": {"currency": "USD", "shortName": "Apple", "regularMarketPrice": 337.38},
                                "timestamp": [1, 2, 3],
                                "indicators": {"quote": [{"close": [100.0, None, 337.38], "volume": [10, None, 20]}]},
                            }
                        ]
                    }
                }

        share = stocks.fetch_one(FakeClient(), "AAPL")
        self.assertEqual(share.prices, [100.0, 337.38])
        self.assertEqual(share.volumes, [10.0, 20.0])
        self.assertEqual(share.name, "Apple")

    def test_yahoo_user_agent_stays_boring(self):
        """Yahoo 429s a full browser UA; a plain one is served."""
        self.assertNotIn("Chrome", stocks.HEADERS["User-Agent"])

    def test_symbol_parsing(self):
        self.assertEqual(stocks.parse_symbols("aapl, msft nvda"), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(stocks.parse_symbols(None), list(stocks.DEFAULT_SYMBOLS))


class CliTests(unittest.TestCase):
    def setUp(self):
        self._fetch = crypto.fetch
        crypto.fetch = lambda _client, top=250, currency="usd": [
            asset("DIP", 85.0, flat(100.0, 30) + [85.0], kind="crypto", turnover=90_000_000.0),
            asset("CALM", 100.0, flat(100.0, 31), kind="crypto", turnover=90_000_000.0),
        ]

    def tearDown(self):
        crypto.fetch = self._fetch

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["crypto", "--quiet", *argv])
        return code, buffer.getvalue()

    def test_json_output(self):
        code, output = self.run_cli("--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["universe"], 2)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["symbol"], "DIP")
        self.assertAlmostEqual(payload["results"][0]["drawdown_pct"], 15.0)

    def test_table_output(self):
        code, output = self.run_cli("--top", "5")
        self.assertEqual(code, 0)
        self.assertIn("DIP", output)
        self.assertIn("-15.0%", output)

    def test_empty_result_is_not_an_error(self):
        code, output = self.run_cli("--min-drop", "90")
        self.assertEqual(code, 0)
        self.assertIn("Nothing fell that far", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
