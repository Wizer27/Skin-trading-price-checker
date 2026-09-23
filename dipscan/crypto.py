"""Crypto prices from CoinGecko: 250 coins per request, no API key needed."""

from __future__ import annotations

from typing import List

from .http import Client, FetchError
from .models import Asset

MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
HEADERS = {"Accept": "application/json"}
PER_PAGE = 250  # the endpoint's maximum


def fetch(client: Client, top: int = 250, currency: str = "usd") -> List[Asset]:
    """Top coins by market cap with a 7-day hourly price series (`sparkline`).

    One request per 250 coins, so `--top 250` costs a single call — that is why the
    coin universe can be wide while the stock one is a hand-picked list.
    """
    assets: List[Asset] = []
    page = 1
    while len(assets) < top:
        rows = client.get_json(
            MARKETS_URL,
            {
                "vs_currency": currency,
                "order": "market_cap_desc",
                "per_page": min(PER_PAGE, top - len(assets)),
                "page": page,
                "sparkline": "true",
                "price_change_percentage": "24h,7d",
            },
        )
        if isinstance(rows, dict):  # CoinGecko reports throttling as {"status": {...}}
            raise FetchError("CoinGecko refused the request: %s" % str(rows)[:160])
        if not rows:
            break
        for row in rows:
            price = row.get("current_price")
            if not price:
                continue
            series = [value for value in (row.get("sparkline_in_7d") or {}).get("price") or [] if value]
            assets.append(
                Asset(
                    kind="crypto",
                    symbol=str(row.get("symbol", "")).upper(),
                    name=row.get("name") or row.get("id") or "?",
                    price=float(price),
                    currency=currency.upper(),
                    prices=[float(value) for value in series] or [float(price)],
                    money_volume=float(row.get("total_volume") or 0.0),
                    change_24h=row.get("price_change_percentage_24h_in_currency"),
                    change_7d=row.get("price_change_percentage_7d_in_currency"),
                    url="https://www.coingecko.com/en/coins/%s" % row.get("id", ""),
                )
            )
        page += 1
    return assets[:top]
