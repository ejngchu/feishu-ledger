---
name: feishu-ledger
description: "持仓记录工具：通过 AKShare 爬取 A 股、港股、ETF、开放式基金的实时价格，同步到飞书多维表格。当用户要查询持仓实时行情、或将行情数据写入飞书表格时调用。配置自动初始化，无需手动编辑。"
metadata:
  requires:
    bins: ["lark-cli"]
    python_pkgs: ["akshare", "pandas", "pytest"]
---

# feishu-ledger

## 何时使用

- 用户说「查（一下/今日）行情」「看（一下）涨跌」「更新价格/净值」
- 用户说「更新自选（表）」「更新持仓（表）」「同步到飞书」
- 用户要查看持仓市值、持有收益、持有收益率
- 用户要查某个代码（如 sz000333 / hk00700）的最新价

> **分流规则：** 管理飞书表结构（建表、字段、视图）→ `lark-base` skill；直接调 AKShare 查单只股票/基金财务数据→ 不走本 skill。

## 前置条件

```bash
# 先检测是否已授权
if lark-cli auth status >nul 2>&1; then
  echo "lark-cli 已授权，跳过登录"
else
  # 首次使用必须认证
  lark-cli auth login --domain base
fi

# 如 ~/.config/feishu-ledger/config.json 不存在，自动初始化
python scripts/feishu_config.py
```

## 命令

```bash
# 行情查询
python scripts/watchlist.py                       # 终端输出
python scripts/crawler.py --codes '["sz000333"]'  # JSON 输出
echo '["sz000333"]' | python scripts/crawler.py   # stdin 模式

# 飞书同步（必须先用 lark-cli auth login）
python scripts/feishu_sync.py                     # 增量（更新日期 < today）
python scripts/feishu_sync.py --dry-run           # 预览
python scripts/feishu_sync.py --force             # 强制更新所有
python scripts/feishu_sync.py --verify            # 校验字段 ID
python scripts/feishu_sync.py --quiet             # 静默模式

# 配置修复（字段 ID 不同步时运行）
python scripts/feishu_config.py [--dry-run]
```

**导入式调用**：
```python
from watchlist import fetch_stock_a_data, query_stock_a, HoldingItem, classify_code
from crawler import crawl          # crawl(codes) → [{code, price, change_pct, date}]
from feishu_base import LarkClient # LarkClient(token).get_records/upsert_batch
```
需设置 `PYTHONPATH=.` 或 `sys.path.insert(0, ".")`。

## 数据源

| 品种 | 主数据源 | 故障转移 |
|------|---------|---------|
| A 股 / 港股 / ETF | 腾讯行情 `qt.gtimg.cn` | AKShare Sina |
| 开放式基金 | 东方财富全市场 | 逐只查询 `fund_open_fund_info_em` |

## 代码前缀

| 前缀格式 | 品种 |
|---------|------|
| `sz`/`sh` + 6 位（A 股码段内） | A 股 |
| `sz`/`sh` + 6 位（码段外） | ETF |
| `hk` + 5 位 | 港股 |
| 无前缀，6 位 | LOF / 开放式基金 |

A 股码段：SZ `000/001/002/003/300/301`，SH `600/601/603/605/688`

## 关键规则

- **增量更新**：不传 `--force` 时仅更新 `更新日期 < today` 的记录；持仓仅在新价格变动 > 0.01 时重算
- **价格缓存**：`crawler.crawl()` 默认走 TTL 缓存（120s），路径 `~/.cache/feishu-ledger/price_cache.json`；缓存命中直接返回，不调 API
- **基金动态列名**：`fund_open_fund_daily_em()` 日期列名运行时解析（如 `2026-05-29-单位净值`）；如 `日增长率` 为空则 fallback `fund_open_fund_info_em` 逐只取
- **lark-cli 编码**：UTF-8 → GBK 回退；Windows 自动查找 `lark-cli.cmd`
- **429 限流**：自动重试一次（延迟翻倍）；仍失败抛出 `RuntimeError`
- **优雅退出**：Ctrl+C / SIGTERM 设 `_interrupted`，当前批次完成后退出

## 配置

| 路径 | 内容 | 生成方式 |
|------|------|---------|
| `~/.config/feishu-ledger/config.json` | base_token, table/field IDs | `feishu_config.py` 自动生成 |
| `~/.cache/feishu-ledger/price_cache.json` | 价格缓存（TTL 120s） | `crawler.crawl()` 自动写入 |
| `assets/config.json` | `upsert_delay`, `cache_ttl_seconds` | git 跟踪，手动编辑 |

环境变量可覆盖 config：`FEISHU_BASE_TOKEN`、`FEISHU_WATCHLIST_TABLE_ID`、`FEISHU_HOLDINGS_TABLE_ID`、`FEISHU_TRADE_TABLE_ID`、`FEISHU_CASH_TABLE_ID`、`FEISHU_UPSERT_DELAY`。

## 常见错误与恢复

| 错误 | 原因 | 恢复 |
|------|------|------|
| 字段 ID 不匹配 | Feishu 表结构变更 | `python scripts/feishu_config.py` 重新同步 |
| 基金净值 `-` | 日增长率为空 | 自动 fallback 逐只查询，无需人工干预 |
| 429 限流 | 写入过快 | 自动重试或 `--rate-limit 1.5` 增大间隔 |
| lark-cli 找不到 | 未安装或 PATH 不包含 | `npm install -g @lark-opdev/lark-cli`；Windows 检测 `lark-cli.cmd` |
| config.json 不存在 | 首次运行 | 设置 `FEISHU_BASE_TOKEN` 环境变量，运行 `feishu_config.py` |
