#!/usr/bin/env python3
"""
财神定时报告脚本 v5
三个模板：
  盘中报告（11:30）— ETF涨跌前3/后3；场外基金只报市值；债券只报市值；含简版行情+操作建议
  收盘报告（16:30）— 各类产品报市值+涨跌；含大盘全天回顾+简版建议
  晚间报告（20:30）— 完整涨跌；大盘全天回顾+板块分析+持仓建议
  非交易日：大盘/板块/建议用最近交易日数据，标注"（非交易日，引用最近交易日行情）"

数据来源：飞书多维表格四张表（只读）
报告生成前：自动检查自选表是否已更新，未更新则触发 feishu_sync + trade_calc 全量重算

用法:
    python3 report.py --time 11:30
    python3 report.py --time 20:30
    python3 report.py --time 20:30 --force
    python3 report.py  # 自动推断时间
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

WORKSPACE = Path("/root/.openclaw/workspace-Eva")
SKILL_DIR = WORKSPACE / ".agents/skills/feishu-ledger"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from feishu_base import LarkClient
from feishu_config import (
    FEISHU_BASE_TOKEN,
    HOLDINGS_TABLE_ID,
    HOLDINGS_FIELD_IDS,
    WATCHLIST_TABLE_ID,
    WATCHLIST_FIELD_IDS,
    FEISHU_CASH_TABLE_ID,
    CASH_FIELD_IDS,
)
from trade_calc import get_hkd_cny_rate

# 汇率从 akshare 实时查询，失败时 fallback 到 0.8659
HKD_CNY_RATE = get_hkd_cny_rate()


# ─────────────────────────────────────────────────────────────
# 数据获取
# ─────────────────────────────────────────────────────────────

def run_feishu_sync_full(force: bool = False) -> bool:
    """运行完整的自选表+分红+持仓重算同步"""
    cmd = ["python3", str(SKILL_DIR / "scripts" / "feishu_sync.py"), "--sync-dividends"]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f"[财神] feishu_sync.py --sync-dividends 执行异常:\n{result.stderr[:300]}")
        return False
    return True


def load_holdings(client: LarkClient) -> list[dict]:
    return client.get_records(HOLDINGS_TABLE_ID, HOLDINGS_FIELD_IDS)


def load_watchlist(client: LarkClient) -> list[dict]:
    return client.get_records(WATCHLIST_TABLE_ID, WATCHLIST_FIELD_IDS)


def load_cash(client: LarkClient) -> list[dict]:
    return client.get_records(FEISHU_CASH_TABLE_ID, CASH_FIELD_IDS)


def build_watchlist_map(wl_records: list[dict]) -> dict:
    m = {}
    for r in wl_records:
        code = r.get("代码", "")
        if code:
            m[code] = {
                "price": r.get("最新价") or 0,
                "change_pct": r.get("涨幅") or "0%",
                "date": r.get("更新日期") or "",
            }
    return m


# ─────────────────────────────────────────────────────────────
# 非交易日判断
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# 自选表新鲜度检查 & 必要时触发同步
# ─────────────────────────────────────────────────────────────

def check_and_sync_if_needed(client: LarkClient) -> None:
    """
    检查自选表所有记录是否已更新到今日。
    如有任何一条记录的更新日期 < today，自动触发完整同步。
    """
    wl_records = load_watchlist(client)
    today_str = date.today().isoformat()

    stale = []
    for r in wl_records:
        r_date = str(r.get("更新日期") or "")[:10]
        if r_date and r_date < today_str:
            stale.append(f"{r.get('代码')} {r.get('名称')} ({r_date})")

    if stale:
        print(f"[财神] 自选表有 {len(stale)} 条记录未更新到今日，正在同步...")
        for s in stale[:5]:
            print(f"       {s}")
        if len(stale) > 5:
            print(f"       ... 共 {len(stale)} 条")
        ok = run_feishu_sync_full(force=False)
        if ok:
            print("[财神] 同步完成")
        else:
            print("[财神] 同步失败，继续用现有数据生成报告")
    else:
        print("[财神] 自选表已是最新，无需同步")


# ─────────────────────────────────────────────────────────────
# 持仓分类与计算
# ─────────────────────────────────────────────────────────────

def is_etf_code(code: str) -> bool:
    if code.startswith(("sh", "sz")) and len(code) == 8:
        suffix = code[2:]
        prefixes = ("000", "001", "002", "003", "300", "301",
                   "600", "601", "603", "605", "688")
        return not any(suffix.startswith(p) for p in prefixes)
    return False


def get_currency(r: dict) -> str:
    currency = (r.get("货币") or [""])[0] if isinstance(r.get("货币"), list) else r.get("货币") or ""
    if currency in ("HKD", "CNY"):
        return currency
    code = str(r.get("代码", ""))
    return "HKD" if code.startswith("hk") else "CNY"


def classify_record(r: dict, wl_map: dict) -> dict | None:
    code = str(r.get("代码", ""))
    name = r.get("名称", "")
    product_type = (r.get("产品类型") or [""])[0]
    currency = get_currency(r)
    if product_type in ("基金", "ETF", "LOF"):
        group = r.get("二级组合") or r.get("一级组合") or "其他"
    elif product_type == "债券":
        group = "债券"
    else:
        group = r.get("一级组合") or "其他"
    total_shares = r.get("总份额", 0) or 0
    total_cost = r.get("总成本", 0) or 0
    market_value_raw = r.get("市值", 0) or 0

    wl = wl_map.get(code, {})
    nav = wl.get("price") or 0
    wl_date = wl.get("date") or ""

    # 持仓表市值（旧净值×份额快照）可能已过时；基金类始终用今日最新净值×份额计算，
    # 确保涨跌幅以当前市值为基准，与持仓表中的旧快照解耦。
    if product_type in ("基金", "ETF", "LOF") and nav > 0 and total_shares > 0:
        market_value = round(total_shares * nav, 2)
    elif market_value_raw > 0:
        market_value = market_value_raw
    elif nav > 0 and total_shares > 0:
        market_value = round(total_shares * nav, 2)
    else:
        return None

    if currency == "HKD":
        market_value_cny = round(market_value * HKD_CNY_RATE, 2)
    else:
        market_value_cny = market_value

    try:
        change_pct = float(str(wl.get("change_pct") or "0%").replace("%", ""))
    except Exception:
        change_pct = 0.0
    # 正确收益 = 今日市值 − 昨日市值（今日市值 ÷ (1+涨幅%)）
    if change_pct != 0 and market_value_cny != 0:
        mv_yesterday = market_value_cny / (1 + change_pct / 100)
        change_amount = round(market_value_cny - mv_yesterday, 2)
    else:
        change_amount = 0.0

    return {
        "code": code, "name": name,
        "product_type": product_type,
        "currency": currency,
        "group": group,
        "total_shares": total_shares,
        "total_cost": total_cost,
        "market_value": market_value,
        "market_value_cny": market_value_cny,
        "nav": nav,
        "change_pct": change_pct,
        "change_amount": change_amount,
        "is_today": bool(wl_date) and str(wl_date)[:10] == str(date.today()),
        "is_etf": is_etf_code(code),
    }


def build_portfolio(records: list[dict], wl_map: dict) -> dict:
    stock, etf, fund, bond, cash = [], [], [], [], []
    for r in records:
        item = classify_record(r, wl_map)
        if not item:
            continue
        pt = item["product_type"]
        if pt == "股票":
            stock.append(item)
        elif pt == "ETF" or item["is_etf"]:
            etf.append(item)
        elif pt == "基金":
            fund.append(item)
        elif pt == "现金":
            cash.append(item)
        else:
            bond.append(item)
    return {"stock": stock, "etf": etf, "fund": fund, "bond": bond, "cash": cash}


def build_cash_summary(cash_records: list[dict]) -> dict:
    total_cny, total_hkd = 0.0, 0.0
    accounts = []
    for r in cash_records:
        currency = (r.get("货币") or ["CNY"])[0]
        balance = r.get("余额") or r.get("金额") or 0
        account = r.get("账户名称") or r.get("名称") or ""
        if currency == "HKD":
            total_hkd += balance
        else:
            total_cny += balance
        accounts.append({"account": account, "currency": currency, "balance": balance})
    total_cny += round(total_hkd * HKD_CNY_RATE, 2)
    return {"accounts": accounts, "total_cny": round(total_cny, 2), "total_hkd": round(total_hkd, 2)}


def chg_pct(mv: float, chg: float) -> float:
    if mv == chg or mv - chg == 0:
        return 0.0
    return round(chg / (mv - chg) * 100, 2)


def fp(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def fm(v: float) -> str:
    return f"{v:,.2f}"


def top_bottom(items: list[dict], n: int = 3) -> tuple[list[dict], list[dict]]:
    s = sorted(items, key=lambda x: x["change_pct"], reverse=True)
    return s[:n], s[-n:]


def item_line(i: dict, tag: str = "") -> str:
    name = (i["name"] + tag)[:10]
    return (f"  {i['code']:12s} {name:<10s} "
            f"市值 {fm(i['market_value_cny']):>10s} {fp(i['change_pct']):>8s} {fm(i['change_amount']):>10s}")


# ─────────────────────────────────────────────────────────────
# 报告生成 — 三个模板
# ─────────────────────────────────────────────────────────────

REPORT_TIME_LABELS = {
    "11:30": "盘中",
    "16:30": "收盘",
    "20:30": "晚间",
}


def _build_market_section(market_data: dict, level: str = "full") -> list[str]:
    """
    生成行情分析段落。
    level: "brief" (盘中) | "summary" (收盘) | "full" (晚间)
    """
    trading_day_note = ""
    if not market_data.get("is_trading_day", True):
        trading_day_note = "（非交易日，引用最近交易日行情）"
    elif market_data.get("source") == "fallback":
        trading_day_note = "（⚠️ 行情暂未更新，引用昨日数据）"

    lines = []
    md = market_data

    if level == "brief":
        # 盘中版：简版大盘快照
        lines.append(f"📈 大盘快照{trading_day_note}（{md['date']}）")
        for idx in md.get("indices", []):
            sign = "🟢" if idx["chg"] >= 0 else "🔴"
            chg_str = f"{'+' if idx['chg']>0 else ''}{idx['chg']}%"
            lines.append(f"  {sign} {idx['name']:<8s} {idx['close']:>10,.2f}{idx['unit']}  {chg_str}")
        return lines

    # 收盘/晚间：完整行情
    lines.append(f"📈 大盘全天回顾{trading_day_note}（{md['date']}）")
    for idx in md.get("indices", []):
        sign = "🟢" if idx["chg"] >= 0 else "🔴"
        chg_str = f"{'+' if idx['chg']>0 else ''}{idx['chg']}%"
        lines.append(f"  {sign} {idx['name']:<8s} {idx['close']:>10,.2f}{idx['unit']}  {chg_str}")
    if md.get("volume"):
        lines.append(f"  📊 两市成交 {md['volume']}")

    if level == "full" and md.get("sectors"):
        lines.append("")
        lines.append("🔥 板块动态")
        hot = sorted(md["sectors"], key=lambda x: -x["chg"])
        for s in hot[:3]:
            sign = "🟢" if s["chg"] >= 0 else "🔴"
            lines.append(f"  {sign} {s['icon']}{s['name']} {s['chg']:+.2f}%  {s['desc']}")
        lines.append("⚠️ 弱势板块")
        for s in hot[-2:]:
            sign = "🟢" if s["chg"] >= 0 else "🔴"
            lines.append(f"  {sign} {s['icon']}{s['name']} {s['chg']:+.2f}%  {s['desc']}")

    return lines


def _build_advice_section(portfolio: dict, market_data: dict, level: str = "brief") -> list[str]:
    """
    生成操作建议段落。
    level: "brief" (盘中) | "summary" (收盘) | "full" (晚间)
    """
    lines = ["💡 操作建议"]
    md = market_data

    if level == "brief":
        # 盘中：简洁提示
        indices = md.get("indices", [])
        if indices:
            sh = next((i for i in indices if "上证" in i["name"]), None)
            if sh:
                direction = "偏多" if sh["chg"] >= 0 else "偏空"
                lines.append(f"• 大盘{direction}，{'可适度加仓' if sh['chg'] >= 0 else '宜控制仓位观望'}")
        lines.append("• 场外基金净值午后确认，关注实际涨跌")
        return lines

    if level == "summary":
        # 收盘：盘中总结+简版建议
        lines.append("• 今日盘中波动较大，注意持仓波动")
        indices = md.get("indices", [])
        if indices:
            worst = min(indices, key=lambda x: x["chg"]) if indices else None
            if worst and worst["chg"] < -1:
                lines.append(f"• {worst['name']}走弱{'，关注明日修复机会' if worst['chg'] > -3 else '，注意止损风险'}")
        lines.append("• 港股/美股隔夜数据明日早盘参考")
        return lines

    # 晚间：完整建议
    stock = portfolio.get("stock", [])
    etf = portfolio.get("etf", [])
    fund = portfolio.get("fund", [])
    all_items = stock + etf + fund

    all_sorted = sorted(all_items, key=lambda x: x["change_pct"])
    worst3 = all_sorted[:3]
    best3 = all_sorted[-3:]

    tech_hurts = [i for i in all_items if i["change_pct"] < -3 and any(k in i["name"] for k in ["芯片", "科技", "AI", "科创", "半导体"])]
    if tech_hurts:
        names = "、".join(i["name"] for i in tech_hurts[:3])
        lines.append(f"• {names} 等科技持仓跌幅较大，关注短期是否企稳")

    if worst3 and worst3[0]["change_pct"] < -5:
        w = worst3[0]
        lines.append(f"• {w['name']}（{w['code']}）单日{w['change_pct']:+.2f}%，建议关注持仓风险")

    if best3 and best3[-1]["change_pct"] > 3:
        b = best3[-1]
        lines.append(f"• {b['name']}（{b['code']}）逆势+{b['change_pct']:+.2f}%，可适度持有或加仓")

    indices = md.get("indices", [])
    if indices:
        sh = next((i for i in indices if "上证" in i["name"]), None)
        if sh and sh["chg"] < -1.5:
            lines.append("• 大盘大幅走弱，增配防御板块（消费/红利/医药），降低高位科技仓位")
        elif sh and sh["chg"] > 1:
            lines.append("• 大盘强势，可维持当前仓位，顺势而为")

    lines.append("• ETF 可定投方式分批布局，降低择时风险")
    return lines


def generate_report(
    portfolio: dict,
    cash: dict,
    report_time: str,
    is_final: bool,
    wl_map: dict,
    market_data: dict | None = None,
    is_trading_day: bool = True,
) -> str:
    """
    三个模板：
      11:30 盘中报告  — level="brief"
      16:30 收盘报告  — level="summary"
      20:30 晚间报告  — level="full"
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d %H:%M")
    time_label = REPORT_TIME_LABELS.get(report_time, report_time)
    actual_time = now.strftime("%H:%M")
    lines = [f"📊 财神{time_label}报告 · 生成于 {today} · 报告时间 {actual_time}", ""]

    # 模板级别
    level_map = {"11:30": "brief", "16:30": "summary", "20:30": "full"}
    level = level_map.get(report_time, "summary")

    # ── 行情分析（所有报告都含） ──
    if market_data:
        lines += _build_market_section(market_data, level=level)
        lines.append("")
        lines += _build_advice_section(portfolio, market_data, level=level)
        lines.append("")

    # ── 股票 ──
    stock = portfolio["stock"]
    if stock:
        total_mv = sum(x["market_value_cny"] for x in stock)
        total_chg = sum(x["change_amount"] for x in stock)
        top_g, bot_l = top_bottom(stock, 3)
        pct = fp(chg_pct(total_mv, total_chg))
        chg_str = fm(total_chg)
        lines += [f"【股 票】市值 {fm(total_mv)} 当日 {chg_str}（{pct}）", ""]
        if top_g:
            lines.append("  涨幅前3：")
            for i in top_g:
                tag = "[港]" if i["currency"] == "HKD" else ""
                lines.append(f"  {i['name'][:8]}{tag}  {fp(i['change_pct'])}")
        if bot_l:
            lines.append("  跌幅前3：")
            for i in bot_l:
                tag = "[港]" if i["currency"] == "HKD" else ""
                lines.append(f"  {i['name'][:8]}{tag}  {fp(i['change_pct'])}")
        lines.append("")

    # ── 基金 ──
    etf = portfolio["etf"]
    fund = portfolio["fund"]
    all_fund = etf + fund

    if all_fund:
        groups = {}
        for i in all_fund:
            g = i["group"] or "其他"
            if g not in groups:
                groups[g] = {"items": [], "total_mv": 0.0, "total_cost": 0.0, "total_chg": 0.0}
            groups[g]["items"].append(i)
            groups[g]["total_mv"] += i["market_value_cny"]
            groups[g]["total_cost"] += i["total_cost"]
            groups[g]["total_chg"] += i["change_amount"]

        group_list = []
        for g, d in groups.items():
            g_pct = chg_pct(d["total_mv"], d["total_chg"])
            group_list.append({
                "group": g, "items": d["items"],
                "total_mv": d["total_mv"], "total_cost": d["total_cost"],
                "change_amount": d["total_chg"], "change_pct": g_pct,
            })

        total_mv_f = sum(g["total_mv"] for g in group_list)
        total_chg_f = sum(g["change_amount"] for g in group_list)
        pct_f = fp(chg_pct(total_mv_f, total_chg_f))

        if is_final:
            lines += [f"【基 金】市值 {fm(total_mv_f)} 当日 {fm(total_chg_f)}（{pct_f}）", ""]
            sorted_groups = sorted(group_list, key=lambda x: x["change_pct"], reverse=True)
            top_g = sorted_groups[:3]
            bot_l = sorted_groups[-3:]
            if top_g:
                lines.append("  涨幅前3组合：")
                for g in top_g:
                    lines.append(f"  {g['group']:<14s} 市值 {fm(g['total_mv']):>10s} 当日 {fm(g['change_amount']):>10s}（{fp(g['change_pct'])}）")
            if bot_l:
                lines.append("  跌幅前3组合：")
                for g in bot_l:
                    lines.append(f"  {g['group']:<14s} 市值 {fm(g['total_mv']):>10s} 当日 {fm(g['change_amount']):>10s}（{fp(g['change_pct'])}）")
        else:
            lines += [f"【基 金】市值 {fm(total_mv_f)}", ""]
            if etf:
                etf_mv = sum(x["market_value_cny"] for x in etf)
                etf_chg = sum(x["change_amount"] for x in etf)
                lines.append(f"  ETF  市值 {fm(etf_mv)} 当日 {fm(etf_chg)}")
                top_g, bot_l = top_bottom(etf, 3)
                if top_g:
                    lines.append("  涨幅前3ETF：")
                    for i in top_g:
                        lines.append(item_line(i))
                if bot_l:
                    lines.append("  跌幅前3ETF：")
                    for i in bot_l:
                        lines.append(item_line(i))
            if fund:
                fund_mv = sum(x["market_value_cny"] for x in fund)
                if is_final:
                    fund_chg = sum(x["change_amount"] for x in fund)
                    lines.append(f"  场外基金  市值 {fm(fund_mv)} 当日 {fm(fund_chg)}")
                else:
                    lines.append(f"  场外基金  市值 {fm(fund_mv)}（净值未更新，涨跌待确认）")
        lines.append("")

    # ── 债券 ──
    bond = portfolio["bond"]
    if bond:
        total_mv = sum(x["market_value_cny"] for x in bond)
        if is_final:
            total_chg = sum(x["change_amount"] for x in bond)
            pct = fp(chg_pct(total_mv, total_chg))
            lines += [f"【债 券】市值 {fm(total_mv)} 当日 {fm(total_chg)}（{pct}）", ""]
        else:
            lines += [f"【债 券】市值 {fm(total_mv)}（净值未更新，涨跌待确认）", ""]

    # ── 现金 ──
    total_cash = cash["total_cny"]
    lines.append(f"【现 金】{fm(total_cash)} 元")
    for acc in cash["accounts"]:
        bal = acc["balance"]
        if acc["currency"] == "HKD":
            lines.append(f"  {acc['account']}  {bal:,.2f} HKD（≈{bal*HKD_CNY_RATE:,.2f} CNY）")
        else:
            lines.append(f"  {acc['account']}  {bal:,.2f} 元")
    lines.append("")

    # ── 总览 ──
    # 公式自洽：
    #   今日收益 = Σ各标的当日收益 + 现金差异
    #   昨日总资产 = 今日总市值 − Σ各标的当日收益
    #   综合涨幅 = 今日收益 / 昨日总资产
    #   验证：今日收益 = 综合涨幅 × 昨日总资产 ✓
    all_items = portfolio["stock"] + portfolio["etf"] + portfolio["fund"] + portfolio["bond"]
    total_mv_all = sum(x["market_value_cny"] for x in all_items)
    total_chg_all = sum(x["change_amount"] for x in all_items)
    total_assets = total_mv_all + total_cash

    # ── 读取昨日基准 ──
    # 基准文件字段：
    #   date：上一交易日
    #   total_mv_with_cash：昨日总资产（含现金）← 核心锚点
    #   total_mv_cny：昨日持仓总市值（港股按昨日汇率折算）
    #   hk_rate：昨日港币汇率
    baseline_date = ""
    baseline_assets = None   # 昨日总资产（含现金）= 昨日市值 + 昨日现金
    baseline_mv = None       # 昨日持仓总市值
    baseline_hk_rate = None
    baseline_file = WORKSPACE / "财神" / "reports" / "baseline.json"
    if baseline_file.exists():
        import json as _json
        try:
            prev = _json.loads(baseline_file.read_text(encoding="utf-8"))
            if prev.get("total_mv_with_cash") is not None:
                baseline_date = prev.get("date", "")
                baseline_assets = float(prev["total_mv_with_cash"])
                baseline_mv = float(prev["total_mv_cny"])
                baseline_hk_rate = float(prev.get("hk_rate", HKD_CNY_RATE))
        except Exception:
            pass

    # 今日收益 = 股票收益 + 基金收益 + 债券收益 + 现金收益
    # 股票/基金/债券收益 = 各标的 change_amount
    # 现金收益 = 今日现金 − 昨日现金
    # 港股持仓总市值用昨日汇率换算（消除汇率波动），change_amount 已是今日汇率下的收益无需调整
    yesterday_cash = 0.0
    if baseline_file.exists():
        import json as _json
        try:
            prev = _json.loads(baseline_file.read_text(encoding="utf-8"))
            yesterday_cash = float(prev.get("cash") or 0)
        except Exception:
            pass

    # 港股持仓总市值用昨日汇率换算（昨日汇率 × 今日港股份额 × 今日港股价格）
    hk_items = [x for x in all_items if x.get("currency") == "HKD"]
    hk_mv_today_rate = sum(x["market_value_cny"] for x in hk_items)
    hk_mv_yesterday_rate = sum(
        round(x["market_value"] * baseline_hk_rate, 2)
        for x in hk_items
    ) if baseline_hk_rate else hk_mv_today_rate
    # 持仓总市值（港股按昨日汇率，消除汇率波动）
    total_mv_all_fixed_hk = total_mv_all - hk_mv_today_rate + hk_mv_yesterday_rate
    # 现金收益
    cash_gain = round(total_cash - yesterday_cash, 2)
    # 今日收益 = 持仓收益（已含港股汇率调整后的市值差） + 现金收益
    today_gain = round((total_mv_all_fixed_hk - baseline_mv) + cash_gain, 2)
    today_pct = fp(chg_pct(total_mv_all, today_gain))
    today_gain_str = f"{fm(today_gain)}（{today_pct}）"

    lines += ["━━━━━━━━━━━━━━━",
              f"总持仓市值（CNY） {fm(total_mv_all)}",
              f"总资产（+现金） {fm(total_assets)}",
              f"今日收益 {today_gain_str}"]

    # ── 更新收盘基准文件（仅20:30晚间报告） ──
    # 保护逻辑：只更新"上一个交易日"的基准，不被同日多次运行覆盖
    # 非交易日不更新基准文件
    if report_time == "20:30" and is_trading_day:
        import json as _json
        today_str = date.today().isoformat()
        update_allowed = True
        if baseline_file.exists():
            try:
                prev = _json.loads(baseline_file.read_text(encoding="utf-8"))
                if prev.get("date") == today_str:
                    # 今日已更新过基准，不要用今日数据覆盖今日
                    update_allowed = False
                    print(f"[财神] 基准文件已是今日({today_str})，不重复更新")
            except Exception:
                pass
        if update_allowed:
            new_baseline = {
                "date": today_str,
                "total_mv_cny": round(total_mv_all, 2),
                "total_mv_with_cash": round(total_assets, 2),
                "cash": round(total_cash, 2),
                "hk_rate": HKD_CNY_RATE,
            }
            baseline_file.parent.mkdir(parents=True, exist_ok=True)
            baseline_file.write_text(_json.dumps(new_baseline, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[财神] 收盘基准已更新: {today_str} 总资产={fm(total_assets)}")

    return "\n".join(lines)


def check_pending(portfolio: dict) -> tuple[bool, list[str]]:
    pending = []
    for item in portfolio["etf"] + portfolio["fund"] + portfolio["bond"]:
        if not item["is_today"]:
            pending.append(f"{item['code']} {item['name']}")
    return len(pending) == 0, pending


def run_report(report_time: str, force: bool = False, do_sync: bool = True) -> str:
    client = LarkClient(FEISHU_BASE_TOKEN)

    # ── 报告生成前：检查并同步自选表 ──
    if do_sync:
        check_and_sync_if_needed(client)

    holdings = load_holdings(client)
    watchlist = load_watchlist(client)
    cash_records = load_cash(client)

    # check_and_sync_if_needed 在子进程中运行，同步完成后需重新读取自选表
    # 否则 wl_map 里的更新日期始终是同步前的旧数据
    wl_map = build_watchlist_map(watchlist)
    portfolio = build_portfolio(holdings, wl_map)
    cash = build_cash_summary(cash_records)

    # ── 20:30 重试逻辑（场外基金净值可能延迟） ──
    if report_time == "20:30" and not force:
        all_updated, pending = check_pending(portfolio)
        now = datetime.now()
        cutoff = now.replace(hour=22, minute=30, second=0)
        retry = 0
        while not all_updated and now < cutoff and retry < 8:
            next_fire = now + timedelta(minutes=15 * (retry + 1))
            if next_fire > cutoff:
                break
            print(f"[财神] 基金净值未全部更新，等待 15 分钟（第 {retry+1} 次）...")
            time.sleep(15 * 60)
            run_feishu_sync_full(force=False)
            holdings = load_holdings(client)
            watchlist = load_watchlist(client)
            wl_map = build_watchlist_map(watchlist)
            portfolio = build_portfolio(holdings, wl_map)
            all_updated, pending = check_pending(portfolio)
            if all_updated:
                print("[财神] 所有基金净值已更新")
            retry += 1

    # 是否显示完整涨跌（所有基金/债券均已更新到今日，则为 final）
    all_updated, pending = check_pending(portfolio)
    is_final = all_updated

    # ── 行情数据（所有报告都含，根据模板级别调整深度） ──
    market_data = None
    try:
        from market_context import get_market_data as _get_market
        md = _get_market(wl_map=wl_map)
        if not md["is_trading_day"]:
            print(f"[财神] 今日非交易日，引用最近交易日 {md['date']} 行情")
        market_data = md
    except Exception as e:
        print(f"[财神] 市场行情加载失败: {e}")

    return generate_report(portfolio, cash, report_time, is_final, wl_map, market_data, is_trading_day=md.get("is_trading_day", True))


# ─────────────────────────────────────────────────────────────
# 图表渲染（返回图表路径列表，供调用方在报告文字之后发送）
# ─────────────────────────────────────────────────────────────

def render_charts_if_available() -> list[tuple[str, str]]:
    """
    渲染股票饼图和基金双圈图，返回 [(图片路径, 说明文字), ...]
    """
    charts = []
    try:
        from render_charts import render_stock_pie, render_fund_donut, load_data
        portfolio, wl_map, primary_of = load_data()
        p1 = render_stock_pie(portfolio, wl_map)
        p2 = render_fund_donut(portfolio, wl_map, primary_of)
        if p1:
            charts.append((str(p1), "📊 股票持仓占比"))
        if p2:
            charts.append((str(p2), "📈 基金持仓双圈"))
    except Exception as e:
        print(f"[财神] 饼图生成失败: {e}")
    return charts


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────

def _infer_report_time() -> str:
    """根据当前时间自动推断报告版本"""
    now = datetime.now()
    h, m = now.hour, now.minute
    today_str = date.today().strftime("%Y-%m-%d")

    if h < 9:
        return "20:30"

    if h < 16 or (h == 16 and m < 30):
        return "11:30"

    try:
        client = LarkClient(FEISHU_BASE_TOKEN)
        wl = client.get_records(WATCHLIST_TABLE_ID, WATCHLIST_FIELD_IDS)
        if wl:
            updated = sum(1 for r in wl if str(r.get("更新日期") or "")[:10] == today_str)
            if updated == len(wl):
                return "20:30"
    except Exception:
        pass

    return "16:30"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--time",
        default=None,
        help="11:30（盘中）/ 16:30（收盘）/ 20:30（晚间），不指定则自动推断"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-sync", action="store_true")
    args = parser.parse_args()

    # 自动推断时间
    if args.time is None:
        inferred = _infer_report_time()
        print(f"[财神] 当前时间 {datetime.now().strftime('%H:%M')}，自动推断为 {inferred} 版报告（{REPORT_TIME_LABELS[inferred]}）")
        args.time = inferred

    # 生成报告（包含同步检查）
    report_text = run_report(args.time, force=args.force, do_sync=not args.no_sync)
    print("\n" + report_text)

    # 保存报告
    today_str = date.today().isoformat()
    out_path = WORKSPACE / "财神" / "reports" / f"{today_str}_{args.time.replace(':', '')}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_text, encoding="utf-8")
    print(f"\n[财神] 已保存: {out_path}")

    # 渲染图表（报告文字之后）
    charts = render_charts_if_available()
    for img_path, caption in charts:
        print(f"[财神] {caption}: {img_path}")
        _send_image(img_path, caption)


def _send_image(image_path: str, caption: str):
    """通过 OpenClaw message 工具发送图片到皮迪克飞书"""
    import subprocess, os
    user_id = "ou_e16fe6c8a8c238730affda790d00844d"
    try:
        result = subprocess.run(
            ["openclaw", "send",
             "--channel", "feishu",
             "--to", f"user:{user_id}",
             "--image", image_path,
             "--text", caption],
            capture_output=True, text=True, timeout=60, env=os.environ.copy()
        )
        if result.returncode == 0:
            print(f"[财神] 图片已发送: {caption}")
        else:
            print(f"[财神] 图片发送失败: {result.stderr[:200]}")
    except Exception as e:
        print(f"[财神] 图片发送异常: {e}")


if __name__ == "__main__":
    main()
