"""
持仓工具 - 使用 AKShare 获取持仓列表中各品种的净值/价格/涨跌信息

支持的品种类型:
  - A股股票 (sz/sz/sh 前缀, 6位代码)
  - 港股     (hk 前缀)
  - ETF     (sz/sh 前缀, 交易所交易基金)
  - LOF/开放式基金 (无前缀的 6 位基金代码)

数据来源:
  - A股/港股/ETF: 新浪财经 (通过 akshare)
  - 开放式基金:   天天基金/东方财富 (通过 akshare)
"""

import sys
import time
import warnings
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

warnings.filterwarnings("ignore")

# ============================================================
# 1. 定义持仓列表 (代码\t名称)
# ============================================================

RAW_WATCHLIST = """
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


# ============================================================
# 2. 品种分类逻辑
# ============================================================

def classify_code(raw_code: str) -> str:
    """
    根据原始代码前缀和数字特征判断品种类型:
      - 'stock_a'  : A股股票
      - 'hk_stock' : 港股
      - 'etf'      : ETF
      - 'fund'     : LOF / 开放式基金
    """
    code = raw_code.strip()

    if code.startswith("hk"):
        return "hk_stock"

    if code.startswith("sz") or code.startswith("sh"):
        digits = code[2:]
        # A股典型代码范围
        # sz: 000xxx, 001xxx, 002xxx, 003xxx, 300xxx, 301xxx
        # sh: 600xxx, 601xxx, 603xxx, 605xxx, 688xxx
        if digits.startswith(("000", "001", "002", "003", "300", "301",
                              "600", "601", "603", "605", "688")):
            return "stock_a"
        else:
            return "etf"

    # 无前缀 – LOF/开放式基金
    return "fund"


def strip_prefix(raw_code: str) -> str:
    """去掉 sz/sh/hk 前缀，返回纯数字代码"""
    return raw_code.strip().lstrip("szshhk")


# ============================================================
# 3. 从 akshare 拉取数据
# ============================================================

def fetch_stock_a_data() -> pd.DataFrame:
    """获取所有 A 股实时行情 (新浪财经)"""
    import akshare as ak
    print("  → 正在获取 A 股实时行情...", end=" ", flush=True)
    t0 = time.time()
    df = ak.stock_zh_a_spot()
    # 新浪返回的代码带前缀 e.g. "sz000333", "sh600887"
    df["代码"] = df["代码"].astype(str)
    print(f"完成 ({len(df)} 只, {time.time()-t0:.1f}s)")
    return df


def fetch_hk_stock_data() -> pd.DataFrame:
    """获取所有港股实时行情 (新浪财经)"""
    import akshare as ak
    print("  → 正在获取港股实时行情...", end=" ", flush=True)
    t0 = time.time()
    df = ak.stock_hk_spot()
    # 新浪返回的港股代码是纯数字 e.g. "00700"
    df["代码"] = df["代码"].astype(str)
    print(f"完成 ({len(df)} 只, {time.time()-t0:.1f}s)")
    return df


def fetch_etf_data() -> pd.DataFrame:
    """获取所有 ETF 实时行情 (新浪财经)"""
    import akshare as ak
    print("  → 正在获取 ETF 实时行情...", end=" ", flush=True)
    t0 = time.time()
    df = ak.fund_etf_category_sina(symbol="ETF基金")
    # 新浪返回的代码带前缀 e.g. "sz159998", "sh512040"
    df["代码"] = df["代码"].astype(str)
    print(f"完成 ({len(df)} 只, {time.time()-t0:.1f}s)")
    return df


def fetch_open_fund_data() -> pd.DataFrame:
    """获取所有开放式基金实时净值 (天天基金/东方财富)"""
    import akshare as ak
    print("  → 正在获取开放式基金净值...", end=" ", flush=True)
    t0 = time.time()
    df = ak.fund_open_fund_daily_em()
    df["基金代码"] = df["基金代码"].astype(str)
    print(f"完成 ({len(df)} 只, {time.time()-t0:.1f}s)")
    return df


# ============================================================
# 3.5 持仓单品 dataclass
# ============================================================

@dataclass
class HoldingItem:
    """持仓单品"""
    raw_code: str          # 原始代码 e.g. 'sz000333'
    name: str              # 用户填的名称
    category: str          # 品种分类
    stripped: str = ""     # 纯数字代码
    # 查询结果
    matched: bool = False
    price: Optional[float] = None       # 最新价 / 净值
    change_pct: Optional[float] = None  # 涨跌幅(%)
    change_amt: Optional[float] = None  # 涨跌额 / 日增长值
    extra: dict = field(default_factory=dict)


# ============================================================
# 4. 查询与匹配
# ============================================================

def query_stock_a(items: list[HoldingItem], df_all: pd.DataFrame):
    """
    从 A股 DataFrame 中匹配。
    Sina A股 代码格式: "sz000333", "sh600887" → 与用户原始代码一致。
    """
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
        it.extra = {
            "最高": r.get("最高"),
            "最低": r.get("最低"),
            "今开": r.get("今开"),
            "昨收": r.get("昨收"),
            "成交量": r.get("成交量"),
            "成交额": r.get("成交额"),
        }


def query_hk_stock(items: list[HoldingItem], df_all: pd.DataFrame):
    """
    从港股 DataFrame 中匹配。
    Sina 港股代码格式: 纯数字 "00700" → 需要从 "hk00700" 中去掉前缀。
    """
    for it in items:
        if it.category != "hk_stock":
            continue
        row = df_all[df_all["代码"] == it.stripped]
        if row.empty:
            continue
        r = row.iloc[0]
        it.matched = True
        it.price = r.get("最新价")
        it.change_pct = r.get("涨跌幅")
        it.change_amt = r.get("涨跌额")
        it.extra = {
            "最高": r.get("最高"),
            "最低": r.get("最低"),
            "今开": r.get("今开"),
            "昨收": r.get("昨收"),
            "成交量": r.get("成交量"),
            "成交额": r.get("成交额"),
        }


def query_etf(items: list[HoldingItem], df_all: pd.DataFrame):
    """
    从 ETF DataFrame 中匹配。
    Sina ETF 代码格式: "sz159998", "sh512040" → 与用户原始代码一致。
    """
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
        it.extra = {
            "最高": r.get("最高"),
            "最低": r.get("最低"),
            "今开": r.get("今开"),
            "昨收": r.get("昨收"),
            "成交量": r.get("成交量"),
            "成交额": r.get("成交额"),
        }


def _fetch_fund_change_from_history(fund_code: str) -> Optional[float]:
    """
    从 fund_open_fund_info_em 获取单只基金的历史数据，
    返回最新一条记录的日涨跌幅（百分比数值，如 -1.14）。
    如果获取失败或无数据返回 None。
    """
    import akshare as ak
    try:
        df = ak.fund_open_fund_info_em(symbol=fund_code)
        if df is None or df.empty:
            return None
        # 历史数据列: 日期, 单位净值, 日增长率
        if len(df.columns) >= 3:
            # 取最新一条（按日期降序）
            df = df.sort_values(df.columns[0], ascending=False)
            latest = df.iloc[0]
            change = latest[df.columns[2]]  # 日增长率列
            return to_float(change)
    except Exception:
        pass
    return None


def query_fund(items: list[HoldingItem], df_all: pd.DataFrame):
    """
    从开放式基金 DataFrame 中匹配。
    基金代码格式: 纯数字 "015600" → 与用户代码一致(无前缀)。

    注意: fund_open_fund_daily_em() 的单位净值和累计净值是日期动态列名
    (如 "2026-05-15-单位净值"), 需要模糊匹配。
    """
    # 找出日期动态列名
    nav_col = None       # 单位净值列
    acc_nav_col = None   # 累计净值列
    prev_nav_col = None  # 前交易日-单位净值
    prev_acc_col = None  # 前交易日-累计净值
    for c in df_all.columns:
        if "单位净值" in str(c) and "累计" not in str(c) and "前" not in str(c):
            nav_col = c
        elif "累计净值" in str(c) and "前" not in str(c):
            acc_nav_col = c
        elif "前交易日" in str(c) and "单位净值" in str(c):
            prev_nav_col = c
        elif "前交易日" in str(c) and "累计净值" in str(c):
            prev_acc_col = c

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
        # 如果日增长率为空（None、NaN或空字符串），尝试从历史数据获取
        if change_pct is None or (isinstance(change_pct, float) and pd.isna(change_pct)) or (isinstance(change_pct, str) and change_pct.strip() == ""):
            change_pct = _fetch_fund_change_from_history(it.raw_code)
        it.change_pct = change_pct
        it.change_amt = r.get("日增长值")
        it.extra = {
            "累计净值": r.get(acc_nav_col) if acc_nav_col else None,
            "前日-单位净值": r.get(prev_nav_col) if prev_nav_col else None,
            "前日-累计净值": r.get(prev_acc_col) if prev_acc_col else None,
            "申购状态": r.get("申购状态"),
            "赎回状态": r.get("赎回状态"),
        }


# ============================================================
# 5. 输出
# ============================================================

def to_float(v) -> Optional[float]:
    """安全转换为 float"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if not pd.isna(v) else None
    try:
        return float(str(v).strip("% "))
    except (ValueError, TypeError):
        return None


