"""
飞书自选表同步脚本 - 读取自选表的全部代码，爬取最新价与涨幅，写回表格。

用法:
    python feishu_sync.py              # 完整执行（读取 → 爬取 → 写入）
    python feishu_sync.py --dry-run    # 预览模式，只打印计划不执行写入
    python feishu_sync.py --rate-limit 1.5   # 自定义写入间隔（秒）
"""

import argparse
import json
import platform
import subprocess
import sys
import time
from typing import Optional

import crawler
from feishu_constants import FEISHU_BASE_TOKEN, TABLE_ID, FIELD_IDS, UPSERT_DELAY

# Windows 下 npm 安装的 CLI 是 .cmd 包装器
_lark_cli = "lark-cli.cmd" if platform.system() == "Windows" else "lark-cli"


# ============================================================
# 1. 调用 lark-cli 的辅助函数
# ============================================================

def _run_lark(args: list[str]) -> dict:
    """
    调用 lark-cli 并返回解析后的 JSON dict。
    如果命令失败则抛出 RuntimeError。
    """
    cmd = [_lark_cli] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=False,  # binary mode to avoid encoding issues
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"lark-cli 超时: {' '.join(cmd)}")
    except FileNotFoundError:
        raise RuntimeError(f"未找到 {_lark_cli}，请确保已安装: npm install -g @lark-opdev/lark-cli")

    # 尝试 UTF-8 解码，失败则用 GBK
    stdout = _decode_output(proc.stdout)
    stderr = _decode_output(proc.stderr)

    if proc.returncode != 0:
        err = stderr.strip() or "(no stderr)"
        raise RuntimeError(f"lark-cli 失败 (exit={proc.returncode}): {err}")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"lark-cli 输出解析失败: {e}\n输出: {stdout[:500]}")


