"""
回測引擎(backtest.py)
======================
事件驅動、逐週(運價週線格點)模擬單檔股票的訊號操作,產出:
  - 逐筆交易明細(clusters:每次訊號的分批進場、分批出場、報酬)
  - 每日權益曲線(idle 現金不計息,保守)
  - 訊號統計(次數、日期、假訊號率、平均持有、空手天數%、勝率、盈虧比、MDD)

進出場定義(見 config,對應需求文件):
  左側 left:  freight_pctile<=P20 且 pbr_pctile<=P10 → 分4批,每季一批
  右側 right: rising_4w 且 margin_up2 且 rev_yoy_pos → 分3批
  出場:
    錯誤出場 = 進場後、且「尚未出現獲利擴張」前 margin_down2 → 全出
    時間停損 = 左12季/右6季內未出現獲利擴張(margin_up2)→ 全出
    獲利出場 = 已擴張後,turn_down 且 margin_down2 → 分3批出
  假訊號   = 進場後 false_signal_horizon 季內未出現獲利擴張(與出場獨立計)

訊號以「條件 False→True 的上升緣、且當下空手」觸發(同一波谷只算一次訊號)。
"""
from __future__ import annotations

import bisect
import datetime as dt

from features import build_features
from util import to_date

_QW = 13  # 一季約 13 週(運價週線)


def _daily_equity(cluster_events, px_dates, px_close, start, end):
    """依 cluster 進出場事件,建每日權益曲線。events 已含 cash/shares 隨時間變化的節點。"""
    # cluster_events: list of (date, cash, shares) 狀態快照(交易後),由舊到新
    grid = [d for d in px_dates if start <= d <= end]
    eq = []
    idx = 0
    cash, shares = 1.0, 0.0
    flat_days = 0
    for d in grid:
        while idx < len(cluster_events) and cluster_events[idx][0] <= d:
            _, cash, shares = cluster_events[idx]
            idx += 1
        i = bisect.bisect_right(px_dates, d) - 1
        price = px_close[i] if i >= 0 else 0.0
        equity = cash + shares * price
        eq.append((d, equity))
        if shares <= 1e-12:
            flat_days += 1
    idle_pct = flat_days / len(grid) * 100.0 if grid else None
    return eq, idle_pct


def _mdd(equity):
    peak = -1e18
    mdd = 0.0
    for _, v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd  # 負值


