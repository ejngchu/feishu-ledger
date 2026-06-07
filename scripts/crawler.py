#!/usr/bin/env python3
"""
爬取模块 - 从 AKShare 获取最新价（净值）与涨幅（日涨跌幅）

可独立 CLI 使用，也可被 feishu_sync.py 导入调用。

特性：
  - 支持内存缓存（基于 price_cache.json）
  - TTL 内直接返回缓存数据，减少不必要的网络请求
  - 缓存时间可配置（默认 2 分钟，见 assets/config.json 的 cache_ttl_seconds）

用法:
    echo '["sz000333","hk00700","015600"]' | python crawler.py
    python -c "import crawler; print(crawler.crawl(['sz000333','hk00700']))"
"""
import argparse
import io
import json
import re
import sys
import time as _time
import warnings
import threading
from dataclasses import dataclass, field
from functools import wraps
from typing import Optional

import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ── 缓存基础设施（来自 feishu_config） ───────────────────────────────
from feishu_config import (
    load_price_cache,
    save_price_cache,
    is_cache_valid,
)

# ── 通用工具 ─────────────────────────────────────────────────────────

class _TimeoutError(Exception):
    pass


def _with_timeout(seconds: int):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = [None]
            exception = [None]
            def target():
                try:
                    result[0] = func(*args, **kwargs)
                except Exception as e:
                    exception[0] = e
            t = threading.Thread(target=target)
            t.daemon = True
            t.start()
            t.join(seconds)
            if t.is_alive():
                raise _TimeoutError(f"{func.__name__} timed out after {seconds}s")
            if exception[0]:
                raise exception[0]
            return result[0]
        return wrapper
    return decorator


def _retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0,
          exceptions=(ConnectionError, _TimeoutError, OSError)):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            wait_time = delay
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        _time.sleep(wait_time)
                        wait_time *= backoff
            raise last_exception
        return wrapper
    return decorator


# ── 持仓原始列表 ─────────────────────────────────────────────────────

_RAW_WATCHLIST = """
sz000333	美的集团
sz000651	格力电器
sh600887	伊利股份
sh600276	恒瑞医药
sz000963	华东医药
sz300003	乐普医疗
sz300760	迈瑞医疗
hk02359	药明康德
hk01810	小米集团-W
hk00700	腾讯控股
hk09626	哔哩哔哩-W
hk09988	阿里巴巴-SW
hk09888	百度集团-SW
sz002049	紫光国微
sh600745	闻泰科技
sh603501	豪威集团
sh603986	兆易创新
sh601012	隆基绿能
sh600309	万华化学
sz300012	华测检测
sz300699	光威复材
sh512040	中证价值ETF
sh515080	中证红利ETF招商
sz159201	自由现金流ETF
sh560050	中国A50ETF
sh560350	中证A50ETF摩根
sh561230	中证A50ETF工银
sz159606	500质量ETF
sz159967	创成长ETF
sh588210	科创100ETF
sh588000	科创50ETF
sh513660	恒生ETF
sh513500	标普500ETF博时
sh513390	纳指100ETF博时
sh513080	法国CAC40ETF
sh513030	德国ETF
sh512600	中证消费ETF
sh513970	恒生消费ETF
sh513060	恒生医疗ETF
sh513010	恒生科技ETF
sh513050	中概互联50ETF
sh512670	国防ETF鹏华
sz159981	能源化工期货ETF
sh515220	煤炭ETF
sz159938	全指医药ETF
sz159939	全指信息ETF
sh515230	软件ETF
sz159995	国证芯片ETF
015600	创业板国泰(LOF)C
023917	自由现金流A
023919	现金流A
217027	央视50A
021231	中证A50工银A
009726	500等权增强招商A
007994	500指数增强华夏A
014201	中证1000增强天弘A
004194	中证1000增强招商A
018177	科创板50增强华夏A
019768	科创板50增强景顺A
020683	科创100南方A
005734	恒生C
050025	标普500博时
040046	纳斯达克100华安A
014424	恒生医疗A
001717	工银医疗A
006003	工银医药C
519915	消费主题A
011309	消费主题C
001917	招商量化精选股票A
004814	中欧红利优享混合A
000628	大成高鑫股票A
006624	中泰玉衡价值优选混合A
020602	中证红利低波A
021457	恒生高股息低波A
006961	南方7-10年国开债A
017837	博时中债7-10政金债指数A
001235	中银国有企业债A
003547	鹏华丰禄债券
004388	鹏华丰享债券
007888	农银金盈债券A
003156	招商招悦纯债A
006210	东方臻宝纯债债券A
000931	国寿安保尊益信用纯债一年
"""


