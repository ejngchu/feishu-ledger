# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Portfolio tracking utilities using AKShare to fetch real-time prices for A-shares, HK stocks, ETFs, and open-end funds. Outputs to terminal or syncs to a Feishu (Lark) Base spreadsheet.

## Commands

```bash
# Install dependencies
pip install akshare pandas pytest

# Core watchlist (hardcoded in RAW_WATCHLIST)
python watchlist.py

# Crawler module (JSON in/out) - supports --codes or stdin
python crawler.py --codes '["sz000333","hk00700","015600"]'
echo '["sz000333","hk00700"]' | python crawler.py

# Feishu sync - 自选表 + 持仓表一体化同步（先更新自选表，再计算并更新持仓表）
lark-cli auth login --domain base   # first-time only
python feishu_sync.py --dry-run     # preview
python feishu_sync.py                # execute (incremental update)
python feishu_sync.py --force       # force update all records
python feishu_sync.py --rate-limit 1.5   # custom write interval
python feishu_sync.py --on-error abort   # stop on error (default: skip)

# Run tests
pytest tests/ -v
pytest tests/test_watchlist.py::TestClassifyCode -v  # single test class
```

## Architecture

```
watchlist.py          # Core data fetching + HoldingItem dataclass
  ├── classify_code()     → categorizes raw code into stock_a/hk_stock/etf/fund
  ├── strip_prefix()      → removes prefix (sz/sh/hk) from code
  ├── to_float()          → safe float conversion (handles %, spaces, NaN)
  ├── fetch_*_data()     → per-category data fetchers (Tencent API → AKShare fallback)
  ├── query_*()          → per-category DataFrame matchers
  ├── HoldingItem        → dataclass for a single holding
  ├── retry()            → exponential backoff retry decorator (3 attempts)
  ├── with_timeout()     → cross-platform timeout decorator (thread-based)
  └── Logger             → timestamped logging class

crawler.py            # Thin wrapper around watchlist for JSON I/O
  ├── crawl(codes)    → returns [{code, name, matched, price, change_pct, date}]
  └── main()          → CLI with --codes argument or stdin

feishu_base.py        # Shared Feishu infrastructure
  ├── LarkClient           → lark-cli wrapper with rate-limit retry + encoding fallback
  │     ├── _run_lark()       → subprocess call with retry
  │     ├── _decode_output()  → UTF-8/GBK auto-detection
  │     ├── get_records()     → generic paginated reader for any table/fields
  │     └── upsert_record()   → generic upsert for any table/record
  ├── setup_signal_handlers() → SIGTERM/SIGINT graceful shutdown
  └── add_common_args()       → shared argparse flags (--dry-run, --force, etc.)

feishu_sync.py        # 自选表 + 持仓表一体化同步
                        # Step 1-4: read → filter → crawl → write 自选表
                        # Step 5-7: read 持仓表 → merge prices → calculate → write 持仓表

feishu_constants.py   # Shared config with table IDs, field IDs, and env var support:
                        # FEISHU_BASE_TOKEN, WATCHLIST_TABLE_ID, HOLDINGS_TABLE_ID, TRADE_TABLE_ID, UPSERT_DELAY
```

## Code Prefix Rules

| Prefix | Type |
|--------|------|
| `sz`/`sh` + 6 digits (in A-share range) | A-share stock |
| `sz`/`sh` + 6 digits (outside A-share range) | ETF |
| `hk` + 5 digits | HK stock |
| No prefix, 6 digits | LOF / open-end fund |

A-share code ranges: `000xxx`, `001xxx`, `002xxx`, `003xxx`, `300xxx`, `301xxx` (SZ); `600xxx`, `601xxx`, `603xxx`, `605xxx`, `688xxx` (SH).

## Data Source Fallback Chain

- **A-stock / HK-stock / ETF**: Tencent quote API (`qt.gtimg.cn`) → AKShare Sina
- **Open-end funds**: East Money full-market daily → East Money per-fund info API

## Key Implementation Notes

- `watchlist.py` is the single source of truth for data fetching logic — `crawler.py` and `feishu_sync.py` all import from it
- `crawler.py` suppresses `watchlist.py` stdout via `io.StringIO` redirection when calling `crawl()` to keep JSON output clean
- `feishu_base.py` provides `LarkClient` — the shared lark-cli wrapper used by `feishu_sync.py`
- Output encoding: tries UTF-8 first, falls back to GBK for lark-cli output
- Fund data from `fund_open_fund_daily_em()` has dynamic date-column names (e.g. `2026-05-15-单位净值`); `query_fund()` parses these at runtime
- Configuration supports environment variables: `FEISHU_BASE_TOKEN`, `FEISHU_WATCHLIST_TABLE_ID`, `FEISHU_HOLDINGS_TABLE_ID`, `FEISHU_UPSERT_DELAY`
- Network calls have retry with exponential backoff (3 attempts) and AKShare calls have 30s timeout
- `feishu_sync.py` handles graceful shutdown via SIGTERM/SIGINT and retries on 429 rate-limit
- `feishu_sync.py` supports incremental updates (only updates records with date older than today) and `--force` flag for full updates
- 持仓表计算: market value = price × shares; profit = market value - cost; profit% = profit / cost
- Tests use `pytest` with mocks for network/subprocess calls
