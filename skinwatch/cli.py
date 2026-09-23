"""Command line front-end: `python3 -m skinwatch`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Sequence

from .analyze import DEFAULT_FEE, Candidate, Filters, eligible, rank
from .steam import APPS, CURRENCIES, Overview, SteamError, SteamMarket


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
                "%+.1f%%" % -item.drop_pct,
                "%+.2f" % item.profit(fee),
                "%+.1f%%" % item.margin_pct(fee),
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
        "median_24h": round(item.reference, 2),
        "drop_pct": round(item.drop_pct, 2),
        "net_resale": round(item.net_resale(fee), 2),
        "profit": round(item.profit(fee), 2),
        "margin_pct": round(item.margin_pct(fee), 2),
        "score": round(item.score(fee), 2),
        "volume_24h": item.volume,
        "listings": item.listings,
        "url": market.market_url(item.app_id, item.name),
    }


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
        fee=args.fee if args.fee is not None else DEFAULT_FEE,
    )

    market = SteamMarket(
        currency=args.currency,
        delay=args.delay,
        transport=args.transport,
        cookie=os.environ.get("STEAM_COOKIE"),
    )
    found: List[Candidate] = []

    try:
        for app_id in app_ids:
            app = APPS.get(app_id, {})
            filters.fee = args.fee if args.fee is not None else float(app.get("fee", DEFAULT_FEE))
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

            picks = eligible(items, filters)[: args.check]
            log(
                "  %d items scanned, checking 24h medians for %d of them (~%.0f s)…"
                % (len(items), len(picks), len(picks) * args.delay),
                args.quiet,
            )

            overviews: Dict[str, Overview] = {}
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
                        log("  stopping here — raise --delay and retry later", args.quiet)
                        break
                    continue
                if not args.quiet and number % 10 == 0:
                    log("  …%d/%d" % (number, len(picks)), args.quiet)

            found.extend(rank(items, overviews, filters, limit=args.top))

        found.sort(key=lambda c: c.score(filters.fee), reverse=True)
        found = found[: args.top]

        if args.json:
            print(
                json.dumps(
                    {
                        "generated_at": int(time.time()),
                        "currency": CURRENCIES.get(args.currency, args.currency),
                        "fee": filters.fee,
                        "results": [to_dict(item, args.currency, filters.fee, market) for item in found],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if not found:
            print(
                style.dim(
                    "Nothing matched the filters — try --min-drop 5, --pages 40"
                    " or a wider --min-price/--max-price range."
                )
            )
            return 0

        print()
        print(style.bold("Top %d skins that dropped and look worth buying" % len(found)))
        print(style.dim("fee %.0f%% · 'usual' = median of what actually sold in the last 24h" % (filters.fee * 100)))
        print()
        print(render_table(found, args.currency, filters.fee, style))
        if args.links:
            print()
            for index, item in enumerate(found, 1):
                print(style.dim("%2d. %s" % (index, market.market_url(item.app_id, item.name))))
        print()
        print(style.dim("%d market requests" % market.requests))
        return 0
    except SteamError as error:
        print(style.red("Steam error: %s" % error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(style.dim("\ninterrupted"), file=sys.stderr)
        return 130


def cmd_apps(_args: argparse.Namespace) -> int:
    print("Supported apps (--app):")
    for app_id, app in APPS.items():
        print("  %-7d %-16s fee %.0f%%" % (app_id, app["name"], float(app["fee"]) * 100))
    print("\nCommon currencies (--currency):")
    common = [1, 2, 3, 5, 18, 20, 21, 37]
    print("  " + "  ".join("%d=%s" % (code, CURRENCIES[code]) for code in common if code in CURRENCIES))
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--currency",
        type=int,
        default=int(os.environ.get("SKINWATCH_CURRENCY", 1)),
        help="Steam currency id: 1=USD 3=EUR 5=RUB 18=UAH 37=KZT (default: %(default)s)",
    )
    common.add_argument("--no-color", action="store_true", help="disable ANSI colors")

    parser = argparse.ArgumentParser(
        prog="skinwatch",
        description="Finds Steam Market skins selling below the median of what they went for today.",
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
        "--check",
        type=int,
        default=40,
        help="how many of the scanned items get a median lookup (default: %(default)s)",
    )
    scan.add_argument("--min-price", type=float, default=0.5, help="ignore items cheaper than this")
    scan.add_argument("--max-price", type=float, default=500.0, help="ignore items pricier than this")
    scan.add_argument("--min-listings", type=int, default=10, help="minimum copies on sale")
    scan.add_argument("--min-volume", type=int, default=5, help="minimum sales per 24h")
    scan.add_argument("--min-drop", type=float, default=8.0, help="minimum %% below the median")
    scan.add_argument("--min-margin", type=float, default=3.0, help="minimum net %% profit after fees")
    scan.add_argument("--fee", type=float, default=None, help="market fee as a fraction (default: per app, 0.15)")
    scan.add_argument(
        "--delay",
        type=float,
        default=4.0,
        help="seconds between requests; Steam allows roughly 20/min (default: %(default)s)",
    )
    scan.add_argument(
        "--transport",
        choices=("auto", "curl", "urllib"),
        default="auto",
        help="HTTP backend; curl is preferred because Steam 429s some Python TLS stacks (default: %(default)s)",
    )
    scan.add_argument("--links", action="store_true", help="print market links under the table")
    scan.add_argument("--json", action="store_true", help="machine readable output")
    scan.add_argument("--quiet", "-q", action="store_true", help="no progress on stderr")
    scan.set_defaults(func=cmd_scan)

    apps = sub.add_parser("apps", parents=[common], help="supported apps and currencies")
    apps.set_defaults(func=cmd_apps)
    return parser


COMMANDS = ("scan", "apps")


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments or (arguments[0] not in COMMANDS and arguments[0] not in ("-h", "--help")):
        arguments = ["scan"] + arguments  # bare `skinwatch --top 5` means `scan`
    args = build_parser().parse_args(arguments)
    return args.func(args)