# ── 品种分类 ─────────────────────────────────────────────────────────

def classify_code(raw_code: str) -> str:
    code = raw_code.strip()
    if code.startswith("hk"):
        return "hk_stock"
    if code.startswith("sz") or code.startswith("sh"):
        digits = code[2:]
        if digits.startswith(("000", "001", "002", "003", "300", "301",
                              "600", "601", "603", "605", "688")):
            return "stock_a"
        else:
            return "etf"
    return "fund"


def strip_prefix(raw_code: str) -> str:
    return raw_code.strip().lstrip("szshhk")


# ── 行情 API ─────────────────────────────────────────────────────────

_TENCENT_URL = "http://qt.gtimg.cn/q={}"


def _get_watchlist_codes_by_category(category: str) -> list[str]:
    codes = []
    for line in _RAW_WATCHLIST.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 1:
            continue
        raw_code = parts[0].strip()
        if classify_code(raw_code) == category:
            codes.append(raw_code)
    return codes


@_retry(max_attempts=3, delay=1.0, backoff=2.0, exceptions=(ConnectionError, _TimeoutError, OSError))
def _fetch_tencent_data(codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    url = _TENCENT_URL.format(",".join(codes))
    try:
        r = requests.get(url, timeout=15)
        r.encoding = "gbk"
        text = r.text
    except Exception as e:
        print(f"  [WARN] 腾讯行情网络错误: {e}", file=sys.stderr)
        return pd.DataFrame()

    rows = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'v_([^=]+)="(.+)"', line)
        if not m:
            continue
        fields = m.group(2).split("~")
        if len(fields) < 35:
            continue
        try:
            price = float(fields[3]) if fields[3] else None
        except ValueError:
            price = None
        name = fields[1] if len(fields) > 1 else ""
        try:
            change_amt = float(fields[31]) if fields[31] else None
        except ValueError:
            change_amt = None
        try:
            change_pct = float(fields[32]) if fields[32] else None
        except ValueError:
            change_pct = None
        try:
            high = float(fields[33]) if fields[33] else None
        except ValueError:
            high = None
        try:
            low = float(fields[34]) if fields[34] else None
        except ValueError:
            low = None
        try:
            open_price = float(fields[5]) if fields[5] else None
        except ValueError:
            open_price = None
        try:
            close_price = float(fields[4]) if fields[4] else None
        except ValueError:
            close_price = None
        try:
            volume = int(float(fields[6])) if fields[6] else None
        except ValueError:
            volume = None
        amount = None
        if len(fields) > 36 and fields[36]:
            parts_36 = fields[36].split("/")
            if len(parts_36) >= 3:
                try:
                    amount = float(parts_36[2])
                except ValueError:
                    pass
        update_date = ""
        timestamp_raw = fields[30] if len(fields) > 30 else ""
        if timestamp_raw:
            if "/" in timestamp_raw:
                update_date = timestamp_raw[:10].replace("/", "-")
            else:
                ts = timestamp_raw.strip()
                if len(ts) >= 8 and ts.isdigit():
                    update_date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        rows.append({
            "代码": m.group(1), "名称": name,
            "最新价": price, "涨跌幅": change_pct, "涨跌额": change_amt,
            "今开": open_price, "昨收": close_price,
            "最高": high, "最低": low,
            "成交量": volume, "成交额": amount,
            "更新日期": update_date,
        })
    return pd.DataFrame(rows)


def _try_sources(category_label: str, sources: list, codes: list[str]) -> pd.DataFrame:
    last_error = None
    for name, fetch_func in sources:
        try:
            t0 = _time.time()
            df = fetch_func(codes)
            elapsed = _time.time() - t0
            if df is not None and not df.empty:
                print(f"[OK] {category_label} 完成 ({len(df)} 只, {elapsed:.1f}s, 来源={name})")
                return df
            else:
                print(f"[INFO] {category_label} 空数据 ({elapsed:.1f}s, 来源={name})")
        except Exception as e:
            print(f"[WARN] {category_label} 失败 ({name}): {e}", file=sys.stderr)
            last_error = e
            continue
    if last_error is not None:
        print(f"[WARN] {category_label} 所有数据源均失败，最后错误: {last_error}", file=sys.stderr)
    return pd.DataFrame()


