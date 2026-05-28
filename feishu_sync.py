"""
飞书同步脚本 - 自选表 + 持仓表一体化更新

执行流程:
  Step 1: 读取自选表全部记录
  Step 2: 过滤需要更新的记录（date < today 或 --force）
  Step 3: 爬取最新价与涨幅
  Step 4: 写入自选表 (最新价、涨幅、更新日期)
  Step 5: 读取持仓表全部记录
  Step 6: 合并自选表最新价 → 计算市值、持有收益、持有收益率
  Step 7: 写入持仓表 (市值、持有收益、持有收益率)

用法:
    python feishu_sync.py --dry-run     # 预览
    python feishu_sync.py               # 正式执行
    python feishu_sync.py --force       # 强制更新所有记录
    python feishu_sync.py --rate-limit 1.5
    python feishu_sync.py --on-error abort
"""

import argparse
import sys
import time
from datetime import datetime
from typing import Optional

import crawler
from feishu_base import LarkClient, setup_signal_handlers, add_common_args, _interrupted
from feishu_constants import (
    FEISHU_BASE_TOKEN,
    WATCHLIST_TABLE_ID,
    WATCHLIST_FIELD_IDS,
    HOLDINGS_TABLE_ID,
    HOLDINGS_FIELD_IDS,
    UPSERT_DELAY,
)


# ─────────────────────────────────────────────────────────────
# 1. 自选表同步 (Step 1-4)
# ─────────────────────────────────────────────────────────────

def sync_watchlist(
    client: LarkClient,
    dry_run: bool,
    force: bool,
    rate_limit: float,
    verbose: bool,
    on_error: str,
    today: str,
):
    """
    Step 1-4: 读取自选表 → 过滤 → 爬取 → 写入
    返回: (更新成功的代码列表, 代码→最新价映射)
    """
    # Step 1: 读取全部记录
    if verbose:
        print("\n[Step 1/7] 读取自选表记录...")
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
        print(f"\n[Step 2/7] 提取 {today} 之前未更新的代码（{mode}模式，{len(codes)}/{len(records)} 条需更新）")
        for c in codes[:5]:
            print(f"    {c}")
        if len(codes) > 5:
            print(f"    ... 共 {len(codes)} 个")

    if not codes:
        if verbose:
            print("  [INFO] 无需更新的记录")
        return [], {}

    # Step 3: 爬取数据
    if verbose:
        print(f"\n[Step 3/7] 爬取最新价与涨幅...")
    results = crawler.crawl(codes)
    code_to_result = {r["code"]: r for r in results}
    matched_count = sum(1 for r in results if r["matched"])
    if verbose:
        print(f"  成功获取: {matched_count}/{len(results)}")

    # Step 4: 写入自选表
    if verbose:
        print(f"\n[Step 4/7] {'预览更新' if dry_run else '写入更新'}（自选表）...")

    success = 0
    skipped = 0
    failed = 0
    code_to_price = {}  # 用于后续持仓表计算

    for record in records_to_update:
        global _interrupted
        if _interrupted:
            print("\n被中断，退出循环", file=sys.stderr)
            break

        code = record["代码"]
        result = code_to_result.get(code)

        if not result or not result["matched"]:
            if verbose:
                print(f"  [SKIP] {code}: 未匹配数据，跳过")
            skipped += 1
            continue

        price = result["price"]
        change_pct = result["change_pct"]
        date = result.get("date")

        # 记录价格映射（用于持仓表计算）
        code_to_price[code] = price

        if verbose:
            price_str = f"{price:.4f}" if price is not None else "N/A"
            change_str = change_pct or "N/A"
            date_str = date or "N/A"
            print(f"  -> {code:12s} 最新价={price_str:>10s}  涨幅={change_str:>8s}  日期={date_str:>10s}")

        # 构造 upsert 字段
        fields = {}
        if price is not None:
            fields[WATCHLIST_FIELD_IDS["最新价"]] = price
        if change_pct is not None:
            fields[WATCHLIST_FIELD_IDS["涨幅"]] = change_pct
        if date:
            fields[WATCHLIST_FIELD_IDS["更新日期"]] = date

        ok = client.upsert_record(
            WATCHLIST_TABLE_ID,
            record["_record_id"],
            fields,
            dry_run=dry_run,
            verbose=False,
        )

        if ok:
            success += 1
        else:
            failed += 1
            if on_error == "abort":
                print("  根据 --on-error=abort 终止流程", file=sys.stderr)
                break

        if not dry_run and rate_limit > 0:
            time.sleep(rate_limit)

    if verbose:
        action = "预览" if dry_run else "写入"
        print(f"\n  [STATS] 自选表: 需更新 {len(records_to_update)} 条 | {action}成功 {success} | 跳过 {skipped} | 失败 {failed}")

    return codes, code_to_price


