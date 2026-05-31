# AGENTS.md

## What this is
Portfolio tracker: fetches A-stock/HK/ETF/fund prices via AKShare, syncs to Feishu Base tables. Single `skill/scripts/` package, no monorepo.

## Setup
- `pip install akshare pandas pytest`
- `lark-cli auth login --domain base` before first run
- Tests mock all network/subprocess — no external deps needed
- No CI, formatter, or typecheck; only pytest

## Commands

```bash
# Tests (PYTHONPATH required)
PYTHONPATH=skill/scripts pytest tests/ -v
PYTHONPATH=skill/scripts pytest tests/test_watchlist.py::TestClassifyCode -v

# CLI tools
python skill/scripts/watchlist.py                                    # prints to stdout
python skill/scripts/crawler.py --codes '["sz000333","hk00700"]'     # JSON → stdout
echo '["sz000333"]' | python skill/scripts/crawler.py                # stdin mode

# Feishu sync
python skill/scripts/feishu_sync.py [--dry-run|--force|--verify] [--rate-limit 1.5] [--on-error abort] [--quiet]

# Config init (auto-discovers tables from Feishu API by Chinese name)
python skill/scripts/feishu_config.py [--dry-run]
```

## Architecture

```
skill/scripts/
├── watchlist.py        # Core: fetch_*_data() + query_*() + HoldingItem — single source of truth
├── crawler.py          # JSON wrapper: crawl(codes) → [{code, price, change_pct}]; handles TTL cache
├── feishu_base.py      # LarkClient: lark-cli wrapper (rate-limit retry, encoding fallback, batch upsert)
├── feishu_sync.py      # 8-step sync: Watchlist(1-4) → Holdings(5-7) → Cash(8)
└── feishu_config.py    # Config/cache: lazy-loaded __getattr__ exports; ~/.config/.../config.json
                        #   Also: load_price_cache / save_price_cache / is_cache_valid
skill/assets/config.json  # Git-tracked: upsert_delay (0.8), cache_ttl_seconds (120)
tests/                     # pytest; conftest.py writes fake ~/.config/feishu-ledger/config.json on import
```

## Config & Cache

| Path | Content | Created by |
|------|---------|-----------|
| `~/.config/feishu-ledger/config.json` | base_token, table IDs, field IDs | `feishu_config.py` auto-generates |
| `~/.cache/feishu-ledger/price_cache.json` | `{prices: {code: {…}}, timestamp}` | `crawler.crawl()` on each run |
| `skill/assets/config.json` | `upsert_delay`, `cache_ttl_seconds` | Git-tracked, edit manually |

`feishu_config` module is lazy-loaded: `_ensure_loaded()` on first `__getattr__`. If config file missing, it auto-fetches from Feishu API (requires `FEISHU_BASE_TOKEN` env). Access `feishu_config.raw` for the full dict.

## Key Data Flow
1. `feishu_sync.py` imports `crawler.crawl(codes)` which calls `watchlist.fetch_*_data()` + `watchlist.query_*()`
2. `crawl()` checks TTL cache first; saves cache after fetching
3. `feishu_sync` writes to Feishu via `LarkClient.upsert_batch()` — single-record upsert with delay between

## Env Vars (override config)

| Variable | Required | Notes |
|----------|----------|-------|
| `FEISHU_BASE_TOKEN` | **Yes** | No fallback if config missing |
| `FEISHU_WATCHLIST_TABLE_ID` | No | Overrides discovered table |
| `FEISHU_HOLDINGS_TABLE_ID` | No | Overrides discovered table |
| `FEISHU_TRADE_TABLE_ID` | No | Overrides discovered table |
| `FEISHU_CASH_TABLE_ID` | No | Cash table (optional) |
| `FEISHU_UPSERT_DELAY` | No | Overrides config (default 0.8) |

## Feishu Table Name Matching (feishu_config.py)

`_fetch_all_from_feishu()` matches tables by Chinese name:
- "自选" / "watchlist" → watchlist
- "持仓" / "holdings" → holdings
- "交易" / "trade" → trade
- "现金" / "cash" → cash

## Code Prefix Rules

| Pattern | Type |
|---------|------|
| `sz`/`sh` + 6 digits in A-stock ranges | A-stock |
| `sz`/`sh` + 6 digits outside ranges | ETF |
| `hk` + 5 digits | HK stock |
| No prefix, 6 digits | LOF / open-end fund |

A-stock ranges: SZ `000/001/002/003/300/301`; SH `600/601/603/605/688`.

## Data Source Fallback

| Category | Primary → Fallback |
|----------|-------------------|
| A-stock, HK, ETF | Tencent API (`qt.gtimg.cn`) → AKShare Sina |
| Open-end funds | East Money full-market (`fund_open_fund_daily_em`) → per-fund (`fund_open_fund_info_em`) |

## Incremental Sync Logic

- Watchlist: skips records where `更新日期 >= today` unless `--force`
- Holdings: recalculates only when `|new_value - old_value| > 0.01`; uses cache prices if watchlist fetch skipped
- Cash: read-only summary, no writes

## Windows Quirks
- lark-cli path: `shutil.which("lark-cli.cmd")` resolves to `lark-cli.cmd`
- `feishu_base.py`: subprocess WITHOUT `shell=True` (avoids JSON `{}` shell parsing); writes inline JSON to temp files (`--json @tmp.json`)
- `feishu_config.py`: subprocess WITH `" ".join(cmd)` and `shell=True`
- Encoding fallback for lark output: UTF-8 → GBK → GB2312 → UTF-16 → replace

## Test Setup

`tests/conftest.py`:
- Writes fake `~/.config/feishu-ledger/config.json` on import
- Directly patches `feishu_config._cached_config` with fake field IDs
- Ensures isolation: no real Feishu API calls during tests

## Gotchas

- **Don't call `watchlist.main()`** from other scripts — use `fetch_*_data()` + `query_*()` directly
- **Tests need `PYTHONPATH=skill/scripts`** or `sys.path.insert(0, ...)` (tests use the latter)
- **基金代码无前缀**（纯6位数字），与股票代码区分
- **429 rate-limit**: auto-retry once with doubled delay; still fails → `RuntimeError`
- **Graceful shutdown**: `setup_signal_handlers()` sets `_interrupted` flag — Ctrl+C / SIGTERM stops after current batch
- **`venv/` and `.env` are gitignored** — don't commit them
- **Performance bottleneck**: `fund_open_fund_daily_em()` fetches all 23k+ funds (~21s). See `doc/todo.md` for planned optimizations (parallel fetch, `fund_open_fund_rank_em`)
