# akshare-utils

基于 [AKShare](https://akshare.akfamily.xyz/) 的持仓工具脚本，可自动获取 A 股、港股、ETF、开放式基金的多品种行情/净值数据。

## 功能总览

| 脚本 | 用途 |
|------|------|
| `watchlist.py` | 硬编码持仓列表，直接输出到终端 |
| `crawler.py` | **爬取模块** - 接收代码列表，返回 JSON 格式的最新价与涨幅 |
| `feishu_sync.py` | **飞书同步** - 读取飞书自选表代码，爬取数据后写回表格 |
| `feishu_constants.py` | 共享常量（飞书 Base token、字段 ID 等） |

## 支持品种

| 品种 | 来源 | 示例代码 |
|------|------|---------|
| A 股股票 | 新浪财经 `stock_zh_a_spot()` | sz000333, sh600887 |
| 港股 | 新浪财经 `stock_hk_spot()` | hk00700, hk01810 |
| ETF | 新浪财经 `fund_etf_category_sina()` | sh512040, sz159201 |
| LOF / 开放式基金 | 天天基金 `fund_open_fund_daily_em()` | 015600, 519915 |

---

## 快速开始

### 方式一：watchlist.py（最简单）

编辑 `watchlist.py` 中的 `RAW_WATCHLIST` 常量，填入持仓代码和名称（制表符分隔）：

```bash
pip install akshare pandas
python watchlist.py
```

### 方式二：feishu_sync.py（推荐 - 与飞书自选表联动）

自动从飞书自选表读取代码，爬取最新价与涨幅后写回表格。

```bash
pip install akshare pandas
lark-cli auth login --domain base   # 首次需授权飞书访问
python feishu_sync.py --dry-run     # 预览模式
python feishu_sync.py               # 正式执行
```

### 方式三：crawler.py（独立爬取模块）

可作为 Python 模块导入或独立 CLI 使用：

```bash
echo '["sz000333","hk00700","015600"]' | python crawler.py
```

输出 JSON：
```json
[
  {"code": "sz000333", "name": "sz000333", "matched": true, "price": 82.83, "change_pct": "+0.32%"},
  {"code": "hk00700", "name": "hk00700", "matched": true, "price": 449.2, "change_pct": "-1.58%"},
  {"code": "015600", "name": "015600", "matched": true, "price": 2.1568, "change_pct": "-0.31%"}
]
```

---

## 代码前缀规则

| 前缀 | 品种 |
|------|------|
| `sh` / `sz` + 6位数字 | A 股、ETF（通过代码范围区分） |
| `hk` + 5位数字 | 港股 |
| 无前缀，6位数字 | LOF / 开放式基金 |

典型 A 股代码范围：
- 深圳：`000xxx`, `001xxx`, `002xxx`, `003xxx`, `300xxx`, `301xxx`
- 上海：`600xxx`, `601xxx`, `603xxx`, `605xxx`, `688xxx`

---

## feishu_sync.py 详细用法

```bash
# 预览模式（只打印计划，不实际写入）
python feishu_sync.py --dry-run

# 自定义写入间隔（默认 0.8s，防止限流）
python feishu_sync.py --rate-limit 1.5

# 遇错立即终止（默认 skip 继续）
python feishu_sync.py --on-error abort

# 静默模式（减少输出）
python feishu_sync.py --quiet
```

### 工作流程

1. **读取** - 从飞书自选表读取全部 83 条记录
2. **提取** - 从每条记录获取代码（`代码` 字段）
3. **爬取** - 调用 crawler 模块批量获取最新价与涨幅
4. **写入** - 通过 `lark-cli +record-upsert` 逐条更新（0.8s 间隔）

---

## 项目文件

| 文件 | 说明 |
|------|------|
| `watchlist.py` | 硬编码持仓列表，数据直接输出到终端 |
| `crawler.py` | 爬取模块，可 import 或 CLI 使用 |
| `feishu_sync.py` | 飞书自选表同步脚本 |
| `feishu_constants.py` | 共享常量配置 |
| `README.md` | 本文件 |

## 依赖

- Python ≥ 3.8
- [AKShare](https://pypi.org/project/akshare/)
- pandas
- lark-cli（仅 feishu_sync.py 需要）