# ─────────────────────────────────────────────────────────────
# 2. 持仓表同步 (Step 5-7)
# ─────────────────────────────────────────────────────────────

def sync_holdings(
    client: LarkClient,
    code_to_price: dict,
    dry_run: bool,
    force: bool,
    rate_limit: float,
    verbose: bool,
    on_error: str,
    today: str,
):
    """
    Step 5-7: 读取持仓表 → 合并自选表价格 → 计算市值/收益 → 写入
    """
    # Step 5: 读取持仓表
    if verbose:
        print(f"\n[Step 5/7] 读取持仓表记录...")
    holdings_records = client.get_records(HOLDINGS_TABLE_ID, HOLDINGS_FIELD_IDS)
    if verbose:
        print(f"  共读取 {len(holdings_records)} 条持仓记录")

    if not holdings_records:
        print("  [WARN] 持仓表为空，跳过持仓同步")
        return

    # Step 6: 过滤需要更新的记录并计算
    records_to_update = []
    for rec in holdings_records:
        code = str(rec.get("代码", "")).strip()
        if not code:
            continue

        # 尝试用原始 code 查价格
        price = code_to_price.get(code)
        # 尝试加前缀变体（如 "000333" → "sz000333"）
        if price is None:
            for prefix in ("sz", "sh", "hk"):
                price = code_to_price.get(f"{prefix}{code}")
                if price is not None:
                    break

        if price is None:
            if verbose:
                print(f"  [SKIP] {code}: 自选表中无最新价")
            continue

        shares = rec.get("总份额")
        cost = rec.get("总成本")

        shares = float(shares) if shares is not None else 0
        cost = float(cost) if cost is not None else 0

        if shares == 0 or cost == 0:
            if verbose:
                print(f"  [SKIP] {code}: 份额或成本为0")
            continue

        # 计算市值、持有收益、持有收益率
        market_value = round(price * shares, 2)
        profit = round(market_value - cost, 2)
        profit_pct = f"{round((profit / cost) * 100, 2)}%"

        # 检查是否需要更新
        needs_update = force
        if not needs_update:
            old_mv = rec.get("市值")
            old_profit = rec.get("持有收益")
            if old_mv is None or old_profit is None:
                needs_update = True
            else:
                try:
                    if abs(market_value - float(old_mv)) > 0.01 or abs(profit - float(old_profit)) > 0.01:
                        needs_update = True
                except (ValueError, TypeError):
                    needs_update = True

        if needs_update:
            records_to_update.append({
                **rec,
                "price": price,
                "new_market_value": market_value,
                "new_profit": profit,
                "new_profit_pct": profit_pct,
            })

    if verbose:
        mode = "强制" if force else "增量"
        print(f"\n[Step 6/7] 过滤{mode}模式需更新的持仓记录: {len(records_to_update)} 条")

    if not records_to_update:
        if verbose:
            print("  [INFO] 无需更新的持仓记录")
        return

    # Step 7: 写入持仓表
    if verbose:
        print(f"\n[Step 7/7] {'预览更新' if dry_run else '写入更新'}（持仓表）...")

    success = 0
    skipped = 0
    failed = 0

    for rec in records_to_update:
        global _interrupted
        if _interrupted:
            print("\n被中断，退出循环", file=sys.stderr)
            break

        if verbose:
            code = rec.get("代码", "")
            name = str(rec.get("名称", ""))[:8]
            print(f"  -> {str(code):12s} 名称={name:8s} "
                  f"最新价={rec.get('price'):>10} 市值={rec['new_market_value']:>12} "
                  f"持有收益={rec['new_profit']:>10} 收益率={rec['new_profit_pct']:>8s}")

        # 构造 upsert 字段
        fields = {}
        fields[HOLDINGS_FIELD_IDS["市值"]] = rec["new_market_value"]
        fields[HOLDINGS_FIELD_IDS["持有收益"]] = rec["new_profit"]
        fields[HOLDINGS_FIELD_IDS["持有收益率"]] = rec["new_profit_pct"]

        ok = client.upsert_record(
            HOLDINGS_TABLE_ID,
            rec["_record_id"],
            fields,
            dry_run=dry_run,
            verbose=False,
        )

        if ok:
            success += 1
        else:
            failed += 1
            if on_error == "abort":
                print("  根据 --on-error=abort 终止流程", file=sys.stderr)
                break

        if not dry_run and rate_limit > 0:
            time.sleep(rate_limit)

    if verbose:
        action = "预览" if dry_run else "写入"
        print(f"\n  [STATS] 持仓表: 需更新 {len(records_to_update)} 条 | {action}成功 {success} | 跳过 {skipped} | 失败 {failed}")


