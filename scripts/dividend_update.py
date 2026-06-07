#!/usr/bin/env python3
"""
分红记录同步脚本

功能：
  1. 从 AKShare 查询持仓股近 N 天除权记录
  2. 判断是否已写入交易表（去重）
  3. 未写入则写入交易表（direction=分红）
  4. 完成后调用 trade_calc.resync_all() 重算持仓表（份额/成本/市值/收益）

触发方式：
  A. feishu_sync.py --sync-dividends（自选表同步后联动）
  B. python dividend_update.py（独立运行）

用法：
  python dividend_update.py                    # 近30天，写入+重算
  python dividend_update.py --days 7          # 近7天
  python dividend_update.py --dry-run         # 预览（不写入）
  python dividend_update.py --check-only        # 仅检查，不写入
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 添加 scripts 目录到路径
_scripts = Path(__file__).parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

from feishu_base import LarkClient, setup_signal_handlers
from feishu_config import (
    FEISHU_BASE_TOKEN,
    HOLDINGS_TABLE_ID,
    HOLDINGS_FIELD_IDS,
    TRADE_TABLE_ID,
    TRADE_FIELD_IDS,
)

# 导入持仓核算逻辑（来自 trade_calc.py）
import trade_calc as _tc


# ── AKShare 分红数据查询 ─────────────────────────────────

def query_recent_dividends(days: int = 30) -> dict[str, dict]:
    """
    查询近 N 天所有除权股票，返回 {code: {date, name, dps, transfer, market}}
    code 为 AKShare 格式（6位数字，无前缀）
    """
    import akshare as ak

    today = datetime.now()
    results: dict[str, dict] = {}

    for days_ago in range(days + 5):
        date_str = (today - timedelta(days=days_ago)).strftime("%Y%m%d")
        date_disp = (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        try:
            df = ak.news_trade_notify_dividend_baidu(date=date_str)
            if df is None or len(df) == 0:
                continue
            for _, row in df.iterrows():
                code_raw = str(row.get("股票代码", "")).strip()
                name = str(row.get("股票简称", "")).strip()
                dps = row.get("分红")
                transfer = row.get("送股") or row.get("转增")
                # 清理 transfer：去除 "-" / "nan" / "None" 等无效值
                if transfer in ("-", "nan", "", "None", None) or (isinstance(transfer, float) and str(transfer) == "nan"):
                    transfer = None
                market = str(row.get("交易所", "")).strip()

                if market == "SH":
                    code = code_raw.zfill(6)
                    prefix = "sh"
                elif market == "SZ":
                    code = code_raw.zfill(6)
                    prefix = "sz"
                elif market == "HK":
                    code = code_raw.zfill(5)
                    prefix = "hk"
                else:
                    continue

                full_code = prefix + code
                if full_code in results:
                    continue
                results[full_code] = {
                    "date": date_disp,
                    "name": name,
                    "dps": dps if dps and str(dps) not in ("nan", "") else 0.0,
                    "transfer": transfer if transfer and str(transfer) not in ("nan", "") else None,
                    "market": market,
                }
        except Exception:
            pass

    return results


# ── 持仓表读取 ───────────────────────────────────────────

def get_holdings_codes(client: LarkClient) -> dict[str, dict]:
    """返回 {raw_code: {record_id, name, shares, cost, currency}}"""
    records = client.get_records(HOLDINGS_TABLE_ID, HOLDINGS_FIELD_IDS)
    result = {}
    for r in records:
        code = str(r.get("代码", "")).strip().lower()
        if not code:
            continue
        result[code] = {
            "record_id": r["_record_id"],
            "name": r.get("名称", ""),
            "shares": r.get("总份额", 0) or 0,
            "cost": r.get("总成本", 0) or 0,
            "currency": (r.get("货币") or ["CNY"])[0] if r.get("货币") else "CNY",
        }
    return result


# ── 交易表读取（去重） ─────────────────────────────────

def get_trade_dividend_dates(client: LarkClient) -> dict[str, list[str]]:
    """返回 {raw_code: [ex_date_str, ...]}，已在交易表中的除权日期"""
    try:
        records = client.get_records(TRADE_TABLE_ID, TRADE_FIELD_IDS)
    except Exception:
        return {}

    result: dict[str, list[str]] = {}
    for r in records:
        direction = (r.get("方向") or [""])[0] if r.get("方向") else ""
        if direction != "分红":
            continue
        code = str(r.get("代码", "")).strip().lower()
        date_val = r.get("交易日期", "")
        if not date_val:
            continue
        date_str = date_val[:10] if "T" in str(date_val) else str(date_val)[:10]
        result.setdefault(code, []).append(date_str)
    return result


# ── 分红记录写入 ─────────────────────────────────────────

def insert_dividend_records(
    client: LarkClient,
    records: list[dict],
    dry_run: bool = False,
    verbose: bool = True,
) -> tuple[int, int]:
    """
    将分红记录批量写入交易表。
    records: [{code, name, ex_date, dps, shares, amount, cost, is_bonus}, ...]
    amount > 0 表示收到分红（现金），cost < 0 表示每股成本减少。
    """
    if not records:
        return 0, 0

    ok, fail = 0, 0
    for i, rec in enumerate(records):
        if dry_run:
            if verbose:
                div_type = "送股/转增" if rec["is_bonus"] else "现金分红"
                print(f"  [DRY-RUN] 写入: {rec['code']} {rec['name']} {rec['ex_date']} "
                      f"{div_type} 份额={rec['shares']} 金额={rec['amount']} 成本={rec['cost']}")
            ok += 1
            continue

        fields = {
            TRADE_FIELD_IDS["代码"]: rec["code"],
            TRADE_FIELD_IDS["名称"]: rec["name"],
            TRADE_FIELD_IDS["方向"]: ["分红"],
            TRADE_FIELD_IDS["交易日期"]: rec["ex_date"] + "T00:00:00",
            TRADE_FIELD_IDS["份额"]: rec["shares"],
            TRADE_FIELD_IDS["金额"]: rec["amount"],
            TRADE_FIELD_IDS["成本"]: rec["cost"],
        }
        if "收益" in TRADE_FIELD_IDS:
            fields[TRADE_FIELD_IDS["收益"]] = None
        if "收益率" in TRADE_FIELD_IDS:
            fields[TRADE_FIELD_IDS["收益率"]] = None

        success = client.upsert_record(TRADE_TABLE_ID, None, fields, verbose=False)
        if success:
            if verbose:
                div_type = "送股/转增" if rec["is_bonus"] else "现金分红"
                print(f"  ✅ 写入交易表: {rec['code']} {rec['name']} {rec['ex_date']} "
                      f"{div_type} 份额={rec['shares']} 金额={rec['amount']} 成本={rec['cost']}")
            ok += 1
        else:
            if verbose:
                print(f"  ❌ 写入失败: {rec['code']} {rec['name']}")
            fail += 1

        if i < len(records) - 1:
            time.sleep(0.8)

    return ok, fail


# ── 核心分析逻辑 ─────────────────────────────────────────

def parse_transfer_ratio(transfer_val) -> float:
    """从转增/送股字符串解析比例，如 '10转增4.90股' → 4.90"""
    if not transfer_val or str(transfer_val) in ("nan", "None", ""):
        return 0.0
    import re
    m = re.search(r"[\d.]+", str(transfer_val))
    return float(m.group()) if m else 0.0


def analyze_dividends(
    holdings: dict[str, dict],
    trade_dividends: dict[str, list[str]],
    all_dividends: dict[str, dict],
    cutoff_date: str,
    check_only: bool = False,
    dry_run: bool = False,
    verbose: bool = True,
) -> tuple[list[dict], int]:
    """
    分析持仓股票中哪些需要写入分红记录。
    返回 (pending_records, pending_count)
    pending_records: 供写入的记录列表
    """
    pending: list[dict] = []

    for code, h in holdings.items():
        if code not in all_dividends:
            continue
        div = all_dividends[code]
        ex_date = div["date"]

        # 过滤不在查询区间
        if ex_date < cutoff_date:
            continue

        # 去重检查
        if code in trade_dividends and ex_date in trade_dividends[code]:
            if verbose:
                print(f"  ⏭️  已存在: {code} {div['name']} 除权日 {ex_date}，跳过")
            continue

        shares = h["shares"]
        # 清理 dps 字符串（去除 "元"/"%" 等单位/符号）
        dps_raw = div["dps"]
        if dps_raw:
            dps_clean = str(dps_raw).replace("元", "").replace("%", "").strip()
            try:
                dps = float(dps_clean)
            except (ValueError, TypeError):
                dps = 0.0
        else:
            dps = 0.0
        transfer = div["transfer"]
        is_bonus = transfer and str(transfer) not in ("nan", "", "None")

        if is_bonus:
            # 送股/转增：份额增加，成本=0，金额=0
            bonus_ratio = parse_transfer_ratio(transfer)
            bonus_shares = round(shares * bonus_ratio / 10, 2)
            amount = 0.0
            cost = 0.0
        else:
            # 现金分红：份额=0，金额=每股分红×份额，成本=-金额（成本减少）
            # ⚠️ AKShare baidu 接口返回的 dps 单位是 **元/10股**（中国 A 股惯例），
            # 需先除以 10 转换为元/股。港股（HK）已是元/股，不需转换。
            dps_per_share = dps / 10 if div.get("market") in ("SH", "SZ") else dps
            amount = round(dps_per_share * shares, 2)
            cost = -amount
            bonus_shares = 0

        pending.append({
            "code": code,
            "name": div["name"],
            "ex_date": ex_date,
            "dps": dps,
            "shares": bonus_shares,
            "amount": amount,
            "cost": cost,
            "is_bonus": is_bonus,
        })

    return pending, len(pending)


# ── 可编程调用入口 ─────────────────────────────────────

def sync_dividends(
    days: int = 30,
    dry_run: bool = False,
    check_only: bool = False,
    verbose: bool = True,
    rate_limit: float = 0.8,
) -> dict:
    """
    执行分红同步：
      1. 查询持仓股近期分红（AKShare）
      2. 去重后写入交易表
      3. 调用 trade_calc.resync_all() 重算全部持仓（份额/成本/市值/收益）
    """
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    client = LarkClient(FEISHU_BASE_TOKEN, upsert_delay=rate_limit)

    # Step 1: 查询分红
    if verbose:
        print("🔍 查询近期除权记录（AKShare）...")
    try:
        all_dividends = query_recent_dividends(days=days)
    except Exception as e:
        print(f"❌ AKShare 查询失败: {e}", file=sys.stderr)
        return {"trade_ok": 0, "trade_fail": 0, "pending": 0, "dry_run": dry_run}

    # Step 2: 持仓表
    if verbose:
        print("📊 读取持仓表...")
    try:
        holdings = get_holdings_codes(client)
    except Exception as e:
        print(f"❌ 读取持仓表失败: {e}", file=sys.stderr)
        return {"trade_ok": 0, "trade_fail": 0, "pending": 0, "dry_run": dry_run}

    holding_codes = set(holdings.keys())
    matched_div = {c: d for c, d in all_dividends.items() if c in holding_codes}
    if verbose:
        print(f"  → 持仓中 {len(matched_div)} 只近期有除权记录")

    # Step 3: 交易表去重
    if verbose:
        print("📋 检查交易表去重...")
    trade_dividends = get_trade_dividend_dates(client)
    if verbose:
        print(f"  → 交易表已有 {sum(len(v) for v in trade_dividends.values())} 条分红记录")

    # Step 4: 分析
    if verbose:
        print("🔎 分析需写入的记录...")
    pending, pending_count = analyze_dividends(
        holdings, trade_dividends, all_dividends, cutoff_date,
        check_only=check_only, dry_run=dry_run, verbose=verbose,
    )

    if not pending:
        if verbose:
            print("  → 无需新增分红记录")
        # 无论是否有分红，都执行全量重算（因为可能有其他交易变化）
        if not dry_run and not check_only:
            if verbose:
                print("\n💰 重算全部持仓...")
            result2 = _tc.resync_all(client, dry_run=False, verbose=verbose, rate_limit=rate_limit)
            return {
                "trade_ok": 0, "trade_fail": 0,
                "pending": 0,
                "resync_updated": result2.get("updated", 0),
                "resync_failed": result2.get("failed", 0),
                "dry_run": False,
            }
        return {"trade_ok": 0, "trade_fail": 0, "pending": 0, "dry_run": dry_run}

    if check_only:
        if verbose:
            print(f"\n⚠️  发现 {pending_count} 条待写入分红记录:")
            for p in pending:
                div_type = "送股/转增" if p["is_bonus"] else "现金分红"
                print(f"  {p['code']} {p['name']} {p['ex_date']} {div_type} "
                      f"份额={p['shares']} 金额={p['amount']}")
        return {"trade_ok": 0, "trade_fail": 0, "pending": pending_count, "dry_run": dry_run}

    # Step 5: 写入交易表
    if verbose:
        print(f"\n📝 写入交易表 ({pending_count} 条)...")
    trade_ok, trade_fail = insert_dividend_records(
        client, pending, dry_run=dry_run, verbose=verbose,
    )

    # Step 6: 全量重算持仓（替换原有的局部更新）
    resync_updated = resync_failed = 0
    if not dry_run and trade_ok > 0:
        if verbose:
            print("\n💰 重算全部持仓...")
        result2 = _tc.resync_all(
            client, dry_run=False, verbose=verbose, rate_limit=rate_limit,
        )
        resync_updated = result2.get("updated", 0)
        resync_failed = result2.get("failed", 0)

    return {
        "trade_ok": trade_ok,
        "trade_fail": trade_fail,
        "pending": pending_count,
        "resync_updated": resync_updated,
        "resync_failed": resync_failed,
        "dry_run": dry_run,
    }


# ── CLI 入口 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="持仓股分红记录同步：查询 → 写入交易表 → 全量重算持仓表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python dividend_update.py                    # 近30天，写入+重算
  python dividend_update.py --days 7           # 近7天
  python dividend_update.py --dry-run          # 预览不写入
  python dividend_update.py --check-only      # 仅检查，不写入
  python dividend_update.py --quiet           # 静默输出
        """
    )
    parser.add_argument("--days", type=int, default=30,
                        help="查询近 N 天的除权记录（默认 30天）")
    parser.add_argument("--cutoff", default=None,
                        help="截止日期 YYYY-MM-DD（默认: today - days）")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式（不实际写入）")
    parser.add_argument("--check-only", action="store_true",
                        help="仅检查是否需要更新，不写入")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="静默模式")
    parser.add_argument("--rate-limit", type=float, default=0.8,
                        help="写入间隔秒数（默认 0.8s）")
    args = parser.parse_args()

    setup_signal_handlers()
    verbose = not args.quiet

    if args.cutoff:
        try:
            datetime.strptime(args.cutoff, "%Y-%m-%d")
        except ValueError:
            print(f"错误: --cutoff 日期格式应为 YYYY-MM-DD，实际: {args.cutoff}", file=sys.stderr)
            sys.exit(1)

    if verbose:
        cutoff = args.cutoff or (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        print(f"📅 查询区间: {cutoff} ~ {datetime.now().strftime('%Y-%m-%d')} (近{args.days}天)")
        print("=" * 50)

    result = sync_dividends(
        days=args.days,
        dry_run=args.dry_run,
        check_only=args.check_only,
        verbose=verbose,
        rate_limit=args.rate_limit,
    )

    if verbose:
        print("\n" + "=" * 50)
        if result["dry_run"]:
            print(f"🔍 [DRY-RUN] 预览: 需写入 {result['pending']} 条")
        else:
            print(f"✅ 交易表: {result['trade_ok']} 成功 / {result['trade_fail']} 失败")
            if result.get("resync_updated", 0) > 0:
                print(f"✅ 持仓表: 重算 {result['resync_updated']} 只成功 / {result.get('resync_failed', 0)} 失败")
            if result["trade_fail"] > 0 or result.get("resync_failed", 0) > 0:
                print("\n⚠️  有失败记录，请检查日志")

    sys.exit(0 if result["trade_fail"] == 0 and result.get("resync_failed", 0) == 0 else 1)


if __name__ == "__main__":
    main()
