#!/usr/bin/env python3
"""
财神持仓饼图生成器 v3
- 股票饼图：标签直接标在切片上/旁，百分比=切片真实比例
- 基金双圈：外圈=一级组合（100%），内圈=二级基金嵌套，
  两圈标签均直接标在对应切片上/旁
- 港币按即时汇率换算成人民币
- 输出 PNG 到 财神/charts/
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np

WORKSPACE = Path("/root/.openclaw/workspace-Eva")
SKILL_DIR = WORKSPACE / "feishu-ledger"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from feishu_base import LarkClient
from feishu_config import (
    FEISHU_BASE_TOKEN, HOLDINGS_TABLE_ID, HOLDINGS_FIELD_IDS,
)
from report import (
    build_portfolio, build_watchlist_map,
    load_holdings, load_watchlist, HKD_CNY_RATE,
)

OUT_DIR = WORKSPACE / "财神" / "charts"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 中文字体
CJK_FONT_PATH = None
for _c in [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy-zenhei/wqy-zenhei.ttc",
]:
    if Path(_c).exists():
        CJK_FONT_PATH = _c
        break

if CJK_FONT_PATH:
    cjk_fp = fm.FontProperties(fname=CJK_FONT_PATH)
    plt.rcParams["font.sans-serif"] = [cjk_fp.get_name(), "DejaVu Sans"]
    plt.rcParams["font.family"] = "sans-serif"
else:
    print("[charts] ⚠️ 未找到中文字体")
plt.rcParams["axes.unicode_minus"] = False


def _fp():
    return fm.FontProperties(fname=CJK_FONT_PATH) if CJK_FONT_PATH else None


def _shorten(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n-1] + "…"


# ─────────── 数据加载 ───────────
def load_data():
    client = LarkClient(FEISHU_BASE_TOKEN)
    holdings = load_holdings(client)
    watchlist = load_watchlist(client)
    wl_map = build_watchlist_map(watchlist)
    portfolio = build_portfolio(holdings, wl_map)
    raw = client.get_records(HOLDINGS_TABLE_ID, HOLDINGS_FIELD_IDS)
    primary_of = {}
    for r in raw:
        code = str(r.get("代码", ""))
        primary = r.get("一级组合") or r.get("组合名称") or "其他"
        if isinstance(primary, list):
            primary = primary[0] if primary else "其他"
        if code:
            primary_of[code] = primary
    return portfolio, wl_map, primary_of


# ─────────── 股票饼图（标签直接标在切片上） ───────────
def render_stock_pie(portfolio: dict, wl_map: dict) -> Path | None:
    stock = [x for x in portfolio["stock"] if x["market_value_cny"] > 0]
    if not stock:
        print("[charts] 股票为空，跳过")
        return None

    stock.sort(key=lambda x: x["market_value_cny"], reverse=True)
    total = sum(x["market_value_cny"] for x in stock)
    if total <= 0:
        return None

    # 小于 2% 的归入"其他"
    threshold = 2.0
    main = [x for x in stock if x["market_value_cny"] / total * 100 >= threshold]
    small = [x for x in stock if x["market_value_cny"] / total * 100 < threshold]
    if small:
        other_mv = sum(x["market_value_cny"] for x in small)
        main.append({"name": "其他", "market_value_cny": other_mv, "currency": ""})

    values = [x["market_value_cny"] for x in main]
    names = [x["name"] for x in main]
    pcts = [v / total * 100 for v in values]
    is_hkd = [x["currency"] == "HKD" for x in main]

    n = len(main)
    colors = [plt.get_cmap("tab20")(i % 20) for i in range(n)]

    fig, ax = plt.subplots(figsize=(9, 8))
    ax.set_aspect("equal")
    ax.axis("off")

    # explode：小切片略微拉出
    explode = [0.03 if p < 4 else 0.0 for p in pcts]

    wedges, _ = ax.pie(
        values,
        colors=colors,
        startangle=90,
        counterclock=False,
        explode=explode,
        wedgeprops={"edgecolor": "white", "linewidth": 2},
    )

    # 中心挖空
    centre_circle = plt.Circle((0, 0), 0.38, fc="white", ec="#ddd", lw=1.5)
    ax.add_artist(centre_circle)
    ax.text(0, 0.10, "股票总市值", ha="center", va="center", fontsize=11, color="#666", fontproperties=_fp())
    ax.text(0, -0.04, f"{total:,.0f}", ha="center", va="center",
            fontsize=22, fontweight="bold", color="#222")
    ax.text(0, -0.18, "CNY", ha="center", va="center", fontsize=10, color="#888")

    # 用 wedge 的真实角度来定位标签
    for i, (w, name, pct, hkd) in enumerate(zip(wedges, names, pcts, is_hkd)):
        # wedge 的角度范围
        t1, t2 = w.theta1, w.theta2
        mid = (t1 + t2) / 2.0
        # 转为弧度（matplotlib 角度：0°=右，逆时针）
        rad = np.deg2rad(mid)

        # 切片中心到边缘的连线方向 = mid 的极坐标方向
        # 切片内侧标签（占，切片内 62% 处）
        inner_r = 0.62
        if pct >= 5:
            x = inner_r * np.cos(rad)
            y = inner_r * np.sin(rad)
            ax.text(x, y, f"{pct:.1f}%", ha="center", va="center",
                    fontsize=9.5, fontweight="bold", color="white",
                    fontproperties=_fp())
            # 名称在外侧
            name_r = 0.82
            nx = name_r * np.cos(rad)
            ny = name_r * np.sin(rad)
            ha = "left" if 90 < (mid % 360) < 270 else "right"
            ax.text(nx, ny, _shorten(name, 6), ha=ha, va="center",
                    fontsize=9, color="#222", fontproperties=_fp())
        else:
            # 小切片：名称+百分比都在外侧
            label_r = 1.02
            lx = label_r * np.cos(rad)
            ly = label_r * np.sin(rad)
            ha = "left" if 90 < (mid % 360) < 270 else "right"
            ax.text(lx, ly, f"{_shorten(name, 6)}  {pct:.1f}%",
                    ha=ha, va="center", fontsize=8.5, color="#222",
                    fontproperties=_fp())

        # 港股小标记
        if hkd and name != "其他":
            tag_r = 0.92
            tx = tag_r * np.cos(rad)
            ty = tag_r * np.sin(rad)
            ax.text(tx, ty, "港", fontsize=7, color="#06c",
                    ha="center", va="center", fontweight="bold",
                    fontproperties=_fp())

    ax.set_title(
        f"股票持仓占比  ·  {len(stock)} 只  ·  HKD→CNY {HKD_CNY_RATE:.4f}",
        fontsize=13, fontweight="bold", pad=12,
    )

    fig.tight_layout()
    out = OUT_DIR / f"stock_pie_{datetime.now().strftime('%Y%m%d_%H%M')}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


# ─────────── 基金双圈（标签直接标在切片上） ───────────
def render_fund_donut(portfolio: dict, wl_map: dict, primary_of: dict) -> Path | None:
    etf = portfolio["etf"]
    fund = portfolio["fund"]
    all_fund = etf + fund
    if not all_fund:
        print("[charts] 基金为空，跳过")
        return None

    # 一级 → 二级 → 标的
    tree: dict[str, dict[str, list]] = {}
    for item in all_fund:
        if item["market_value_cny"] <= 0:
            continue
        primary = primary_of.get(item["code"], item["group"] or "其他")
        secondary = item["group"] or "其他"
        tree.setdefault(primary, {}).setdefault(secondary, []).append(item)

    if not tree:
        return None

    # 汇总
    primary_data = []
    for p, secs in tree.items():
        sec_data = []
        for s, items in secs.items():
            mv = sum(x["market_value_cny"] for x in items)
            sec_data.append({"name": s, "mv": mv, "items": items})
        sec_data.sort(key=lambda x: -x["mv"])
        primary_data.append({
            "name": p,
            "mv": sum(x["mv"] for x in sec_data),
            "secondary": sec_data,
        })
    primary_data.sort(key=lambda x: -x["mv"])

    total = sum(p["mv"] for p in primary_data)
    if total <= 0:
        return None

    n_primary = len(primary_data)
    primary_colors = [plt.get_cmap("tab10")(i / max(n_primary, 1)) for i in range(n_primary)]

    # 准备外圈/内圈数据
    outer_vals = [p["mv"] for p in primary_data]
    inner_items = []  # (p_idx, s_name, s_mv)
    for pi, p in enumerate(primary_data):
        for s in p["secondary"]:
            inner_items.append((pi, s["name"], s["mv"]))
    inner_vals = [x[2] for x in inner_items]

    fig, ax = plt.subplots(figsize=(10, 9))
    ax.set_aspect("equal")
    ax.axis("off")

    # 画外圈（一级）
    outer_wedges, _ = ax.pie(
        outer_vals,
        radius=1.25,
        colors=primary_colors,
        startangle=90,
        counterclock=False,
        wedgeprops=dict(width=0.45, edgecolor="white", linewidth=2.5),
    )

    # 画内圈（二级）
    inner_colors = []
    sec_idx = 0
    pastel = plt.get_cmap("Pastel1").colors
    for pi, p in enumerate(primary_data):
        for s in p["secondary"]:
            # 同色系浅色
            base = np.array(primary_colors[pi][:3])
            frac = 0.25 + 0.55 * (sec_idx % 3) / 2.0
            c = tuple(base * (1 - frac) + np.array([1, 1, 1]) * frac)
            inner_colors.append(c)
            sec_idx += 1

    inner_wedges, _ = ax.pie(
        inner_vals,
        radius=0.80,
        colors=inner_colors,
        startangle=90,
        counterclock=False,
        wedgeprops=dict(width=0.38, edgecolor="white", linewidth=1.5),
    )

    # 中心
    ax.text(0, 0.10, "基金总市值", ha="center", va="center",
            fontsize=11, color="#666", fontproperties=_fp())
    ax.text(0, -0.04, f"{total:,.0f}", ha="center", va="center",
            fontsize=20, fontweight="bold", color="#222")
    ax.text(0, -0.18, "CNY", ha="center", va="center",
            fontsize=10, color="#888")

    # 外圈标签（一级）
    for pi, (w, p) in enumerate(zip(outer_wedges, primary_data)):
        t1, t2 = w.theta1, w.theta2
        mid = (t1 + t2) / 2.0
        rad = np.deg2rad(mid)
        pct = p["mv"] / total * 100

        if pct >= 6:
            # 大切片：百分比在切片内
            x = 1.03 * np.cos(rad)
            y = 1.03 * np.sin(rad)
            ha = "left" if 90 < (mid % 360) < 270 else "right"
            ax.text(x, y, f"{pct:.1f}%", ha=ha, va="center",
                    fontsize=10, fontweight="bold", color="#222",
                    fontproperties=_fp())
            # 名称在内侧
            nx = 0.95 * np.cos(rad)
            ny = 0.95 * np.sin(rad)
            ax.text(nx, ny, _shorten(p["name"], 5), ha="center", va="center",
                    fontsize=8, color="white", fontproperties=_fp())
        else:
            # 小切片：标签在外侧
            label_r = 1.38
            lx = label_r * np.cos(rad)
            ly = label_r * np.sin(rad)
            ha = "left" if 90 < (mid % 360) < 270 else "right"
            ax.text(lx, ly, f"{_shorten(p['name'], 5)}  {pct:.1f}%",
                    ha=ha, va="center", fontsize=8.5, color="#222",
                    fontproperties=_fp())

    # 内圈标签（二级，只标 >= 1%）
    for wi, (w, (pi, s_name, s_mv)) in enumerate(zip(inner_wedges, inner_items)):
        t1, t2 = w.theta1, w.theta2
        mid = (t1 + t2) / 2.0
        rad = np.deg2rad(mid)
        pct = s_mv / total * 100
        if pct < 1.0:
            continue
        # 百分比在内圈切片内
        x = 0.55 * np.cos(rad)
        y = 0.55 * np.sin(rad)
        ax.text(x, y, f"{pct:.1f}%", ha="center", va="center",
                fontsize=7.5, color="#333", fontproperties=_fp())
        # 名称在内圈外侧
        nx = 0.70 * np.cos(rad)
        ny = 0.70 * np.sin(rad)
        ha = "left" if 90 < (mid % 360) < 270 else "right"
        ax.text(nx, ny, _shorten(s_name, 5), ha=ha, va="center",
                fontsize=7, color="#444", fontproperties=_fp())

    n_total_sec = sum(len(p["secondary"]) for p in primary_data)
    ax.set_title(
        f"基金持仓双圈  ·  外圈=一级组合（100%）  ·  内圈=二级基金\n"
        f"{n_primary} 个一级组合 / {n_total_sec} 个二级基金  ·  {total:,.0f} CNY",
        fontsize=12, fontweight="bold", pad=18,
    )

    fig.tight_layout()
    out = OUT_DIR / f"fund_donut_{datetime.now().strftime('%Y%m%d_%H%M')}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


# ─────────── main ───────────
def main():
    portfolio, wl_map, primary_of = load_data()
    p1 = render_stock_pie(portfolio, wl_map)
    p2 = render_fund_donut(portfolio, wl_map, primary_of)
    if p1:
        print(f"[charts] 股票饼图: {p1}")
    if p2:
        print(f"[charts] 基金双圈: {p2}")
    return p1, p2


if __name__ == "__main__":
    main()
