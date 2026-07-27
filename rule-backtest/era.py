"""
時代切段 + 組合層級 (era.py)
============================
回答一個問題:**A/B/買進持有的結論,是不是只是「成長股大時代」的產物?**

三件事:

1. `slice_era()` —— 把「同一次回測」的淨值曲線切成時代分段分別結算。
   ★ 關鍵:**不是各時代分開重跑**。分開重跑會讓每段重新暖身、重新計算 PE 分位數,
     等於偷偷讓策略在後段「知道前段的分位數已經穩定」,而且切斷了跨段的持倉狀態。
     這裡的作法是:整段照常跑一次,再把淨值曲線依日期切開、各自從 1.0 重新歸一化。
     跨越切點的持倉會自然延續 —— 這才是真實情況。

2. `portfolio_equal_weight()` —— 等權組合。
   每日把資金等分給「當日有部位」的標的;當日全部空手就是現金(報酬 0)。
   組合日報酬 = 當日有部位標的的日報酬**等權平均**(等同每日再平衡)。
   用各標的的 `curve_net`(已含交易成本)推導日報酬,`active` 決定是否佔用資金,
   其中 active 已把「買入日(付買費)」與「賣出日(價格變動+賣費)」都算進去。

3. `summarize_curve()` —— 對任何一條淨值曲線算年化/最大回撤/總報酬。

⚠ 限制(報告會照抄):
  - 等權組合假設可無摩擦地每日再平衡,實務上會有額外成本與零股問題,**是樂觀假設**。
  - 空手期間以 0% 計息(保守),實務上現金有利息,對「常空手的策略」不利。
"""

from __future__ import annotations

from datetime import date

import params as P


# ─────────────────────────────────────────────────────────────────────
# 基礎:任意淨值曲線 → 指標
# ─────────────────────────────────────────────────────────────────────
def _years_between(d1: str, d2: str) -> float:
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    return max((b - a).days / 365.25, 1e-9)


def _max_drawdown(curve: list[float]) -> float:
    if len(curve) < 2:
        return 0.0
    peak, mdd = curve[0], 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = v / peak - 1.0
        if dd < mdd:
            mdd = dd
    return mdd


def summarize_curve(dates: list[str], curve: list[float],
                    active: list[bool] | None = None) -> dict:
    """淨值曲線 → {總報酬, 年化, 最大回撤, 在市比例}。曲線會先歸一化為 1.0 起算。"""
    if len(curve) < 2:
        return {"n_days": len(curve), "insufficient": True}
    base = curve[0]
    if base <= 0:
        return {"n_days": len(curve), "insufficient": True}
    norm = [c / base for c in curve]
    yrs = _years_between(dates[0], dates[-1])
    return {
        "start_date": dates[0],
        "end_date": dates[-1],
        "years": round(yrs, 2),
        "n_days": len(curve),
        "total_return": norm[-1] - 1.0,
        "cagr": norm[-1] ** (1.0 / yrs) - 1.0,
        "max_drawdown": _max_drawdown(norm),
        "final_equity": norm[-1],
        "time_in_market": (sum(active) / len(active)) if active else None,
        "insufficient": False,
    }


# ─────────────────────────────────────────────────────────────────────
# 1) 時代切段
# ─────────────────────────────────────────────────────────────────────
def slice_era(result: dict, era: dict, *, use_net: bool = True) -> dict:
    """把一次回測的結果切出某個時代區間並重新結算。

    只取「該標的實際可交易之後」與「時代區間」的交集;
    交集不足 ERA_MIN_DAYS 交易日就標 insufficient,**不硬算年化**
    (短區間年化會爆炸性放大,是常見的誤導來源)。
    """
    dates = result["dates"]
    curve = result["curve_net"] if use_net else result["curve_gross"]
    active = result.get("active")
    s0 = result.get("first_tradable_idx") or 0

    idx = [i for i in range(s0, len(dates)) if era["start"] <= dates[i] <= era["end"]]
    if len(idx) < P.ERA_MIN_DAYS:
        return {"era": era["key"], "era_name": era["name"], "insufficient": True,
                "n_days": len(idx)}
    lo, hi = idx[0], idx[-1] + 1
    out = summarize_curve(dates[lo:hi], curve[lo:hi],
                          active[lo:hi] if active else None)
    out.update({"era": era["key"], "era_name": era["name"]})

    # 該時代內「完成」的交易(以出場日落在區間內為準)
    trades = [t for t in result.get("trades", [])
              if era["start"] <= str(t.get("exit_date", "")) <= era["end"]]
    key = "ret_net" if use_net else "ret_gross"
    closed = [t for t in trades if not t.get("open")]
    wins = [t for t in closed if t[key] > 0]
    out.update({
        "n_trades": len(trades),
        "n_closed": len(closed),
        "win_rate": (len(wins) / len(closed)) if closed else None,
    })
    return out


