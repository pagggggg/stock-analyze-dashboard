"""
指標與對照組(metrics.py)
=========================
從權益曲線 + 逐筆交易算出所有需求指標;並提供對照組:
  0050 買進持有、個股買進持有、台積電同策略(過擬合檢測)、隨機日進場基準。
"""
from __future__ import annotations

import bisect
import random

import sources_finmind as F
from util import to_date


def _ann_return(equity):
    if len(equity) < 2:
        return None
    d0, v0 = equity[0]
    d1, v1 = equity[-1]
    days = (to_date(d1) - to_date(d0)).days
    if days <= 0 or v0 <= 0 or v1 <= 0:
        return None
    return (v1 / v0) ** (365.0 / days) - 1.0


def _total_return(equity):
    if len(equity) < 2:
        return None
    return equity[-1][1] / equity[0][1] - 1.0


def _mdd(equity):
    peak = -1e18
    mdd = 0.0
    for _, v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def trade_stats(trades, cfg):
    n = len(trades)
    if n == 0:
        return {"n_signals": 0, "false_signal_ratio": None, "win_rate": None,
                "profit_factor": None, "avg_hold_days": None,
                "exit_reasons": {}, "signal_dates": []}
    false_n = sum(1 for t in trades if t["false_signal"])
    wins = [t["return"] for t in trades if t["return"] > 0]
    losses = [t["return"] for t in trades if t["return"] <= 0]
    # 盈虧比 = 總獲利 / 總虧損(古典 profit factor;避免用平均值在虧損極小時爆表)
    sum_win = sum(wins)
    sum_loss = abs(sum(losses))
    pf = (sum_win / sum_loss) if sum_loss > 1e-9 else None
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    return {
        "n_signals": n,
        "false_signal_ratio": false_n / n,
        "win_rate": len(wins) / n,
        "profit_factor": pf,
        "avg_hold_days": sum(t["hold_days"] for t in trades) / n,
        "avg_return": sum(t["return"] for t in trades) / n,
        "exit_reasons": reasons,
        "signal_dates": [t["signal_date"] for t in trades],
    }


def equity_stats(equity, idle_pct=None):
    return {
        "total_return": _total_return(equity),
        "annualized": _ann_return(equity),
        "mdd": _mdd(equity),
        "idle_pct": idle_pct,
    }


# ---------------------------------------------------------------------
# 對照組
# ---------------------------------------------------------------------
def buy_hold_equity(stock_id, start, end):
    """買進持有:start 首個交易日買、end 賣。回傳每日權益曲線。"""
    px = F.fetch_prices(stock_id)
    dates = [r["date"] for r in px]
    close = [r["close"] for r in px]
    i0 = bisect.bisect_left(dates, start)
    if i0 >= len(dates):
        return []
    p0 = close[i0]
    eq = []
    for d, c in zip(dates, close):
        if d < dates[i0] or d > end:
            continue
        eq.append((d, c / p0))
    return eq


def random_baseline(stock_id, start, end, avg_hold_days, runs, seed):
    """隨機日進場基準:隨機挑進場日,持有 avg_hold_days 後賣出,回傳年化報酬分佈統計。

    代表『同期隨機擇時』的期望;與策略的擇時能力對照。
    """
    px = F.fetch_prices(stock_id)
    dates = [r["date"] for r in px]
    close = [r["close"] for r in px]
    lo = bisect.bisect_left(dates, start)
    hi = bisect.bisect_right(dates, end)
    if hi - lo < 5:
        return None
    rng = random.Random(seed)
    hold = max(int(avg_hold_days or 180), 5)
    rets = []
    for _ in range(runs):
        ei = rng.randrange(lo, hi)
        d0 = dates[ei]
        dsell = (to_date(d0) + __import__("datetime").timedelta(days=hold)).isoformat()
        si = min(bisect.bisect_left(dates, dsell), len(dates) - 1)
        if si <= ei:
            continue
        r = close[si] / close[ei] - 1.0
        held = (to_date(dates[si]) - to_date(d0)).days
        if held > 0:
            ann = (1 + r) ** (365.0 / held) - 1.0
            rets.append(ann)
    if not rets:
        return None
    rets.sort()
    n = len(rets)
    return {
        "runs": n,
        "mean_annualized": sum(rets) / n,
        "median_annualized": rets[n // 2],
        "p25": rets[n // 4],
        "p75": rets[3 * n // 4],
        "hold_days_used": hold,
    }


def combine_equity(equities):
    """等權合併多檔每日權益曲線(對齊交集日期);回傳合併曲線。"""
    if not equities:
        return []
    date_sets = [set(d for d, _ in eq) for eq in equities]
    common = sorted(set.intersection(*date_sets)) if date_sets else []
    maps = [dict(eq) for eq in equities]
    out = []
    for d in common:
        out.append((d, sum(m[d] for m in maps) / len(maps)))
    return out
