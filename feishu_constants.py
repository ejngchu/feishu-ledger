"""
飞书 Base 配置常量 - 供 crawler.py / feishu_sync.py 共享使用
"""

# 自选表所在的 Base
FEISHU_BASE_TOKEN = "FlZObdBVNawsG0s9GhHch2xDnAc"

# 自选表
TABLE_ID = "tblIP0LuVvZFMjZD"

# 字段 ID（通过 +field-list 获取）
FIELD_IDS = {
    "代码": "fldl32yudS",       # text
    "名称": "fldJTAr4Di",       # text
    "最新价": "fldvK1axvU",     # number(4dp)
    "涨幅": "fldmLzSWJB",       # text (如 "-0.88%")
    "产品类型": "fldxkBho1q",   # select
}

# 字段 ID → 字段名（反向映射，便于日志）
FIELD_NAMES = {v: k for k, v in FIELD_IDS.items()}

# 写入速率限制（两次 upsert 之间的间隔秒数）
UPSERT_DELAY = 0.8