# ─────────────────────────────────────────────────────────────
# 3. 主同步逻辑
# ─────────────────────────────────────────────────────────────

def sync(
    dry_run: bool = False,
    force: bool = False,
    rate_limit: float = UPSERT_DELAY,
    verbose: bool = True,
    on_error: str = "skip",
):
    """
    主流程:
      Step 1-4: 自选表同步（读取 → 过滤 → 爬取 → 写入）
      Step 5-7: 持仓表同步（读取 → 合并价格 → 计算 → 写入）
    """
    today = datetime.now().strftime("%Y-%m-%d")

    if verbose:
        print("=" * 60)
        title = "[DRY-RUN] 预览模式（不执行写入）" if dry_run else "同步飞书数据"
        print(f"  {title}")
        if not force:
            print(f"  增量模式：只更新 {today} 之前未更新的记录")
        else:
            print(f"  强制模式：更新所有记录")
        print("=" * 60)

    # 初始化 lark-cli client
    client = LarkClient(FEISHU_BASE_TOKEN, rate_limit)

    # Step 1-4: 自选表同步
    updated_codes, code_to_price = sync_watchlist(
        client, dry_run, force, rate_limit, verbose, on_error, today
    )

    # Step 5-7: 持仓表同步（依赖自选表最新价）
    sync_holdings(
        client, code_to_price, dry_run, force, rate_limit, verbose, on_error, today
    )

    # 汇总
    if verbose:
        print(f"\n{'=' * 60}")
        print(f"  [OK] 同步完成")
        print(f"{'=' * 60}")


# ─────────────────────────────────────────────────────────────
# 4. CLI 入口
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="飞书同步工具 - 自选表 + 持仓表一体化更新",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python feishu_sync.py                         # 增量更新
  python feishu_sync.py --dry-run               # 预览模式
  python feishu_sync.py --force                 # 强制更新所有记录
  python feishu_sync.py --rate-limit 1.5        # 自定义写入间隔 1.5s
  python feishu_sync.py --on-error abort         # 遇错立即终止
        """,
    )
    add_common_args(parser, UPSERT_DELAY)
    args = parser.parse_args()

    setup_signal_handlers()

    try:
        sync(
            dry_run=args.dry_run,
            force=args.force,
            rate_limit=args.rate_limit,
            verbose=not args.quiet,
            on_error=args.on_error,
        )
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n用户中断", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
