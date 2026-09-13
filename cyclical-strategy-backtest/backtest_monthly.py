"""
月營收版回測(backtest_monthly.py)—— 階段一補充測試
====================================================
與 backtest.simulate_side 的執行語意完全一致(同樣的交易成本、同樣「進場次一週起才
監控出場」、同樣的權益曲線與空手認定),**只有訊號來源不同**,才能做乾淨對照:

  進場:三檔月營收加總 YoY 連續2個月 > 0(上升緣 = 由負轉正)且 運價方向向上
        → 分 3 批,每月一批
  出場:加總月營收 YoY 連續2個月 < 0 → 分 3 批出
  錯誤出場:進場後 3 個月內月營收 YoY 再度轉負 → 全出
  (需求文件未定義時間停損 → 不加,避免多一個參數)

假訊號定義**沿用季報版**(進場後 2 季內未出現「毛利率連2季升」),
否則兩版的假訊號率不可比。
"""
from __future__ import annotations

import bisect

from backtest import _daily_equity
from util import to_date

_QW = 13   # 一季約 13 週
_MW = 4.33 # 一個月約 4.33 週


def simulate_monthly(stock_id, features, cfg, start, end):
    p = cfg["entry"]["monthly_revenue"]["params"]
    xm = cfg["exit_monthly"]
    fee = cfg["assumptions"]["fee_bps"] / 10000.0
    tax = cfg["assumptions"]["tax_bps"] / 10000.0
    fs_weeks = cfg["assumptions"]["false_signal_horizon_quarters"] * _QW

    batches = p["batches"]
    gap = p["batch_gap_weeks"]
    exit_batches_n = xm["normal_exit"]["batches"]
    wrong_weeks = round(xm["wrong_exit"]["within_months"] * _MW)

    import sources_finmind as F
    px = F.fetch_prices(stock_id)
    px_dates = [r["date"] for r in px]
    px_close = [r["close"] for r in px]

    feats = [f for f in features if start <= f["date"] <= end and f["close"] is not None]

    def entry_cond(f):
        return bool(f.get("agg_yoy_pos2") and f.get("freight_up_4w"))

    cash, shares = 1.0, 0.0
    events, trades = [], []
    prev_cond = False
    n = len(feats)
    i = 0
    while i < n:
        f = feats[i]
        cond = entry_cond(f)
        rising_edge = cond and not prev_cond
        prev_cond = cond
        if rising_edge and shares <= 1e-12:
            base_cash = cash
            tranche = base_cash / batches
            entry_start = f["date"]
            entry_legs, exit_legs = [], []
            batches_done = 0
            next_batch_idx = i
            expanded = False
            expanded_within_fs = False
            exiting = False
            exit_reason = None
            exit_batches_left = 0
            shares_at_exit = 0.0
            next_exit_idx = 0
            j = i
            while j < n:
                g = feats[j]
                weeks_since = j - i
                price = g["close"]
                if g.get("profit_expansion"):
                    expanded = True
                    if weeks_since <= fs_weeks:
                        expanded_within_fs = True
                # 分批進場(每月一批)
                if (not exiting) and batches_done < batches and j >= next_batch_idx:
                    spend = min(cash, tranche)
                    if spend > 0:
                        sh = spend * (1 - fee) / price
                        shares += sh
                        cash -= spend
                        entry_legs.append({"date": g["date"], "price": price, "cash": spend})
                        events.append((g["date"], cash, shares))
                        batches_done += 1
                        next_batch_idx = j + gap
                # 出場(自進場次一週起監控)
                if (not exiting) and weeks_since >= 1:
                    reason = None
                    if weeks_since <= wrong_weeks and g.get("agg_yoy_latest") is not None \
                            and g["agg_yoy_latest"] < 0:
                        reason = "wrong_exit"          # 進場後3個月內月營收YoY再度轉負
                    elif g.get("agg_yoy_neg2"):
                        reason = "normal_exit"         # 連2個月轉負 → 分3批出
                    if reason:
                        exiting = True
                        exit_reason = reason
                        if reason == "normal_exit":
                            exit_batches_left = exit_batches_n
                            shares_at_exit = shares
                            sell = min(shares, shares_at_exit / exit_batches_n)
                            cash += sell * price * (1 - fee - tax)
                            shares -= sell
                            exit_legs.append({"date": g["date"], "price": price, "reason": reason})
                            events.append((g["date"], cash, shares))
                            exit_batches_left -= 1
                            next_exit_idx = j + gap
                        else:
                            cash += shares * price * (1 - fee - tax)
                            shares = 0.0
                            exit_legs.append({"date": g["date"], "price": price, "reason": reason})
                            events.append((g["date"], cash, shares))
                elif exiting and exit_reason == "normal_exit" and exit_batches_left > 0 \
                        and j >= next_exit_idx:
                    sell = min(shares, shares_at_exit / exit_batches_n)
                    cash += sell * price * (1 - fee - tax)
                    shares -= sell
                    exit_legs.append({"date": g["date"], "price": price, "reason": "normal_exit_batch"})
                    events.append((g["date"], cash, shares))
                    exit_batches_left -= 1
                    next_exit_idx = j + gap
                if exiting and shares <= 1e-12:
                    break
                j += 1
            if shares > 1e-12:
                last = feats[min(j, n - 1)]
                cash += shares * last["close"] * (1 - fee - tax)
                shares = 0.0
                exit_legs.append({"date": last["date"], "price": last["close"], "reason": "period_end"})
                events.append((last["date"], cash, shares))
                if exit_reason is None:
                    exit_reason = "period_end"
            invested = sum(l["cash"] for l in entry_legs)
            exit_value = cash - (base_cash - invested)
            ret = (exit_value / invested - 1.0) if invested > 0 else 0.0
            first_date = entry_legs[0]["date"] if entry_legs else entry_start
            last_date = exit_legs[-1]["date"] if exit_legs else first_date
            trades.append({
                "side": "monthly", "signal_date": entry_start,
                "entry_legs": entry_legs, "exit_legs": exit_legs,
                "exit_reason": exit_reason, "return": ret,
                "hold_days": (to_date(last_date) - to_date(first_date)).days,
                "expanded": expanded,
                "false_signal": not expanded_within_fs,
            })
            i = j + 1
            prev_cond = entry_cond(feats[i]) if i < n else False
            continue
        i += 1

    equity, idle_pct = _daily_equity(events, px_dates, px_close, start, end)
    return {"stock_id": stock_id, "side": "monthly",
            "trades": trades, "equity": equity, "idle_pct": idle_pct}
