"""
回測引擎 (backtest.py)
======================
把「觸發事件 + 股價 + 基準(0050)」算成未來報酬與超額報酬,並彙整指標。

核心口徑(全部誠實、可檢查):
  - 進場:公開日「之後」第一個交易日收盤(嚴格 > 公開日 → 杜絕當日前視)。
  - 出場:進場日 + N 個日曆月後,第一個可交易日收盤。
  - 個股報酬 = 出場收盤 / 進場收盤 − 1(未還原權值,股利拖累已於報告揭露)。
  - 基準報酬 = 0050 在「同一組進出場日期」的報酬(as-of 對齊)。
  - 超額報酬 = 個股報酬 − 基準報酬(這是我們真正關心的 alpha)。
  - 最大回撤:持有期間內,個股收盤自波段高點的最深跌幅(負值)。
"""

from __future__ import annotations

import bisect
import statistics
from calendar import monthrange


# ─────────────────────────────────────────────────────────────────────
# 日期 / 價格小工具(ISO 日期字串的字典序 = 時間序,可直接比較與二分)
# ─────────────────────────────────────────────────────────────────────
def add_months(iso: str, months: int) -> str:
    y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    total = (y * 12 + (m - 1)) + months
    ny, nm = total // 12, total % 12 + 1
    nd = min(d, monthrange(ny, nm)[1])
    return f"{ny:04d}-{nm:02d}-{nd:02d}"


class PriceSeries:
    """單一標的的日收盤序列,支援 as-of 查詢與『某日之後第一筆』。"""

    def __init__(self, rows: list[dict]):
        self.dates = [r["date"] for r in rows]
        self.closes = [r["close"] for r in rows]

    def __len__(self):
        return len(self.dates)

    def first_after(self, iso: str) -> tuple[str, float] | None:
        """第一個 date 嚴格大於 iso 的 (date, close)。"""
        i = bisect.bisect_right(self.dates, iso)
        if i >= len(self.dates):
            return None
        return self.dates[i], self.closes[i]

    def first_on_or_after(self, iso: str) -> tuple[str, float] | None:
        i = bisect.bisect_left(self.dates, iso)
        if i >= len(self.dates):
            return None
        return self.dates[i], self.closes[i]

    def asof(self, iso: str) -> tuple[str, float] | None:
        """date <= iso 的最後一筆(基準對齊用)。"""
        i = bisect.bisect_right(self.dates, iso) - 1
        if i < 0:
            return None
        return self.dates[i], self.closes[i]

    def max_drawdown(self, start_iso: str, end_iso: str) -> float | None:
        """[start, end] 區間內,收盤自波段高點的最深跌幅(負值,如 -0.23)。"""
        lo = bisect.bisect_left(self.dates, start_iso)
        hi = bisect.bisect_right(self.dates, end_iso)
        window = self.closes[lo:hi]
        if len(window) < 2:
            return None
        peak = window[0]
        mdd = 0.0
        for c in window:
            if c > peak:
                peak = c
            dd = c / peak - 1.0
            if dd < mdd:
                mdd = dd
        return mdd


# ─────────────────────────────────────────────────────────────────────
# 單筆交易:一個觸發事件 × 一個持有期
# ─────────────────────────────────────────────────────────────────────
def run_trade(
    stock_px: PriceSeries,
    bench_px: PriceSeries,
    available_date: str,
    months: int,
) -> dict | None:
    """回傳單筆交易結果 dict,資料不足回 None。"""
    entry = stock_px.first_after(available_date)  # 嚴格 > 公開日
    if entry is None:
        return None
    entry_date, entry_close = entry
    target_exit = add_months(entry_date, months)
    ex = stock_px.first_on_or_after(target_exit)
    if ex is None:
        return None  # 未來資料不足(還沒到出場日)→ 不計入
    exit_date, exit_close = ex
    if entry_close <= 0:
        return None
    stock_ret = exit_close / entry_close - 1.0

    # 基準 0050:同一組進出場日期 as-of 對齊
    b_in = bench_px.asof(entry_date)
    b_out = bench_px.asof(exit_date)
    if not b_in or not b_out or b_in[1] <= 0:
        return None
    bench_ret = b_out[1] / b_in[1] - 1.0

    mdd = stock_px.max_drawdown(entry_date, exit_date)

    return {
        "months": months,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_close": round(entry_close, 3),
        "exit_close": round(exit_close, 3),
        "stock_ret": round(stock_ret * 100, 2),   # %
        "bench_ret": round(bench_ret * 100, 2),    # %
        "excess_ret": round((stock_ret - bench_ret) * 100, 2),  # %
        "max_drawdown": round(mdd * 100, 2) if mdd is not None else None,  # %
    }


# ─────────────────────────────────────────────────────────────────────
# 指標彙整
# ─────────────────────────────────────────────────────────────────────
def summarize(trades: list[dict]) -> dict:
    """勝率 / 平均超額 / 中位數 / 最大回撤 / 樣本數 等(對一組交易)。"""
    if not trades:
        return {"n": 0}
    ex = [t["excess_ret"] for t in trades]
    st = [t["stock_ret"] for t in trades]
    bm = [t["bench_ret"] for t in trades]
    mdds = [t["max_drawdown"] for t in trades if t["max_drawdown"] is not None]
    wins = sum(1 for e in ex if e > 0)
    return {
        "n": len(trades),
        "win_rate": round(wins / len(trades) * 100, 1),          # 勝率(超額>0 佔比)
        "avg_excess": round(statistics.mean(ex), 2),             # 平均超額報酬
        "median_excess": round(statistics.median(ex), 2),        # 中位數超額
        "avg_stock_ret": round(statistics.mean(st), 2),          # 平均個股絕對報酬
        "avg_bench_ret": round(statistics.mean(bm), 2),          # 平均同期基準報酬
        "avg_max_drawdown": round(statistics.mean(mdds), 2) if mdds else None,  # 平均持有期最大回撤
        "worst_max_drawdown": round(min(mdds), 2) if mdds else None,           # 最糟單筆回撤
        "worst_excess": round(min(ex), 2),                       # 最糟單筆超額
        "best_excess": round(max(ex), 2),                        # 最佳單筆超額
    }
