# dipscan

A terminal screener that finds **coins and stocks trading unusually far below their own
recent range** — not simply "what fell the most", but "what fell more than is normal for
this particular asset".

* no dependencies — Python 3.8+ standard library only;
* no API keys, no signup: CoinGecko and Yahoo Finance serve this data publicly;
* stateless — run it, read the table, done; `--json` for scripts;
* `--watch` keeps it on screen and updates the printed table in place.

```
$ python3 -m dipscan

Top 8 assets trading well below their recent range
score = depth of the fall × how abnormal it is · z ≤ -1.0 required · window: crypto 7d hourly

#  Asset                    Price     24h     7d  From high     Z  Vol×  To median  Turnover
─  ──────────────────────  ──────  ──────  ─────  ─────────  ────  ────  ─────────  ────────
1  SPX · SPX6900           0.4344  -18.2%  -1.7%     -19.0%  -2.4     -     +12.6%     25.9M
2  ASTER · Aster           0.6895   -5.2%  +0.9%     -12.3%  -2.6     -      +7.2%    169.3M
3  TRUMP · Official Trump    1.98   -9.0%  +7.7%     -12.7%  -1.0     -      +5.0%    443.7M
```

## Why not just "it dropped"

A 40% fall means nothing if the asset routinely swings 40% a week. So the price is judged
against its own spread over the window:

```
From high  how far below the window's high it trades      (depth)
Z          standard deviations below the window mean      (abnormality)
Vol×       last session's volume ÷ the window's typical   (confirmation)
To median  how far it must rise to reach the window median (room to recover)

score = From high × min(1, −Z / 2.5) × volume bonus (≤15%)
```

The important part: **abnormality has no floor.** An asset that doubled and gave half of it
back sits 50% below its high while resting exactly on its own mean — that is noise, not a
dip, and it scores zero. A `--max-z -1.0` filter backs this up: the price must be at least
one sigma below the mean to be listed at all.

## Sources and what a scan costs

| Source | What it gives | Requests |
|---|---|---|
| CoinGecko `/coins/markets` | 250 coins: price, turnover, 24h/7d change, 167 hourly points | **1** per 250 coins |
| Yahoo Finance `/v8/finance/chart` | daily candles and volumes for one ticker | 1 per ticker |

That is why crypto is scanned wholesale in a couple of seconds while stocks go through a
ticker list — 74 liquid US names and ETFs by default, about 40 seconds.

## Watch mode

```bash
python3 -m dipscan crypto --watch              # refresh every 60s
python3 -m dipscan crypto --watch 30 --top 15
python3 -m dipscan stocks --watch 300          # 74 requests per pass; no point going faster
python3 -m dipscan crypto --watch 30 --no-clear  # append blocks instead of updating in place
python3 -m dipscan crypto --watch 60 --json      # one JSON document per refresh
```

The table is printed once; after that only its own lines are rewritten — the cursor moves
back up by the height of the block and each line is cleared and redrawn. The screen is
never cleared, so whatever scrolled above stays put and nothing flickers. The bottom line
carries a countdown that ticks once a second, and a **Δ** column shows what happened to
each price since the previous refresh (`new` means the asset just entered the top):

```
#  Asset                 Price     24h      7d  From high     Z  To median  Turnover       Δ
1  龙虾 · 龙虾 (Lobster)  0.1595  +15.9%  -23.6%     -44.9%  -1.8     +42.7%     28.5M  +0.28%
3  CASHCAT · Cash Cat    0.1602   -4.4%   +1.5%     -30.6%  -1.2     +12.6%     17.4M  +0.05%
```

Column widths only ever grow between refreshes, so the table never shifts sideways, and
names with CJK characters or emoji are measured in terminal cells rather than code points,
so rows stay aligned.

Choosing an interval: going below 60 seconds for crypto is pointless — CoinGecko serves a
cached response and the Δ column fills up with `=`. Stocks cost one request per ticker and
a pass takes about 40 seconds, so use `--watch 300` or narrow the list with `--symbols`. If
a pass outlasts the interval the next one starts immediately. A failed refresh (throttling,
network) does not end the loop: the message appears inside the frame, the last good table
stays on screen, and work continues on the next pass.

## Usage

```bash
python3 -m dipscan                            # crypto, top 10
python3 -m dipscan crypto --top 20 --links
python3 -m dipscan stocks                     # the built-in ticker list
python3 -m dipscan stocks --symbols "AAPL,MSFT,NVDA,TSLA" --days 180
python3 -m dipscan all --min-drop 8           # both, in one table
python3 -m dipscan crypto --vs eur
python3 -m dipscan --json > dips.json
```

`crypto` is the default command, so `python3 -m dipscan --top 5` works too.

### Flags

| Flag | What it does | Default |
|---|---|---|
| `--top` | how many rows to print | `10` |
| `--min-drop` | minimum % below the window high | `5` |
| `--max-z` | price must sit at least this many sigma below the window mean | `-1.0` |
| `--min-turnover` | minimum money traded per day | `5000000` |
| `--min-price` / `--max-price` | price range | `0` / `∞` |
| `--top-coins` | how deep into the market-cap list to go | `250` |
| `--vs` | quote currency for crypto | `usd` |
| `--symbols` | stock tickers, comma or space separated | built-in list of 74 |
| `--days` | stock history window, days | `90` |
| `--delay` / `--stock-delay` | seconds between requests | `1.5` / `0.5` |
| `--watch [SECONDS]` | stay running, refreshing the table | off (60s if bare) |
| `--cycles` | stop after N refreshes (0 = until Ctrl+C) | `0` |
| `--no-clear` | in `--watch`, append blocks instead of updating in place | — |
| `--transport` | `auto` / `curl` / `urllib` | `auto` |
| `--links` | print a link under each row | — |
| `--json` | machine readable output | — |
| `--quiet` | no progress on stderr | — |

### Source quirks worth knowing

* **Yahoo answers 429 to a full browser `User-Agent`** containing Chrome — it expects a real
  browser carrying consent cookies. The client sends a plain `dipscan/0.1` instead; do not
  "improve" it into a browser string.
* **CoinGecko** without a key allows roughly 5–15 requests per minute. A crypto scan is a
  single request, so this is hard to hit; on a 429 the client backs off and retries.
* A renamed or delisted ticker answers 404 (the default list had `SQ`, now `XYZ`). Those are
  skipped without counting against the "the endpoint is unhappy, stop" breaker.
* Requests go through `curl` by default because some Python TLS fingerprints get filtered at
  the CDN; `--transport urllib` switches to the standard library.

## Development

```bash
python3 -m unittest discover -s tests -v   # 18 tests, fully offline
```

```
dipscan/http.py      transport (curl/urllib), throttling, retries
dipscan/models.py    Asset — the one shape every source produces
dipscan/crypto.py    CoinGecko
dipscan/stocks.py    Yahoo Finance
dipscan/analyze.py   dip metrics and ranking
dipscan/cli.py       arguments, table, live block
```

Adding a source means writing one `fetch(client, …) -> List[Asset]`; metrics and output need
no changes.

## Disclaimer

This is a screener, not investment advice. An abnormal fall is a reason to look up what
happened, not a reason to buy: there may be news, a delisting, a split, a dividend gap or
dilution behind it, and nothing promises a return to the median.