def fmt_val(v, decimals=4) -> str:
    """格式化数值"""
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
    """格式化涨跌幅 (带 +/- 符号)"""
    vf = to_float(v)
    if vf is None:
        return "-"
    return f"{vf:+.2f}"
    
    
def fmt_amt(v) -> str:
    """格式化涨跌额"""
    vf = to_float(v)
    if vf is None:
        return "-"
    return f"{vf:+.4f}"


def print_summary(items: list[HoldingItem]):
    """打印概览表"""
    rows = []
    for it in items:
        if not it.matched:
            rows.append({
                "品种": it.category,
                "代码": it.raw_code,
                "名称": it.name,
                "最新价/净值": "-",
                "涨跌幅(%)": "-",
                "涨跌额": "-",
                "状态": "? 未匹配",
            })
        else:
            pct = fmt_pct(it.change_pct)
            amt = fmt_amt(it.change_amt)
            price = fmt_val(it.price)
            cat_map = {
                "stock_a": "A股",
                "hk_stock": "港股",
                "etf": "ETF",
                "fund": "基金",
            }
            rows.append({
                "品种": cat_map.get(it.category, it.category),
                "代码": it.raw_code,
                "名称": it.name,
                "最新价/净值": price,
                "涨跌幅(%)": pct,
                "涨跌额": amt,
                "状态": "OK",
            })

    df_out = pd.DataFrame(rows)
    print("\n" + "=" * 100)
    print("持仓概览 (summary)")
    print("=" * 100)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.unicode.east_asian_width", True)
    print(df_out.to_string(index=False))
    print("=" * 100)

    matched_count = sum(1 for it in items if it.matched)
    print(f"共 {len(items)} 只, 成功获取 {matched_count} 只, 未匹配 {len(items)-matched_count} 只\n")


