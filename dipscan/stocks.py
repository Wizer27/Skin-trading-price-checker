"""Stock prices from Yahoo Finance's chart endpoint: daily candles, no API key needed."""

from __future__ import annotations

from typing import List, Optional, Sequence

from .http import Client, FetchError, NotFound
from .models import Asset

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s"

# Yahoo answers 429 to a full browser User-Agent (it expects a real browser carrying
# consent cookies) but serves a plain client fine — keep this UA boring.
HEADERS = {"User-Agent": "dipscan/0.1", "Accept": "application/json"}

# A default universe of liquid US names and ETFs, so the tool runs with no arguments.
DEFAULT_SYMBOLS = (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD INTC MU QCOM TXN ARM SMCI PLTR CRM ORCL ADBE NOW "
    "NFLX DIS CMCSA T VZ JPM BAC WFC GS MS V MA PYPL XYZ COIN HOOD SHOP UBER ABNB DASH "
    "XOM CVX COP SLB JNJ PFE MRK LLY ABBV UNH WMT COST TGT HD NKE SBUX MCD KO PEP PG "
    "BA CAT DE GE LMT RTX F GM RIVN LCID SPY QQQ IWM DIA"
).split()


def parse_symbols(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(DEFAULT_SYMBOLS)
    return [part.strip().upper() for part in raw.replace(",", " ").split() if part.strip()]


def fetch_one(client: Client, symbol: str, days: int = 90) -> Optional[Asset]:
    """One request per ticker — Yahoo's chart endpoint takes a single symbol."""
    payload = client.get_json(
        CHART_URL % symbol,
        {"range": "%dd" % days, "interval": "1d", "includePrePost": "false"},
    )
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        raise FetchError("%s: %s" % (symbol, chart["error"].get("description", "unknown error")))
    results = chart.get("result") or []
    if not results:
        return None
    result = results[0]
    meta = result.get("meta") or {}
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]

    closes: List[float] = []
    volumes: List[float] = []
    for close, volume in zip(quote.get("close") or [], quote.get("volume") or []):
        if close is None:  # holidays and halted sessions come back as null
            continue
        closes.append(float(close))
        volumes.append(float(volume or 0))
    if not closes:
        return None

    price = float(meta.get("regularMarketPrice") or closes[-1])
    return Asset(
        kind="stock",
        symbol=symbol,
        name=meta.get("shortName") or meta.get("longName") or symbol,
        price=price,
        currency=meta.get("currency") or "USD",
        prices=closes,
        volumes=volumes,
        money_volume=price * volumes[-1] if volumes else None,
        change_24h=(price / closes[-2] - 1.0) * 100.0 if len(closes) > 1 else None,
        change_7d=(price / closes[-6] - 1.0) * 100.0 if len(closes) > 5 else None,
        url="https://finance.yahoo.com/quote/%s" % symbol,
    )


def fetch(
    client: Client,
    symbols: Sequence[str],
    days: int = 90,
    on_item=None,
) -> List[Asset]:
    assets: List[Asset] = []
    misses = 0
    for number, symbol in enumerate(symbols, 1):
        try:
            asset = fetch_one(client, symbol, days)
            misses = 0
        except NotFound:
            # A renamed or delisted ticker is the list's problem, not the endpoint's.
            if on_item:
                on_item(number, symbol, "%s: no such ticker" % symbol)
            continue
        except FetchError as error:
            misses += 1
            if on_item:
                on_item(number, symbol, str(error))
            if misses >= 5:  # the endpoint is clearly unhappy, stop digging
                break
            continue
        if asset is not None:
            assets.append(asset)
        if on_item:
            on_item(number, symbol, None)
    return assets
