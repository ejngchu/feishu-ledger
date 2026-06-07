---
name: feishu-ledger
description: "飞书持仓账本：AKShare 爬取 A 股/港股/ETF/基金实时价格同步到飞书多维表格，交易截图自动识别写入交易表并实时核算持仓表（以交易表为唯一数据源），定投截图自动合并到最新定投记录中，报告生成自动处理非交易日（不更新基准）。触发词：「查行情」「看涨跌」「更新自选表」「更新持仓表」「同步到飞书」「持仓市值」「持有收益」「新增交易」「录入交易」「记一笔交易」「卖出/买入」「交易截图」「分红」「持仓分红」「持仓报告」「今日报告」「持仓快照」「生成报告」「今日赚了多少钱」「查持仓」「看账户」「资产总览」「饼图」「更新定投交易」「定投截图」「发一张定投记录」。覆盖持仓表、自选表、交易表、现金表四表的读取写入联动。配置自动初始化，无需手动编辑。"
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
- 用户说「新增交易」「录入交易」「记一笔交易」「有一笔卖出/买入」
- 用户发送了**交易截图/确认单**（同花顺/支付宝/天天基金等）→ 写入交易表 + 自动核算持仓
- 用户说「更新定投交易」+ 发送定投截图 → 合并到最新定投记录
- 用户说「查分红」「近期有哪些股票分红」「持仓股有无分红」
- 用户说「报告」「持仓报告」「今日报告」「我今天赚了多少钱」→ 生成持仓报告

> **分流规则：** 管理飞书表结构（建表、字段、视图）→ `lark-base` skill；直接调 AKShare 查单只股票/基金财务数据→ 不走本 skill。
>
> **注意：**「查行情」默认同时执行行情爬取 + 飞书自选表同步（`feishu_sync.py`），无需用户额外要求。如只需终端预览行情，使用 `crawler.py`。

## 功能路由

| 功能块 | 触发关键词 | 详细工作流 |
|--------|----------|----------|
| 行情同步 | 「查行情」「看涨跌」「更新自选表」 | [watchlist-sync.md](references/watchlist-sync.md) |
| 交易记录录入 | 「新增交易」「记一笔交易」+ 交易截图 | [trade-write.md](references/trade-write.md) |
| 定投交易更新 | 「更新定投交易」+ 定投截图 | [dca-update.md](references/dca-update.md) |
| 持仓表全面同步 | 「更新持仓表」「重算持仓」 | [holdings-resync.md](references/holdings-resync.md) |
| 分红记录同步 | 「查分红」「近期有哪些股票分红」 | [dividend-sync.md](references/dividend-sync.md) |
| 持仓报告生成 | 「生成报告」「今日报告」「晚间报告」 | [report-generation.md](references/report-generation.md) |

## 凭证与环境

- Base URL: https://zcnnhdtqb3jk.feishu.cn/base/FlZObdBVNawsG0s9GhHch2xDnAc
- Base token: `FlZObdBVNawsG0s9GhHch2xDnAc`
- 自选表 table_id: `tblIP0LuVvZFMjZD`
- 持仓表 table_id: `tblIqUClte8harRW`
- 交易表 table_id: `tblkzlJG97qsMFfK`
- 现金表 table_id: `tblLxJaexFUr0hCP`
- 用户身份：`--as user`（默认）

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

## 代码前缀

| 前缀格式 | 品种 |
|---------|------|
| `sz`/`sh` + 6 位（A 股码段内） | A 股 |
| `sz`/`sh` + 6 位（码段外） | ETF |
| `hk` + 5 位 | 港股 |
| 无前缀，6 位 | LOF / 开放式基金 |

A 股码段：SZ `000/001/002/003/300/301`，SH `600/601/603/605/688`

## 数据源

| 品种 | 主数据源 | 故障转移 |
|------|---------|---------|
| A 股 / 港股 / ETF | 腾讯行情 `qt.gtimg.cn` | AKShare Sina |
| 开放式基金 | 东方财富全市场 | 逐只查询 `fund_open_fund_info_em` |

## 配置

| 路径 | 内容 | 生成方式 |
|------|------|---------|
| `~/.config/feishu-ledger/config.json` | base_token, table/field IDs | `feishu_config.py` 自动生成 |
| `~/.cache/feishu-ledger/price_cache.json` | 价格缓存（TTL 120s） | `crawler.crawl()` 自动写入 |
| `assets/config.json` | `upsert_delay`, `cache_ttl_seconds` | git 跟踪，手动编辑 |

环境变量可覆盖 config：`FEISHU_BASE_TOKEN`、`FEISHU_WATCHLIST_TABLE_ID`、`FEISHU_HOLDINGS_TABLE_ID`、`FEISHU_TRADE_TABLE_ID`、`FEISHU_CASH_TABLE_ID`、`FEISHU_UPSERT_DELAY`。

## 关键规则（跨功能）

- **增量更新**：不传 `--force` 时仅更新 `更新日期 < today` 的记录；持仓仅在新价格变动 > 0.01 时重算
- **价格缓存**：`crawler.crawl()` 默认走 TTL 缓存（120s），路径 `~/.cache/feishu-ledger/price_cache.json`；缓存命中直接返回，不调 API
- **基金动态列名**：`fund_open_fund_daily_em()` 日期列名运行时解析（如 `2026-05-29-单位净值`）；如 `日增长率` 为空则 fallback `fund_open_fund_info_em` 逐只取
- **lark-cli 编码**：UTF-8 → GBK 回退；Windows 自动查找 `lark-cli.cmd`
- **429 限流**：自动重试一次（延迟翻倍）；仍失败抛出 `RuntimeError`
- **优雅退出**：Ctrl+C / SIGTERM 设 `_interrupted`，当前批次完成后退出
- **港股汇率**：`fx_spot_quote()` 三级降级（akshare 实时 → cache → MEMORY.md 0.8659）
- **持仓计算**：以交易表为唯一数据源；港股市值按即时汇率折算为 CNY
- **报告输出**：不得截断，完整展示给用户；不使用 `head`/`tail`/`grep` 过滤
- **图表发送**：`render_charts.py` 生成的图片，通过 `message` tool 发给皮迪克

## 常见错误（跨功能）

| 错误 | 原因 | 恢复 |
|------|------|------|
| 字段 ID 不匹配 | Feishu 表结构变更 | `python scripts/feishu_config.py` 重新同步 |
| 429 限流 | 写入过快 | 自动重试或 `--rate-limit 1.5` 增大间隔 |
| lark-cli 找不到 | 未安装或 PATH 不包含 | `npm install -g @lark-opdev/lark-cli`；Windows 检测 `lark-cli.cmd` |
| config.json 不存在 | 首次运行 | 设置 `FEISHU_BASE_TOKEN` 环境变量，运行 `feishu_config.py` |

## 导入式调用

```python
from scripts.crawler import crawl, HoldingItem, classify_code, fetch_stock_a_data, query_fund
from scripts.trade_calc import calc_trade_summary, resync_all, get_hkd_cny_rate
from scripts.feishu_base import LarkClient
```

需设置 `sys.path.insert(0, "feishu-ledger/scripts")`。
