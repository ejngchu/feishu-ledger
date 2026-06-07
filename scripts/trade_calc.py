"""
交易录入与持仓核算脚本

功能:
  1. 写入交易记录(买入/卖出/定投/分红)到交易表
  2. 以交易表为唯一数据源,核算持仓表的全部字段(份额/成本/市值/收益/收益率/年化)
  3. 支持单标的操作或全量重算(--resync-all)
  4. 与 feishu_sync.py 配合:自选表行情更新后,调用本脚本重算持仓表

用法:
  # 单标的:写入交易 + 更新持仓
  python trade_calc.py --code 007994 --direction sell --shares 100 --amount 300 --nav 3.0 --date "2026-06-01 10:00"

  # 单标的:仅列出历史交易
  python trade_calc.py --code 007994 --list

  # 单标的:仅重算持仓(不写入交易)
  python trade_calc.py --code 007994 --recalc

  # 全量重算(所有持仓标的)
  python trade_calc.py --resync-all

  # 全量重算 + 只显示差异
  python trade_calc.py --resync-all --dry-run

字段规范:
  买入/定投:份额>0, 金额>0, 成本=金额(正数)
  卖出:      份额<0, 金额<0, 成本=-avg_cost×shares(负数)
  现金分红:  份额=0, 金额=+分红总额, 成本=-分红总额(负数,体现成本减少)
  分红再投:  份额=+新增股数, 金额=0, 成本=0

持仓核算规则:
  总份额 = 累计买入 + 累计定投 + 累计分红再投 - 累计卖出
  总成本 = 累计买入成本 + 累计定投成本 - 累计卖出成本 - 现金分红金额
  市值   = 总份额 × 最新价
  持有收益 = 市值 - 总成本
  持有收益率 = 持有收益 / 总成本 × 100%
  年化收益率 = (市值/总成本)^(365/持有天数) - 1

注意:
  - 港股持仓(市值以HKD计)按即时汇率折算为CNY后计入总市值
  - 分红(方向=分红)的成本字段必须为负值,以体现每股成本减少
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# 添加 scripts 目录到路径
_scripts = Path(__file__).parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

import crawler
from feishu_base import LarkClient, setup_signal_handlers
from feishu_config import (
    FEISHU_BASE_TOKEN,
    HOLDINGS_TABLE_ID,
    HOLDINGS_FIELD_IDS,
    TRADE_TABLE_ID,
    TRADE_FIELD_IDS,
    WATCHLIST_TABLE_ID,
    WATCHLIST_FIELD_IDS,
    load_price_cache,
)


# ── 方向标准化 ─────────────────────────────────────────

DIRECTION_ALIASES = {
    "buy": "买入", "b": "买入",
    "sell": "卖出", "s": "卖出",
    "dca": "定投", "定投": "定投", "d": "定投",
    "dividend": "分红", "分红": "分红",
}

CASH_DIV_ALIASES = {"cash", "现金分红", "cd"}
BONUS_DIV_ALIASES = {"bonus", "送股", "再投", "分红再投", "转增"}


def parse_direction(d: str) -> list[str]:
    """返回标准化方向列表"""
    d_lower = d.lower().strip()
    if d_lower in DIRECTION_ALIASES:
        return [DIRECTION_ALIASES[d_lower]]
    for alias in ("买入", "卖出", "定投", "分红"):
        if alias in d:
            return [alias]
    return [d]


# ── 核心计算函数 ────────────────────────────────────────

def calc_trade_summary(trade_records: list[dict]) -> dict:
    """
    以交易表为基准,计算所有持仓指标。
    支持:买入、卖出、定投、现金分红、分红再投(送股/转增)
    """
    buy_shares = 0.0     # 买入+定投份额合计
    buy_cost = 0.0        # 买入+定投成本合计
    sell_shares = 0.0    # 卖出份额合计
    sell_cost = 0.0      # 卖出成本合计(正数)
    div_shares = 0.0      # 分红再投(送股)份额增加合计
    cash_div_cost = 0.0  # 现金分红导致的成本减少合计(正数)

    for r in trade_records:
        direction = r.get("方向", [""])[0] if r.get("方向") else ""
        shares = r.get("份额", 0) or 0
        cost_val = r.get("成本", 0) or 0

        if direction == "买入":
            buy_shares += shares
            buy_cost += cost_val

        elif direction == "定投":
            buy_shares += shares
            buy_cost += cost_val

        elif direction == "卖出":
            sell_shares += abs(shares)
            sell_cost += abs(cost_val)

        elif direction == "分红":
            # 判断是现金分红还是分红再投
            # 现金分红:份额=0, 金额>0, 成本为负(成本减少)
            # 分红再投:份额>0, 成本=0(份额增加,成本不变)
            if shares == 0:
                # 现金分红:cost_val 为负数(减少的成本)
                cash_div_cost += abs(cost_val)
            else:
                # 分红再投
                div_shares += shares

    remaining_shares = round(buy_shares - sell_shares + div_shares, 2)
    remaining_cost = round(buy_cost - sell_cost - cash_div_cost, 2)
    avg_cost_per_share = round(buy_cost / buy_shares, 5) if buy_shares else 0

    return {
        "buy_shares": buy_shares,
        "buy_cost": buy_cost,
        "sell_shares": sell_shares,
        "sell_cost": sell_cost,
        "div_shares": div_shares,
        "cash_div_cost": cash_div_cost,
        "remaining_shares": remaining_shares,
        "remaining_cost": remaining_cost,
        "avg_cost_per_share": avg_cost_per_share,
    }


def calc_new_trade(
    direction: str,
    shares: float,
    amount: float,
    nav: float,
    summary: dict,
) -> dict:
    """
    计算新交易记录的完整字段值。
    返回 shares_signed, amount_signed, cost_signed, profit, profit_pct
    """
    if direction == "卖出":
        avg_cost = summary["avg_cost_per_share"]
        cost_sold = round(abs(shares) * avg_cost, 2)
        profit = round(abs(amount) - cost_sold, 2)
        profit_pct = f"{round(profit / cost_sold * 100, 2):.2f}%" if cost_sold else "0%"
        return {
            "shares": -abs(shares),
            "amount": -abs(amount),
            "cost": -cost_sold,
            "profit": profit,
            "profit_pct": profit_pct,
            "nav": nav,
        }

    elif direction == "定投":
        # 定投 = 买入
        cost_per_share = round(amount / abs(shares), 4) if abs(shares) else 0
        return {
            "shares": abs(shares),
            "amount": abs(amount),
            "cost": abs(amount),
            "profit": 0,
            "profit_pct": "0%",
            "nav": nav,
            "cost_per_share": cost_per_share,
        }

    elif direction == "分红":
        # 分两种:
        # 1. 现金分红:shares=0, amount=分红总额 → cost = -amount(成本减少)
        # 2. 分红再投:shares>0, amount=0 → cost=0, shares=新增份额
        if shares == 0:
            # 现金分红
            return {
                "shares": 0,
                "amount": abs(amount),
                "cost": -abs(amount),  # 成本减少
                "profit": 0,
                "profit_pct": "0%",
                "nav": nav,
            }
        else:
            # 分红再投(送股/转增)
            return {
                "shares": abs(shares),
                "amount": 0,
                "cost": 0,
                "profit": 0,
                "profit_pct": "0%",
                "nav": nav,
            }

    else:  # 买入
        cost_per_share = round(amount / abs(shares), 4) if abs(shares) else 0
        return {
            "shares": abs(shares),
            "amount": abs(amount),
            "cost": abs(amount),
            "profit": 0,
            "profit_pct": "0%",
            "nav": nav,
            "cost_per_share": cost_per_share,
        }


def recalc_holdings(
    code: str,
    name: str,
    trade_summary: dict,
    current_price: float,
    first_buy_date: Optional[str],
    today: date,
) -> dict:
    """
    重算持仓(市值/收益/收益率/年化),基于交易汇总 + 最新价格。
    不涉及飞书 API 调用。
    """
    remaining_shares = trade_summary["remaining_shares"]
    remaining_cost = trade_summary["remaining_cost"]

    if remaining_shares <= 0 or remaining_cost <= 0:
        return {
            "total_shares": remaining_shares,
            "total_cost": remaining_cost,
            "market_value": 0.0,
            "profit": -remaining_cost,
            "profit_pct": "-100.00%",
            "annual_pct": "0%",
            "nav": current_price,
        }

    market_value = round(remaining_shares * current_price, 2)
    profit = round(market_value - remaining_cost, 2)
    profit_pct = round(profit / remaining_cost * 100, 2)

    annual_pct_str = "0%"
    if first_buy_date and remaining_shares > 0 and remaining_cost > 0 and market_value > 0:
        try:
            buy_date = date.fromisoformat(first_buy_date.replace("/", "-"))
            days = (today - buy_date).days
            if days > 0:
                annual_val = round(((market_value / remaining_cost) ** (365.25 / days) - 1) * 100, 2)
                annual_pct_str = f"{annual_val:.2f}%"
        except Exception:
            pass

    return {
        "total_shares": remaining_shares,
        "total_cost": remaining_cost,
        "market_value": market_value,
        "profit": profit,
        "profit_pct": f"{profit_pct:.2f}%",
        "annual_pct": annual_pct_str,
        "nav": current_price,
    }


# ── 飞书操作 ─────────────────────────────────────────

def get_all_trade_records(client: LarkClient, code: str) -> list[dict]:
    """读取某标的全部交易记录(从交易表)"""
    records = client.get_records(TRADE_TABLE_ID, TRADE_FIELD_IDS)
    result = []
    for r in records:
        if (r.get("代码") or "").strip() == code.strip():
            result.append(r)
    result.sort(key=lambda x: x.get("交易日期", ""))
    return result


def get_all_holdings(client: LarkClient) -> list[dict]:
    """读取全部持仓记录"""
    return client.get_records(HOLDINGS_TABLE_ID, HOLDINGS_FIELD_IDS)


def get_current_price(client: LarkClient, code: str) -> tuple[float, str]:
    """
    获取最新价:优先读缓存,否则爬取。
    返回 (price, source)
    """
    cache = load_price_cache()
    cached = cache.get(code)
    if cached and cached.get("price"):
        return cached["price"], "cache"
    # 爬取
    results = crawler.crawl([code])
    if results and results[0].get("matched"):
        return results[0]["price"], "crawl"
    return 0.0, "none"


def upsert_holdings_record(
    client: LarkClient,
    record_id: str,
    fields: dict,
) -> bool:
    """写入持仓表单条记录"""
    ok = client.upsert_record(
        HOLDINGS_TABLE_ID,
        record_id,
        fields,
        verbose=False,
    )
    return bool(ok)


# ── 汇率换算 ─────────────────────────────────────────

def get_hkd_cny_rate() -> float:
    """
    获取 HKD/CNY 即时汇率。
    优先级:akshare 实时 → 缓存 → MEMORY.md 参考值(兜底)
    """
    import akshare as ak
    import math
    # 优先:从 akshare 实时获取
    try:
        df = ak.fx_spot_quote()
        hk = df[df["货币对"].str.contains("HKD")]
        if not hk.empty:
            bid = float(hk["买报价"].values[0])
            ask = float(hk["卖报价"].values[0])
            rate = round((bid + ask) / 2, 4)
            if rate > 0 and not math.isnan(rate):
                return rate
    except Exception:
        pass
    # 其次:从缓存读取（注意：nan 是 falsy，直接用 and 会跳过走到兜底）
    try:
        import math
        cache = load_price_cache()
        cached_rate = cache.get("hkd_rate")
        if cached_rate is not None and not (isinstance(cached_rate, float) and math.isnan(cached_rate)) and cached_rate > 0:
            return float(cached_rate)
    except Exception:
        pass
    # 兜底:MEMORY.md 参考值(2025-05-25)
    return 0.8659


# ── 全量重算 ─────────────────────────────────────────

def resync_all(
    client: LarkClient,
    dry_run: bool = False,
    verbose: bool = True,
    rate_limit: float = 0.8,
) -> dict:
    """
    全量重算:读取全部持仓 → 对每只标的从交易表核算 → 更新持仓表。
    港股(HKD)持仓市值按即时汇率折算为CNY后计入总市值。
    """
    today = date.today()
    fx_rate = get_hkd_cny_rate()

    if verbose:
        print(f"\n{'='*60}")
        print(f"  全量持仓重算 (日期: {today})")
        print(f"  HKD/CNY 汇率: {fx_rate}")
        print(f"{'='*60}")

    holdings_records = get_all_holdings(client)
    if not holdings_records:
        return {"updated": 0, "failed": 0, "total_mv_cny": 0, "fx_rate": fx_rate}

    # 按代码建立持仓索引
    holdings_by_code = {}
    for h in holdings_records:
        code = (h.get("代码") or "").strip()
        if code:
            holdings_by_code[code] = h

    # 按代码建立交易记录索引(避免重复读取)
    # 一次性读取全部交易记录,按代码分组
    all_trades = client.get_records(TRADE_TABLE_ID, TRADE_FIELD_IDS)
    trades_by_code: dict[str, list] = {}
    for t in all_trades:
        code = (t.get("代码") or "").strip()
        if code:
            if code not in trades_by_code:
                trades_by_code[code] = []
            trades_by_code[code].append(t)
    # 各组内按日期排序
    for code in trades_by_code:
        trades_by_code[code].sort(key=lambda x: x.get("交易日期", ""))

    updated, failed = 0, 0
    total_mv_cny = 0.0
    total_mv_hkd = 0.0
    fx_rate_used = fx_rate

    # 打印头
    if verbose:
        print(f"\n{'代码':<12} {'名称':<18} {'份额':>12} {'总成本':>12} {'市值':>12} {'收益':>10} {'收益率':>8} {'货币'}")
        print("-"*100)

    for code, h_record in holdings_by_code.items():
        record_id = h_record["_record_id"]
        name = h_record.get("名称", "")
        raw_currency = h_record.get("货币")
        # 兼容 list ["HKD"] 或 string "HKD"
        if isinstance(raw_currency, list):
            currency = raw_currency[0] if raw_currency else "CNY"
        elif isinstance(raw_currency, str):
            currency = raw_currency
        else:
            currency = "CNY"
        is_hkd = currency == "HKD"

        trades = trades_by_code.get(code, [])
        summary = calc_trade_summary(trades)

        # 找首次买入日
        first_buy = next(
            (t.get("交易日期", "")[:10] for t in trades if t.get("方向", [""])[0] in ("买入", "定投")),
            None
        )

        # 获取当前价格
        current_price, price_source = get_current_price(client, code)
        if current_price <= 0:
            # 价格获取失败,沿用持仓表的市值
            current_price = h_record.get("市值", 0) / h_record.get("总份额", 1) if h_record.get("总份额", 0) > 0 else 0

        result = recalc_holdings(code, name, summary, current_price, first_buy, today)

        mv = result["market_value"]
        if is_hkd:
            total_mv_hkd += mv
            mv_cny = round(mv * fx_rate, 2)
            total_mv_cny += mv_cny
        else:
            total_mv_cny += mv

        fields = {
            HOLDINGS_FIELD_IDS["总份额"]: result["total_shares"],
            HOLDINGS_FIELD_IDS["总成本"]: result["total_cost"],
            HOLDINGS_FIELD_IDS["市值"]: mv,
            HOLDINGS_FIELD_IDS["持有收益"]: result["profit"],
            HOLDINGS_FIELD_IDS["持有收益率"]: result["profit_pct"],
        }
        if "年化收益率" in HOLDINGS_FIELD_IDS:
            fields[HOLDINGS_FIELD_IDS["年化收益率"]] = result["annual_pct"]

        if verbose:
            pct = result["profit_pct"]
            print(f"{code:<12} {name:<18} {result['total_shares']:>12} {result['total_cost']:>12,.2f} "
                  f"{mv:>12,.2f} {result['profit']:>+10,.2f} {pct:>8} {currency}")

        if dry_run:
            updated += 1
            continue

        ok = upsert_holdings_record(client, record_id, fields)
        if ok:
            updated += 1
        else:
            failed += 1
        time.sleep(rate_limit)

    if verbose:
        print(f"\n{'='*60}")
        print(f"  重算完成: {updated} 成功 / {failed} 失败")
        print(f"  总持仓市值: {total_mv_cny:,.2f} CNY(含港股 {total_mv_hkd:,.2f} HKD @ {fx_rate_used})")

    return {
        "updated": updated,
        "failed": failed,
        "total_mv_cny": total_mv_cny,
        "total_mv_hkd": total_mv_hkd,
        "fx_rate": fx_rate_used,
    }


# ── 持仓表全面同步（交易表验证 + 持仓表修正）────────────────────────────

def _get_dir(r) -> str:
    """解析方向字段，兼容 list / str"""
    d = r.get("方向", "")
    return d[0] if isinstance(d, list) else str(d)


def sync_holdings(
    client: LarkClient,
    dry_run: bool = False,
    verbose: bool = True,
    rate_limit: float = 0.8,
) -> dict:
    """
    持仓表全面同步:
      1. 验证:交易表数据完整性(名称/符号/平均成本法/收益缺失/分红规则)
      2. 以交易表为依据:更新持仓表(份额/总成本)
      3. 以自选表为依据:更新持仓表(市值/持有收益/收益率)
    返回: {fixed_holdings: [...], issues: [...]}
    """
    from collections import defaultdict

    # ── 加载数据 ───────────────────────────────────────
    all_trades = client.get_records(TRADE_TABLE_ID, TRADE_FIELD_IDS)
    all_holdings = client.get_records(HOLDINGS_TABLE_ID, HOLDINGS_FIELD_IDS)
    wl_records = client.get_records(WATCHLIST_TABLE_ID, WATCHLIST_FIELD_IDS)

    # 自选表: code → price / change_pct
    wl_map: dict[str, dict] = {}
    for r in wl_records:
        code = str(r.get("代码") or "").strip()
        if code:
            wl_map[code] = {
                "price": r.get("最新价"),
                "change_pct": r.get("涨幅"),
                "name": r.get("名称"),
            }

    # 持仓表: code → record
    holdings_by_code: dict[str, dict] = {}
    for h in all_holdings:
        code = str(h.get("代码") or "").strip()
        if code:
            holdings_by_code[code] = h

    # 交易表: code → [records sorted by date]
    trades_by_code: dict[str, list] = defaultdict(list)
    for t in all_trades:
        code = str(t.get("代码") or "").strip()
        if code:
            trades_by_code[code].append(t)
    for code in trades_by_code:
        trades_by_code[code].sort(key=lambda r: str(r.get("交易日期") or ""))

    # 字段 ID
    F = HOLDINGS_FIELD_IDS

    # ── 逐标的处理 ─────────────────────────────────────
    fixed_holdings: list[dict] = []
    issues: list[dict] = []

    codes = sorted(set(trades_by_code.keys()) | set(holdings_by_code.keys()))

    if verbose:
        print(f"\n{'='*60}")
        print("  持仓表全面同步")
        print(f"  交易表标的: {len(set(trades_by_code.keys()))}  持仓表标的: {len(set(holdings_by_code.keys()))}")
        print(f"{'='*60}")

    for code in codes:
        trades = trades_by_code.get(code, [])
        holding = holdings_by_code.get(code)
        wl = wl_map.get(code, {})
        name_in_wl = wl.get("name", "")
        name_in_holding = holding.get("名称", "") if holding else ""
        name_in_trade = trades[0].get("名称", "") if trades else ""

        # ── ① 交易表验证 ────────────────────────────────
        for t in trades:
            d = _get_dir(t)
            date = str(t.get("交易日期") or "")[:10]
            shares = t.get("份额")
            amount = t.get("金额")
            cost = t.get("成本")
            profit = t.get("收益")
            pct = t.get("收益率")

            # 卖出: 份额/金额/成本 必须为负
            if d == "卖出":
                if shares is not None and float(shares) > 0:
                    issues.append({"code": code, "date": date, "type": "卖出份额正", "value": shares})
                if amount is not None and float(amount) > 0:
                    issues.append({"code": code, "date": date, "type": "卖出金额正", "value": amount})
                if cost is not None and float(cost) > 0:
                    issues.append({"code": code, "date": date, "type": "卖出成本正", "value": cost})
            else:
                if shares is not None and float(shares) < 0 and d not in ("分红",):
                    issues.append({"code": code, "date": date, "type": f"{d}份额负", "value": shares})

            # 平均成本法验算(仅卖出)
            if d == "卖出" and shares is not None and cost is not None:
                # 找出此卖出之前的交易,计算正确的成本
                sell_date = str(t.get("交易日期") or "")
                prev_trades = [r for r in trades if str(r.get("交易日期") or "") < sell_date]
                prev_summary = calc_trade_summary(prev_trades)
                rs, rc = prev_summary["remaining_shares"], prev_summary["remaining_cost"]
                if rs > 0 and rc > 0:
                    correct_cost = abs(float(shares)) * (rc / rs)
                    if abs(abs(float(cost)) - correct_cost) > 0.10:
                        issues.append({
                            "code": code, "date": date, "type": "平均成本法偏差",
                            "recorded": cost, "correct": round(-correct_cost, 2),
                            "diff": round(abs(float(cost)) - correct_cost, 2),
                        })

            # 卖出缺少收益/收益率
            if d == "卖出":
                if profit is None or str(profit).strip() == "":
                    issues.append({"code": code, "date": date, "type": "卖出缺少收益", "value": None})
                if pct is None or str(pct).strip() == "":
                    issues.append({"code": code, "date": date, "type": "卖出缺少收益率", "value": None})

            # 分红规则: 现金分红份额=0,金额=正,成本=负; 分红再投份额>0,成本=0
            if d == "分红":
                s = float(shares or 0)
                a = float(amount or 0)
                c = float(cost or 0)
                if s == 0 and a <= 0:
                    issues.append({"code": code, "date": date, "type": "现金分红金额非正", "value": a})
                if s == 0 and c >= 0:
                    issues.append({"code": code, "date": date, "type": "现金分红成本非负", "value": c})
                if s > 0 and c != 0:
                    issues.append({"code": code, "date": date, "type": "分红再投成本非零", "value": c})

        # ── ② 以交易表为依据:份额/成本 ─────────────────
        if not trades:
            continue

        summary = calc_trade_summary(trades)
        remaining_shares = summary["remaining_shares"]
        remaining_cost = summary["remaining_cost"]

        # ── ③ 以自选表为依据:市值/收益 ─────────────────
        current_price = wl.get("price") or 0
        if current_price <= 0 and remaining_shares > 0 and holding:
            # fallback: 从持仓表现有市值反推
            mv = holding.get("市值") or 0
            sh = holding.get("总份额") or 1
            if mv and sh:
                current_price = mv / sh

        if remaining_shares > 0 and current_price > 0:
            market_value = round(remaining_shares * current_price, 2)
            profit_val = round(market_value - remaining_cost, 2)
            profit_pct = f"{round(profit_val / remaining_cost * 100, 2):.2f}%"
        elif remaining_shares > 0 and current_price <= 0:
            market_value = 0.0
            profit_val = -remaining_cost
            profit_pct = "-100.00%"
        else:
            market_value = 0.0
            profit_val = -remaining_cost
            profit_pct = "-100.00%"

        # ── ④ 写入持仓表 ────────────────────────────────
        if not holding:
            if verbose:
                print(f"  跳过 {code}: 持仓表中无记录")
            continue

        record_id = holding["_record_id"]

        # 判断是否有变化
        old_shares = holding.get("总份额") or 0
        old_cost = holding.get("总成本") or 0
        old_mv = holding.get("市值") or 0

        shares_changed = abs(float(remaining_shares) - float(old_shares)) > 0.01
        cost_changed = abs(float(remaining_cost) - float(old_cost)) > 0.01
        mv_changed = abs(float(market_value) - float(old_mv)) > 0.01

        if dry_run:
            if shares_changed or cost_changed or mv_changed:
                print(f"  [DRY] {code}: 份额 {old_shares:.2f}→{remaining_shares:.2f}  "
                      f"成本 {old_cost:.2f}→{remaining_cost:.2f}  "
                      f"市值 {old_mv:.2f}→{market_value:.2f}")
            continue

        if not (shares_changed or cost_changed or mv_changed):
            continue

        fields = {}
        if shares_changed:
            fields[F["总份额"]] = remaining_shares
        if cost_changed:
            fields[F["总成本"]] = remaining_cost
        if mv_changed:
            fields[F["市值"]] = market_value
            fields[F["持有收益"]] = profit_val
            fields[F["持有收益率"]] = profit_pct

        upsert_holdings_record(client, record_id, {
            F["总份额"]: remaining_shares,
            F["总成本"]: remaining_cost,
            F["市值"]: market_value,
            F["持有收益"]: profit_val,
            F["持有收益率"]: profit_pct,
        })
        fixed_holdings.append({
            "code": code,
            "name": name_in_trade or name_in_wl or name_in_holding,
            "old_shares": old_shares, "new_shares": remaining_shares,
            "old_cost": old_cost, "new_cost": remaining_cost,
            "market_value": market_value,
        })
        if verbose:
            print(f"  ✅ {code}: 份额 {old_shares:.2f}→{remaining_shares:.2f}  "
                  f"成本 {old_cost:.2f}→{remaining_cost:.2f}  市值→{market_value:.2f}")
        time.sleep(rate_limit)

    # ── 汇总 ─────────────────────────────────────────
    if verbose:
        print(f"\n{'='*60}")
        print("  持仓同步完成")
        print(f"  持仓表修正: {len(fixed_holdings)} 只")
        print(f"  交易表问题: {len(issues)} 条")
        print(f"{'='*60}")
        if issues:
            print("\n  交易表问题列表:")
            for iss in issues[:20]:
                print(f"    {iss['code']} [{iss['date']}] {iss['type']}: {iss.get('value','')}")

    return {
        "fixed_holdings": fixed_holdings,
        "issues": issues,
    }


# ── 单标的操作 ────────────────────────────────────────

def cmd_recalc_one(
    client: LarkClient,
    code: str,
    name: str,
    direction: str,
    trade_date: str,
    shares: float,
    amount: float,
    nav: float,
    write_trade: bool,
    dry_run: bool,
    verbose: bool,
    rate_limit: float,
):
    """单标的:重算并可选写入交易"""
    if verbose:
        print(f"\n=== {code} {name} 持仓核算 ===")

    # 读取(含新交易)
    all_trades = client.get_records(TRADE_TABLE_ID, TRADE_FIELD_IDS)
    code_trades = [t for t in all_trades if (t.get("代码") or "").strip() == code.strip()]
    code_trades.sort(key=lambda x: x.get("交易日期", ""))

    if write_trade and not dry_run:
        # 写入新交易
        summary_before = calc_trade_summary(code_trades)
        calc = calc_new_trade(direction, shares, amount, nav, summary_before)

        trade_fields = {
            TRADE_FIELD_IDS["代码"]: code,
            TRADE_FIELD_IDS["名称"]: name,
            TRADE_FIELD_IDS["方向"]: parse_direction(direction),
            TRADE_FIELD_IDS["交易日期"]: trade_date,
            TRADE_FIELD_IDS["份额"]: calc["shares"],
            TRADE_FIELD_IDS["金额"]: calc["amount"],
            TRADE_FIELD_IDS["成本"]: calc["cost"],
        }
        if direction == "卖出":
            trade_fields[TRADE_FIELD_IDS.get("收益")] = calc["profit"]
            trade_fields[TRADE_FIELD_IDS.get("收益率")] = calc["profit_pct"]

        ok = client.upsert_record(TRADE_TABLE_ID, None, trade_fields, verbose=False)
        if ok:
            print("  ✅ 交易记录写入成功")
            # 重新读取(含新交易)
            all_trades = client.get_records(TRADE_TABLE_ID, TRADE_FIELD_IDS)
            code_trades = [t for t in all_trades if (t.get("代码") or "").strip() == code.strip()]
            code_trades.sort(key=lambda x: x.get("交易日期", ""))
        else:
            print("  ❌ 交易记录写入失败")
            return

    # 核算
    summary = calc_trade_summary(code_trades)
    first_buy = next(
        (t.get("交易日期", "")[:10] for t in code_trades if t.get("方向", [""])[0] in ("买入", "定投")),
        None
    )
    current_price, src = get_current_price(client, code)
    result = recalc_holdings(code, name, summary, current_price, first_buy, date.today())

    print(f"\n  交易汇总:买入 {summary['buy_shares']:.2f} 份 / 卖出 {summary['sell_shares']:.2f} 份 "
          f"/ 定投 {summary['buy_shares']:.2f} 份 / 分红再投 {summary['div_shares']:.2f} 份")
    print(f"  总份额: {result['total_shares']:.2f}  总成本: {result['total_cost']:.2f}")
    print(f"  最新价: {result['nav']} (来源: {src})")
    print(f"  市值: {result['market_value']:.2f}  持有收益: {result['profit']:+.2f}  "
          f"({result['profit_pct']})  年化: {result['annual_pct']}")

    # 找持仓记录
    holdings = get_all_holdings(client)
    h_record = next((h for h in holdings if (h.get("代码") or "").strip() == code.strip()), None)

    if dry_run:
        print("\n  [DRY-RUN] 不写入持仓表")
        return

    fields = {
        HOLDINGS_FIELD_IDS["总份额"]: result["total_shares"],
        HOLDINGS_FIELD_IDS["总成本"]: result["total_cost"],
        HOLDINGS_FIELD_IDS["市值"]: result["market_value"],
        HOLDINGS_FIELD_IDS["持有收益"]: result["profit"],
        HOLDINGS_FIELD_IDS["持有收益率"]: result["profit_pct"],
    }
    if "年化收益率" in HOLDINGS_FIELD_IDS:
        fields[HOLDINGS_FIELD_IDS["年化收益率"]] = result["annual_pct"]

    if h_record:
        ok = upsert_holdings_record(client, h_record["_record_id"], fields)
        print(f"  {'✅ 持仓表更新成功' if ok else '❌ 持仓表更新失败'}")
    else:
        print(f"  ⚠️  持仓表未找到 {code},请确认代码存在")


# ── CLI 入口 ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="交易录入与持仓核算",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--code", help="标的代码(单标的模式)")
    parser.add_argument("--name", default="", help="名称(用于显示)")
    parser.add_argument(
        "--direction", default="买入",
        choices=["buy", "sell", "dca", "dividend", "买入", "卖出", "定投", "分红"],
        help="交易方向"
    )
    parser.add_argument("--date", dest="trade_date", default="",
                        help="交易日期时间 YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--shares", type=float, default=0, help="交易份额")
    parser.add_argument("--amount", type=float, default=0, help="交易金额")
    parser.add_argument("--nav", type=float, default=0, help="净值/价格")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="仅列出历史交易")
    parser.add_argument("--recalc", action="store_true",
                        help="重新核算持仓(不写入交易)")
    parser.add_argument("--write-trade", action="store_true",
                        help="写入交易记录(需与 --recalc 联用)")
    parser.add_argument("--resync-all", action="store_true",
                        help="全量重算所有持仓标的（市值/收益/份额/成本），自动从交易表核算并更新持仓表")
    parser.add_argument("--resync-holdings", action="store_true",
                        help="持仓表全面同步:验证交易表数据+以交易表为依据更新持仓表份额/成本+以自选表为依据更新市值/收益")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式（不实际写入）")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="静默模式")
    parser.add_argument("--rate-limit", type=float, default=0.8,
                        help="写入间隔秒数(默认 0.8s)")

    args = parser.parse_args()

    if not FEISHU_BASE_TOKEN:
        print("错误:未设置 FEISHU_BASE_TOKEN 环境变量", file=sys.stderr)
        sys.exit(1)

    setup_signal_handlers()
    client = LarkClient(FEISHU_BASE_TOKEN, upsert_delay=args.rate_limit)
    verbose = not args.quiet

    # 全量重算模式
    if args.resync_all:
        result = resync_all(
            client,
            dry_run=args.dry_run,
            verbose=verbose,
            rate_limit=args.rate_limit,
        )
        sys.exit(0 if result["failed"] == 0 else 1)

    # 持仓表全面同步（验证交易表 + 修正持仓表）
    if args.resync_holdings:
        result = sync_holdings(
            client,
            dry_run=args.dry_run,
            verbose=verbose,
            rate_limit=args.rate_limit,
        )
        sys.exit(0)

    # 单标的模式
    if not args.code:
        print("错误:--resync-all / --resync-holdings / --code 必须指定其一", file=sys.stderr)
        sys.exit(1)

    if args.list_only:
        records = get_all_trade_records(client, args.code)
        if not records:
            print(f"交易表无 {args.code} 记录")
            return
        summary = calc_trade_summary(records)
        print(f"\n=== {args.code} 历史交易 ({len(records)} 条) ===")
        print(f"  买入/定投: {summary['buy_shares']:.4f} 份, 成本 {summary['buy_cost']:.2f}")
        print(f"  卖出: {summary['sell_shares']:.4f} 份, 成本 {summary['sell_cost']:.2f}")
        print(f"  分红再投: {summary['div_shares']:.4f} 份")
        print(f"  现金分红成本减少: {summary['cash_div_cost']:.2f}")
        print(f"  剩余: {summary['remaining_shares']:.4f} 份, 成本 {summary['remaining_cost']:.2f}")
        print()
        for r in records:
            d = (r.get("方向") or [""])[0]
            print(f"  [{r.get('交易日期','')[:10]}] {d} "
                  f"份额={r.get('份额')} 金额={r.get('金额')} 成本={r.get('成本')}")
        return

    if not args.trade_date:
        args.trade_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.shares == 0 and args.amount > 0 and args.nav > 0:
        args.shares = round(args.amount / args.nav, 4)
    elif args.amount == 0 and args.shares > 0 and args.nav > 0:
        args.amount = round(args.shares * args.nav, 2)

    if args.recalc or args.write_trade:
        cmd_recalc_one(
            client,
            code=args.code,
            name=args.name,
            direction=args.direction,
            trade_date=args.trade_date,
            shares=args.shares,
            amount=args.amount,
            nav=args.nav,
            write_trade=args.write_trade,
            dry_run=args.dry_run,
            verbose=verbose,
            rate_limit=args.rate_limit,
        )


if __name__ == "__main__":
    main()
