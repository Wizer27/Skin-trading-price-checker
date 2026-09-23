"""Thin, dependency-free client for the public Steam Community Market endpoints."""

from __future__ import annotations

import gzip
import json
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

BASE = "https://steamcommunity.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Steam currency ids -> human label (only the common ones).
CURRENCIES: Dict[int, str] = {
    1: "USD",
    2: "GBP",
    3: "EUR",
    4: "CHF",
    5: "RUB",
    6: "PLN",
    7: "BRL",
    8: "JPY",
    9: "NOK",
    10: "IDR",
    11: "MYR",
    12: "PHP",
    13: "SGD",
    14: "THB",
    15: "VND",
    16: "KRW",
    17: "TRY",
    18: "UAH",
    19: "MXN",
    20: "CAD",
    21: "AUD",
    22: "NZD",
    23: "CNY",
    24: "INR",
    25: "CLP",
    26: "PEN",
    27: "COP",
    28: "ZAR",
    29: "HKD",
    30: "TWD",
    31: "SAR",
    32: "AED",
    34: "ARS",
    35: "ILS",
    37: "KZT",
}

# Apps with a liquid skin market. Fee = Steam cut + publisher cut.
APPS: Dict[int, Dict[str, object]] = {
    730: {"name": "CS2", "fee": 0.15},
    570: {"name": "Dota 2", "fee": 0.15},
    440: {"name": "Team Fortress 2", "fee": 0.15},
    252490: {"name": "Rust", "fee": 0.15},
    578080: {"name": "PUBG", "fee": 0.15},
    232090: {"name": "Killing Floor 2", "fee": 0.15},
}

_NUMBER_RE = re.compile(r"\d[\d\s .,']*\d|\d")


class SteamError(RuntimeError):
    """Raised when the market endpoint keeps failing or answers with success=false."""


