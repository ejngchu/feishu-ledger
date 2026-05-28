"""
飞书 Base 配置常量 - 供 feishu_sync.py 使用
"""
import os

# ── Table IDs ─────────────────────────────────────────────
FEISHU_BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "FlZObdBVNawsG0s9GhHch2xDnAc")

# 自选表 (Watchlist)
WATCHLIST_TABLE_ID = os.environ.get(
    "FEISHU_WATCHLIST_TABLE_ID",
    os.environ.get("FEISHU_ZIXUAN_TABLE_ID", "tblIP0LuVvZFMjZD")  # backward compat
)
# 持仓表 (Holdings)
HOLDINGS_TABLE_ID = os.environ.get(
    "FEISHU_HOLDINGS_TABLE_ID",
    os.environ.get("FEISHU_CHICHANG_TABLE_ID", "tblIqUClte8harRW")  # backward compat
)
# 交易表 (Trades)
TRADE_TABLE_ID = os.environ.get("FEISHU_TRADE_TABLE_ID", "tblkzlJG97qsMFfK")

# ── Field IDs ────────────────────────────────────────────
WATCHLIST_FIELD_IDS = {
    "代码":     "fldl32yudS",
    "名称":     "fldJTAr4Di",
    "最新价":   "fldvK1axvU",
    "涨幅":     "fldmLzSWJB",
    "产品类型": "fldxkBho1q",
    "更新日期": "fldWOPocEc",
}

HOLDINGS_FIELD_IDS = {
    "代码":        "fld8lHFqd9",
    "名称":        "fldvKDBTYp",
    "产品类型":    "fldMIQN9D7",
    "交易市场":    "fldOEEzfh9",
    "组合名称":    "fldsN6i2Hv",
    "总成本":      "fldXOpKRfs",
    "总份额":      "fld991DpTR",
    "市值":        "fldS3UtFOG",
    "持有收益":    "fld3Wh4UlO",
    "持有收益率":  "fld9k8xRi9",
    "年化收益率":  "fldVz1CDok",
}

TRADE_FIELD_IDS = {
    "代码":     "fldSqpR5Bd",
    "名称":     "fldOwrX5CT",
    "方向":     "fldpxk2mm9",
    "交易日期": "fldkrOH1Gr",
    "成本":     "flduBsxItu",
    "金额":     "fld3hOg0I8",
    "份额":     "fldHGSNYCC",
    "收益":     "fldV3661g2",
    "收益率":   "fldpmjJcwR",
}

# ── Helpers ───────────────────────────────────────────────
WATCHLIST_FIELD_NAMES = {v: k for k, v in WATCHLIST_FIELD_IDS.items()}

UPSERT_DELAY = float(os.environ.get("FEISHU_UPSERT_DELAY", "0.8"))
