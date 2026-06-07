# 分红记录同步

`dividend_update.py` — 从 AKShare 查询持仓股近 30 天除权记录，写入交易表并联动持仓表。

## 触发场景

- 用户说「查分红」「近期有哪些股票分红」「持仓股有无分红」
- 每日 21:00 cron 触发（自动联动）

## 触发路径

| 触发方式 | 命令 | 说明 |
|---------|------|------|
| A. 每日 21:00 cron | `feishu_sync.py --sync-dividends` | 自选表同步完成后自动联动 |
| B. 手动检查 | `dividend_update.py --check-only` | 仅报告，不写入 |
| C. 手动执行 | `dividend_update.py` | 查询 → 写入 → 联动，更新持仓表 |

## 命令

```bash
# 每日 21:00 自动触发（自选表同步后联动）
python scripts/feishu_sync.py --sync-dividends

# 手动检查（不写入）
python scripts/dividend_update.py --check-only

# 手动执行（写入交易表 + 联动持仓表）
python scripts/dividend_update.py

# 自定义时间范围
python scripts/dividend_update.py --days 7
```

## 工作流程

1. 从 AKShare 查询持仓股**近 30 天**除权记录（百度股市通）
2. 对比交易表，**去重**（已存在则跳过）
3. 写入**交易表**（direction=分红）
4. **全量重算持仓表**：调用 `trade_calc.resync_all()`，以交易表为唯一数据源重新核算全部持仓的份额/成本/市值/收益/收益率/年化

> 港股持仓（HKD）市值按即时汇率（akshare `fx_spot_quote`）折算为 CNY 后计入总市值。

## 分红记录格式规范

| 类型 | 份额 | 金额 | 成本 |
|------|------|------|------|
| **现金分红** | `0` | `+分红金额` | `-分红金额`（每股成本减少） |
| **送股/转增** | `+新增股数` | `0` | `0` |

## 分红后持仓表联动规则

```
新总成本 = 原总成本 - 分红金额（成本减少）
新持有收益 = 市值 - 新总成本
```

> 注意：市值由当日收盘价计算，不受分红影响；联动更新只改成本和持有收益，不改市值。

## A 股 vs 港股 DPS 单位差异（重要）

- **A 股（SH/SZ）**：AKShare `news_trade_notify_dividend_baidu` 返回的 `dps` 单位是 **元/10股**，需 `dps / 10` 转换
- **港股（HK）**：`dps` 已是元/股，不需转换

**错误示例**（已修复）：
- 伊利股份 2026-06-05 分红被误算为 7,200 元（实际 720 元）— A 股 dps 未 ÷10

**修复位置**：`dividend_update.py` 的 `analyze_dividends()` 中：

```python
if market in ("SH", "SZ"):
    dps = dps / 10  # 元/10股 → 元/股
```

## 本功能常见错误

| 错误 | 原因 | 恢复 |
|------|------|------|
| A 股分红数量级错误（10×） | dps 单位是元/10股 | 已修复：`dividend_update.py` 中 A 股 `dps / 10` |
