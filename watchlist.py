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
import re
import warnings
import threading
from collections import Counter
from dataclasses import dataclass, field
from functools import wraps
from typing import Optional

import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ============================================================
# 0. 通用工具函数
# ============================================================

class TimeoutError(Exception):
    """操作超时异常"""
    pass


def with_timeout(seconds: int):
    """跨平台超时装饰器（基于线程，适用于 Windows）"""
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
                raise TimeoutError(f"{func.__name__} timed out after {seconds}s")
            if exception[0]:
                raise exception[0]
            return result[0]
        return wrapper
    return decorator


def retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0,
          exceptions=(ConnectionError, TimeoutError, OSError)):
    """指数退避重试装饰器"""
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
                        time.sleep(wait_time)
                        wait_time *= backoff
            raise last_exception
        return wrapper
    return decorator


class Logger:
    """简单的时间戳日志记录器"""
    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def _stamp(self, level: str, msg: str) -> str:
        return f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}"

    def info(self, msg: str):
        if not self.quiet:
            print(self._stamp("INFO", msg))

    def warn(self, msg: str):
        print(self._stamp("WARN", msg), file=sys.stderr)

    def error(self, msg: str):
        print(self._stamp("ERROR", msg), file=sys.stderr)

    def success(self, msg: str):
        if not self.quiet:
            print(self._stamp("OK", msg))


# 全局日志实例
log = Logger()


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
# 3. 从行情 API 拉取数据
# ============================================================

# 腾讯行情 API (qt.gtimg.cn) – 稳定可靠，替代已失效的新浪/东财接口
_TENCENT_URL = "http://qt.gtimg.cn/q={}"


def _get_watchlist_codes_by_category(category: str) -> list[str]:
    """从 RAW_WATCHLIST 中提取指定品种的代码列表"""
    codes = []
    for line in RAW_WATCHLIST.strip().splitlines():
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


@retry(max_attempts=3, delay=1.0, backoff=2.0,
       exceptions=(ConnectionError, TimeoutError, OSError, TimeoutError))
def _fetch_tencent_data(codes: list[str]) -> pd.DataFrame:
    """
    通过腾讯行情 API 获取指定代码的实时行情。

    返回 DataFrame, 列名与旧版新浪接口一致（代码、名称、最新价、涨跌幅等）。
    codes 格式示例: ["sh600887", "sz000333", "hk00700"]
    """
    if not codes:
        return pd.DataFrame()

    url = _TENCENT_URL.format(",".join(codes))
    try:
        r = requests.get(url, timeout=15)
    except Exception as e:
        log.warn(f"腾讯行情网络错误: {e}")
        return pd.DataFrame()
    try:
        r.encoding = "gbk"
        text = r.text
    except Exception:
        return pd.DataFrame()

    rows = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'v_([^=]+)="(.+)"', line)
        if not m:
            continue
        raw_code = m.group(1)  # e.g. "sh600887", "hk00700"
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

        # 成交额: index 36 格式 "price/volume/amount"
        amount = None
        if len(fields) > 36 and fields[36]:
            parts_36 = fields[36].split("/")
            if len(parts_36) >= 3:
                try:
                    amount = float(parts_36[2])
                except ValueError:
                    pass

        # 更新时间: field [30] 包含交易时间戳
        #   A股/ETF 格式: YYYYMMDDHHMMSS (e.g. 20260519161500)
        #   港股 格式:   YYYY/MM/DD HH:MM:SS (e.g. 2026/05/19 16:08:27)
        update_date = ""
        timestamp_raw = fields[30] if len(fields) > 30 else ""
        if timestamp_raw:
            if "/" in timestamp_raw:
                # 港股格式 YYYY/MM/DD
                update_date = timestamp_raw[:10].replace("/", "-")
            else:
                # A/ETF 格式 YYYYMMDDHHMMSS
                # 至少需要 8 位 (YYYYMMDD)
                ts = timestamp_raw.strip()
                if len(ts) >= 8 and ts.isdigit():
                    update_date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"

        rows.append({
            "代码": raw_code,
            "名称": name,
            "最新价": price,
            "涨跌幅": change_pct,
            "涨跌额": change_amt,
            "今开": open_price,
            "昨收": close_price,
            "最高": high,
            "最低": low,
            "成交量": volume,
            "成交额": amount,
            "更新日期": update_date,
        })

    return pd.DataFrame(rows)


