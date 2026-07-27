"""
策略與回測引擎 (strategy.py)
============================
兩個策略「只差在出場」,進場條件刻意完全相同 —— 這樣績效差異才能歸因到出場規則,
而不是被進場時機混淆(這是本次比較的實驗設計重點)。

  策略 A(純 PE 進出)
      進場:PE < 自身歷史 PE 中位數
      出場:PE > 自身歷史 PE 第 80 百分位

  策略 B(PE 進場 + 基本面證偽出場)
      進場:同 A,且「基本面未處於惡化狀態」(見下方說明)
      出場:滿足任一 →
            (a) PE > 第 80 百分位
            (b) 共識 EPS 連兩季年減(無共識者用實際 EPS 連兩季年減)
            (c) 毛利率連兩季下滑

  ⚠ 關於 B 的進場為何多一個「基本面未惡化」條件(必須誠實交代的實作決定):
     若完全照字面「進場條件與 A 相同」,會發生:基本面觸發賣出的隔天,
     PE 仍然低於中位數(基本面轉壞的股票通常 PE 也低)→ 立刻買回,
     接著又因為同一個基本面條件再賣出……如此反覆。
     **實測結果**(見報告 5.3):字面版 B 不是「退化成 A」,而是變成每年買賣十幾次的
     高頻空轉,交易成本與來回摩擦讓最大回撤反而**比 A 更深**(2454 甚至到 -86.6%)。
     所以本專案:
       - 主結果:B 的基本面惡化是一個**狀態**,惡化期間不進場、且持有就出場。
       - 同時附上「字面版 B(進場完全同 A)」的完整數字,讓讀者自己檢查這個決定合不合理。

執行與成本:
  - T 日收盤產生訊號 → T+1 日收盤成交(EXECUTION_LAG_DAYS),杜絕當日前視。
  - 報酬用還原股利價(close_adj);PE 用未還原價(close_raw)。
  - 同時輸出「未計成本」與「計入成本」兩種績效。
"""

from __future__ import annotations

import params as P

# 出場原因代碼 → 中文說明
EXIT_LABELS = {
    "PE_HIGH": "PE 高於歷史80百分位",
    "EPS_DOWN": "EPS 連兩季年減",
    "GM_DOWN": "毛利率連兩季下滑",
    "OPEN": "期末未平倉(仍持有)",
}


def _entry_signal(row: dict, use_fundamental_filter: bool) -> bool:
    """進場訊號:PE 低於歷史中位數(B 另要求基本面未處於惡化狀態)。"""
    if not row["tradable"] or row["pe"] is None or row["pe_entry_thr"] is None:
        return False
    if row["pe"] >= row["pe_entry_thr"]:
        return False
    if use_fundamental_filter and (row["eps_bad"] or row["gm_bad"]):
        return False
    return True


def _exit_reasons(row: dict, use_fundamental: bool) -> list[str]:
    """回傳所有被觸發的出場原因(可能同時多個)。

    注意:PE 為 None(TTM EPS 轉負 → PE 無意義)時,**純 PE 規則沒有任何訊號**,
    策略 A 只能繼續抱著。這是「純估值規則」的結構性盲點,不是 bug,
    報告會把它當成發現寫出來。
    """
    out: list[str] = []
    if row["pe"] is not None and row["pe_exit_thr"] is not None and row["pe"] > row["pe_exit_thr"]:
        out.append("PE_HIGH")
    if use_fundamental:
        if row["eps_bad"]:
            out.append("EPS_DOWN")
        if row["gm_bad"]:
            out.append("GM_DOWN")
    return out


def _max_drawdown(values: list[float]) -> float:
    """序列自波段高點的最深跌幅(負值,例如 -0.35)。"""
    if len(values) < 2:
        return 0.0
    peak = values[0]
    mdd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = v / peak - 1.0
        if dd < mdd:
            mdd = dd
    return mdd