class Transport:
    """Minimal GET transport: returns (http_status, body_text)."""

    name = "transport"

    def get(self, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        raise NotImplementedError


class UrllibTransport(Transport):
    name = "urllib"

    def get(self, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return response.status, body.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


class CurlTransport(Transport):
    """Steam answers 429 to Python's TLS fingerprint on some setups; curl gets through."""

    name = "curl"

    def __init__(self, binary: str = "curl") -> None:
        self.binary = binary

    @staticmethod
    def available() -> bool:
        return shutil.which("curl") is not None

    def get(self, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        command = [self.binary, "-sS", "--compressed", "--max-time", str(int(timeout)), "-w", "\n%{http_code}"]
        for key, value in headers.items():
            if key.lower() == "accept-encoding":
                continue  # --compressed already negotiates this
            command += ["-H", "%s: %s" % (key, value)]
        command.append(url)
        finished = subprocess.run(command, capture_output=True, timeout=timeout + 10)
        if finished.returncode != 0:
            raise OSError(finished.stderr.decode("utf-8", "replace").strip() or "curl failed")
        output = finished.stdout.decode("utf-8", "replace")
        body, _, status = output.rpartition("\n")
        try:
            return int(status.strip()), body
        except ValueError:
            raise OSError("unexpected curl output")


def make_transport(kind: str = "auto") -> Transport:
    if kind == "urllib":
        return UrllibTransport()
    if kind == "curl":
        return CurlTransport()
    return CurlTransport() if CurlTransport.available() else UrllibTransport()


def parse_money(text: Optional[str]) -> Optional[float]:
    """Parse a localized Steam price string ("$1.23", "1 234,56 pуб.") into a float."""
    if not text:
        return None
    match = _NUMBER_RE.search(text.replace(" ", " "))
    if not match:
        return None
    raw = match.group(0)
    for junk in (" ", " ", "'"):
        raw = raw.replace(junk, "")
    cut = max(raw.rfind(","), raw.rfind("."))
    if cut == -1:
        return float(raw)
    tail = raw[cut + 1 :]
    if len(tail) in (1, 2):  # decimal separator
        head = re.sub(r"[.,]", "", raw[:cut])
        return float("%s.%s" % (head or "0", tail))
    return float(re.sub(r"[.,]", "", raw))  # thousands separators only


def parse_int(text: Optional[str]) -> Optional[int]:
    value = parse_money(text)
    return None if value is None else int(round(value))


@dataclass
class MarketItem:
    """One item as returned by the market search page."""

    app_id: int
    name: str
    price: float  # cheapest listing right now
    listings: int  # how many copies are on sale

    @property
    def price_cents(self) -> int:
        return int(round(self.price * 100))


@dataclass
class Overview:
    """Result of /market/priceoverview: the 24h median is our "fair price" anchor."""

    lowest: Optional[float]
    median: Optional[float]
    volume: Optional[int]


class SteamMarket:
    """Polite HTTP client: one request at a time, retries on 429/5xx."""

    def __init__(
        self,
        currency: int = 1,
        delay: float = 4.0,
        timeout: float = 25.0,
        retries: int = 3,
        cookie: Optional[str] = None,
        transport: str = "auto",
        sleep=time.sleep,
    ) -> None:
        self.currency = currency
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.cookie = cookie
        self.transport = make_transport(transport)
        self._sleep = sleep
        self._last_request = 0.0
        self.requests = 0

    # ---------------------------------------------------------------- transport
    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Referer": "%s/market/" % BASE,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _get_json(self, path: str, params: Dict[str, object]) -> dict:
        url = "%s%s?%s" % (BASE, path, urllib.parse.urlencode(params))
        last_error: Optional[str] = None

        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                self.requests += 1
                status, body = self.transport.get(url, self._headers(), self.timeout)
                self._last_request = time.monotonic()
                if status == 200:
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError:
                        last_error = "malformed JSON"
                elif status in (429, 500, 502, 503, 504):
                    last_error = "HTTP %d" % status
                else:
                    raise SteamError("HTTP %d for %s" % (status, url))
            except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
                self._last_request = time.monotonic()
                last_error = str(exc) or exc.__class__.__name__
            if attempt < self.retries:
                backoff = min(20.0, self.delay * (2 ** attempt)) + random.uniform(0, 1.0)
                self._sleep(backoff)

        hint = ""
        if last_error == "HTTP 429" and self.transport.name == "urllib":
            hint = " (Steam throttles this TLS stack — install curl or pass --transport curl)"
        raise SteamError("giving up on %s: %s%s" % (url, last_error, hint))

    # ------------------------------------------------------------------ queries
    def search(
        self,
        app_id: int,
        pages: int = 20,
        per_page: int = 10,
        sort_column: str = "quantity",
        query: str = "",
        on_page=None,
    ) -> List[MarketItem]:
        """Walk the market search pages and return the cheapest listing per item.

        `sort_column="quantity"` puts the most liquid items first, which is exactly
        the pool where a sudden price drop is both real and tradeable. Steam currently
        caps a search page at 10 rows regardless of `count`, so we follow whatever the
        endpoint actually returns instead of trusting `per_page`.
        """
        items: List[MarketItem] = []
        seen = set()
        start = 0
        for page in range(pages):
            data = self._get_json(
                "/market/search/render/",
                {
                    "appid": app_id,
                    "norender": 1,
                    "start": start,
                    "count": per_page,
                    "currency": self.currency,
                    "sort_column": sort_column,
                    "sort_dir": "desc",
                    "search_descriptions": 0,
                    "query": query,
                },
            )
            if not data.get("success"):
                raise SteamError("market search failed (appid=%s start=%s)" % (app_id, start))
            results = data.get("results") or []
            total = int(data.get("total_count") or 0)
            for row in results:
                name = row.get("hash_name") or row.get("name")
                if not name or name in seen:
                    continue
                cents = row.get("sell_price")
                price = (
                    float(cents) / 100.0
                    if isinstance(cents, (int, float)) and cents
                    else parse_money(row.get("sell_price_text"))
                )
                if not price:
                    continue
                seen.add(name)
                items.append(
                    MarketItem(
                        app_id=app_id,
                        name=name,
                        price=price,
                        listings=int(row.get("sell_listings") or 0),
                    )
                )
            start += len(results)
            if on_page:
                on_page(page + 1, len(items), total)
            if not results or (total and start >= total):
                break
        return items

    def price_overview(self, app_id: int, name: str) -> Overview:
        """Current lowest listing, 24h median sale price and 24h sales volume."""
        data = self._get_json(
            "/market/priceoverview/",
            {
                "appid": app_id,
                "currency": self.currency,
                "market_hash_name": name,
            },
        )
        if not data.get("success"):
            raise SteamError("no price overview for %r" % name)
        return Overview(
            lowest=parse_money(data.get("lowest_price")),
            median=parse_money(data.get("median_price")),
            volume=parse_int(data.get("volume")),
        )

    def market_url(self, app_id: int, name: str) -> str:
        return "%s/market/listings/%d/%s" % (BASE, app_id, urllib.parse.quote(name))