# ============================================================
# 3.5 多数据源故障转移（fallback chain）
# ============================================================

def _try_sources(
    category_label: str,
    sources: list,
    codes: list[str],
) -> pd.DataFrame:
    """
    依次尝试多个数据源，直到其中一个返回非空 DataFrame。

    参数:
        category_label: 品种名称（用于日志）
        sources: [(source_name, fetch_func), ...]
                 fetch_func(codes) -> pd.DataFrame
        codes:   待查询的代码列表

    返回:
        第一个成功的数据源返回的 DataFrame；全部失败则返回空 DataFrame。
    """
    last_error = None
    for name, fetch_func in sources:
        try:
            t0 = time.time()
            df = fetch_func(codes)
            elapsed = time.time() - t0
            if df is not None and not df.empty:
                log.success(f"{category_label} 完成 ({len(df)} 只, {elapsed:.1f}s, 来源={name})")
                return df
            else:
                log.info(f"{category_label} 空数据 ({elapsed:.1f}s, 来源={name})")
        except Exception as e:
            log.warn(f"{category_label} 失败 ({name}): {e}")
            last_error = e
            continue

    if last_error is not None:
        log.warn(f"{category_label} 所有数据源均失败，最后错误: {last_error}")
    else:
        log.warn(f"{category_label} 所有数据源均返回空数据")
    return pd.DataFrame()


# ============================================================
# 3.6 各品种的备选数据源
# ============================================================

def _fetch_tencent_by_codes(codes: list[str]) -> pd.DataFrame:
    """通过腾讯行情 API 批量查询（已有实现）"""
    return _fetch_tencent_data(codes)


@with_timeout(30)
def _fetch_sina_a_spot(codes: list[str]) -> pd.DataFrame:
    """通过 AKShare 新浪全市场 A 股行情（作为腾讯的备用）"""
    import akshare as ak
    df = ak.stock_zh_a_spot()
    return df


@with_timeout(30)
def _fetch_sina_hk_spot(codes: list[str]) -> pd.DataFrame:
    """通过 AKShare 新浪全市场港股行情（作为腾讯的备用）"""
    import akshare as ak
    df = ak.stock_hk_spot()
    return df


@with_timeout(30)
def _fetch_sina_etf_spot(codes: list[str]) -> pd.DataFrame:
    """通过 AKShare 新浪全市场 ETF 行情（作为腾讯的备用）"""
    import akshare as ak
    df = ak.fund_etf_category_sina()
    return df


@with_timeout(30)
def _fetch_em_fund_daily(codes: list[str]) -> pd.DataFrame:
    """通过 AKShare 东方财富全市场基金日数据"""
    import akshare as ak
    df = ak.fund_open_fund_daily_em()
    df["基金代码"] = df["基金代码"].astype(str)
    return df


@with_timeout(30)
def _fetch_em_fund_by_info(codes: list[str]) -> pd.DataFrame:
    """
    通过 AKShare 东方财富逐只基金历史信息接口（作为全市场接口的备用）。
    返回 DataFrame, 列与 query_fund 兼容: 基金代码, 单位净值, 日增长率, 更新日期, ...
    """
    import pandas as pd
    import akshare as ak
    rows = []
    for code in codes:
        try:
            df_info = ak.fund_open_fund_info_em(symbol=code)
            if df_info is None or df_info.empty:
                continue
            # 按日期降序排列，取最新一条
            df_info = df_info.sort_values(by=df_info.columns[0], ascending=False)
            latest = df_info.iloc[0]
            # df_info 通常有 3 列: 净值日期, 单位净值, 增长率
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
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# 各品种的数据源故障转移链
# 顺序: 优先使用最快最可靠的源，失败后降级
_STOCK_A_SOURCES = [
    ("腾讯行情", _fetch_tencent_by_codes),
    ("新浪行情", _fetch_sina_a_spot),
]
_HK_STOCK_SOURCES = [
    ("腾讯行情", _fetch_tencent_by_codes),
    ("新浪行情", _fetch_sina_hk_spot),
]
_ETF_SOURCES = [
    ("腾讯行情", _fetch_tencent_by_codes),
    ("新浪行情", _fetch_sina_etf_spot),
]
_FUND_SOURCES = [
    ("东方财富-全市场", _fetch_em_fund_daily),
    ("东方财富-逐只查询", _fetch_em_fund_by_info),
]


