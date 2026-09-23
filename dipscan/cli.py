"""Command line front-end: `python3 -m dipscan [crypto|stocks|all]`."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import time
import unicodedata
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


def cell_width(text: str) -> int:
    """Terminal cells a string occupies: CJK and emoji take two, not one."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def clip(text: str, width: int) -> str:
    if cell_width(text) <= width:
        return text
    kept, used = [], 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + step > width - 1:
            break
        kept.append(char)
        used += step
    return "".join(kept) + "…"


def pad(text: str, width: int, left: bool = False, fill: str = " ") -> str:
    """ljust/rjust that counts terminal cells instead of code points."""
    room = max(0, width - cell_width(text))
    return text + fill * room if left else fill * room + text


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


def delta_cell(asset: Asset, previous: Dict[str, float]) -> str:
    """What happened to this asset since the previous refresh."""
    was = previous.get(asset.symbol)
    if was is None:
        return "new"
    if not was:
        return "-"
    change = (asset.price / was - 1.0) * 100.0
    if abs(change) < 0.01:
        return "="
    return "%+.2f%%" % change


def render_table(
    dips: Sequence[Dip],
    style: Style,
    previous: Optional[Dict[str, float]] = None,
    sticky: Optional[List[int]] = None,
) -> str:
    """`previous` maps symbol → price from the last refresh; given, a Δ column appears.

    `sticky` carries the widths of the previous frame and is updated in place: columns
    may only grow, so a live table does not shuffle sideways on every repaint.
    """
    headers = ["#", "Asset", "Price", "24h", "7d", "From high", "Z", "Vol×", "To median", "Turnover"]
    if previous is not None:
        headers.append("Δ".ljust(7))  # room for "+00.00%" so the column never resizes
    rows: List[List[str]] = []
    for index, dip in enumerate(dips, 1):
        asset = dip.asset
        row = [
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
        if previous is not None:
            row.append(delta_cell(asset, previous))
        rows.append(row)

    widths = [cell_width(head) for head in headers]
    for row in rows:
        for column, cell in enumerate(row):
            widths[column] = max(widths[column], cell_width(cell))
    if sticky is not None:
        for column, width in enumerate(sticky[: len(widths)]):
            widths[column] = max(widths[column], width)
        sticky[:] = widths

    def line(cells: Sequence[str], fill: str = " ") -> str:
        return "  ".join(
            pad(cell, widths[column], left=(column == 1), fill=fill) for column, cell in enumerate(cells)
        )

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


def collect(args: argparse.Namespace, style: Style, quiet: bool = False) -> List[Asset]:
    """`quiet` is forced on in watch mode: stray stderr lines would shift the live block."""
    assets: List[Asset] = []
    quiet = quiet or args.quiet

    if args.command in ("crypto", "all"):
        client = Client(delay=args.delay, transport=args.transport, headers=crypto.HEADERS)
        log("• crypto: CoinGecko, top %d by market cap…" % args.top_coins, quiet)
        coins = crypto.fetch(client, top=args.top_coins, currency=args.vs.lower())
        log("  %d coins, %d requests" % (len(coins), client.requests), quiet)
        assets.extend(coins)

    if args.command in ("stocks", "all"):
        symbols = stocks.parse_symbols(args.symbols)
        client = Client(delay=args.stock_delay, transport=args.transport, headers=stocks.HEADERS)
        log(
            "• stocks: Yahoo Finance, %d tickers (~%.0f s)…"
            % (len(symbols), len(symbols) * args.stock_delay),
            quiet,
        )

        def progress(number: int, symbol: str, error: Optional[str]) -> None:
            if error:
                log("  ! %s" % error, quiet)
            elif number % 20 == 0:
                log("  …%d/%d" % (number, len(symbols)), quiet)

        shares = stocks.fetch(client, symbols, days=args.days, on_item=progress)
        log("  %d tickers, %d requests" % (len(shares), client.requests), quiet)
        assets.extend(shares)

    return assets


def make_filters(args: argparse.Namespace) -> Filters:
    return Filters(
        min_drop=args.min_drop,
        max_z=args.max_z,
        min_money_volume=args.min_turnover,
        min_price=args.min_price,
        max_price=args.max_price,
    )


def print_json(assets: Sequence[Asset], dips: Sequence[Dip]) -> None:
    print(
        json.dumps(
            {
                "generated_at": int(time.time()),
                "universe": len(assets),
                "results": [to_dict(dip) for dip in dips],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


ANSI_RE = re.compile(r"\033\[[0-9;?]*[a-zA-Z]")


def visible_len(text: str) -> int:
    return cell_width(ANSI_RE.sub("", text))


def fit(text: str, width: int) -> str:
    """Keep a line inside the terminal: a wrapped line would shift the whole block."""
    if width <= 0 or visible_len(text) <= width:
        return text
    return ANSI_RE.sub("", text)[: max(0, width - 1)] + "…"


class LiveBlock:
    """Repaints a block of lines in place — cursor up, clear line, rewrite.

    Nothing is cleared beyond the block itself, so whatever scrolled above it stays
    readable and the table does not flicker the way a full-screen clear does.
    """

    def __init__(self, stream=None, enabled: bool = True) -> None:
        self.stream = stream or sys.stdout
        self.enabled = enabled
        self.height = 0

    def width(self) -> int:
        return shutil.get_terminal_size((120, 40)).columns if self.enabled else 0

    def draw(self, lines: Sequence[str]) -> None:
        width = self.width()
        lines = [fit(line, width) for line in lines]
        if not self.enabled:  # piped output: plain blocks, one after another
            self.stream.write("\n".join(lines) + "\n\n")
            self.stream.flush()
            return
        if self.height:
            self.stream.write("\033[%dA" % self.height)  # back to the top of the block
        self.height = max(self.height, len(lines))
        padded = list(lines) + [""] * (self.height - len(lines))
        self.stream.write("".join("\033[2K" + line + "\n" for line in padded))
        self.stream.flush()

    def update_last(self, line: str) -> None:
        """Rewrite only the bottom line — used for the countdown."""
        if not self.enabled or not self.height:
            return
        self.stream.write("\033[1A\033[2K" + fit(line, self.width()) + "\n")
        self.stream.flush()

    def hide_cursor(self) -> None:
        if self.enabled:
            self.stream.write("\033[?25l")
            self.stream.flush()

    def show_cursor(self) -> None:
        if self.enabled:
            self.stream.write("\033[?25h")
            self.stream.flush()


def report_lines(
    args: argparse.Namespace,
    assets: Sequence[Asset],
    dips: Sequence[Dip],
    style: Style,
    previous: Optional[Dict[str, float]] = None,
    sticky: Optional[List[int]] = None,
) -> List[str]:
    """The table as a list of lines — the live block needs to know its own geometry."""
    if not dips:
        return [style.dim("Nothing fell that far — try --min-drop 3 or a lower --min-turnover.")]

    lines = [
        style.bold("Top %d assets trading well below their recent range" % len(dips)),
        style.dim(
            "score = depth of the fall × how abnormal it is · z ≤ %.1f required · "
            "window: crypto 7d hourly, stocks %dd daily" % (args.max_z, args.days)
        ),
        "",
    ]
    lines.extend(render_table(dips, style, previous, sticky).split("\n"))
    if args.links:
        lines.append("")
        lines.extend(style.dim("%2d. %s" % (index, dip.asset.url)) for index, dip in enumerate(dips, 1))
    return lines


def run_once(args: argparse.Namespace, style: Style) -> int:
    assets = collect(args, style)
    dips = rank(assets, make_filters(args), limit=args.top)
    if args.json:
        print_json(assets, dips)
        return 0
    print()
    print("\n".join(report_lines(args, assets, dips, style)))
    print()
    print(style.dim("scanned %d assets · a falling price is information, not a discount" % len(assets)))
    return 0


def run_watch(args: argparse.Namespace, style: Style) -> int:
    """Rescan every `--watch` seconds, updating the printed table instead of reprinting it."""
    filters = make_filters(args)
    block = LiveBlock(enabled=style.enabled and not args.no_clear)
    previous: Dict[str, float] = {}
    sticky: List[int] = []
    body: List[str] = []
    cycle = 0

    block.hide_cursor()
    try:
        while True:
            cycle += 1
            started = time.monotonic()
            if body and block.enabled:
                block.draw(body + ["", style.dim("refreshing…")])

            error = ""
            try:
                assets = collect(args, style, quiet=True)
            except FetchError as exc:
                # A failed refresh is not the end of the loop: keep the last good table.
                assets, error = [], str(exc)

            if assets:
                dips = rank(assets, filters, limit=args.top)
                if args.json:
                    print_json(assets, dips)
                else:
                    body = report_lines(args, assets, dips, style, previous=previous, sticky=sticky)
                previous = {dip.asset.symbol: dip.asset.price for dip in dips}
                took = time.monotonic() - started
                status = "refresh #%d at %s · %d assets in %.0f s" % (
                    cycle,
                    time.strftime("%H:%M:%S"),
                    len(assets),
                    took,
                )
            else:
                status = style.red("refresh #%d failed: %s" % (cycle, error or "no data"))

            if args.cycles and cycle >= args.cycles:
                if not args.json:
                    block.draw(body + ["", style.dim(status)])
                return 0

            if args.json:
                time.sleep(max(0.0, args.watch - (time.monotonic() - started)))
                continue

            # Countdown: only the bottom line is rewritten once a second.
            def footer(seconds: float) -> str:
                return style.dim(
                    "%s · next in %d s · Ctrl+C to stop" % (status, max(0, int(math.ceil(seconds))))
                )

            block.draw(body + ["", footer(args.watch - (time.monotonic() - started))])
            while True:
                left = args.watch - (time.monotonic() - started)
                if left <= 0:
                    break
                time.sleep(min(1.0, left))
                remaining = args.watch - (time.monotonic() - started)
                if remaining > 0:
                    block.update_last(footer(remaining))
    finally:
        block.show_cursor()


def cmd_scan(args: argparse.Namespace) -> int:
    style = Style(sys.stdout.isatty() and not args.no_color and not args.json)
    try:
        if args.watch:
            return run_watch(args, style)
        return run_once(args, style)
    except FetchError as error:
        print(style.red("fetch failed: %s" % error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(style.dim("\nstopped"), file=sys.stderr)
        return 130


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
    common.add_argument(
        "--watch",
        nargs="?",
        type=float,
        const=60.0,
        default=None,
        metavar="SECONDS",
        help="keep running and refresh the table every SECONDS (default 60 when the flag is bare)",
    )
    common.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="stop after this many refreshes in --watch mode (0 = run until Ctrl+C)",
    )
    common.add_argument(
        "--no-clear",
        action="store_true",
        help="in --watch mode, print every refresh as a new block instead of updating in place",
    )
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
