"""
期望值 + 安全邊際 進場法 (expected_value.py)
============================================
每季末重算一次,只用**當時已公布**的資訊:

  1. 近 3 年實際 EPS 的 CAGR(用 TTM EPS:當時已公布的近四季合計)
  2. 外推 3 年後 EPS,三情境:悲觀 = 成長率打對折、基準 = 維持、樂觀 = ×1.3
  3. PE 假設用**當時可得**的歷史 PE 分布:悲觀 = P25、基準 = 中位、樂觀 = P75
  4. 目標價 = 三年後EPS × PE;機率 25 / 55 / 20 → 期望值
  5. 進場:現價 < 期望值 × (1 − 安全邊際)

★ 前視偏誤的四道防線(這是本回測最容易出錯的地方):
   a. EPS 只取 available_date <= 當日 的季度(用法定申報期,不是季末日)
   b. PE 分布只用當日(含)以前的每日 PE,擴張視窗
   c. 訊號 T 日成立 → T+1 日收盤才進場
   d. CAGR 用「當時已公布的 TTM」對「三年前已公布的 TTM」,兩端都是當時看得到的數字

★ 這個規則本身的一個內建偏誤(報告會標明,不是我加的):
   期望值是「三年後的目標價」,但條件卻拿它跟**今天的現價**比,中間**沒有折現**。
   等於預設「三年後一定會漲到目標價」且不計時間成本 ——
   這對長期上漲的標的天然有利,會讓規則看起來比實際好。
"""

from __future__ import annotations

import bisect
from datetime import date

import params as P
import sources_tw as TW
from prices import PriceSeries, fetch_prices

# 題目給定,不做最佳化
SCEN_PROB = {"bear": 0.25, "base": 0.55, "bull": 0.20}
GROWTH_MULT = {"bear": 0.5, "base": 1.0, "bull": 1.3}
PE_PCTL = {"bear": 25, "base": 50, "bull": 75}
HORIZON_YEARS = 3
MARGIN_DEFAULT = 0.20            # 安全邊際 20%
MARGINS_SENSITIVITY = (0.0, 0.10, 0.20, 0.30)
HOLD_YEARS = (1, 3, 5)
WARMUP = P.WARMUP_TRADING_DAYS
COOLDOWN_DAYS = 180              # 同前:避免同一段低估期被重複計算

UNIVERSE = [
    {"code": "2330", "name": "台積電", "yf": "2330.TW"},
    {"code": "2308", "name": "台達電", "yf": "2308.TW"},
    {"code": "2454", "name": "聯發科", "yf": "2454.TW"},
    {"code": "2317", "name": "鴻海", "yf": "2317.TW"},
]


def _pctl(sorted_vals: list[float], p: float) -> float:
    """線性內插百分位(p 為 0~100)。"""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def ttm_eps_asof(quarters: list[dict], d: str) -> tuple[float | None, str | None]:
    """當日可得的『近四季 EPS 合計』。回傳 (TTM EPS, 最新一季季末)。

    只採 available_date <= d 的季度 —— 這是防前視的關鍵:
    2010Q1 的財報雖然季末是 3/31,但要到 5/15 才看得到。
    """
    avail = [q for q in quarters
             if q.get("available_date") and q["available_date"] <= d and q.get("eps") is not None]
    if len(avail) < 4:
        return None, None
    avail.sort(key=lambda q: q["quarter_end"])
    last4 = avail[-4:]
    return sum(q["eps"] for q in last4), last4[-1]["quarter_end"]


def _ttm_at_quarter(quarters: list[dict], q_end: str) -> float | None:
    """以某個季末為終點的 TTM EPS(用於取『三年前』的基準)。"""
    seq = sorted([q for q in quarters if q.get("eps") is not None],
                 key=lambda q: q["quarter_end"])
    idx = next((i for i, q in enumerate(seq) if q["quarter_end"] == q_end), None)
    if idx is None or idx < 3:
        return None
    return sum(q["eps"] for q in seq[idx - 3: idx + 1])


def _shift_quarter(q_end: str, years: int) -> str:
    y, m, d = q_end.split("-")
    return f"{int(y) - years:04d}-{m}-{d}"


def compute_ev(quarters: list[dict], pe_hist_sorted: list[float], d: str,
               price: float) -> dict | None:
    """在日期 d 算出期望值。資料不足回 None(不猜、不補值)。"""
    ttm, last_q = ttm_eps_asof(quarters, d)
    if ttm is None or ttm <= 0 or last_q is None:
        return None
    base_q = _shift_quarter(last_q, HORIZON_YEARS)
    ttm_3y_ago = _ttm_at_quarter(quarters, base_q)
    # 三年前的那筆也必須是「當時已公布」——用季末+法定申報期保守判斷
    if ttm_3y_ago is None or ttm_3y_ago <= 0:
        return None
    if len(pe_hist_sorted) < WARMUP:
        return None

    cagr = (ttm / ttm_3y_ago) ** (1.0 / HORIZON_YEARS) - 1.0
    pes = {k: _pctl(pe_hist_sorted, v) for k, v in PE_PCTL.items()}
    targets, eps_f = {}, {}
    for k, mult in GROWTH_MULT.items():
        g = cagr * mult
        e = ttm * (1.0 + g) ** HORIZON_YEARS
        eps_f[k] = e
        targets[k] = e * pes[k]
    ev = sum(targets[k] * SCEN_PROB[k] for k in targets)
    return {
        "date": d, "price": price, "ttm_eps": ttm, "last_q": last_q,
        "ttm_3y_ago": ttm_3y_ago, "base_q": base_q, "cagr": cagr,
        "eps_future": eps_f, "pe_assumed": pes, "targets": targets,
        "ev": ev, "upside": (ev / price - 1.0) if price else None,
    }


def quarter_end_indices(rows: list[dict]) -> list[int]:
    """每季最後一個交易日的索引(規則規定每季末重算一次)。"""
    out = []
    for i, r in enumerate(rows):
        if i + 1 >= len(rows):
            out.append(i)
            break
        cq = (int(r["date"][5:7]) - 1) // 3
        nq = (int(rows[i + 1]["date"][5:7]) - 1) // 3
        if cq != nq or rows[i + 1]["date"][:4] != r["date"][:4]:
            out.append(i)
    return out


def build_timeline(code: str, yf_ticker: str) -> tuple[list[dict], list[dict]]:
    """每日列 + 每季末的期望值評估(擴張視窗,無前視)。"""
    px_rows = fetch_prices(yf_ticker)
    px = PriceSeries(px_rows)
    pe_daily = {r["date"]: r["pe"] for r in TW.fetch_pe_daily_tw(code)}
    quarters = TW.quarterly_fundamentals_tw(code)

    rows: list[dict] = []
    for i, d in enumerate(px.dates):
        rows.append({"date": d, "close_raw": px.raw[i], "close_adj": px.adj[i],
                     "pe": pe_daily.get(d)})

    # 季末重算:PE 分布用「當日(含)以前」的擴張視窗快照
    qidx = set(quarter_end_indices(rows))
    seen: list[float] = []
    evals: list[dict] = []
    for i, r in enumerate(rows):
        if i in qidx:
            ev = compute_ev(quarters, seen, r["date"], r["close_raw"])
            if ev:
                ev["idx"] = i
                evals.append(ev)
        if r["pe"] is not None:
            bisect.insort(seen, r["pe"])   # 當日結束後才併入 → 明天才看得到
    return rows, evals
