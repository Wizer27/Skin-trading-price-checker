"""Turn raw prices into a ranked list of "dropped too far, worth buying" candidates."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .steam import MarketItem, Overview

# Steam takes 5% and the publisher another 10% from every sale.
DEFAULT_FEE = 0.15


@dataclass
class Filters:
    min_price: float = 0.5
    max_price: float = 500.0
    min_listings: int = 10
    min_volume: int = 5  # sales per day
    min_drop: float = 8.0  # percent below the 24h median
    min_margin: float = 3.0  # percent net profit over the buy price
    fee: float = DEFAULT_FEE


@dataclass
class Candidate:
    app_id: int
    name: str
    price: float  # what you pay now (cheapest listing)
    listings: int
    reference: float  # 24h median — what the item normally goes for
    volume: Optional[int] = None
    sources: List[str] = field(default_factory=lambda: ["median24h"])

    @property
    def drop_pct(self) -> float:
        return (self.reference - self.price) / self.reference * 100.0

    def net_resale(self, fee: float = DEFAULT_FEE) -> float:
        """What lands in your wallet if you resell at the reference price."""
        return self.reference * (1.0 - fee)

    def profit(self, fee: float = DEFAULT_FEE) -> float:
        return self.net_resale(fee) - self.price

    def margin_pct(self, fee: float = DEFAULT_FEE) -> float:
        return self.profit(fee) / self.price * 100.0

    def confidence(self) -> float:
        """0..1 — how tradeable the drop is, judged by how briskly the item sells."""
        if self.volume:
            liquidity = min(1.0, math.log10(1 + self.volume) / 2.0)  # ~100 sales/day saturates
        else:
            liquidity = 0.5 * min(1.0, math.log10(1 + self.listings) / 2.0)
        return 0.6 + 0.4 * liquidity

    def score(self, fee: float = DEFAULT_FEE) -> float:
        return self.margin_pct(fee) * self.confidence()


def build_candidate(item: MarketItem, overview: Optional[Overview]) -> Optional[Candidate]:
    """Compare the cheapest listing against the median of what actually sold today."""
    if overview is None or not overview.median or overview.median <= 0:
        return None
    return Candidate(
        app_id=item.app_id,
        name=item.name,
        price=item.price,
        listings=item.listings,
        reference=overview.median,
        volume=overview.volume,
    )


def rank(
    items: Sequence[MarketItem],
    overviews: Dict[str, Overview],
    filters: Filters,
    limit: int = 10,
) -> List[Candidate]:
    """Filter, score and sort. Returns at most `limit` best buys, best first."""
    candidates: List[Candidate] = []
    for item in items:
        candidate = build_candidate(item, overviews.get(item.name))
        if candidate is None:
            continue
        if candidate.volume is not None and candidate.volume < filters.min_volume:
            continue
        if candidate.drop_pct < filters.min_drop:
            continue
        if candidate.margin_pct(filters.fee) < filters.min_margin:
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda c: (c.score(filters.fee), c.drop_pct, c.volume or 0), reverse=True)
    return candidates[:limit]


def eligible(items: Sequence[MarketItem], filters: Filters) -> List[MarketItem]:
    """Items worth spending a (rate-limited) median request on, most liquid first."""
    pool = [
        item
        for item in items
        if filters.min_price <= item.price <= filters.max_price
        and item.listings >= filters.min_listings
    ]
    pool.sort(key=lambda item: item.listings, reverse=True)
    return pool
