"""
市场大盘数据（使用腾讯证券 qt.gtimg.cn，数据格式为 GBK ~ 分隔）
"""
from __future__ import annotations

import http.client
import json
import ssl
import time
from datetime import date

# ─── 兜底数据（腾讯 API 不可用时回退）───
INDEX_DATA_FALLBACK = [
    {"name": "上证指数",  "code": "sh000001", "close": 4068.57, "chg": -0.73, "unit": "点"},
    {"name": "深证成指",  "code": "sz399001", "close": 15575.13, "chg": -1.81, "unit": "点"},
    {"name": "创业板指",  "code": "sz399006", "close": 4037.95, "chg": -2.11, "unit": "点"},
    {"name": "科创50",   "code": "sh000688", "close": 1751.32, "chg": -5.04, "unit": "点"},
    {"name": "恒生指数",  "code": "hkHSI",    "close": 25182.39, "chg": +0.70, "unit": "点"},
    {"name": "恒生科技",  "code": "hkHSTECH",  "close": 4884.23, "chg": -0.09, "unit": "点"},
]

# 板块热点ETF映射（用于从自选表提取真实板块涨跌，覆盖静态缓存）
SECTOR_ETF_MAP = [
    # (板块名, ETF代码, 图标, 描述)
    ("白酒食品", "sh512600", "🍶", "消费复苏预期，资金避险"),
    ("煤炭",     "sh515220", "🔥", "高股息+用电旺季"),
    ("商贸零售", "sh512040", "🛒", "低位轮动"),
    ("医药",     "sz159938", "💊", "防御属性，资金流入"),
    ("港股医药", "sh513060", "🏥", "港股估值修复"),
    ("港股消费", "sh513970", "🛍", "港股消费走弱"),
    ("恒生科技", "sh513010", "💻", "科技板块承压"),
    ("半导体",   "sh512480", "🔲", "科技集体杀跌"),
]

SECTOR_HOT_FALLBACK = [
    {"name": "白酒食品",  "chg": +3.02, "icon": "🍶", "desc": "消费复苏预期，资金避险"},
    {"name": "煤炭",      "chg": +2.41, "icon": "🔥", "desc": "高股息+用电旺季"},
    {"name": "商贸零售",  "chg": +1.50, "icon": "🛒", "desc": "低位轮动"},
    {"name": "半导体",   "chg": -3.75, "icon": "💻", "desc": "高位科技集体杀跌"},
    {"name": "AI算力",   "chg": -4.00, "icon": "🤖", "desc": "获利资金出逃"},
]

VOLUME_FALLBACK = "3.32万亿（放量3508亿）"
FALLBACK_DATE = "2026-05-29"

# ─── 重试配置 ───
MAX_RETRIES = 3
RETRY_DELAYS = [3, 6, 12]

# 可重试的异常类名
RETRY_EXCEPTIONS = (
    "ConnectionError", "ConnectionResetError", "RemoteDisconnected",
    "ConnectTimeoutError", "ReadTimeoutError", "TimeoutError",
    "SSLError", "HTTPError",
)
RETRY_MSG_KEYWORDS = (
    "connection refused", "connection aborted", "timed out",
    "temporary failure", "name or service not known",
)


def _is_network_error(exc: Exception) -> bool:
    if type(exc).__name__ in RETRY_EXCEPTIONS:
        return True
    err_msg = str(exc).lower()
    return any(kw in err_msg for kw in RETRY_MSG_KEYWORDS)


def _retry_until_ok(fetch_fn, label: str):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            result = fetch_fn()
            if result is not None:
                return result, "live"
        except Exception as e:
            last_exc = e
            if not _is_network_error(e):
                print(f"[market] {label} 非网络错误，放弃重试: {e}")
                return None, "fallback"
            delay = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
            print(f"[market] {label} 第 {attempt+1}/{MAX_RETRIES} 次失败: {e}，{delay}s 后重试...")
            time.sleep(delay)
    print(f"[market] {label} 全部 {MAX_RETRIES} 次重试失败，回退到缓存数据")
    return None, "fallback"


# ─── 腾讯证券 API ───