def fetch_stock_a_data() -> pd.DataFrame:
    """获取自选 A 股实时行情（腾讯 → 新浪 故障转移）"""
    codes = _get_watchlist_codes_by_category("stock_a")
    log.info(f"正在获取 A 股实时行情 ({len(codes)} 只)...")
    return _try_sources("A股", _STOCK_A_SOURCES, codes)


def fetch_hk_stock_data() -> pd.DataFrame:
    """获取自选港股实时行情（腾讯 → 新浪 故障转移）"""
    codes = _get_watchlist_codes_by_category("hk_stock")
    log.info(f"正在获取港股实时行情 ({len(codes)} 只)...")
    return _try_sources("港股", _HK_STOCK_SOURCES, codes)


def fetch_etf_data() -> pd.DataFrame:
    """获取自选 ETF 实时行情（腾讯 → 新浪 故障转移）"""
    codes = _get_watchlist_codes_by_category("etf")
    log.info(f"正在获取 ETF 实时行情 ({len(codes)} 只)...")
    return _try_sources("ETF", _ETF_SOURCES, codes)


def fetch_open_fund_data() -> pd.DataFrame:
    """获取所有开放式基金实时净值（东方财富全市场 → 逐只查询 故障转移）"""
    codes = _get_watchlist_codes_by_category("fund")
    log.info(f"正在获取开放式基金净值 ({len(codes)} 只)...")
    return _try_sources("基金", _FUND_SOURCES, codes)


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
    date: Optional[str] = None          # 更新日期 (YYYY-MM-DD)
    extra: dict = field(default_factory=dict)


# ============================================================
# 4. 查询与匹配
# ============================================================

def _extract_date_from_row(r: pd.Series) -> Optional[str]:
    """
    从 DataFrame 行中提取日期。
    优先级: "更新日期" → "时间" → None。
    """
    for col in ("更新日期", "时间"):
        val = r.get(col)
        if val is not None and val != "" and not (isinstance(val, float) and pd.isna(val)):
            s = str(val).strip()
            # 处理 "2026-05-19 16:08:27" → "2026-05-19"
            if len(s) >= 10:
                return s[:10]
    return None


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
        it.date = _extract_date_from_row(r)
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
    腾讯行情代码格式: "hk00700" → 与 raw_code 一致。
    """
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
        it.date = _extract_date_from_row(r)
        it.extra = {
            "最高": r.get("最高"),
            "最低": r.get("最低"),
            "今开": r.get("今开"),
            "昨收": r.get("昨收"),
            "成交量": r.get("成交量"),
            "成交额": r.get("成交额"),
        }


@with_timeout(15)
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
    except TimeoutError:
        raise
    except Exception as e:
        sys.stderr.write(f"  [WARN] 获取基金 {fund_code} 历史数据失败: {e}\n")
    return None


def _parse_fund_date_from_columns(df_all: pd.DataFrame) -> Optional[str]:
    """
    从基金全市场数据的列名中提取日期（fund_open_fund_daily_em 的日期动态列名如 "2026-05-15-单位净值"）。
    返回 YYYY-MM-DD 格式。
    """
    for c in df_all.columns:
        s = str(c)
        # 匹配 "YYYY-MM-DD-单位净值" 等模式
        import re as _re
        m = _re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
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

    # 从列名提取日期
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
        # 如果日增长率为空（None、NaN或空字符串），尝试从历史数据获取
        if change_pct is None or (isinstance(change_pct, float) and pd.isna(change_pct)) or (isinstance(change_pct, str) and change_pct.strip() == ""):
            change_pct = _fetch_fund_change_from_history(it.raw_code)
        it.change_pct = change_pct
        it.change_amt = r.get("日增长值")
        it.date = fund_date
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
    log.info(f"持仓总数: {len(items)} 只")
    log.info(f"  A股: {cat_count.get('stock_a', 0)}  |  港股: {cat_count.get('hk_stock', 0)}  |  ETF: {cat_count.get('etf', 0)}  |  基金: {cat_count.get('fund', 0)}")
    log.info("")

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
