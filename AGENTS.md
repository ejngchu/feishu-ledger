# AGENTS.md

## Setup & Prerequisites
- `pip install akshare pandas pytest`
- `lark-cli auth login --domain base` before first feishu_sync run
- Tests mock network/subprocess — no external deps needed

## Commands

```bash
pytest tests/ -v                                          # all tests
pytest tests/test_watchlist.py::TestClassifyCode -v        # single class

python watchlist.py                                        # hardcoded RAW_WATCHLIST
python crawler.py --codes '["sz000333","hk00700"]'         # JSON output
echo '["sz000333"]' | python crawler.py                    # stdin mode

# Feishu sync (Watchlist + Holdings一体化)
python feishu_sync.py [--dry-run|--force] [--rate-limit 1.5] [--on-error abort] [--quiet]
```

## Architecture

```
watchlist.py           # Data fetching + HoldingItem dataclass
  ├── classify_code()  → stock_a/hk_stock/etf/fund  (prefix-based)
  ├── strip_prefix()   → removes sz/sh/hk
  ├── fetch_*_data()  → per-category (Tencent → Sina fallback)
  ├── query_*()        → DataFrame matchers
  ├── retry()          → exponential backoff (3 attempts)
  ├── with_timeout()   → 30s timeout (thread-based, cross-platform)
  └── HoldingItem      → dataclass

crawler.py             # Thin wrapper around watchlist for JSON I/O
  └── crawl(codes)   → suppresses watchlist stdout, returns [{code, matched, price, change_pct}]

feishu_base.py         # Shared Feishu infrastructure
  ├── LarkClient          → lark-cli wrapper (rate-limit retry + encoding fallback)
  ├── setup_signal_handlers()  → SIGTERM/SIGINT graceful shutdown
  └── add_common_args()        → shared --dry-run/--force/--rate-limit flags

feishu_sync.py         # Watchlist + Holdings一体化同步
                        # Step 1-4: read → filter → crawl → write Watchlist
                        # Step 5-7: read Holdings → merge prices → calculate → write Holdings

feishu_constants.py    # Shared: table IDs, field IDs, env var defaults
```

## Data Source Fallback Chain

| Category | Primary → Fallback |
|----------|-------------------|
| A-stock, HK, ETF | Tencent API (`qt.gtimg.cn`) → AKShare Sina |
| Open-end funds | East Money full-market → East Money per-fund info |

## Key Patterns

- **watchlist.py is the single source of truth** — all other scripts import from it
- **`crawler.crawl(quiet=True)` by default** — redirects watchlist stdout to `io.StringIO`
- **lark-cli output**: tries UTF-8 first, falls back to GBK (`_decode_output`)
- **feishu_sync incremental**: skips records where `update_date >= today` unless `--force`
- **Holdings calculation**: market value = price × shares; profit = market value - cost; profit% = profit / cost
- **Graceful shutdown**: `SIGTERM`/`SIGINT` sets `_interrupted` flag
- **429 rate-limit**: auto-retry once with doubled delay
- **Fund date columns**: `fund_open_fund_daily_em()` uses dynamic columns like `2026-05-15-单位净值`; `query_fund()` parses at runtime

## Env Vars (code reads these names)

| Variable | Default (in code) |
|----------|-------------------|
| `FEISHU_BASE_TOKEN` | `FlZObdBVNawsG0s9GhHch2xDnAc` |
| `FEISHU_WATCHLIST_TABLE_ID` | `tblIP0LuVvZFMjZD` |
| `FEISHU_HOLDINGS_TABLE_ID` | `tblIqUClte8harRW` |
| `FEISHU_TRADE_TABLE_ID` | `tblkzlJG97qsMFfK` |
| `FEISHU_UPSERT_DELAY` | `0.8` |

## Code Prefix Rules

| Prefix/Format | Type |
|---------------|------|
| `sz`/`sh` + 6 digits in range `000/001/002/003/300/301` (SZ) or `600/601/603/605/688` (SH) | A-stock |
| `sz`/`sh` + 6 digits outside above ranges | ETF |
| `hk` + 5 digits | HK stock |
| No prefix, 6 digits | LOF / open-end fund |

## Gotchas

- Don't call `watchlist.main()` from other scripts — it prints to stdout; use `fetch_*_data()` + `query_*()` instead
- 基金代码无前缀（纯6位数字），与股票代码区分
- Tests use `sys.path.insert(0, ...)` pattern to import from parent dir
- Windows: lark-cli path may be `lark-cli.cmd`; scripts detect via `shutil.which()`
- `venv/` and `.env` are gitignored — don't commit them