@_with_timeout(30)
def _fetch_sina_a_spot(codes: list[str]) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_spot()


@_with_timeout(30)
def _fetch_sina_hk_spot(codes: list[str]) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_hk_spot()


@_with_timeout(30)
def _fetch_sina_etf_spot(codes: list[str]) -> pd.DataFrame:
    import akshare as ak
    return ak.fund_etf_category_sina()


@_with_timeout(30)
def _fetch_em_fund_daily(codes: list[str]) -> pd.DataFrame:
    import akshare as ak
    df = ak.fund_open_fund_daily_em()
    df["基金代码"] = df["基金代码"].astype(str)
    return df


@_with_timeout(30)
def _fetch_em_fund_by_info(codes: list[str]) -> pd.DataFrame:
    import akshare as ak
    rows = []
    for code in codes:
        try:
            df_info = ak.fund_open_fund_info_em(symbol=code)
            if df_info is None or df_info.empty:
                continue
            df_info = df_info.sort_values(by=df_info.columns[0], ascending=False)
            latest = df_info.iloc[0]
            date_val = str(latest.iloc[0]) if len(df_info.columns) >= 1 else ""
            nav = latest.iloc[1] if len(df_info.columns) >= 2 else None
            change_pct = latest.iloc[2] if len(df_info.columns) >= 3 else None
            rows.append({
                "基金代码": code,
                "单位净值": nav,
                "日增长率": change_pct,
                "更新日期": date_val[:10] if date_val else "",
            })
        except Exception:
            continue
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fetch_stock_a_data() -> pd.DataFrame:
    codes = _get_watchlist_codes_by_category("stock_a")
    print(f"正在获取 A 股实时行情 ({len(codes)} 只)...")
    return _try_sources("A股", [("腾讯行情", _fetch_tencent_data), ("新浪行情", _fetch_sina_a_spot)], codes)


def fetch_hk_stock_data() -> pd.DataFrame:
    codes = _get_watchlist_codes_by_category("hk_stock")
    print(f"正在获取港股实时行情 ({len(codes)} 只)...")
    return _try_sources("港股", [("腾讯行情", _fetch_tencent_data), ("新浪行情", _fetch_sina_hk_spot)], codes)


def fetch_etf_data() -> pd.DataFrame:
    codes = _get_watchlist_codes_by_category("etf")
    print(f"正在获取 ETF 实时行情 ({len(codes)} 只)...")
    return _try_sources("ETF", [("腾讯行情", _fetch_tencent_data), ("新浪行情", _fetch_sina_etf_spot)], codes)


def fetch_open_fund_data() -> pd.DataFrame:
    codes = _get_watchlist_codes_by_category("fund")
    print(f"正在获取开放式基金净值 ({len(codes)} 只)...")
    return _try_sources("基金", [("东方财富-全市场", _fetch_em_fund_daily), ("东方财富-逐只查询", _fetch_em_fund_by_info)], codes)


# ── 持仓单品 ─────────────────────────────────────────────────────────

@dataclass
class HoldingItem:
    raw_code: str
    name: str
    category: str
    stripped: str = ""
    matched: bool = False
    price: Optional[float] = None
    change_pct: Optional[float] = None
    change_amt: Optional[float] = None
    date: Optional[str] = None
    extra: dict = field(default_factory=dict)


# ── 工具函数 ─────────────────────────────────────────────────────────

def to_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if not pd.isna(v) else None
    try:
        return float(str(v).strip("% "))
    except (ValueError, TypeError):
        return None


def fmt_val(v, decimals=4) -> str:
    vf = to_float(v)
    if vf is None:
        return "-"
    try:
        if abs(vf) >= 1e8:
            return f"{vf/1e8:.2f}亿"
        elif abs(vf) >= 1e4:
            return f"{vf/1e4:.2f}万"
        return f"{vf:.{decimals}f}"
    except (ValueError, TypeError):
        return str(v)


def fmt_pct(v) -> str:
    vf = to_float(v)
    if vf is None:
        return "-"
    return f"{vf:+.2f}"


