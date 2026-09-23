"""Turn raw prices into a ranked list of "dropped too far, worth buying" candidates."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .steam import MarketItem, Overview
from .storage import Baseline

# Steam takes 5% and the publisher another 10% from every sale.
DEFAULT_FEE = 0.15


@dataclass
class Filters:
    min_price: float = 0.5
    max_price: float = 500.0
    min_listings: int = 10
    min_volume: int = 5  # sales per day, checked only when we know it
    min_drop: float = 8.0  # percent below the reference price
    min_margin: float = 3.0  # percent net profit over the buy price
    fee: float = DEFAULT_FEE


@dataclass
class Candidate:
    app_id: int
    name: str
    price: float  # what you pay now (cheapest listing)
    listings: int
    reference: float  # what it normally costs
    sources: List[str] = field(default_factory=list)
    median_24h: Optional[float] = None
    volume: Optional[int] = None
    baseline: Optional[Baseline] = None

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
        """0..1 — how much the two independent signals back this drop up."""
        liquidity = 0.0
        if self.volume:
            liquidity = min(1.0, math.log10(1 + self.volume) / 2.0)  # ~100 sales/day saturates
        elif self.listings:
            liquidity = 0.5 * min(1.0, math.log10(1 + self.listings) / 2.0)
        history = 0.0
        if self.baseline and self.baseline.points:
            history = min(1.0, self.baseline.points / 6.0)
        return 0.55 + 0.25 * liquidity + 0.20 * history

    def score(self, fee: float = DEFAULT_FEE) -> float:
        return self.margin_pct(fee) * self.confidence()


def build_candidate(
    item: MarketItem,
    baseline: Optional[Baseline] = None,
    overview: Optional[Overview] = None,
) -> Optional[Candidate]:
    """Pick the reference price conservatively: the *lowest* of the signals we trust.

    A 24h median says "this is what people actually paid today"; our own history says
    "this is what it cost on previous scans". Taking the minimum keeps the estimated
    profit honest instead of flattering a single noisy number.
    """
    price = item.price
    references: List[float] = []
    sources: List[str] = []

    median = overview.median if overview else None
    if median and median > 0:
        references.append(median)
        sources.append("median24h")
    if baseline and baseline.price and baseline.price > 0:
        references.append(baseline.price)
        sources.append("history")

    if not references:
        return None

    return Candidate(
        app_id=item.app_id,
        name=item.name,
        price=price,
        listings=item.listings,
        reference=min(references),
        sources=sources,
        median_24h=median,
        volume=overview.volume if overview else None,
        baseline=baseline,
    )


def rank(
    items: Sequence[MarketItem],
    baselines: Dict[str, Baseline],
    overviews: Dict[str, Overview],
    filters: Filters,
    limit: int = 10,
) -> List[Candidate]:
    """Filter, score and sort. Returns at most `limit` best buys, best first."""
    candidates: List[Candidate] = []
    for item in items:
        if not (filters.min_price <= item.price <= filters.max_price):
            continue
        if item.listings < filters.min_listings:
            continue
        candidate = build_candidate(item, baselines.get(item.name), overviews.get(item.name))
        if candidate is None:
            continue
        if candidate.volume is not None and candidate.volume < filters.min_volume:
            continue
        if candidate.drop_pct < filters.min_drop:
            continue
        if candidate.margin_pct(filters.fee) < filters.min_margin:
            continue
        candidates.append(candidate)

    candidates.sort(
        key=lambda c: (c.score(filters.fee), c.drop_pct, c.volume or 0),
        reverse=True,
    )
    return candidates[:limit]


def shortlist(
    items: Sequence[MarketItem],
    baselines: Dict[str, Baseline],
    filters: Filters,
    size: int,
) -> List[MarketItem]:
    """Cheap pre-filter deciding which items deserve a (rate-limited) API check.

    With history available we already know who fell; on a first run we fall back to
    the most liquid items, where a median is most likely to be meaningful.
    """
    pool = [
        item
        for item in items
        if filters.min_price <= item.price <= filters.max_price and item.listings >= filters.min_listings
    ]

    def key(item: MarketItem):
        baseline = baselines.get(item.name)
        if baseline and baseline.price:
            return (1, (baseline.price - item.price) / baseline.price, item.listings)
        return (0, 0.0, item.listings)

    pool.sort(key=key, reverse=True)
    return pool[:size]