def print_detail(items: list[HoldingItem]):
    """打印详细数据"""
    for it in items:
        if not it.matched:
            continue

        cat_map = {
            "stock_a": "A股",
            "hk_stock": "港股",
            "etf": "ETF",
            "fund": "基金",
        }
        print(f"{'─'*60}")
        print(f"  {it.raw_code}  {it.name}  [{cat_map.get(it.category, it.category)}]")
        print(f"{'─'*60}")
        price = fmt_val(it.price)
        pct = fmt_pct(it.change_pct) + "%"
        amt = fmt_amt(it.change_amt)
        print(f"  最新价/净值:  {price}")
        print(f"  涨跌幅:       {pct}")
        print(f"  涨跌额:       {amt}")
        for k, v in it.extra.items():
            vf = to_float(v)
            if vf is not None:
                print(f"  {k}: {fmt_val(v)}")
            elif v is not None and not (isinstance(v, float) and pd.isna(v)):
                print(f"  {k}: {v}")


# ============================================================
# 6. main
# ============================================================

def main():
    # 解析持仓列表
    items: list[HoldingItem] = []
    seen = set()
    for line in RAW_WATCHLIST.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        raw_code, name = parts[0].strip(), parts[1].strip()
        if raw_code in seen:
            continue
        seen.add(raw_code)
        cat = classify_code(raw_code)
        stripped = strip_prefix(raw_code)
        items.append(HoldingItem(
            raw_code=raw_code,
            name=name,
            category=cat,
            stripped=stripped,
        ))

    # 统计各品种数量
    cat_count = Counter(it.category for it in items)
    print(f"持仓总数: {len(items)} 只")
    print(f"  A股: {cat_count.get('stock_a', 0)}  |  港股: {cat_count.get('hk_stock', 0)}  |  ETF: {cat_count.get('etf', 0)}  |  基金: {cat_count.get('fund', 0)}")
    print()

    # 按类型分批拉取数据
    stock_a_items = [it for it in items if it.category == "stock_a"]
    hk_items = [it for it in items if it.category == "hk_stock"]
    etf_items = [it for it in items if it.category == "etf"]
    fund_items = [it for it in items if it.category == "fund"]

    if stock_a_items:
        df = fetch_stock_a_data()
        query_stock_a(items, df)

    if hk_items:
        df = fetch_hk_stock_data()
        query_hk_stock(items, df)

    if etf_items:
        df = fetch_etf_data()
        query_etf(items, df)

    if fund_items:
        df = fetch_open_fund_data()
        query_fund(items, df)

    # 输出
    print_summary(items)
    print_detail(items)


if __name__ == "__main__":
    main()
