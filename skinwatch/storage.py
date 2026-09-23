"""SQLite price journal: every scan appends a snapshot, so drops become measurable."""

from __future__ import annotations

import os
import sqlite3
import statistics
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    app_id   INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    currency INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    price    INTEGER NOT NULL,          -- cheapest listing, in minor units
    listings INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, name, currency, ts)
);
CREATE INDEX IF NOT EXISTS snapshots_lookup
    ON snapshots (app_id, currency, name, ts);
"""

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".skinwatch", "prices.db")


@dataclass
class Baseline:
    """What the item used to cost, measured from our own history."""

    price: Optional[float]  # robust "normal" price (median of past snapshots)
    high: Optional[float]  # highest past snapshot in the window
    points: int  # how many past snapshots backed it up
    first_ts: Optional[int]


class PriceStore:
    def __init__(self, path: str = DEFAULT_DB) -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "PriceStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------- writes
    def record(
        self,
        rows: Iterable[Tuple[int, str, int, int]],
        currency: int,
        ts: Optional[int] = None,
    ) -> int:
        """Append snapshots. `rows` yields (app_id, name, price_cents, listings)."""
        stamp = int(ts if ts is not None else time.time())
        payload = [(app_id, name, currency, stamp, price, listings) for app_id, name, price, listings in rows]
        self.db.executemany(
            "INSERT OR REPLACE INTO snapshots (app_id, name, currency, ts, price, listings)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            payload,
        )
        self.db.commit()
        return len(payload)

    def prune(self, keep_days: float, now: Optional[int] = None) -> int:
        cutoff = int((now if now is not None else time.time()) - keep_days * 86400)
        cursor = self.db.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        self.db.commit()
        return cursor.rowcount

    # -------------------------------------------------------------------- reads
    def series(
        self,
        app_id: int,
        name: str,
        currency: int,
        since: Optional[int] = None,
    ) -> List[Tuple[int, float, int]]:
        sql = "SELECT ts, price, listings FROM snapshots WHERE app_id=? AND name=? AND currency=?"
        args: List[object] = [app_id, name, currency]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " ORDER BY ts"
        return [(ts, price / 100.0, listings) for ts, price, listings in self.db.execute(sql, args)]

    def baselines(
        self,
        app_id: int,
        names: Sequence[str],
        currency: int,
        window_days: float,
        before: Optional[int] = None,
        now: Optional[int] = None,
    ) -> dict:
        """Baseline price per item from snapshots inside the window, excluding `before`.

        `before` is the timestamp of the current run: the freshly written snapshot must
        not drag the baseline down towards the very price we are testing.
        """
        stamp = int(now if now is not None else time.time())
        since = int(stamp - window_days * 86400)
        result = {}
        for chunk_start in range(0, len(names), 400):
            chunk = names[chunk_start : chunk_start + 400]
            placeholders = ",".join("?" * len(chunk))
            sql = (
                "SELECT name, ts, price FROM snapshots"
                " WHERE app_id=? AND currency=? AND ts>=? AND name IN (%s)" % placeholders
            )
            args: List[object] = [app_id, currency, since]
            args.extend(chunk)
            if before is not None:
                sql += " AND ts < ?"
                args.append(before)
            buckets = {}
            for name, ts, price in self.db.execute(sql, args):
                buckets.setdefault(name, []).append((ts, price / 100.0))
            for name, points in buckets.items():
                prices = [price for _ts, price in points]
                result[name] = Baseline(
                    price=statistics.median(prices),
                    high=max(prices),
                    points=len(prices),
                    first_ts=min(ts for ts, _price in points),
                )
        return result

    def stats(self) -> Tuple[int, int, Optional[int], Optional[int]]:
        row = self.db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT name), MIN(ts), MAX(ts) FROM snapshots"
        ).fetchone()
        return row[0], row[1], row[2], row[3]