def _build_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_qt_indices() -> list[dict] | None:
    """
    使用腾讯证券 qt.gtimg.cn 拉取 A 股 + 港股主要指数。
    返回 [{name, code, close, chg, unit}, ...]
    """
    ctx = _build_ssl_ctx()
    # 腾讯的代码：A 股 sh/sz 前缀，港股 hk 前缀
    codes = "sh000001,sz399001,sz399006,sh000688,hkHSI,hkHSTECH"
    url_path = f"/q={codes}"

    try:
        conn = http.client.HTTPSConnection("qt.gtimg.cn", 443, timeout=10, context=ctx)
        conn.request("GET", url_path, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        })
        resp = conn.getresponse()
        raw = resp.read().decode("gbk").strip()
        conn.close()
        conn.close()

        if not raw or raw.startswith("v="):
            # 空响应或错误
            return None

        indices = []
        wanted = {
            "v_sh000001": "上证指数",
            "v_sz399001": "深证成指",
            "v_sz399006": "创业板指",
            "v_sh000688": "科创50",
            "v_hkHSI":    "恒生指数",
            "v_hkHSTECH": "恒生科技",
        }
        for line in raw.split("\n"):
            if "=" not in line:
                continue
            key = line.split("=")[0].strip()
            if key not in wanted:
                continue
            parts = line.split("~")
            if len(parts) < 5:
                continue
            name = wanted[key]
            try:
                close = float(parts[3]) if parts[3] else 0
                prev_close = float(parts[4]) if parts[4] else close
                chg = round(close - prev_close, 2)
                chg_pct = round(chg / prev_close * 100, 2) if prev_close else 0
            except (ValueError, ZeroDivisionError):
                continue
            indices.append({
                "name": name,
                "code": parts[2],  # 腾讯格式：字段2=代码
                "close": close,
                "chg": chg_pct,   # 存为百分比
                "unit": "点",
            })

        if len(indices) < 4:
            return None
        return indices

    except Exception as e:
        print(f"[market] 腾讯证券 API 失败: {e}")
        return None


