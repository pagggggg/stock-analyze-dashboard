"""
估值旗標層 (valuation_flag.py)
=============================
只加旗標、不淘汰任何標的。用「個股自己的近N年每日本益比分布」給三段旗標:

  🟢 合理偏低:PEG < green_peg_below  且  前瞻PE < 該股近N年PE中位數
  🔴 高估值警戒:前瞻PE > 該股近N年PE的90百分位,或 PEG > red_peg_above,或 前瞻PE > red_pe_above
  🟡 一般:其餘
  ⚪ 估值資料不足:沒有前瞻PE(共識缺)

★ 百分位一律用「個股自己的歷史」,不用全市場平均——不同產業 PE 水準天生不同。
"""

from __future__ import annotations

from datetime import date

from .river import _percentile

FLAG = {
    "green": ("🟢", "合理偏低"),
    "yellow": ("🟡", "一般"),
    "red": ("🔴", "高估值警戒"),
    "na": ("⚪", "估值資料不足"),
}

# 紅旗必附警語(逐字,依需求)
RED_WARNING = (
    "此標的基本面通過篩選,但現價已隱含極高成長預期。買進前請書面回答:"
    "①你相信的成長劇本是什麼 ②什麼證據會證明它失敗 ③若劇本完全兌現,現價對應PE是多少。"
)


def pe_history_stats(pe_series: list, forward_pe: float | None,
                     years: int = 5, min_days: int = 60) -> dict | None:
    """由 [(date, pe)] 每日本益比序列,算近 N 年的中位數 / 90百分位 / 前瞻PE 所在百分位。"""
    cut = date.today().year - years + 1
    vals = sorted(pe for d, pe in pe_series if int(d[:4]) >= cut and pe and pe > 0)
    if len(vals) < min_days:                      # 近N年不足就退而用全部可得
        vals = sorted(pe for _, pe in pe_series if pe and pe > 0)
        if len(vals) < min_days:
            return None
    median = round(_percentile(vals, 0.5), 1)
    p90 = round(_percentile(vals, 0.9), 1)
    pct = None
    if forward_pe and forward_pe > 0:
        pct = round(sum(1 for v in vals if v < forward_pe) / len(vals) * 100, 0)
    return {"median": median, "p90": p90, "percentile": pct, "years": years, "n": len(vals)}


def historical_peg(annual: dict, price: float | None, years: int = 5) -> dict | None:
    """『歷史PEG』——完全不依賴分析師共識,只用實際財報 EPS 與現價。

    ★ 與前瞻PEG 口徑不同,兩者**不可直接比較、不可互相取代**:
        前瞻PEG = 現價÷今年共識EPS  ÷  共識預估成長率   (看未來,需分析師覆蓋)
        歷史PEG = 現價÷最新年度EPS  ÷  實際EPS年化成長率 (看過去,人人都有)
      台股多數個股(金融/傳產尤甚)沒有分析師覆蓋,前瞻PEG 永遠是空的;
      歷史PEG 至少提供一個『用已發生事實』算出的估值/成長對照。

    限制(誠實揭露):
      - 過去成長不代表未來,循環股在景氣高點會算出很漂亮的低 PEG,**最容易騙人**。
      - 起始年 EPS ≤ 0 無法算年化成長 → 回 None(不硬湊、不補值)。
      - 衰退(成長率 ≤ 0)不給 PEG(負 PEG 無意義),但仍回報成長率供判讀。
    """
    if not annual or not price or price <= 0:
        return None
    ys = sorted(int(y) for y, a in annual.items() if (a or {}).get("eps") is not None)
    if len(ys) < 2:
        return None
    window = ys[-years:] if len(ys) >= years else ys
    y0, y1 = window[0], window[-1]
    e0 = annual[str(y0)]["eps"]
    e1 = annual[str(y1)]["eps"]
    if e1 is None or e1 <= 0:                 # 最新年度虧損 → 本益比無意義
        return None
    trailing_pe = price / e1
    n = y1 - y0
    cagr = None
    if e0 is not None and e0 > 0 and n >= 1:
        cagr = ((e1 / e0) ** (1 / n) - 1) * 100
    peg = (trailing_pe / cagr) if (cagr and cagr > 0) else None
    return {
        "trailing_pe": round(trailing_pe, 1),
        "eps_cagr": round(cagr, 1) if cagr is not None else None,
        "peg": round(peg, 2) if peg is not None else None,
        "eps_last": e1, "year_from": y0, "year_to": y1, "span": n,
    }


def pe_series_us(hist, annual_eps: dict, years: int = 5) -> list:
    """美股:用『每日收盤 ÷ 最近會計年度 EPS(step)』近似每日本益比序列。

    yfinance 免費只有年度 EPS,故以年度 EPS 當 TTM 近似(粗略,僅供估值分布參考)。
    """
    out: list = []
    if hist is None or not len(hist) or not annual_eps:
        return out
    yrs = sorted(int(y) for y in annual_eps)
    cut_year = date.today().year - years
    for ts, row in hist.iterrows():
        y = ts.year
        if y < cut_year:
            continue
        use = [fy for fy in yrs if fy <= y] or [fy for fy in yrs if fy > y]
        if not use:
            continue
        try:
            eps = float(annual_eps[str(use[-1])])
            close = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue
        if eps and eps > 0 and close > 0:
            out.append((ts.strftime("%Y-%m-%d"), close / eps))
    return out


def compute_flag(forward_pe: float | None, peg: float | None,
                 pe_median: float | None, pe_p90: float | None, cfg: dict) -> str:
    """回傳 green / yellow / red / na。"""
    vf = cfg.get("valuation_flag", {})
    if forward_pe is None:
        return "na"
    # 🔴 高估值警戒(任一成立)
    if ((vf.get("red_pe_above_p90", True) and pe_p90 is not None and forward_pe > pe_p90)
            or (peg is not None and peg > vf.get("red_peg_above", 2.0))
            or (forward_pe > vf.get("red_pe_above", 60))):
        return "red"
    # 🟢 合理偏低(兩者皆需成立)
    if (peg is not None and peg < vf.get("green_peg_below", 1.0)
            and pe_median is not None and forward_pe < pe_median):
        return "green"
    return "yellow"