def _decode_output(data: bytes) -> str:
    """尝试用 UTF-8 解码，回退到 GBK"""
    for enc in ("utf-8", "gbk", "gb2312", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


# ============================================================
# 2. 读取自选表全部记录
# ============================================================

def get_all_records() -> list[dict]:
    """
    读取自选表全部记录，返回列表。
    每条记录格式: {
        "record_id": str,
        "code": str,
        "name": str,
        "current_price": float | None,
        "current_change": str | None,
        "product_type": str | None,
    }
    """
    records = []
    page_token = None

    while True:
        args = [
            "base", "+record-list",
            "--base-token", FEISHU_BASE_TOKEN,
            "--table-id", TABLE_ID,
            "--as", "user",
            "--format", "json",
            "--limit", "200",
        ]
        if page_token:
            args += ["--offset", page_token]

        result = _run_lark(args)
        data = result.get("data", {})

        record_ids = data.get("record_id_list", [])
        field_ids = data.get("field_id_list", [])
        rows = data.get("data", [])

        # 建立字段名 → 索引的映射
        field_index = {fid: idx for idx, fid in enumerate(field_ids)}

        code_idx = field_index.get(FIELD_IDS["代码"])
        name_idx = field_index.get(FIELD_IDS["名称"])
        price_idx = field_index.get(FIELD_IDS["最新价"])
        change_idx = field_index.get(FIELD_IDS["涨幅"])
        type_idx = field_index.get(FIELD_IDS["产品类型"])

        for i, record_id in enumerate(record_ids):
            row = rows[i] if i < len(rows) else []
            code = row[code_idx] if code_idx is not None and code_idx < len(row) else None
            name = row[name_idx] if name_idx is not None and name_idx < len(row) else None
            price = row[price_idx] if price_idx is not None and price_idx < len(row) else None
            change = row[change_idx] if change_idx is not None and change_idx < len(row) else None
            prod_type = row[type_idx] if type_idx is not None and type_idx < len(row) else None

            records.append({
                "record_id": record_id,
                "code": str(code) if code else "",
                "name": str(name) if name else "",
                "current_price": price,
                "current_change": change,
                "product_type": prod_type,
            })

        if not data.get("has_more", False):
            break
        page_token = data.get("next_page_token", "")

    return records


# ============================================================
# 3. 写入单条记录
# ============================================================

def update_record(record_id: str, price: Optional[float], change_pct: Optional[str],
                  dry_run: bool = False, verbose: bool = True) -> bool:
    """
    更新单条记录的 最新价 和 涨幅。

    返回 True 表示成功（或 dry-run），False 表示失败。
    """
    # 构造只包含需要更新的字段
    cell_value = {}
    if price is not None:
        cell_value[FIELD_IDS["最新价"]] = price
    if change_pct is not None:
        cell_value[FIELD_IDS["涨幅"]] = change_pct

    if not cell_value:
        if verbose:
            print(f"  [SKIP] {record_id}: 无数据可更新（price/change 均为 None）")
        return True

    cell_json = json.dumps(cell_value, ensure_ascii=False)

    if dry_run:
        if verbose:
            print(f"  [DRY-RUN] 更新 {record_id}: {cell_json}")
        return True

    args = [
        "base", "+record-upsert",
        "--base-token", FEISHU_BASE_TOKEN,
        "--table-id", TABLE_ID,
        "--record-id", record_id,
        "--json", cell_json,
        "--as", "user",
    ]

    try:
        _run_lark(args)
        return True
    except RuntimeError as e:
        print(f"  [FAIL] 更新 {record_id} 失败: {e}", file=sys.stderr)
        return False


# ============================================================
# 4. 主同步逻辑
# ============================================================

def sync(dry_run: bool = False, rate_limit: float = UPSERT_DELAY,
         verbose: bool = True, max_retries: int = 3,
         on_error: str = "skip"):
    """
    主流程: 读取自选表 → 爬取数据 → 逐条写回
    """
    if verbose:
        print("=" * 60)
        title = "[DRY-RUN] 预览模式（不执行写入）" if dry_run else "同步自选表行情数据"
        print(f"  {title}")
        print("=" * 60)

    # Step 1: 读取全部记录
    if verbose:
        print("\n[Step 1/4] 读取自选表记录...")
    records = get_all_records()
    if verbose:
        print(f"  共读取 {len(records)} 条记录")

    if not records:
        print("  [WARN] 空表，退出")
        return

    # Step 2: 提取代码列表
    codes = [r["code"] for r in records if r["code"]]
    if verbose:
        print(f"\n[Step 2/4] 提取代码列表（{len(codes)} 个）")
        for c in codes[:5]:
            print(f"    {c}")
        if len(codes) > 5:
            print(f"    ... 共 {len(codes)} 个")

    # Step 3: 爬取数据
    if verbose:
        print(f"\n[Step 3/4] 爬取最新价与涨幅...")

    results = crawler.crawl(codes)
    code_to_result = {r["code"]: r for r in results}

    matched_count = sum(1 for r in results if r["matched"])
    if verbose:
        print(f"  成功获取: {matched_count}/{len(results)}")

    # Step 4: 逐条写回
    if verbose:
        print(f"\n[Step 4/4] {'预览更新' if dry_run else '写入更新'}...")

    success = 0
    skipped = 0
    failed = 0

    for record in records:
        code = record["code"]
        result = code_to_result.get(code)

        if not result or not result["matched"]:
            if verbose:
                print(f"  [SKIP] {code}: 未匹配数据，跳过")
            skipped += 1
            continue

        price = result["price"]
        change_pct = result["change_pct"]

        if verbose:
            price_str = f"{price:.4f}" if price is not None else "N/A"
            change_str = change_pct or "N/A"
            print(f"  -> {code:12s} 最新价={price_str:>10s}  涨幅={change_str:>8s}")

        ok = update_record(
            record["record_id"],
            price,
            change_pct,
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

        # 写入后等待，避免触发限流
        if not dry_run and rate_limit > 0:
            time.sleep(rate_limit)

    # 汇总
    if verbose:
        action = "预览" if dry_run else "写入"
        print(f"\n{'=' * 60}")
        print(f"  [OK] 同步完成")
        print(f"  [STATS] 总计: {len(records)} 条 | {action}成功: {success} | 跳过: {skipped} | 失败: {failed}")
        print(f"{'=' * 60}")


# ============================================================
# 5. CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="飞书自选表行情同步工具 - 读取自选表代码，爬取最新价与涨幅并写回",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python feishu_sync.py                    # 完整执行
  python feishu_sync.py --dry-run          # 预览模式
  python feishu_sync.py --rate-limit 1.5   # 自定义写入间隔 1.5s
  python feishu_sync.py --on-error abort   # 遇错立即终止
        """,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="预览模式：打印计划写入内容，不实际执行 upsert"
    )
    parser.add_argument(
        "--rate-limit", type=float, default=UPSERT_DELAY,
        help=f"两次写入之间的间隔秒数（默认 {UPSERT_DELAY}s）"
    )
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="单次 upsert 失败后的最大重试次数（默认 3）"
    )
    parser.add_argument(
        "--on-error", choices=["skip", "abort"], default="skip",
        help="遇到 upsert 错误时的处理策略：skip（跳过继续）或 abort（终止）（默认 skip）"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="静默模式：仅输出必要的日志"
    )

    args = parser.parse_args()

    try:
        sync(
            dry_run=args.dry_run,
            rate_limit=args.rate_limit,
            verbose=not args.quiet,
            max_retries=args.max_retries,
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