def run_strategy(
    rows: list[dict],
    *,
    use_fundamental_exit: bool,
    use_fundamental_entry_filter: bool,
    market: str,
) -> dict:
    """跑一個策略,回傳 {trades, equity_curve, dates, ...}。

    equity 以 1.0 起算;空手期間為現金(利息以 0 計,保守)。
    同時算「未計成本」與「計入成本」兩條淨值曲線。
    """
    cost = P.COSTS[market]
    buy_c, sell_c = cost["buy_pct"] / 100.0, cost["sell_pct"] / 100.0
    lag = P.EXECUTION_LAG_DAYS

    holding = False
    eq_gross, eq_net = 1.0, 1.0
    curve_gross: list[float] = []
    curve_net: list[float] = []
    dates: list[str] = []
    trades: list[dict] = []
    pending: tuple[str, list[str], str] | None = None  # (action, reasons, signal_date)
    entry_ctx: dict | None = None
    in_market_days = 0
    first_tradable_idx: int | None = None
    # 組合層級需要的逐日資訊:
    #   active[i] = 該日資金是否投入本標的
    #             = 進場執行前已持有 or 執行後持有
    #   這樣「買入日(承擔買費)」「賣出日(承擔價格變動+賣費)」都會被正確算進組合,
    #   只有真正空手的日子才不佔資金。
    active_flags: list[bool] = []

    for i, row in enumerate(rows):
        holding_before = holding
        # 1) 先按昨→今的價格變動更新淨值(只有昨天收盤時是持有狀態才算)
        if i > 0 and holding:
            ratio = row["close_adj"] / rows[i - 1]["close_adj"]
            eq_gross *= ratio
            eq_net *= ratio
        if holding:
            in_market_days += 1

        # 2) 執行上一個訊號日排定的委託(以今天收盤價成交)
        if pending is not None:
            action, reasons, sig_date = pending
            if action == "BUY" and not holding:
                holding = True
                eq_net *= (1 - buy_c)
                entry_ctx = {
                    "signal_date": sig_date,
                    "entry_date": row["date"],
                    "entry_price_raw": row["close_raw"],
                    "entry_price_adj": row["close_adj"],
                    "entry_pe": row["pe"],
                    "entry_pe_thr": row["pe_entry_thr"],
                    "eq_gross_at_entry": eq_gross,
                    "eq_net_at_entry": eq_net,
                    "idx": i,
                }
            elif action == "SELL" and holding:
                eq_net *= (1 - sell_c)
                holding = False
                seg = [r["close_adj"] for r in rows[entry_ctx["idx"]: i + 1]]
                trades.append({
                    **{k: v for k, v in entry_ctx.items() if k not in ("eq_gross_at_entry", "eq_net_at_entry", "idx")},
                    "exit_date": row["date"],
                    "exit_price_raw": row["close_raw"],
                    "exit_price_adj": row["close_adj"],
                    "exit_pe": row["pe"],
                    "exit_pe_thr": row["pe_exit_thr"],
                    "exit_reasons": reasons,
                    "ret_gross": row["close_adj"] / entry_ctx["entry_price_adj"] - 1.0,
                    "ret_net": (row["close_adj"] / entry_ctx["entry_price_adj"]) * (1 - buy_c) * (1 - sell_c) - 1.0,
                    "holding_days": i - entry_ctx["idx"],
                    "trade_mdd": _max_drawdown(seg),
                    "exit_eps_bad": row["eps_bad"],
                    "exit_gm_bad": row["gm_bad"],
                    "exit_quarter": row["latest_quarter"],
                    "open": False,
                })
                entry_ctx = None
            pending = None

        # 3) 用今天收盤的資料算訊號 → 排定 T+lag 執行
        if row["tradable"] and first_tradable_idx is None:
            first_tradable_idx = i

        if i + lag < len(rows):
            if not holding:
                if _entry_signal(row, use_fundamental_entry_filter):
                    pending = ("BUY", [], row["date"])
            else:
                reasons = _exit_reasons(row, use_fundamental_exit)
                if reasons:
                    pending = ("SELL", reasons, row["date"])

        curve_gross.append(eq_gross)
        curve_net.append(eq_net)
        dates.append(row["date"])
        active_flags.append(bool(holding_before or holding))

    # 期末仍持有 → 記為未平倉交易(不強制賣出,但績效已反映在淨值曲線)
    if holding and entry_ctx is not None:
        last = rows[-1]
        seg = [r["close_adj"] for r in rows[entry_ctx["idx"]:]]
        trades.append({
            **{k: v for k, v in entry_ctx.items() if k not in ("eq_gross_at_entry", "eq_net_at_entry", "idx")},
            "exit_date": last["date"],
            "exit_price_raw": last["close_raw"],
            "exit_price_adj": last["close_adj"],
            "exit_pe": last["pe"],
            "exit_pe_thr": last["pe_exit_thr"],
            "exit_reasons": ["OPEN"],
            "ret_gross": last["close_adj"] / entry_ctx["entry_price_adj"] - 1.0,
            "ret_net": (last["close_adj"] / entry_ctx["entry_price_adj"]) * (1 - buy_c) - 1.0,
            "holding_days": len(rows) - 1 - entry_ctx["idx"],
            "trade_mdd": _max_drawdown(seg),
            "exit_eps_bad": last["eps_bad"],
            "exit_gm_bad": last["gm_bad"],
            "exit_quarter": last["latest_quarter"],
            "open": True,
        })

    return {
        "trades": trades,
        "dates": dates,
        "curve_gross": curve_gross,
        "curve_net": curve_net,
        "active": active_flags,
        "in_market_days": in_market_days,
        "total_days": len(rows),
        "first_tradable_idx": first_tradable_idx,
    }


def run_buy_and_hold(rows: list[dict], start_idx: int, market: str) -> dict:
    """買進持有基準:從第一個可交易日買進,抱到最後(同樣扣一次買賣成本)。"""
    cost = P.COSTS[market]
    buy_c, sell_c = cost["buy_pct"] / 100.0, cost["sell_pct"] / 100.0
    seg = rows[start_idx:]
    base = seg[0]["close_adj"]
    curve_gross = [r["close_adj"] / base for r in seg]
    curve_net = [(r["close_adj"] / base) * (1 - buy_c) * (1 - sell_c) for r in seg]
    return {
        "dates": [r["date"] for r in seg],
        "curve_gross": curve_gross,
        "curve_net": curve_net,
        "active": [True] * len(seg),      # 買進持有:全程投入
        "trades": [],
        "in_market_days": len(seg),
        "total_days": len(seg),
        "first_tradable_idx": 0,
    }
