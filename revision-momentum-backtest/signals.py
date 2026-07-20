"""
訊號定義 (signals.py)
=====================
把「盈餘/營收被大幅上修」用兩個**代理訊號**操作化(FinMind 免費版無分析師共識,
故用實際數字的「突然轉強」當上修代理)。兩個訊號都參數化、都嚴格處理「資訊公開日」
以避免前視偏誤(look-ahead bias):

代理一(月營收動能上修)REV_ACCEL
    加速度 = 本月營收YoY − 上月營收YoY。 >= REV_ACCEL_PP(參數①,預設20pp)
    且 本月YoY > 0(真的在成長)。
    公開日 = 該月營收「次月10日」(台股月營收法定公告期限)。

代理二(單季盈餘上修)EPS_SURGE
    單季EPS YoY = 本季EPS / 去年同季EPS − 1。 >= EPS_YOY_PCT(參數②,預設50%)
    且 去年同季EPS > 0,且 本季YoY「超前四季趨勢」(> 前四季YoY平均)。
    公開日 = 各季財報法定申報期限(Q1→5/15, Q2→8/14, Q3→11/14, Q4→隔年3/31)。

輸出:每個觸發事件 = {stock_id, kind, period, available_date, metric...}
「進場」一律取 available_date 之後「第一個交易日收盤」(嚴格 > 公開日,杜絕當日前視)。
"""

from __future__ import annotations

from datetime import date

import params as P


# ─────────────────────────────────────────────────────────────────────
# 公開日(資訊何時可被交易)——避免前視偏誤的關鍵
# ─────────────────────────────────────────────────────────────────────
def revenue_available_date(next_month_first: str) -> str:
    """月營收公開日 = FinMind 'date'(次月一日)當月的 10 日。

    例:1月營收 → date='YYYY-02-01' → 公開 'YYYY-02-10'(法定公告期限)。
    """
    return f"{next_month_first[:8]}10"


def eps_available_date(quarter_end: str) -> str:
    """季 EPS 公開日 = 台股財報法定申報期限(保守,不會早於實際公布)。

    Q1(3/31)→5/15;Q2(6/30)→8/14;Q3(9/30)→11/14;Q4(12/31)→隔年 3/31。
    """
    y = int(quarter_end[:4])
    m = int(quarter_end[5:7])
    if m == 3:
        return f"{y}-05-15"
    if m == 6:
        return f"{y}-08-14"
    if m == 9:
        return f"{y}-11-14"
    return f"{y + 1}-03-31"  # Q4 / 年報


def _in_study_window(available: str) -> bool:
    return P.STUDY_START <= available <= P.STUDY_END


# ─────────────────────────────────────────────────────────────────────
# 代理一:月營收 YoY 加速
# ─────────────────────────────────────────────────────────────────────
def detect_revenue_signals(stock_id: str, rev_rows: list[dict], accel_pp: float) -> list[dict]:
    """rev_rows = [{ry, rm, revenue, date}] 由舊到新。回傳觸發事件清單。"""
    by_key = {(r["ry"], r["rm"]): r for r in rev_rows}
    out: list[dict] = []
    for r in rev_rows:
        y, mo = r["ry"], r["rm"]
        # 本月 YoY
        prev_year = by_key.get((y - 1, mo))
        if not prev_year or prev_year["revenue"] <= 0:
            continue
        yoy_now = r["revenue"] / prev_year["revenue"] - 1.0
        # 上月 YoY(上一個日曆月)
        pm_y, pm_mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
        prev_month = by_key.get((pm_y, pm_mo))
        prev_month_yago = by_key.get((pm_y - 1, pm_mo))
        if not prev_month or not prev_month_yago or prev_month_yago["revenue"] <= 0:
            continue
        yoy_prev = prev_month["revenue"] / prev_month_yago["revenue"] - 1.0
        accel = (yoy_now - yoy_prev) * 100.0  # 百分點
        # 觸發條件:加速 >= 門檻 且 本月 YoY > 0
        if accel >= accel_pp and yoy_now > 0:
            available = revenue_available_date(r["date"])
            if not _in_study_window(available):
                continue
            out.append({
                "stock_id": stock_id,
                "kind": "REV_ACCEL",
                "period": f"{y}-{mo:02d}",
                "available_date": available,
                "metric": round(accel, 1),          # 加速幅度(pp)
                "yoy_now": round(yoy_now * 100, 1),  # 本月YoY(%)
            })
    return out


# ─────────────────────────────────────────────────────────────────────
# 代理二:單季 EPS YoY 大增且超前趨勢
# ─────────────────────────────────────────────────────────────────────
def detect_eps_signals(stock_id: str, eps_rows: list[dict], yoy_pct: float) -> list[dict]:
    """eps_rows = [{date, eps}] 由舊到新(季末日期)。回傳觸發事件清單。"""
    n = len(eps_rows)
    eps = [r["eps"] for r in eps_rows]
    out: list[dict] = []

    def yoy_at(i: int):
        """第 i 季的單季 EPS YoY(去年同季 EPS 需 > 0,否則回 None)。"""
        if i - 4 < 0:
            return None
        base = eps[i - 4]
        if base <= 0:
            return None
        return eps[i] / base - 1.0

    for i in range(n):
        yoy_now = yoy_at(i)
        if yoy_now is None:
            continue
        # 超前四季趨勢:前四季 YoY 的平均(可得幾個算幾個)
        prior = [yoy_at(j) for j in range(i - 4, i)]
        prior = [v for v in prior if v is not None]
        if not prior:
            continue
        prior_avg = sum(prior) / len(prior)
        # 觸發:YoY >= 門檻 且 超越前四季趨勢
        if yoy_now * 100.0 >= yoy_pct and yoy_now > prior_avg:
            available = eps_available_date(eps_rows[i]["date"])
            if not _in_study_window(available):
                continue
            qend = eps_rows[i]["date"]
            q = (int(qend[5:7]) - 1) // 3 + 1
            out.append({
                "stock_id": stock_id,
                "kind": "EPS_SURGE",
                "period": f"{qend[:4]}Q{q}",
                "available_date": available,
                "metric": round(yoy_now * 100, 1),      # 單季EPS YoY(%)
                "yoy_now": round(yoy_now * 100, 1),
                "trend_gap": round((yoy_now - prior_avg) * 100, 1),  # 超前趨勢幅度(pp)
            })
    return out
