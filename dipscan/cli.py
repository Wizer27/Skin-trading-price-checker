"""Command line front-end: `python3 -m dipscan [crypto|stocks|all]`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Dict, List, Optional, Sequence

from . import crypto, stocks
from .analyze import Dip, Filters, rank
from .http import Client, FetchError
from .models import Asset

WINDOWS = {"crypto": "7 days, hourly", "stock": "daily candles"}


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


def fmt_price(value: float) -> str:
    if value >= 1000:
        return format(value, ",.0f")
    if value >= 1:
        return "%.2f" % value
    if value >= 0.01:
        return "%.4f" % value
    return "%.6f" % value


def fmt_pct(value: Optional[float]) -> str:
    return "-" if value is None else "%+.1f%%" % value


def fmt_money(value: Optional[float]) -> str:
    if not value:
        return "-"
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= size:
            return "%.1f%s" % (value / size, unit)
    return "%.0f" % value


def render_table(dips: Sequence[Dip], style: Style) -> str:
    headers = ["#", "Asset", "Price", "24h", "7d", "From high", "Z", "Vol×", "To median", "Turnover"]
    rows: List[List[str]] = []
    for index, dip in enumerate(dips, 1):
        asset = dip.asset
        rows.append(
            [
                str(index),
                clip("%s · %s" % (asset.symbol, asset.name), 34),
                fmt_price(asset.price),
                fmt_pct(asset.change_24h),
                fmt_pct(asset.change_7d),
                "-%.1f%%" % dip.drawdown,
                "%+.1f" % dip.z,
                "%.1f" % dip.vol_ratio if dip.vol_ratio else "-",
                "%+.1f%%" % dip.to_median,
                fmt_money(asset.money_volume),
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


def to_dict(dip: Dip) -> dict:
    asset = dip.asset
    return {
        "kind": asset.kind,
        "symbol": asset.symbol,
        "name": asset.name,
        "currency": asset.currency,
        "price": asset.price,
        "change_24h": asset.change_24h,
        "change_7d": asset.change_7d,
        "drawdown_pct": round(dip.drawdown, 2),
        "z_score": round(dip.z, 2),
        "abnormality": round(dip.abnormality, 3),
        "volume_ratio": round(dip.vol_ratio, 2) if dip.vol_ratio else None,
        "to_median_pct": round(dip.to_median, 2),
        "turnover": asset.money_volume,
        "score": round(dip.score, 2),
        "points": len(asset.prices),
        "url": asset.url,
    }


def collect(args: argparse.Namespace, style: Style) -> List[Asset]:
    assets: List[Asset] = []

    if args.command in ("crypto", "all"):
        client = Client(delay=args.delay, transport=args.transport, headers=crypto.HEADERS)
        log("• crypto: CoinGecko, top %d by market cap…" % args.top_coins, args.quiet)
        coins = crypto.fetch(client, top=args.top_coins, currency=args.vs.lower())
        log("  %d coins, %d requests" % (len(coins), client.requests), args.quiet)
        assets.extend(coins)

    if args.command in ("stocks", "all"):
        symbols = stocks.parse_symbols(args.symbols)
        client = Client(delay=args.stock_delay, transport=args.transport, headers=stocks.HEADERS)
        log(
            "• stocks: Yahoo Finance, %d tickers (~%.0f s)…"
            % (len(symbols), len(symbols) * args.stock_delay),
            args.quiet,
        )

        def progress(number: int, symbol: str, error: Optional[str]) -> None:
            if error:
                log("  ! %s" % error, args.quiet)
            elif number % 20 == 0:
                log("  …%d/%d" % (number, len(symbols)), args.quiet)

        shares = stocks.fetch(client, symbols, days=args.days, on_item=progress)
        log("  %d tickers, %d requests" % (len(shares), client.requests), args.quiet)
        assets.extend(shares)

    return assets


def cmd_scan(args: argparse.Namespace) -> int:
    style = Style(sys.stdout.isatty() and not args.no_color and not args.json)
    filters = Filters(
        min_drop=args.min_drop,
        max_z=args.max_z,
        min_money_volume=args.min_turnover,
        min_price=args.min_price,
        max_price=args.max_price,
    )
    try:
        assets = collect(args, style)
    except FetchError as error:
        print(style.red("fetch failed: %s" % error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(style.dim("\ninterrupted"), file=sys.stderr)
        return 130

    dips = rank(assets, filters, limit=args.top)

    if args.json:
        print(
            json.dumps(
                {
                    "generated_at": int(time.time()),
                    "universe": len(assets),
                    "results": [to_dict(dip) for dip in dips],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not dips:
        print(style.dim("Nothing fell that far — try --min-drop 3 or a lower --min-turnover."))
        return 0

    print()
    print(style.bold("Top %d assets trading well below their recent range" % len(dips)))
    print(
        style.dim(
            "score = depth of the fall × how abnormal it is · z ≤ %.1f required · "
            "window: crypto 7d hourly, stocks %dd daily" % (args.max_z, args.days)
        )
    )
    print()
    print(render_table(dips, style))
    if args.links:
        print()
        for index, dip in enumerate(dips, 1):
            print(style.dim("%2d. %s" % (index, dip.asset.url)))
    print()
    print(style.dim("scanned %d assets · a falling price is information, not a discount" % len(assets)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--top", type=int, default=10, help="how many assets to show (default: %(default)s)")
    common.add_argument("--min-drop", type=float, default=5.0, help="minimum %% below the window high (default: %(default)s)")
    common.add_argument(
        "--max-z",
        type=float,
        default=-1.0,
        help="price must sit at least this many sigma below its window mean (default: %(default)s)",
    )
    common.add_argument(
        "--min-turnover",
        type=float,
        default=5_000_000.0,
        help="minimum money traded per day (default: %(default)s)",
    )
    common.add_argument("--min-price", type=float, default=0.0)
    common.add_argument("--max-price", type=float, default=float("inf"))
    common.add_argument("--top-coins", type=int, default=250, help="how deep into the market-cap list to go (default: %(default)s)")
    common.add_argument("--vs", default="usd", help="quote currency for crypto (default: %(default)s)")
    common.add_argument("--symbols", default=None, help="stock tickers, comma or space separated (default: a built-in liquid list)")
    common.add_argument("--days", type=int, default=90, help="stock history window in days (default: %(default)s)")
    common.add_argument("--delay", type=float, default=1.5, help="seconds between CoinGecko requests (default: %(default)s)")
    common.add_argument("--stock-delay", type=float, default=0.5, help="seconds between Yahoo requests (default: %(default)s)")
    common.add_argument("--transport", choices=("auto", "curl", "urllib"), default="auto")
    common.add_argument("--links", action="store_true", help="print a link per row")
    common.add_argument("--json", action="store_true", help="machine readable output")
    common.add_argument("--no-color", action="store_true")
    common.add_argument("--quiet", "-q", action="store_true", help="no progress on stderr")

    parser = argparse.ArgumentParser(
        prog="dipscan",
        description="Finds coins and stocks trading unusually far below their own recent range.",
        epilog="Run without a command to scan crypto.",
    )
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("crypto", "scan coins (CoinGecko, 1 request per 250)"),
        ("stocks", "scan tickers (Yahoo Finance, 1 request each)"),
        ("all", "scan both"),
    ):
        command = sub.add_parser(name, parents=[common], help=help_text)
        command.set_defaults(func=cmd_scan, command=name)
    return parser


COMMANDS = ("crypto", "stocks", "all")


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments or (arguments[0] not in COMMANDS and arguments[0] not in ("-h", "--help")):
        arguments = ["crypto"] + arguments  # bare `dipscan --top 5` scans crypto
    args = build_parser().parse_args(arguments)
    return args.func(args)
