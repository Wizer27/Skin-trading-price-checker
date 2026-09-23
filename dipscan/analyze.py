"""Rank assets by how abnormally they fell — a drop is only interesting versus its own noise."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .models import Asset

MIN_POINTS = 10  # below this a window says nothing about "normal"


@dataclass
class Filters:
    min_drop: float = 5.0  # percent below the window high
    max_z: float = -1.0  # how far below its own mean the price must sit
    min_money_volume: float = 5_000_000.0  # traded money per day
    min_price: float = 0.0
    max_price: float = float("inf")


@dataclass
class Dip:
    asset: Asset
    drawdown: float  # % below the highest price in the window
    z: float  # standard deviations from the window mean (negative = unusually cheap)
    to_median: float  # % the price must rise to reach the window median
    vol_ratio: Optional[float]  # today's volume vs the window's typical volume

    @property
    def abnormality(self) -> float:
        """0..1 — how far outside its own noise the price sits (-2.5σ counts as 1)."""
        return max(0.0, min(1.0, -self.z / 2.5))

    @property
    def score(self) -> float:
        """Depth of the fall × how abnormal it is. No floor on abnormality on purpose:

        an asset that doubled and gave half of it back is 50% off its high while sitting
        right on its own mean — that is noise, not a dip, and it scores zero.
        Volume adds at most 15%: a flush with real turnover beats a quiet slide.
        """
        volume_bonus = 1.0
        if self.vol_ratio and self.vol_ratio > 1.0:
            volume_bonus = 1.0 + 0.15 * min(1.0, math.log10(self.vol_ratio) / math.log10(5.0))
        return self.drawdown * self.abnormality * volume_bonus


def measure(asset: Asset) -> Optional[Dip]:
    prices = [price for price in asset.prices if price and price > 0]
    if len(prices) < MIN_POINTS:
        return None
    high = max(prices)
    mean = statistics.fmean(prices)
    spread = statistics.pstdev(prices)
    median = statistics.median(prices)
    price = asset.price

    vol_ratio = None
    if len(asset.volumes) >= MIN_POINTS:
        past = [volume for volume in asset.volumes[:-1] if volume > 0]
        typical = statistics.median(past) if past else 0.0
        if typical > 0:
            vol_ratio = asset.volumes[-1] / typical

    return Dip(
        asset=asset,
        drawdown=(high - price) / high * 100.0 if high > 0 else 0.0,
        z=(price - mean) / spread if spread > 0 else 0.0,
        to_median=(median - price) / price * 100.0 if price > 0 else 0.0,
        vol_ratio=vol_ratio,
    )


def rank(assets: Sequence[Asset], filters: Filters, limit: int = 10) -> List[Dip]:
    dips: List[Dip] = []
    for asset in assets:
        if not (filters.min_price <= asset.price <= filters.max_price):
            continue
        if asset.money_volume is not None and asset.money_volume < filters.min_money_volume:
            continue
        dip = measure(asset)
        if dip is None or dip.drawdown < filters.min_drop or dip.z > filters.max_z:
            continue
        dips.append(dip)

    dips.sort(key=lambda dip: (dip.score, dip.drawdown), reverse=True)
    return dips[:limit]
