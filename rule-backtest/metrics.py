"""
績效指標 (metrics.py)
=====================
全部由淨值曲線與交易明細直接算出,定義寫在函式裡,任何人都能複核:

  總報酬      = 期末淨值 / 期初淨值 − 1
  年化報酬    = 期末淨值^(1/年數) − 1   (年數用實際日曆天 / 365.25)
  最大回撤    = 淨值曲線自波段高點的最深跌幅(**本次比較的關鍵指標**)
                注意:算的是「策略淨值」的回撤,不是股價回撤 —— 空手期間不會產生回撤,
                這正是出場規則該被檢驗的地方。
  勝率        = 獲利交易數 / 總交易數(含未平倉那筆,會標注)
  在市天數比  = 持有天數 / 總交易日(用來看策略是不是靠「少待在市場」躲掉風險)
"""

from __future__ import annotations

from datetime import date


def _years_between(d1: str, d2: str) -> float:
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    return max((b - a).days / 365.25, 1e-9)


def max_drawdown(curve: list[float]) -> tuple[float, int, int]:
    """回傳 (最大回撤, 高點索引, 谷底索引)。回撤為負值。"""
    if len(curve) < 2:
        return 0.0, 0, 0
    peak = curve[0]
    peak_i = 0
    mdd, mdd_peak_i, mdd_trough_i = 0.0, 0, 0
    for i, v in enumerate(curve):
        if v > peak:
            peak, peak_i = v, i
        dd = v / peak - 1.0
        if dd < mdd:
            mdd, mdd_peak_i, mdd_trough_i = dd, peak_i, i
    return mdd, mdd_peak_i, mdd_trough_i


def summarize(result: dict, *, use_net: bool = True) -> dict:
    """把 run_strategy() 的輸出整理成報告用的指標字典。"""
    curve = result["curve_net"] if use_net else result["curve_gross"]
    dates = result["dates"]
    if not curve:
        return {"n_trades": 0}

    # 只從「第一個可交易日」起算績效(暖身期不算,否則會把空手期灌進年化)
    s = result.get("first_tradable_idx") or 0
    curve = curve[s:]
    dates = dates[s:]
    if len(curve) < 2:
        return {"n_trades": 0}
    # 在市時間也要用「回測期間」當分母,不能用完整股價歷史(否則會被暖身期稀釋)
    period_days = len(curve)

    base = curve[0]
    norm = [c / base for c in curve]
    total_ret = norm[-1] - 1.0
    yrs = _years_between(dates[0], dates[-1])
    cagr = norm[-1] ** (1.0 / yrs) - 1.0
    mdd, pi, ti = max_drawdown(norm)

    trades = result["trades"]
    key = "ret_net" if use_net else "ret_gross"
    # 勝率只算「已平倉」交易:期末仍持有的部位還沒有最終損益,算進去會失真
    closed = [t for t in trades if not t.get("open")]
    rets_closed = [t[key] for t in closed]
    rets_all = [t[key] for t in trades]
    wins = [r for r in rets_closed if r > 0]

    return {
        "start_date": dates[0],
        "end_date": dates[-1],
        "years": round(yrs, 2),
        "total_return": total_ret,
        "cagr": cagr,
        "max_drawdown": mdd,
        "mdd_peak_date": dates[pi] if dates else None,
        "mdd_trough_date": dates[ti] if dates else None,
        "n_trades": len(trades),
        "n_closed": len(closed),
        "n_open": len(trades) - len(closed),
        "win_rate": (len(wins) / len(rets_closed)) if rets_closed else None,
        "avg_trade_ret": (sum(rets_closed) / len(rets_closed)) if rets_closed else None,
        "best_trade": max(rets_all) if rets_all else None,
        "worst_trade": min(rets_all) if rets_all else None,
        "time_in_market": result["in_market_days"] / period_days if period_days else 0,
        "final_equity": norm[-1],
    }


def worst_trade_detail(result: dict, use_net: bool = True) -> dict | None:
    """找出最大單筆虧損那筆交易(報告要描述它發生在什麼情境)。"""
    trades = result["trades"]
    if not trades:
        return None
    key = "ret_net" if use_net else "ret_gross"
    return min(trades, key=lambda t: t[key])
