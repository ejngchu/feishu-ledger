"""
飞书自选表同步脚本

功能：
  Step 1: 读取自选表全部记录
  Step 2: 过滤需要更新的记录（date < today 或 --force）
  Step 3: 爬取最新价与涨幅，并更新价格缓存
  Step 4: 批量写入自选表（最新价、涨幅、更新日期）

与 trade_calc.py 的协作关系：
  - 本脚本仅负责自选表行情同步
  - 持仓表重算由 trade_calc.py --resync-all 负责
  - 分红同步（--sync-dividends）内部会调用 trade_calc.resync_all()

用法:
    python feishu_sync.py --dry-run           # 预览
    python feishu_sync.py                    # 正式执行（自选表行情同步）
    python feishu_sync.py --force            # 强制更新所有记录
    python feishu_sync.py --rate-limit 1.5
    python feishu_sync.py --on-error abort
    python feishu_sync.py --sync-dividends  # 自选表同步 + 分红同步（含持仓全量重算）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

WORKSPACE = Path("/root/.openclaw/workspace-Eva")

import crawler
from feishu_base import LarkClient, setup_signal_handlers
from feishu_config import (
    FEISHU_BASE_TOKEN,
    WATCHLIST_TABLE_ID,
    WATCHLIST_FIELD_IDS,
    FEISHU_CASH_TABLE_ID,
    HOLDINGS_TABLE_ID,
    HOLDINGS_FIELD_IDS,
    load_price_cache,
)

import akshare as ak  # 汇率查询

# 添加 scripts 目录到路径
import trade_calc as _tc
import dividend_update as _du


# ─────────────────────────────────────────────────────────────
# 1. 自选表同步 (Step 1-4)
# ─────────────────────────────────────────────────────────────

def sync_watchlist(
    client: LarkClient,
    dry_run: bool,
    force: bool,
    rate_limit: float,
    verbose: bool,
    today: str,
):
    """
    Step 1-4: 读取自选表 → 过滤 → 爬取 → 批量写入
    返回: (codes_list, code_to_price_dict)
    code_to_price_dict: {code: {"price": float, "date": str, ...}} 统一格式
    """
    # Step 1: 读取全部记录
    if verbose:
        print("\n[Step 1/4] 读取自选表记录...")
    records = client.get_records(WATCHLIST_TABLE_ID, WATCHLIST_FIELD_IDS)
    if verbose:
        print(f"  共读取 {len(records)} 条记录")

    if not records:
        print("  [WARN] 自选表为空，退出")
        return [], {}

    # Step 2: 过滤需要更新的记录
    codes = []
    records_to_update = []
    for r in records:
        code = r.get("代码", "")
        if not code:
            continue
        if not force and r.get("更新日期") and r.get("更新日期") >= today:
            continue
        codes.append(code)
        records_to_update.append(r)

    if verbose:
        mode = "强制" if force else "增量"
        print(f"\n[Step 2/4] 过滤 {today} 之前未更新的代码（{mode}，{len(codes)}/{len(records)} 条需更新）")

    if not codes:
        if verbose:
            print("  [INFO] 无需更新的记录")
        return [], {}

    # Step 3: 爬取
    if verbose:
        print(f"\n[Step 3/4] 爬取 {len(codes)} 个标的价格...")
    try:
        results = crawler.crawl(codes)
    except Exception as e:
        print(f"  [ERROR] 爬取失败: {e}", file=sys.stderr)
        if verbose:
            print("  [WARN] 继续使用缓存价格")
        results = []

    # 构建 code -> price (统一为 float)
    code_to_price_float = {}
    price_map_for_cache = {}
    for r in results:
        if r.get("matched") and r.get("price") is not None:
            code = r["code"]
            price = float(r["price"])
            code_to_price_float[code] = price
            price_map_for_cache[code] = r  # 存入缓存用完整 dict

    if verbose:
        print(f"  爬取成功 {len(code_to_price_float)}/{len(codes)} 个")

    # 补充缓存价格
    cache = load_price_cache()
    cached = cache.get("prices", {})
    cached_count = 0
    for code in codes:
        if code not in code_to_price_float and code in cached:
            entry = cached[code]
            # 统一格式：entry 可能是 dict {"price": float} 或 已经是 float
            if isinstance(entry, dict):
                p = entry.get("price")
            elif isinstance(entry, (int, float)):
                p = float(entry)
            else:
                continue
            if p:
                code_to_price_float[code] = float(p)
                cached_count += 1
    if verbose and cached_count:
        print(f"  缓存补充 {cached_count} 个")

    # 补充缓存（写入新爬取的结果，含汇率）
    if price_map_for_cache and not dry_run:
        fx_rate = _tc.get_hkd_cny_rate()
        crawler.save_price_cache(price_map_for_cache, hkd_rate=fx_rate)
        if verbose:
            print(f"  写入缓存（含汇率 {fx_rate}）")

    # 构建 crawl 结果 map：code → result dict（含真实 date）
    code_result_map = {res["code"]: res for res in results if res.get("matched")}

    # Step 4: 批量写入
    batch_records = []
    for r in records_to_update:
        code = r.get("代码", "")
        price = code_to_price_float.get(code)
        if price is None:
            continue

        # 涨幅：从爬虫结果取
        change_pct = None
        update_date = None  # 更新日期：只有从爬虫取到真实数据才更新
        res = code_result_map.get(code)
        if res:
            change_pct = res.get("change_pct")
            # 只有爬虫实际返回了 date（非缓存）才更新日期
            crawled_date = res.get("date")
            if crawled_date:
                update_date = str(crawled_date)[:10]

        fields = {
            WATCHLIST_FIELD_IDS["最新价"]: price,
        }
        # 只有拿到真实爬取日期才更新（基金净值未更新时不应改日期）
        if update_date:
            fields[WATCHLIST_FIELD_IDS["更新日期"]] = update_date
        if change_pct is not None and "涨幅" in WATCHLIST_FIELD_IDS:
            fields[WATCHLIST_FIELD_IDS["涨幅"]] = change_pct

        batch_records.append({
            "record_id": r["_record_id"],
            "fields": fields,
        })

    if not batch_records:
        if verbose:
            print("  [INFO] 无有效数据可写入")
        return [], code_to_price_float

    if verbose:
        print(f"\n[Step 4/4] 写入自选表 {len(batch_records)} 条...")
        for br in batch_records[:5]:
            print(f"  -> {br['record_id']}: fields={br['fields']}")
        if len(batch_records) > 5:
            print(f"  ... 共 {len(batch_records)} 条")

    ok_count, fail_count = client.upsert_batch(
        WATCHLIST_TABLE_ID, batch_records, dry_run=dry_run, verbose=verbose
    )

    if verbose:
        action = "预览" if dry_run else "写入"
        print(f"\n  [STATS] 自选表: {action}成功 {ok_count} | 失败 {fail_count}")

    # code_to_price_float: 统一为 {code: float}
    return codes, code_to_price_float


# ─────────────────────────────────────────────────────────────
# 2. 现金表 → 持仓表现金字段同步
# ─────────────────────────────────────────────────────────────

def _get_hkd_rate() -> float:
    """查询 HKD/CNY 即时汇率"""
    try:
        df = ak.fx_spot_quote()
        hk = df[df["货币对"].str.contains("HKD")]
        rate = (float(hk["买报价"].values[0]) + float(hk["卖报价"].values[0])) / 2
        return float(rate)
    except Exception:
        return 0.8659  # fallback


def sync_cash(
    client: LarkClient,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict:
    """
    从现金表读取各账户余额 → 按即时汇率换算 CNY → 更新持仓表现金记录市值字段。

    持仓表产品类型=现金的记录，需含字段：代码='CNY_CASH'、名称='现金合计(CNY)'。
    市值字段写入 field_id=fldS3UtFOG。

    返回 {"ok": bool, "total_cny": float, "updated": bool, "record_id": str}
    """
    # 现金表字段 ID
    CASH_FIELD_IDS = {
        "账户": "fldWVbD2AN",
        "余额": "fldKFKjrF1",
        "货币": "fld4wbqgnC",
        "备注": "fldyB3q3T6",
        "账户类型": "fldCAGTglz",
    }

    # 持仓表字段 ID
    HOLDINGS_FIELD_IDS = {
        "代码": "fld8lHFqd9",
        "名称": "fldvKDBTYp",
        "市值": "fldS3UtFOG",
        "总份额": "fld991DpTR",
        "总成本": "fldXOpKRfs",
        "持有收益": "fld3Wh4UlO",
        "持有收益率": "fld9k8xRi9",
        "年化收益率": "fldVz1CDok",
        "产品类型": "fldMIQN9D7",
    }

    if verbose:
        print("\n  [现金同步] 读取现金表...")

    # 1. 读取现金表（传入正确的 field_ids）
    cash_records = client.get_records(FEISHU_CASH_TABLE_ID, CASH_FIELD_IDS)
    if not cash_records:
        print("  [现金同步] 警告：现金表无记录")
        return {"ok": False, "total_cny": 0.0, "updated": False, "record_id": None}

    # 2. 计算总 CNY（含 HKD 换算）
    # 货币 SingleSelect 字段返回 ["CNY"] / ["HKD"]，取 [0]
    rate = _get_hkd_rate()
    total_cny = 0.0
    for r in cash_records:
        raw_ccy = r.get("货币", ["CNY"])
        ccy = raw_ccy[0] if isinstance(raw_ccy, list) else str(raw_ccy)
        bal = float(r.get("余额") or 0)
        if ccy == "CNY":
            total_cny += bal
        elif ccy == "HKD":
            total_cny += bal * rate  # 透支余额为负数时自动扣减

    total_cny = round(total_cny, 2)
    if verbose:
        print(f"  [现金同步] HKD/CNY 汇率={rate:.4f}")
        for r in cash_records:
            bal = float(r.get("余额") or 0)
            raw_ccy = r.get("货币", ["CNY"])
            ccy = raw_ccy[0] if isinstance(raw_ccy, list) else str(raw_ccy)
            ccy_val = bal if ccy == "CNY" else round(bal * rate, 2)
            acc_name = r.get("账户") or "(无名称)"
            print(f"    {acc_name:<22} {bal:>12,.2f} {ccy} → CNY {ccy_val:>12,.2f}")
        print(f"  [现金同步] 现金合计: {total_cny:,.2f} CNY")

    # 3. 找到持仓表现金记录（产品类型=现金，SingleSelect 返回列表）
    def _unwrap(v):
        """SingleSelect 返回 ["option"]，其他返回原值"""
        return v[0] if isinstance(v, list) else v

    holdings_records = client.get_records(HOLDINGS_TABLE_ID, HOLDINGS_FIELD_IDS)
    cash_record = None
    for r in holdings_records:
        if _unwrap(r.get("产品类型")) == "现金" and r.get("代码") == "CNY_CASH":
            cash_record = r
            break

    if not cash_record:
        print("  [现金同步] 错误：持仓表未找到产品类型=现金、代码=CNY_CASH 的记录")
        return {"ok": False, "total_cny": total_cny, "updated": False, "record_id": None}

    # get_records 返回 "_record_id"，upsert_record 用 record_id
    record_id = cash_record.get("_record_id")
    if not record_id:
        # 兜底：从列表中重新查找
        record_id = next(
            r.get("_record_id") for r in holdings_records
            if r.get("代码") == "CNY_CASH"
        )

    current_mv = float(cash_record.get("市值") or 0)

    if abs(current_mv - total_cny) < 0.01 and not dry_run:
        if verbose:
            print(f"  [现金同步] 市值无变化（{current_mv:,.2f}），跳过写入")
        return {"ok": True, "total_cny": total_cny, "updated": False, "record_id": record_id}

    if verbose:
        print(f"  [现金同步] 更新持仓表记录 {record_id}: 市值 {current_mv:,.2f} → {total_cny:,.2f}")

    # 4. 写入持仓表（field_id=fldS3UtFOG 即"市值"字段）
    ok = client.upsert_record(
        HOLDINGS_TABLE_ID,
        record_id,
        {"fldS3UtFOG": total_cny},
        dry_run=dry_run,
        verbose=verbose,
    )

    return {"ok": ok, "total_cny": total_cny, "updated": True, "record_id": record_id}


# ─────────────────────────────────────────────────────────────
# 3. 主入口
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="预览模式（不写入）")
    parser.add_argument("--force", action="store_true", help="强制更新所有记录")
    parser.add_argument("--rate-limit", type=float, default=0.8,
                       help="写入间隔秒数（默认 0.8s）")
    parser.add_argument("--on-error", default="continue",
                       choices=["continue", "abort"],
                       help="遇到错误处理策略")
    parser.add_argument("--quiet", "-q", action="store_true", help="静默模式")
    parser.add_argument(
        "--sync-dividends", action="store_true",
        help="自选表同步完成后，自动执行分红同步（含持仓全量重算）"
    )
    parser.add_argument(
        "--dividend-days", type=int, default=30,
        help="--sync-dividends 时查询近 N 天（默认 30天）"
    )
    parser.add_argument(
        "--sync-holdings", action="store_true",
        help="自选表同步完成后,执行持仓表全面同步(验证交易表+修正持仓表)"
    )
    parser.add_argument(
        "--sync-cash", action="store_true",
        help="读取现金表→HKD换算→更新持仓表现金记录（持仓表、市值字段）"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="全部完成后执行财神持仓报告"
    )
    parser.add_argument(
        "--report-time", default="20:30",
        help="--report 时指定报告时间(11:30/16:30/20:30)"
    )
    args = parser.parse_args()

    if not FEISHU_BASE_TOKEN:
        print("错误：未设置 FEISHU_BASE_TOKEN 环境变量", file=sys.stderr)
        sys.exit(1)

    setup_signal_handlers()
    client = LarkClient(FEISHU_BASE_TOKEN, upsert_delay=args.rate_limit)
    verbose = not args.quiet
    today = datetime.now().strftime("%Y-%m-%d")

    # Step 1-4: 同步自选表
    if verbose:
        print("=" * 60)
        print(f"  自选表同步 (日期: {today})")
        print("=" * 60)

    codes, code_to_price = sync_watchlist(
        client,
        dry_run=args.dry_run,
        force=args.force,
        rate_limit=args.rate_limit,
        verbose=verbose,
        today=today,
    )

    # 现金同步（可选，独立步骤，先于其他同步执行）
    if args.sync_cash:
        if verbose:
            print("\n" + "=" * 60)
            print("💵 执行现金同步（--sync-cash）")
            print("=" * 60)
        cash_result = sync_cash(
            client,
            dry_run=args.dry_run,
            verbose=verbose,
        )
        if verbose:
            print(f"  现金同步结果: {'成功' if cash_result['ok'] else '失败'} | "
                  f"合计 CNY {cash_result['total_cny']:,.2f} | "
                  f"{'已更新' if cash_result['updated'] else '无变化'}")

    # 分红同步（可选）
    if args.sync_dividends and not args.dry_run:
        if verbose:
            print("\n" + "=" * 60)
            print("📦 执行分红同步（--sync-dividends）")
            print("=" * 60)
        div_result = _du.sync_dividends(
            days=args.dividend_days,
            dry_run=False,
            verbose=verbose,
            rate_limit=args.rate_limit,
        )
        if verbose:
            print("=" * 60)
            print(f"  交易表: {div_result['trade_ok']} 成功 / {div_result['trade_fail']} 失败")
            if div_result.get("resync_updated", 0) > 0:
                print(f"  持仓表: 重算 {div_result['resync_updated']} 只成功 "
                      f"/ {div_result.get('resync_failed', 0)} 失败")
    elif args.sync_dividends and args.dry_run:
        if verbose:
            print("\n[DRY-RUN] 分红同步预览:")
        div_result = _du.sync_dividends(
            days=args.dividend_days,
            dry_run=True,
            verbose=verbose,
            rate_limit=args.rate_limit,
        )

    # 持仓表全面同步（可选）
    if args.sync_holdings:
        from trade_calc import sync_holdings
        if verbose:
            print("\n" + "=" * 60)
            print("📊 执行持仓表全面同步（--sync-holdings）")
            print("=" * 60)
        result = sync_holdings(
            client,
            dry_run=args.dry_run,
            verbose=verbose,
            rate_limit=args.rate_limit,
        )
        if args.dry_run:
            print(f"  [DRY-RUN] 持仓表: {len(result['fixed_holdings'])} 只需修正  交易表问题: {len(result['issues'])} 条")
        else:
            print(f"  持仓表: {len(result['fixed_holdings'])} 只已修正  交易表问题: {len(result['issues'])} 条")

    # 财神报告（可选）
    if args.report:
        if verbose:
            print("\n" + "=" * 60)
            print(f"📈 执行财神持仓报告（--report-time {args.report_time}）")
            print("=" * 60)
        _report_cmd = [
            "python3", str(WORKSPACE / "feishu-ledger" / "scripts" / "report.py"),
            "--time", args.report_time,
        ]
        if args.dry_run:
            _report_cmd.append("--dry-run")
        result = subprocess.run(
            _report_cmd,
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "PYTHONPATH": str(WORKSPACE / "feishu-ledger" / "scripts")},
        )
        if verbose:
            if result.stdout:
                print(result.stdout)
            if result.returncode != 0 and result.stderr:
                print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            print(f"[WARN] 报告执行失败（退出码 {result.returncode}）", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
