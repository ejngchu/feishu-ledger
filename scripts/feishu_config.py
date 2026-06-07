"""
飞书 Base 配置管理

配置分为两层：
  ~/.config/feishu-ledger/config.json  - base_token、table IDs、field IDs（从飞书自动同步）
  skill/assets/config.json              - 性能调优参数（upsert_delay 等）

首次运行流程：
  1. 检查 ~/.config/feishu-ledger/config.json 是否存在
  2. 若不存在，尝试从环境变量获取 base_token
  3. 若无 env token，报错退出
  4. 自动调用飞书 API 同步所有 table/field ID
  5. 写入 ~/.config/feishu-ledger/config.json

用法（同步字段 ID）:
    python skill/scripts/feishu_config.py [--dry-run]
    需要先登录: lark-cli auth login --domain base
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── 路径常量 ───────────────────────────────────────────────

def _get_config_dir() -> Path:
    if platform.system() == "Windows":
        base = Path.home() / ".config"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "feishu-ledger"


def _get_cache_dir() -> Path:
    if platform.system() == "Windows":
        base = Path.home() / ".cache"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "feishu-ledger"


CONFIG_PATH = _get_config_dir() / "config.json"
CACHE_DIR = _get_cache_dir()
PRICE_CACHE_PATH = CACHE_DIR / "price_cache.json"
SETTINGS_PATH = Path(__file__).resolve().parent.parent / "assets" / "config.json"

# ── 内部状态（延迟加载） ───────────────────────────────────

_cached_config: dict | None = None
_cached_settings: dict = {}
_upsert_delay: float = 0.8
_cache_ttl_seconds: int = 120


def _ensure_loaded():
    """首次访问任意配置常量时调用，触发一次性加载"""
    global _cached_config, _cached_settings, _upsert_delay
    if _cached_config is not None:
        return

    # 性能配置（必定从文件读取，不退出）
    if SETTINGS_PATH.exists():
        _cached_settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    _upsert_delay = float(_cached_settings.get("upsert_delay", 0.8))
    _cache_ttl_seconds = int(_cached_settings.get("cache_ttl_seconds", 120))

    # 主配置
    if CONFIG_PATH.exists():
        _cached_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return

    # 首次运行
    token = os.environ.get("FEISHU_BASE_TOKEN")
    if not token:
        print("ERROR: ~/.config/feishu-ledger/config.json not found.", file=sys.stderr)
        print("请设置环境变量 FEISHU_BASE_TOKEN 后重试，或运行以下命令初始化：", file=sys.stderr)
        print(f"  python {Path(__file__).name}", file=sys.stderr)
        sys.exit(1)

    print("Initializing config from Feishu API...")
    _cached_config = _fetch_all_from_feishu(token)
    _write_config(_cached_config)
    print(f"Config written to {CONFIG_PATH}")


_KEY_MAP = {
    "FEISHU_BASE_TOKEN": "feishu_base_token",
    "FEISHU_WATCHLIST_TABLE_ID": "watchlist_table_id",
    "FEISHU_HOLDINGS_TABLE_ID": "holdings_table_id",
    "FEISHU_TRADE_TABLE_ID": "trade_table_id",
    "FEISHU_CASH_TABLE_ID": "cash_table_id",
}
_FIELD_IDS_KEY_MAP = {
    "WATCHLIST_FIELD_IDS": "watchlist_field_ids",
    "HOLDINGS_FIELD_IDS": "holdings_field_ids",
    "TRADE_FIELD_IDS": "trade_field_ids",
    "CASH_FIELD_IDS": "cash_field_ids",
}


def __getattr__(name: str):
    _ensure_loaded()
    if name in _KEY_MAP:
        return _cached_config.get(_KEY_MAP[name], "")
    if name in ("WATCHLIST_TABLE_ID", "HOLDINGS_TABLE_ID", "TRADE_TABLE_ID"):
        return _cached_config.get(_KEY_MAP.get(f"FEISHU_{name}", name.lower()), "")
    if name in _FIELD_IDS_KEY_MAP:
        return _cached_config.get(_FIELD_IDS_KEY_MAP[name], {})
    if name == "UPSERT_DELAY":
        return _upsert_delay
    if name == "CACHE_TTL_SECONDS":
        return _cache_ttl_seconds
    if name == "raw":
        return _cached_config
    raise AttributeError(name)


# ── 飞书 API ───────────────────────────────────────────────

def _run_lark(args: list[str], token: str) -> dict:
    cmd = ["lark-cli", "base"] + args + ["--as", "user"]
    if platform.system() == "Windows":
        result = subprocess.run(" ".join(cmd), capture_output=True, shell=True)
    else:
        result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        print(f"lark-cli error (code {result.returncode}): {stderr}", file=sys.stderr)
        sys.exit(1)
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        output = result.stdout.decode("gbk")
    match = re.search(r'\{[\s\S]*\}', output)
    if match:
        return json.loads(match.group())
    print(f"No JSON in output: {output[:500]}", file=sys.stderr)
    sys.exit(1)


def _get_fields(token: str, table_id: str) -> dict[str, str]:
    data = _run_lark(["+field-list", "--base-token", token, "--table-id", table_id], token)
    return {f["name"]: f["id"] for f in data["data"]["fields"]}


def _fetch_all_from_feishu(token: str) -> dict:
    table_ids: dict[str, str | None] = {
        "watchlist_table_id": None,
        "holdings_table_id": None,
        "trade_table_id": None,
        "cash_table_id": None,
    }

    print("Fetching table list...")
    data = _run_lark(["+table-list", "--base-token", token], token)
    tables = data.get("data", {}).get("tables", [])
    print(f"  Found {len(tables)} tables")

    for t in tables:
        name = t.get("name", "")
        tid = t.get("id", "")
        if not tid:
            continue
        if "自选" in name or "watchlist" in name.lower():
            table_ids["watchlist_table_id"] = tid
            print(f"  watchlist_table_id = {tid} ({name})")
        elif "持仓" in name or "holdings" in name.lower():
            table_ids["holdings_table_id"] = tid
            print(f"  holdings_table_id = {tid} ({name})")
        elif "交易" in name or "trade" in name.lower():
            table_ids["trade_table_id"] = tid
            print(f"  trade_table_id = {tid} ({name})")
        elif "现金" in name or "cash" in name.lower():
            table_ids["cash_table_id"] = tid
            print(f"  cash_table_id = {tid} ({name})")

    existing = {}
    if CONFIG_PATH.exists():
        existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in table_ids:
        if table_ids[key] is None:
            table_ids[key] = existing.get(key)

    field_allowlist = {
        "watchlist_field_ids":  ["代码", "名称", "最新价", "涨幅", "产品类型", "更新日期"],
        "holdings_field_ids":   ["代码", "名称", "产品类型", "货币", "总成本", "总份额", "市值", "持有收益", "持有收益率", "年化收益率", "一级组合", "二级组合"],
        "trade_field_ids":      ["代码", "名称", "方向", "交易日期", "成本", "金额", "份额", "收益", "收益率"],
        "cash_field_ids":       ["账户", "余额", "备注", "货币", "账户类型"],
    }

    config: dict = {"feishu_base_token": token, "upsert_delay": 0.8}

    for key in ["watchlist_field_ids", "holdings_field_ids", "trade_field_ids", "cash_field_ids"]:
        table_key = key.replace("_field_ids", "_table_id")
        table_id = table_ids.get(table_key)
        if not table_id:
            if existing.get(key):
                config[key] = existing[key]
            print(f"  {key}: no table_id, skipping")
            continue
        print(f"Fetching {key} from {table_id}...")
        all_fields = _get_fields(token, table_id)
        allowed = field_allowlist.get(key, [])
        selected = {n: all_fields[n] for n in allowed if n in all_fields}
        missing = [n for n in allowed if n not in all_fields]
        if missing:
            print(f"  WARNING: fields not found in Feishu: {missing}")
        config[key] = selected
        print(f"  Synced {len(selected)} fields")

    for k, v in table_ids.items():
        if v:
            config[k] = v

    return config


def _write_config(config: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_config(config: dict) -> bool:
    required = [
        "watchlist_table_id", "holdings_table_id",
        "trade_table_id", "cash_table_id",
        "watchlist_field_ids", "holdings_field_ids",
        "trade_field_ids", "cash_field_ids",
    ]
    missing = [k for k in required if not config.get(k)]
    if missing:
        print(f"Config missing: {missing}", file=sys.stderr)
        return False
    return bool(config.get("feishu_base_token"))


# ── 价格缓存 ───────────────────────────────────────────────

def load_price_cache() -> dict:
    """返回 {'prices': {code: price_entry, ...}, 'hkd_rate': float, 'timestamp': 'ISO str'}"""
    if PRICE_CACHE_PATH.exists():
        try:
            data = json.loads(PRICE_CACHE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and "prices" in data else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_price_cache(price_map: dict, hkd_rate: float | None = None):
    """
    保存价格缓存和汇率。
    price_map: {code: crawl_result_dict}（完整爬取结果，含 price/change_pct 等）
    hkd_rate: 当前使用的 HKD/CNY 汇率
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import math
    _rate = hkd_rate
    if _rate is not None and isinstance(_rate, float) and math.isnan(_rate):
        _rate = None  # 不保存无效汇率
    payload = {
        "prices": price_map,
        "hkd_rate": _rate,
        "timestamp": datetime.now().isoformat(),
    }
    PRICE_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_cache_valid() -> bool:
    """检查缓存是否在 TTL 时间内有效"""
    data = load_price_cache()
    ts = data.get("timestamp", "")
    if not ts:
        return False
    try:
        cached_time = datetime.fromisoformat(ts)
        age = (datetime.now() - cached_time).total_seconds()
        return age < _cache_ttl_seconds
    except (ValueError, OSError):
        return False


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync field IDs from Feishu to ~/.config/feishu-ledger/config.json")
    parser.add_argument("--dry-run", action="store_true", help="print config without writing")
    args = parser.parse_args()

    token = os.environ.get("FEISHU_BASE_TOKEN")
    if not token:
        print("ERROR: FEISHU_BASE_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    print("Fetching config from Feishu...")
    config = _fetch_all_from_feishu(token)
    if not validate_config(config):
        print("ERROR: Config validation failed.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n=== Dry run - would write: ===")
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return

    _write_config(config)
    print(f"\nUpdated {CONFIG_PATH}")


if __name__ == "__main__":
    main()