def simulate_side(stock_id, features, cfg, side, start, end):
    """單檔、單一進場哲學(side='left'/'right')的完整模擬。"""
    ecfg = cfg["entry"]["left_supply" if side == "left" else "right_demand"]["params"]
    xcfg = cfg["exit"]
    fee = cfg["assumptions"]["fee_bps"] / 10000.0
    tax = cfg["assumptions"]["tax_bps"] / 10000.0
    fs_horizon_q = cfg["assumptions"]["false_signal_horizon_quarters"]

    # 價格序列(供權益曲線與成交價)
    import sources_finmind as F
    px = F.fetch_prices(stock_id)
    px_dates = [r["date"] for r in px]
    px_close = [r["close"] for r in px]

    def price_on(d):
        i = bisect.bisect_right(px_dates, d) - 1
        return px_close[i] if i >= 0 else None

    # 只在區間內的週線格點決策
    feats = [f for f in features if start <= f["date"] <= end and f["close"] is not None]

    if side == "left":
        batches = ecfg["batches"]
        p20 = ecfg["freight_percentile_below"]
        p10 = ecfg["pb_percentile_below"]
        def entry_cond(f):
            return (f["freight_pctile"] is not None and f["freight_pctile"] <= p20
                    and f["pbr_pctile_10y"] is not None and f["pbr_pctile_10y"] <= p10)
        time_stop_q = xcfg["time_stop"]["left_quarters"]
    else:
        batches = ecfg["batches"]
        def entry_cond(f):
            return bool(f["rising_4w"] and f["margin_up2"] and f["rev_yoy_pos"])
        time_stop_q = xcfg["time_stop"]["right_quarters"]

    cash, shares = 1.0, 0.0
    events = []            # (date, cash, shares) 交易後快照
    trades = []            # 每個 cluster 的明細
    prev_cond = False
    n = len(feats)
    i = 0
    while i < n:
        f = feats[i]
        cond = entry_cond(f)
        rising_edge = cond and not prev_cond
        prev_cond = cond
        if rising_edge and shares <= 1e-12:
            # ---- 開一個 cluster ----
            base_cash = cash
            tranche_cash = base_cash / batches
            entry_start = f["date"]
            entry_legs = []
            batches_done = 0
            expanded = False
            expanded_within_fs = False
            exiting = False
            exit_reason = None
            exit_legs = []
            exit_batches_left = 0
            shares_at_exit = 0.0
            next_batch_idx = i
            j = i
            deadline_weeks = time_stop_q * _QW
            fs_weeks = fs_horizon_q * _QW
            while j < n:
                g = feats[j]
                weeks_since = j - i
                price = g["close"]
                # 追蹤是否已出現獲利擴張
                if g["profit_expansion"]:
                    expanded = True
                    if weeks_since <= fs_weeks:
                        expanded_within_fs = True
                # ---- 分批進場(每季一批)----
                if (not exiting) and batches_done < batches and j >= next_batch_idx:
                    spend = min(cash, tranche_cash)
                    if spend > 0:
                        sh = spend * (1 - fee) / price
                        shares += sh
                        cash -= spend
                        entry_legs.append({"date": g["date"], "price": price, "cash": spend})
                        events.append((g["date"], cash, shares))
                        batches_done += 1
                        next_batch_idx = j + _QW
                # ---- 出場判斷(自進場次一週起才監控,避免同根K線買賣)----
                if not exiting and weeks_since >= 1:
                    reason = None
                    if (not expanded) and g["margin_down2"]:
                        reason = "wrong_exit"           # 錯誤出場:未擴張前又轉降
                    elif (not expanded) and weeks_since >= deadline_weeks:
                        reason = "time_stop"            # 時間停損:期限內未擴張
                    elif expanded and g["turn_down"] and g["margin_down2"]:
                        reason = "profit_exit"          # 獲利出場:擴張後由升轉降
                    if reason:
                        exiting = True
                        exit_reason = reason
                        if reason == "profit_exit":
                            exit_batches_left = xcfg["profit_exit"]["batches"]
                            shares_at_exit = shares
                            # 立即賣第一批
                            sell_sh = min(shares, shares_at_exit / exit_batches_left)
                            proceeds = sell_sh * price * (1 - fee - tax)
                            shares -= sell_sh
                            cash += proceeds
                            exit_legs.append({"date": g["date"], "price": price, "reason": reason})
                            events.append((g["date"], cash, shares))
                            exit_batches_left -= 1
                            next_exit_idx = j + _QW
                        else:
                            # 全出
                            proceeds = shares * price * (1 - fee - tax)
                            cash += proceeds
                            shares = 0.0
                            exit_legs.append({"date": g["date"], "price": price, "reason": reason})
                            events.append((g["date"], cash, shares))
                elif exiting and exit_reason == "profit_exit" and exit_batches_left > 0 and j >= next_exit_idx:
                    sell_sh = min(shares, shares_at_exit / xcfg["profit_exit"]["batches"])
                    proceeds = sell_sh * price * (1 - fee - tax)
                    shares -= sell_sh
                    cash += proceeds
                    exit_legs.append({"date": g["date"], "price": price, "reason": "profit_exit_batch"})
                    events.append((g["date"], cash, shares))
                    exit_batches_left -= 1
                    next_exit_idx = j + _QW
                # cluster 結束條件:已開始出場且賣完
                if exiting and shares <= 1e-12:
                    break
                j += 1
            # 若跑到期末仍有部位 → 期末平倉(用最後價格),歸類 open_end
            if shares > 1e-12:
                last = feats[min(j, n - 1)]
                price = last["close"]
                proceeds = shares * price * (1 - fee - tax)
                cash += proceeds
                shares = 0.0
                exit_legs.append({"date": last["date"], "price": price, "reason": "period_end"})
                events.append((last["date"], cash, shares))
                if exit_reason is None:
                    exit_reason = "period_end"
            # 記錄 cluster
            invested = sum(l["cash"] for l in entry_legs)
            exit_value = cash - (base_cash - invested)  # 該 cluster 回收的現金
            ret = (exit_value / invested - 1.0) if invested > 0 else 0.0
            first_date = entry_legs[0]["date"] if entry_legs else entry_start
            last_date = exit_legs[-1]["date"] if exit_legs else first_date
            hold_days = (to_date(last_date) - to_date(first_date)).days
            trades.append({
                "side": side, "signal_date": entry_start,
                "entry_legs": entry_legs, "exit_legs": exit_legs,
                "exit_reason": exit_reason, "return": ret,
                "hold_days": hold_days,
                "expanded": expanded,
                "false_signal": not expanded_within_fs,  # 進場後 fs 季內未擴張
            })
            # 從 cluster 結束後繼續掃描
            i = j + 1
            prev_cond = entry_cond(feats[i]) if i < n else False
            continue
        i += 1

    equity, idle_pct = _daily_equity(events, px_dates, px_close, start, end)
    return {
        "stock_id": stock_id, "side": side,
        "trades": trades, "equity": equity, "idle_pct": idle_pct,
    }