def slice_all_eras(result: dict, *, use_net: bool = True) -> dict:
    return {e["key"]: slice_era(result, e, use_net=use_net) for e in P.ERAS}


# ─────────────────────────────────────────────────────────────────────
# 2) 等權組合
# ─────────────────────────────────────────────────────────────────────
def _daily_returns(dates: list[str], curve: list[float], active: list[bool],
                   start_idx: int) -> dict[str, tuple[float, bool]]:
    """{date: (當日報酬, 當日是否佔用資金)};從 start_idx 之後才算。"""
    out: dict[str, tuple[float, bool]] = {}
    for i in range(max(start_idx, 1), len(dates)):
        prev = curve[i - 1]
        r = (curve[i] / prev - 1.0) if prev > 0 else 0.0
        out[dates[i]] = (r, bool(active[i]) if active else False)
    return out


def portfolio_equal_weight(per_stock: list[dict]) -> dict:
    """等權組合回測。

    per_stock: [{code, name, group, dates, curve, active, first_tradable_idx}]
    回傳組合的日期序列 + 淨值曲線 + 每日持有檔數。
    """
    if not per_stock:
        return {"dates": [], "curve": [], "insufficient": True}

    maps = {}
    for s in per_stock:
        maps[s["code"]] = _daily_returns(s["dates"], s["curve"], s.get("active") or [],
                                         s.get("first_tradable_idx") or 0)

    all_dates = sorted({d for m in maps.values() for d in m})
    if not all_dates:
        return {"dates": [], "curve": [], "insufficient": True}

    eq = 1.0
    curve, held_counts, avail_counts = [], [], []
    for d in all_dates:
        rets = []
        avail = 0
        for m in maps.values():
            if d in m:
                avail += 1
                r, act = m[d]
                if act:
                    rets.append(r)
        if rets:                                   # 等權分配給當日有部位者
            eq *= (1.0 + sum(rets) / len(rets))
        # 全部空手 → 純現金,淨值不變(0% 計息,保守)
        curve.append(eq)
        held_counts.append(len(rets))
        avail_counts.append(avail)

    return {
        "dates": all_dates,
        "curve": curve,
        "held_counts": held_counts,
        "avail_counts": avail_counts,
        "avg_held": sum(held_counts) / len(held_counts) if held_counts else 0,
        "max_avail": max(avail_counts) if avail_counts else 0,
        "insufficient": False,
    }


def portfolio_summary(pf: dict, era: dict | None = None) -> dict:
    """組合曲線 → 指標;可限定時代區間。"""
    if pf.get("insufficient"):
        return {"insufficient": True}
    dates, curve = pf["dates"], pf["curve"]
    held = pf.get("held_counts") or []
    if era is not None:
        idx = [i for i in range(len(dates)) if era["start"] <= dates[i] <= era["end"]]
        if len(idx) < P.ERA_MIN_DAYS:
            return {"insufficient": True, "n_days": len(idx)}
        lo, hi = idx[0], idx[-1] + 1
        dates, curve, held = dates[lo:hi], curve[lo:hi], held[lo:hi]
    out = summarize_curve(dates, curve, [h > 0 for h in held] if held else None)
    out["avg_held"] = (sum(held) / len(held)) if held else 0
    return out
