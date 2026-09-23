"""The one shape every source produces and every metric consumes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Asset:
    kind: str  # "crypto" | "stock"
    symbol: str  # BTC, AAPL
    name: str
    price: float  # latest price
    currency: str
    prices: List[float] = field(default_factory=list)  # oldest → newest, the analysis window
    volumes: List[float] = field(default_factory=list)  # matching volumes, may be empty
    money_volume: Optional[float] = None  # traded money per day, for the liquidity filter
    change_24h: Optional[float] = None  # percent, straight from the source when available
    change_7d: Optional[float] = None
    url: str = ""
