# 行情同步

`feishu_sync.py` / `crawler.py` — 爬取 A 股/港股/ETF/基金实时行情并同步到飞书自选表。

## 触发场景

- 用户说「查（一下/今日）行情」「看（一下）涨跌」「更新价格/净值」
- 用户说「更新自选（表）」「同步到飞书」
- 用户要查看持仓市值、持有收益
- 用户要查某个代码（如 sz000333 / hk00700）的最新价
- 每日 21:00 cron 触发

## 命令

```bash
# 飞书同步（推荐）
python scripts/feishu_sync.py                     # 增量同步自选表
python scripts/feishu_sync.py --dry-run           # 预览
python scripts/feishu_sync.py --force             # 强制更新所有
python scripts/feishu_sync.py --sync-dividends    # 同步自选表 + 分红记录 + 持仓全量重算
python scripts/feishu_sync.py --sync-holdings     # 同步自选表 + 持仓表全面同步
python scripts/feishu_sync.py --quiet             # 静默模式

# 行情预览（终端表格，无需飞书）
python scripts/crawler.py                          # 终端输出
python scripts/crawler.py --codes '["sz000333"]'  # JSON 输出

# 一体化（行情 + 报告）
python scripts/feishu_sync.py --report --report-time {11:30|16:30|20:30}
```

## 关键规则

- **增量更新**：不传 `--force` 时仅更新 `更新日期 < today` 的记录
- **价格缓存**：`crawler.crawl()` 默认走 TTL 缓存（120s），路径 `~/.cache/feishu-ledger/price_cache.json`
- **基金动态列名**：`fund_open_fund_daily_em()` 日期列名运行时解析
- **429 限流**：自动重试一次（延迟翻倍）

## 本功能常见错误

| 错误 | 原因 | 恢复 |
|------|------|------|
| 基金净值 `-` | 日增长率为空 | 自动 fallback 逐只查询，无需人工干预 |
| 分页查询漏数据 | `--limit 200` 不自动分页 | `feishu_base.py:get_records()` 已实现分页（`has_more` + `offset += page_size`） |
| 闻泰科技误判为空壳 | 单次查询未翻第二页 | 用 `lark-cli base +record-list --offset 200` 查第二页 |
