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
