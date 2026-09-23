"""Command line front-end: `python3 -m skinwatch scan`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Sequence

from .analyze import Candidate, Filters, rank, shortlist
from .steam import APPS, CURRENCIES, Overview, SteamError, SteamMarket
from .storage import DEFAULT_DB, PriceStore


# --------------------------------------------------------------------- output
class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return "\033[%sm%s\033[0m" % (code, text) if self.enabled else text

    def bold(self, text: str) -> str:
        return self(text, "1")

    def dim(self, text: str) -> str:
        return self(text, "2")

    def green(self, text: str) -> str:
        return self(text, "32")

    def red(self, text: str) -> str:
        return self(text, "31")

    def cyan(self, text: str) -> str:
        return self(text, "36")


def log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message, file=sys.stderr)


def money(value: float, currency: int) -> str:
    return "%.2f %s" % (value, CURRENCIES.get(currency, "#%d" % currency))


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(candidates: Sequence[Candidate], currency: int, fee: float, style: Style) -> str:
    code = CURRENCIES.get(currency, "#%d" % currency)
    headers = ["#", "Skin", "Now, %s" % code, "Usual, %s" % code, "Drop", "Profit", "Margin", "Vol/24h", "Lots"]
    rows: List[List[str]] = []
    for index, item in enumerate(candidates, 1):
        rows.append(
            [
                str(index),
                clip(item.name, 42),
                "%.2f" % item.price,
                "%.2f" % item.reference,
                "-%.1f%%" % item.drop_pct,
                "+%.2f" % item.profit(fee),
                "+%.1f%%" % item.margin_pct(fee),
                str(item.volume) if item.volume is not None else "-",
                str(item.listings),
            ]
        )

    widths = [len(head) for head in headers]
    for row in rows:
        for column, cell in enumerate(row):
            widths[column] = max(widths[column], len(cell))

    def line(cells: Sequence[str], pad: str = " ") -> str:
        out = []
        for column, cell in enumerate(cells):
            out.append(cell.ljust(widths[column], pad) if column == 1 else cell.rjust(widths[column], pad))
        return "  ".join(out)

    body = [style.bold(line(headers)), style.dim(line(["" for _ in headers], "─"))]
    for index, row in enumerate(rows):
        rendered = line(row)
        body.append(style.green(rendered) if index == 0 else rendered)
    return "\n".join(body)


def to_dict(item: Candidate, currency: int, fee: float, market: SteamMarket) -> dict:
    return {
        "app_id": item.app_id,
        "app": APPS.get(item.app_id, {}).get("name", str(item.app_id)),
        "name": item.name,
        "currency": CURRENCIES.get(currency, currency),
        "price": round(item.price, 2),
        "reference_price": round(item.reference, 2),
        "reference_sources": item.sources,
        "drop_pct": round(item.drop_pct, 2),
        "net_resale": round(item.net_resale(fee), 2),
        "profit": round(item.profit(fee), 2),
        "margin_pct": round(item.margin_pct(fee), 2),
        "confidence": round(item.confidence(), 3),
        "score": round(item.score(fee), 2),
        "median_24h": round(item.median_24h, 2) if item.median_24h else None,
        "volume_24h": item.volume,
        "listings": item.listings,
        "history_points": item.baseline.points if item.baseline else 0,
        "history_price": round(item.baseline.price, 2) if item.baseline and item.baseline.price else None,
        "url": market.market_url(item.app_id, item.name),
    }


# ----------------------------------------------------------------- scan command
def cmd_scan(args: argparse.Namespace) -> int:
    style = Style(sys.stdout.isatty() and not args.no_color and not args.json)
    app_ids = [int(part) for part in str(args.app).replace(",", " ").split()]
    filters = Filters(
        min_price=args.min_price,
        max_price=args.max_price,
        min_listings=args.min_listings,
        min_volume=args.min_volume,
        min_drop=args.min_drop,
        min_margin=args.min_margin,
        fee=args.fee if args.fee is not None else 0.15,
    )

    market = SteamMarket(
        currency=args.currency,
        delay=args.delay,
        transport=args.transport,
        cookie=os.environ.get("STEAM_COOKIE"),
    )
    store = PriceStore(args.db)
    now = int(time.time())
    all_candidates: List[Candidate] = []

    try:
        for app_id in app_ids:
            app = APPS.get(app_id, {})
            fee = args.fee if args.fee is not None else float(app.get("fee", 0.15))
            filters.fee = fee
            label = app.get("name", "app %d" % app_id)

            log("• %s: loading market pages…" % style.cyan(str(label)), args.quiet)

            def progress(page: int, collected: int, total: int) -> None:
                log("  page %d — %d items (market has %d)" % (page, collected, total), args.quiet)

            items = market.search(
                app_id,
                pages=args.pages,
                sort_column=args.sort,
                query=args.query,
                on_page=progress,
            )
            if not items:
                log("  nothing returned, skipping", args.quiet)
                continue

            names = [item.name for item in items]
            baselines = store.baselines(app_id, names, args.currency, args.window, before=now, now=now)
            if not args.no_save:
                store.record(
                    ((item.app_id, item.name, item.price_cents, item.listings) for item in items),
                    currency=args.currency,
                    ts=now,
                )

            overviews: Dict[str, Overview] = {}
            if args.enrich > 0:
                picks = shortlist(items, baselines, filters, args.enrich)
                log(
                    "  checking 24h medians for %d items (~%.0f s)…"
                    % (len(picks), len(picks) * args.delay),
                    args.quiet,
                )
                misses = 0
                for number, item in enumerate(picks, 1):
                    try:
                        overviews[item.name] = market.price_overview(app_id, item.name)
                        misses = 0
                    except SteamError as error:
                        misses += 1
                        log("  ! %s: %s" % (clip(item.name, 40), error), args.quiet)
                        if misses >= 3:
                            # Steam has clearly cut us off; grinding on only digs deeper.
                            log("  stopping the median check — raise --delay and retry later", args.quiet)
                            break
                        continue
                    if not args.quiet and number % 10 == 0:
                        log("  …%d/%d" % (number, len(picks)), args.quiet)

            found = rank(items, baselines, overviews, filters, limit=args.top)
            if not found and not baselines:
                log(
                    style.dim(
                        "  no history yet — run the scan again later and drops will be measured"
                        " against your own snapshots"
                    ),
                    args.quiet,
                )
            all_candidates.extend(found)

        all_candidates.sort(key=lambda c: c.score(filters.fee), reverse=True)
        all_candidates = all_candidates[: args.top]

        if args.json:
            print(
                json.dumps(
                    {
                        "generated_at": now,
                        "currency": CURRENCIES.get(args.currency, args.currency),
                        "fee": filters.fee,
                        "results": [to_dict(item, args.currency, filters.fee, market) for item in all_candidates],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if not all_candidates:
            print(style.dim("Nothing matched the filters — try --min-drop 5 or more --pages."))
            return 0

        print()
        print(style.bold("Top %d skins that dropped and look worth buying" % len(all_candidates)))
        print(style.dim("fee %.0f%% · reference = lowest of 24h median and your own history" % (filters.fee * 100)))
        print()
        print(render_table(all_candidates, args.currency, filters.fee, style))
        if args.links:
            print()
            for index, item in enumerate(all_candidates, 1):
                print(style.dim("%2d. %s" % (index, market.market_url(item.app_id, item.name))))
        print()
        print(style.dim("%d market requests · db: %s" % (market.requests, store.path)))
        return 0
    except SteamError as error:
        print(style.red("Steam error: %s" % error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(style.dim("\ninterrupted"), file=sys.stderr)
        return 130
    finally:
        if args.keep_days:
            store.prune(args.keep_days, now=now)
        store.close()


# -------------------------------------------------------------- other commands
def cmd_history(args: argparse.Namespace) -> int:
    style = Style(sys.stdout.isatty() and not args.no_color)
    with PriceStore(args.db) as store:
        points = store.series(args.app, args.name, args.currency)
        if not points:
            print("No snapshots for %r yet." % args.name)
            return 1
        prices = [price for _ts, price, _lots in points]
        low, high = min(prices), max(prices)
        flat = high == low
        span = (high - low) or 1.0
        print(style.bold(args.name))
        for ts, price, lots in points:
            filled = 32 if flat else int(round((price - low) / span * 32))
            bar = "█" * filled + "·" * (32 - filled)
            print(
                "%s  %s  %s  %s"
                % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
                    money(price, args.currency).rjust(12),
                    style.dim(bar),
                    style.dim("%d lots" % lots),
                )
            )
        print(style.dim("min %s · max %s · %d snapshots" % (money(low, args.currency), money(high, args.currency), len(points))))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with PriceStore(args.db) as store:
        rows, names, first, last = store.stats()
        print("database : %s" % store.path)
        print("snapshots: %d" % rows)
        print("items    : %d" % names)
        if first and last:
            print("range    : %s → %s" % (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(first)),
                time.strftime("%Y-%m-%d %H:%M", time.localtime(last)),
            ))
    return 0


def cmd_apps(_args: argparse.Namespace) -> int:
    print("Supported apps (--app):")
    for app_id, app in APPS.items():
        print("  %-7d %-16s fee %.0f%%" % (app_id, app["name"], float(app["fee"]) * 100))
    print("\nCommon currencies (--currency):")
    common = [1, 2, 3, 5, 18, 20, 21, 37]
    print("  " + "  ".join("%d=%s" % (code, CURRENCIES[code]) for code in common if code in CURRENCIES))
    return 0


# ------------------------------------------------------------------ arg parsing
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=os.environ.get("SKINWATCH_DB", DEFAULT_DB),
        help="SQLite price journal (default: %(default)s)",
    )
    common.add_argument(
        "--currency",
        type=int,
        default=int(os.environ.get("SKINWATCH_CURRENCY", 1)),
        help="Steam currency id: 1=USD 3=EUR 5=RUB 18=UAH 37=KZT (default: %(default)s)",
    )
    common.add_argument("--no-color", action="store_true", help="disable ANSI colors")

    parser = argparse.ArgumentParser(
        prog="skinwatch",
        description="Finds Steam Market skins whose price just dropped below what they normally sell for.",
        epilog="Run without a command to scan with the defaults.",
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", parents=[common], help="scan the market and print the best buys")
    scan.add_argument("--app", default="730", help="app id or comma separated ids (default: 730 = CS2)")
    scan.add_argument(
        "--pages",
        type=int,
        default=20,
        help="market search pages; Steam caps a page at 10 items (default: %(default)s)",
    )
    scan.add_argument(
        "--sort",
        choices=("quantity", "price", "default", "name"),
        default="quantity",
        help="how the market pages are ordered before scanning (default: %(default)s = most liquid first)",
    )
    scan.add_argument("--query", default="", help="only items whose name matches, e.g. \"AK-47\"")
    scan.add_argument("--top", type=int, default=10, help="how many skins to show (default: %(default)s)")
    scan.add_argument(
        "--enrich",
        type=int,
        default=40,
        help="how many shortlisted items to verify via priceoverview, 0 = skip (default: %(default)s)",
    )
    scan.add_argument("--window", type=float, default=7.0, help="days of own history used as baseline (default: %(default)s)")
    scan.add_argument("--min-price", type=float, default=0.5, help="ignore items cheaper than this")
    scan.add_argument("--max-price", type=float, default=500.0, help="ignore items pricier than this")
    scan.add_argument("--min-listings", type=int, default=10, help="minimum copies on sale")
    scan.add_argument("--min-volume", type=int, default=5, help="minimum sales per 24h when known")
    scan.add_argument("--min-drop", type=float, default=8.0, help="minimum %% below the reference price")
    scan.add_argument("--min-margin", type=float, default=3.0, help="minimum net %% profit after fees")
    scan.add_argument("--fee", type=float, default=None, help="market fee as a fraction (default: per app, 0.15)")
    scan.add_argument(
        "--delay",
        type=float,
        default=4.0,
        help="seconds between requests; Steam allows roughly 20/min across market endpoints (default: %(default)s)",
    )
    scan.add_argument(
        "--transport",
        choices=("auto", "curl", "urllib"),
        default="auto",
        help="HTTP backend; curl is preferred because Steam 429s some Python TLS stacks (default: %(default)s)",
    )
    scan.add_argument("--keep-days", type=float, default=90.0, help="drop snapshots older than this (0 = keep everything)")
    scan.add_argument("--no-save", action="store_true", help="do not write this scan into the journal")
    scan.add_argument("--links", action="store_true", help="print market links under the table")
    scan.add_argument("--json", action="store_true", help="machine readable output")
    scan.add_argument("--quiet", "-q", action="store_true", help="no progress on stderr")
    scan.set_defaults(func=cmd_scan)

    history = sub.add_parser("history", parents=[common], help="show stored price history for one item")
    history.add_argument("name", help="exact market_hash_name")
    history.add_argument("--app", type=int, default=730)
    history.set_defaults(func=cmd_history)

    stats = sub.add_parser("stats", parents=[common], help="what is inside the price journal")
    stats.set_defaults(func=cmd_stats)

    apps = sub.add_parser("apps", parents=[common], help="supported apps and currencies")
    apps.set_defaults(func=cmd_apps)
    return parser


COMMANDS = ("scan", "history", "stats", "apps")


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments or (arguments[0] not in COMMANDS and arguments[0] not in ("-h", "--help")):
        arguments = ["scan"] + arguments  # bare `skinwatch --top 5` means `scan`
    args = build_parser().parse_args(arguments)
    return args.func(args)