def fmt_amt(v) -> str:
    vf = to_float(v)
    if vf is None:
        return "-"
    return f"{vf:+.4f}"


# ── 查询与匹配 ───────────────────────────────────────────────────────

def _extract_date_from_row(r: pd.Series) -> Optional[str]:
    for col in ("更新日期", "时间"):
        val = r.get(col)
        if val is not None and val != "" and not (isinstance(val, float) and pd.isna(val)):
            s = str(val).strip()
            if len(s) >= 10:
                return s[:10]
    return None


def query_stock_a(items: list[HoldingItem], df_all: pd.DataFrame):
    for it in items:
        if it.category != "stock_a":
            continue
        row = df_all[df_all["代码"] == it.raw_code]
        if row.empty:
            continue
        r = row.iloc[0]
        it.matched = True
        it.price = r.get("最新价")
        it.change_pct = r.get("涨跌幅")
        it.change_amt = r.get("涨跌额")
        it.date = _extract_date_from_row(r)
        it.extra = {k: r.get(k) for k in ("最高", "最低", "今开", "昨收", "成交量", "成交额") if r.get(k) is not None}


def query_hk_stock(items: list[HoldingItem], df_all: pd.DataFrame):
    for it in items:
        if it.category != "hk_stock":
            continue
        row = df_all[df_all["代码"] == it.raw_code]
        if row.empty:
            continue
        r = row.iloc[0]
        it.matched = True
        it.price = r.get("最新价")
        it.change_pct = r.get("涨跌幅")
        it.change_amt = r.get("涨跌额")
        it.date = _extract_date_from_row(r)
        it.extra = {k: r.get(k) for k in ("最高", "最低", "今开", "昨收", "成交量", "成交额") if r.get(k) is not None}


def query_etf(items: list[HoldingItem], df_all: pd.DataFrame):
    for it in items:
        if it.category != "etf":
            continue
        row = df_all[df_all["代码"] == it.raw_code]
        if row.empty:
            continue
        r = row.iloc[0]
        it.matched = True
        it.price = r.get("最新价")
        it.change_pct = r.get("涨跌幅")
        it.change_amt = r.get("涨跌额")
        it.date = _extract_date_from_row(r)
        it.extra = {k: r.get(k) for k in ("最高", "最低", "今开", "昨收", "成交量", "成交额") if r.get(k) is not None}


@_with_timeout(15)
def _fetch_fund_change_from_history(fund_code: str) -> Optional[float]:
    import akshare as ak
    try:
        df = ak.fund_open_fund_info_em(symbol=fund_code)
        if df is None or df.empty:
            return None
        if len(df.columns) >= 3:
            df = df.sort_values(df.columns[0], ascending=False)
            latest = df.iloc[0]
            change = latest[df.columns[2]]
            return to_float(change)
    except _TimeoutError:
        raise
    except Exception as e:
        print(f"  [WARN] 获取基金 {fund_code} 历史数据失败: {e}", file=sys.stderr)
    return None


def _parse_fund_date_from_columns(df_all: pd.DataFrame) -> Optional[str]:
    for c in df_all.columns:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(c))
        if m:
            return m.group(1)
    return None


def query_fund(items: list[HoldingItem], df_all: pd.DataFrame):
    nav_col = None
    prev_nav_col = None
    prev_acc_col = None
    for c in df_all.columns:
        if "单位净值" in str(c) and "累计" not in str(c) and "前" not in str(c):
            nav_col = c
        elif "前交易日" in str(c) and "单位净值" in str(c):
            prev_nav_col = c
        elif "前交易日" in str(c) and "累计净值" in str(c):
            prev_acc_col = c
    fund_date = _parse_fund_date_from_columns(df_all)
    for it in items:
        if it.category != "fund":
            continue
        row = df_all[df_all["基金代码"] == it.raw_code]
        if row.empty:
            continue
        r = row.iloc[0]
        it.matched = True
        it.price = r.get(nav_col) if nav_col else None
        change_pct = r.get("日增长率")
        if change_pct is None or (isinstance(change_pct, float) and pd.isna(change_pct)) or (isinstance(change_pct, str) and change_pct.strip() == ""):
            change_pct = _fetch_fund_change_from_history(it.raw_code)
        it.change_pct = change_pct
        it.change_amt = r.get("日增长值")
        it.date = fund_date
        it.extra = {}
        for k in ("累计净值", "前日-单位净值", "前日-累计净值", "申购状态", "赎回状态"):
            v = r.get(prev_nav_col if k == "前日-单位净值" and prev_nav_col else
                      prev_acc_col if k == "前日-累计净值" and prev_acc_col else k)
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                it.extra[k] = v


