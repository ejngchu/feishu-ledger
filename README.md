# feishu-ledger

飞书持仓账本 skill：AKShare 爬取 A 股/港股/ETF/基金实时价格同步到飞书多维表格，交易截图自动识别写入交易表并实时核算持仓表（以交易表为唯一数据源），定投截图自动合并到最新定投记录中，报告生成自动处理非交易日（不更新基准）。

> **skill 入口**：[SKILL.md](SKILL.md)
>
> **本 README 简介**：项目结构、快速开始、部署说明。

## 项目结构

```
feishu-ledger/
├── SKILL.md                 # skill 入口（始终加载）：跨功能概念 + 路由表
├── references/              # 按需加载：6 个功能块
│   ├── watchlist-sync.md    # 1. 行情同步
│   ├── trade-write.md       # 2. 交易记录录入
│   ├── dca-update.md        # 3. 定投交易更新
│   ├── holdings-resync.md   # 4. 持仓表全面同步
│   ├── dividend-sync.md     # 5. 分红记录同步
│   └── report-generation.md # 6. 报告生成
├── scripts/                 # 9 个 Python 模块
│   ├── crawler.py
│   ├── feishu_base.py
│   ├── feishu_config.py
│   ├── feishu_sync.py
│   ├── market_context.py
│   ├── render_charts.py
│   ├── report.py
│   ├── trade_calc.py
│   └── dividend_update.py
└── assets/                  # git 跟踪的运行时配置
    └── config.json
```

## 快速开始

```bash
# 1. 安装依赖
pip install akshare pandas
npm install -g @lark-opdev/lark-cli

# 2. 飞书授权
lark-cli auth login --domain base

# 3. 初始化配置（首次使用）
python scripts/feishu_config.py

# 4. 同步行情
python scripts/feishu_sync.py

# 5. 生成报告
python scripts/report.py
```

## 触发词

| 触发词 | 功能块 |
|--------|--------|
| 「查行情」「看涨跌」「更新自选表」 | 行情同步 |
| 「新增交易」「记一笔交易」+ 交易截图 | 交易记录录入 |
| 「更新定投交易」+ 定投截图 | 定投交易更新 |
| 「更新持仓表」「重算持仓」 | 持仓表全面同步 |
| 「查分红」「近期有哪些股票分红」 | 分红记录同步 |
| 「生成报告」「今日报告」「晚间报告」 | 持仓报告生成 |

## 依赖

- Python ≥ 3.8
- [AKShare](https://pypi.org/project/akshare/) — A 股/港股/基金行情
- pandas
- [lark-cli](https://github.com/larksuite/lark-cli) — 飞书 SDK
- mineru2md — 截图解析（交易录入/定投更新时使用）

## 部署到其他机器

```bash
git clone git@github.com:ejngchu/feishu-ledger.git ~/skills/feishu-ledger
# 然后软链接到目标平台的 skills/ 目录，例如：
ln -s ~/skills/feishu-ledger ~/.openclaw/workspace-*/.agents/skills/feishu-ledger
```

## 飞书表配置

| 表 | table_id | 用途 |
|----|---------|------|
| 自选表 | `tblIP0LuVvZFMjZD` | 最新价、涨幅 |
| 持仓表 | `tblIqUClte8harRW` | 份额、成本、市值、收益 |
| 交易表 | `tblkzlJG97qsMFfK` | 历史买卖/分红记录 |
| 现金表 | `tblLxJaexFUr0hCP` | 各账户余额 |

Base token: `FlZObdBVNawsG0s9GhHch2xDnAc`（皮迪克专属）
