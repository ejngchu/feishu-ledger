# 持仓表全面同步

`trade_calc.py --resync-holdings` — 以交易表为唯一数据源，全面验证并修正持仓表的份额/成本/市值/收益/收益率/年化。

## 触发场景

- 用户说「更新持仓表」「重算持仓」
- 交易表有数据变更后（写入/删除/分红）需要联动更新
- 每日 21:00 cron 触发（通过 `--sync-dividends` 链路自动调用）

## 命令

```bash
# 验证交易表 + 修正持仓表（推荐每日运行）
python scripts/trade_calc.py --resync-holdings

# 预览模式（仅看差异，不实际写入）
python scripts/trade_calc.py --resync-holdings --dry-run

# 单标的查询
python scripts/trade_calc.py --code 007994        # 查某标的历史与成本
python scripts/trade_calc.py --code 007994 --list  # 仅列出历史交易
python scripts/trade_calc.py --code 007994 --recalc # 仅重新核算持仓（不写入交易）

# 全量重算所有持仓标的
python scripts/trade_calc.py --resync-all

# 与 feishu_sync 联动
python scripts/feishu_sync.py --sync-holdings    # 自选表同步 + 持仓表全面同步
python scripts/feishu_sync.py --sync-dividends   # 自选表 + 分红同步 + 持仓全量重算
```

## 核心规则（以交易表为唯一数据源）

```
总份额 = 累计买入 + 累计定投 + 累计分红再投 - 累计卖出
总成本 = 累计买入成本 + 累计定投成本 - 累计卖出成本 - 现金分红金额
市值   = 总份额 × 最新价
持有收益 = 市值 - 总成本
持有收益率 = 持有收益 / 总成本 × 100%
年化收益率 = (市值/总成本)^(365/持有天数) - 1
```

## 现金分红联动（特殊处理）

**核心规则**：
- 总成本减少（=分红金额），体现为每股成本降低
- 市值不变
- 持有收益增加

**示例**（hk00700 腾讯控股）：
- 分红前：成本 28,430
- 分红 530 元后：成本 28,430 − 530 = 27,900

> 详见 [dividend-sync.md](dividend-sync.md) 现金分红记录规范。

## 港股处理

- 港股持仓（市值以 HKD 计）按即时汇率（akshare `fx_spot_quote`）折算为 CNY
- 汇率三级降级：akshare 实时 → 缓存 hkd_rate → MEMORY.md 参考值（0.8659）

## 关键差异检测

`--resync-holdings` 会输出：
- 持仓表修正：N 只
- 交易表问题：N 条（典型：卖出份额/金额未取负）

## 本功能常见错误

| 错误 | 原因 | 恢复 |
|------|------|------|
| 持仓表市值错误 | 净值引用了其他基金 | 用 crawler 重新获取正确净值后重算 |
| 港股市值计算为负 | 汇率缓存为 `nan` | `trade_calc.py` / `crawler.py` 中加 `math.isnan()` 检查 |