def _fetch_qt_volume() -> str | None:
    """从上证+深证合计计算两市成交额（单位：亿元）"""
    ctx = _build_ssl_ctx()
    try:
        conn = http.client.HTTPSConnection("qt.gtimg.cn", 443, timeout=10, context=ctx)
        conn.request("GET", "/q=sh000001,sz399001", headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        resp = conn.getresponse()
        raw = resp.read().decode("gbk").strip()
        conn.close()

        total_amt = 0.0
        for line in raw.split("\n"):
            if "=" not in line:
                continue
            parts = line.split("~")
            if len(parts) < 36:
                continue
            # 字段35: "close/vol/amount"，字段36: 成交量（手）
            try:
                field35 = parts[35]
                f35 = field35.split("/")
                if len(f35) >= 3:
                    amt = float(f35[2])  # 成交额（元）
                    total_amt += amt
            except (ValueError, IndexError):
                continue

        if total_amt > 0:
            if total_amt > 1e12:
                return f"{total_amt/1e12:.2f}万亿"
            elif total_amt > 1e8:
                return f"{total_amt/1e8:.0f}亿"
        return None
    except Exception as e:
        print(f"[market] 成交额获取失败: {e}")
    return None


def _fetch_indices_inner() -> tuple[list[dict], str] | None:
    indices = _fetch_qt_indices()
    if indices is None:
        return None
    volume = _fetch_qt_volume() or ""
    return indices, volume


def _fetch_indices() -> tuple[list[dict], str] | None:
    result, source = _retry_until_ok(_fetch_indices_inner, "大盘指数")
    return result


def _fetch_sectors_inner() -> list[dict] | None:
    # 腾讯证券不提供板块热度数据，沿用 akshare 逻辑或静态 fallback
    # 这里直接返回 None，由调用方使用 fallback
    return None


def _fetch_sectors() -> list[dict] | None:
    result, source = _retry_until_ok(_fetch_sectors_inner, "板块数据")
    return result


# ─── 对外接口 ───

def _sign(chg: float) -> str:
    return "🟢" if chg >= 0 else "🔴"


def is_trading_day() -> bool:
    return date.today().weekday() < 5


def last_trading_day() -> str:
    from datetime import timedelta
    d = date.today()
    for i in range(1, 8):
        check = d - timedelta(days=i)
        if check.weekday() < 5:
            return check.isoformat()
    return d.isoformat()


def get_market_data(wl_map: dict | None = None) -> dict:
    indices_data = _fetch_indices()
    sectors_data = _fetch_sectors()
    today_str = date.today().isoformat()

    if indices_data:
        indices, volume = indices_data
        source = "live"
    else:
        indices = INDEX_DATA_FALLBACK
        volume = VOLUME_FALLBACK
        today_str = FALLBACK_DATE
        source = "fallback"
        print(f"[market] 回退到 {FALLBACK_DATE} 缓存数据（大盘指数）")

    # 板块数据：优先从自选表代表性ETF实时涨跌提取
    if wl_map:
        sector_data = []
        for sec_name, etf_code, icon, desc in SECTOR_ETF_MAP:
            wl = wl_map.get(etf_code, {})
            try:
                chg = float(str(wl.get("change_pct", "0%")).replace("%", ""))
            except (ValueError, TypeError):
                chg = 0.0
            if chg != 0 or wl.get("price"):
                sector_data.append({
                    "name": sec_name,
                    "chg": chg,
                    "icon": icon,
                    "desc": desc,
                })
        if sector_data:
            sectors = sector_data
        else:
            sectors = SECTOR_HOT_FALLBACK
    elif sectors_data:
        sectors = sectors_data
    else:
        sectors = SECTOR_HOT_FALLBACK

    trading_day = is_trading_day()
    if not trading_day:
        today_str = last_trading_day()

    return {
        "date": today_str,
        "indices": indices,
        "volume": volume,
        "sectors": sectors,
        "source": source,
        "is_trading_day": trading_day,
    }


def market_brief() -> str:
    md = get_market_data()
    note = "" if md["source"] == "live" else "（⚠️ 服务器网络受限，引用缓存数据）"
    lines = [f"📈 大盘快照（{md['date']}）{note}"]
    for idx in md["indices"]:
        sign = _sign(idx["chg"])
        chg_str = f"{'+' if idx['chg'] > 0 else ''}{idx['chg']:.2f}%"
        lines.append(f"  {sign} {idx['name']:<8s} {idx['close']:>10,.2f}{idx['unit']}  {chg_str}")
    if md["volume"]:
        lines.append(f"  📊 两市成交 {md['volume']}")
    return "\n".join(lines)


def sector_hot_lines() -> list[str]:
    md = get_market_data()
    sectors = md["sectors"]
    hot = sorted(sectors, key=lambda x: -x["chg"])
    lines = ["🔥 强势板块"]
    for s in hot[:3]:
        sign = _sign(s["chg"])
        lines.append(f"  {sign} {s['icon']}{s['name']} {s['chg']:+.2f}%  {s['desc']}")
    lines.append("⚠️ 弱势板块")
    for s in hot[-2:]:
        sign = _sign(s["chg"])
        lines.append(f"  {sign} {s['icon']}{s['name']} {s['chg']:+.2f}%  {s['desc']}")
    return lines


def generate_advice(portfolio: dict, is_final: bool) -> list[str]:
    stock = portfolio.get("stock", [])
    etf = portfolio.get("etf", [])
    fund = portfolio.get("fund", [])
    all_items = stock + etf + fund

    all_sorted = sorted(all_items, key=lambda x: x["change_pct"])
    worst3 = all_sorted[:3]
    best3 = all_sorted[-3:]

    advice = []
    advice.append(("📋 市场建议", [
        "• 关注当日指数走势，控制仓位在合理区间",
        "• 防御板块资金流入时，可顺势布局低估值",
        "• 放量下跌时谨慎追涨，注意止损或分批建仓",
    ]))

    tech_hurts = [i for i in all_items if i["change_pct"] < -3 and any(k in i["name"] for k in ["芯片", "科技", "AI", "科创", "半导体"])]
    if tech_hurts:
        names = "、".join(i["name"] for i in tech_hurts[:3])
        advice.append(("⚠️ 持仓提醒", [
            f"• {names} 等科技持仓跌幅较大，关注短期是否企稳",
            "• 关注科创50/恒生科技指数是否破位",
        ]))

    if worst3 and worst3[0]["change_pct"] < -5:
        w = worst3[0]
        advice.append(("🔍 重点关注", [
            f"• {w['name']}（{w['code']}）单日{w['change_pct']:+.2f}%，建议关注持仓风险",
        ]))

    if best3 and best3[-1]["change_pct"] > 3:
        b = best3[-1]
        advice.append(("✅ 强势持仓", [
            f"• {b['name']}（{b['code']}）逆势+{b['change_pct']:+.2f}%，可适度持有或加仓",
        ]))

    advice.append(("💡 组合配置", [
        "• 增配防御型（消费/红利/医药），降低高位科技仓位",
        "• 港股医药/消费组合相对抗跌，可维持",
        "• ETF 可定投方式分批布局，降低择时风险",
    ]))

    result = []
    for title, items in advice:
        result.append(title)
        result.extend(items)
    return result