# ── 统一爬取接口（crawler.py 核心）────────────────────────────────────

def crawl(codes: list[str], quiet: bool = True, use_cache: bool = True) -> list[dict]:
    """
    爬取指定代码列表的最新价和涨幅。
    统一接口，内部整合 watchlist 的全部数据获取逻辑。
    """
    if not codes:
        return []

    codes = [c.strip() for c in codes if c.strip()]

    # TTL 缓存
    if use_cache and is_cache_valid():
        cached = load_price_cache()
        cached_prices = cached.get("prices", {})
        results = []
        for raw_code in codes:
            if raw_code in cached_prices:
                entry = cached_prices[raw_code]
                results.append(entry)
            else:
                results.append({
                    "code": raw_code, "name": raw_code,
                    "matched": False, "price": None,
                    "change_pct": None, "date": None,
                })
        if results and any(r["matched"] for r in results):
            _log_cache_hit(len([r for r in results if r["matched"]]), len(codes))
        return results

    # 构建 HoldingItem
    items = []
    seen = set()
    for raw_code in codes:
        if not raw_code or raw_code in seen:
            continue
        seen.add(raw_code)
        cat = classify_code(raw_code)
        items.append(HoldingItem(
            raw_code=raw_code, name=raw_code,
            category=cat, stripped=strip_prefix(raw_code),
        ))

    stock_a_items = [it for it in items if it.category == "stock_a"]
    hk_items = [it for it in items if it.category == "hk_stock"]
    etf_items = [it for it in items if it.category == "etf"]
    fund_items = [it for it in items if it.category == "fund"]

    _stdout = sys.stdout
    if quiet:
        sys.stdout = io.StringIO()
    try:
        if stock_a_items:
            try:
                df = fetch_stock_a_data()
                query_stock_a(items, df)
            except Exception as e:
                print(f"  [WARN] A股数据获取失败: {e}", file=sys.stderr)
        if hk_items:
            try:
                df = fetch_hk_stock_data()
                query_hk_stock(items, df)
            except Exception as e:
                print(f"  [WARN] 港股数据获取失败: {e}", file=sys.stderr)
        if etf_items:
            try:
                df = fetch_etf_data()
                query_etf(items, df)
            except Exception as e:
                print(f"  [WARN] ETF数据获取失败: {e}", file=sys.stderr)
        if fund_items:
            try:
                df = fetch_open_fund_data()
                query_fund(items, df)
            except Exception as e:
                print(f"  [WARN] 基金数据获取失败: {e}", file=sys.stderr)
    finally:
        if quiet:
            sys.stdout = _stdout

    results = []
    price_map = {}
    for it in items:
        price = to_float(it.price)
        change_raw = to_float(it.change_pct)
        change_pct = f"{change_raw:+.2f}%" if change_raw is not None else None
        result = {
            "code": it.raw_code, "name": it.name, "matched": it.matched,
            "price": price, "change_pct": change_pct, "date": it.date,
        }
        results.append(result)
        if it.matched and price is not None:
            price_map[it.raw_code] = result

    if use_cache and price_map:
        save_price_cache(price_map)

    return results


def _log_cache_hit(hit_count: int, total_count: int):
    msg = f"[CACHE] 命中 {hit_count}/{total_count} 条（TTL 内有效）"
    print(msg, file=sys.stderr)


# ── CLI 入口 ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="爬取股票/ETF/基金数据")
    parser.add_argument("--codes", type=str, help='JSON 数组字符串，如 \'["sz000333","hk00700"]\'')
    args = parser.parse_args()
    try:
        if args.codes:
            codes = json.loads(args.codes)
        else:
            raw = sys.stdin.read()
            if not raw.strip():
                print(json.dumps([], ensure_ascii=False))
                return
            codes = json.loads(raw)
        if not isinstance(codes, list):
            print(json.dumps({"error": "输入必须是 JSON 数组"}, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"JSON 解析失败: {e}"}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    results = crawl(codes)
    print(json.dumps(results, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
