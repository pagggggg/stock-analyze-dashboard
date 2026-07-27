"""
時間軸建構 (timeline.py)
========================
把「每日 PE」與「每季基本面」壓成一條**逐日、point-in-time** 的狀態時間軸,
這是整份回測避免前視偏誤(look-ahead bias)的核心。

三個關鍵設計:

1. **擴張視窗分位數(expanding window)**
   第 t 天的「歷史 PE 中位數 / 第80百分位」只用「第 t 天(含)以前」的 PE 算。
   絕不用全樣本分位數 —— 那等於拿未來資訊決定今天買賣,會做出假的漂亮結果。

2. **暖身期**
   PE 樣本數不足 WARMUP_TRADING_DAYS 之前,一律不產生訊號(分位數不可信)。

3. **基本面用「可用日」對齊,不是「季末日」**
   台股用法定申報期限、美股用實際財報日/送件日。第 t 天只看得到
   available_date <= t 的季度。

輸出:每個交易日一筆
   {date, close_raw, close_adj, pe, pe_entry_thr, pe_exit_thr, tradable,
    eps_bad, gm_bad, latest_quarter}
"""

from __future__ import annotations

import bisect

import params as P


# ─────────────────────────────────────────────────────────────────────
# 分位數(線性內插,與 numpy.percentile 預設一致)
# ─────────────────────────────────────────────────────────────────────
def _percentile(sorted_vals: list[float], pct: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, n - 1)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# ─────────────────────────────────────────────────────────────────────
# 基本面惡化旗標(策略 B 的 (b)(c) 條件)
# ─────────────────────────────────────────────────────────────────────
def _yoy_inputs_ready(vals: list[float | None], i: int, n_quarters: int) -> bool:
    """判斷第 i 季「有沒有足夠資料去評估」連續 n 季年減(與訊號真假無關)。"""
    for k in range(n_quarters):
        cur, prev = i - k, i - k - 4
        if prev < 0 or vals[cur] is None or vals[prev] is None:
            return False
    return True


def _qoq_inputs_ready(vals: list[float | None], i: int, n_quarters: int) -> bool:
    """判斷第 i 季「有沒有足夠資料去評估」連續 n 季環比下滑。"""
    for k in range(n_quarters):
        cur, prev = i - k, i - k - 1
        if prev < 0 or vals[cur] is None or vals[prev] is None:
            return False
    return True


def _consecutive_yoy_decline(vals: list[float | None], i: int, n_quarters: int) -> bool:
    """第 i 季起,往回連續 n 季「年減」(與去年同季比,季節性中性)。

    需要 vals[i-4], vals[i-1-4] … 都存在;任何一季缺值就回 False(不猜)。
    """
    if not _yoy_inputs_ready(vals, i, n_quarters):
        return False
    return all(vals[i - k] < vals[i - k - 4] for k in range(n_quarters))


def _consecutive_qoq_decline(vals: list[float | None], i: int, n_quarters: int) -> bool:
    """第 i 季起,往回連續 n 季「較前一季下滑」(毛利率用,題目定義為連續下滑)。"""
    if not _qoq_inputs_ready(vals, i, n_quarters):
        return False
    return all(vals[i - k] < vals[i - k - 1] for k in range(n_quarters))


def build_quarter_flags(quarters: list[dict], eps_field: str) -> list[dict]:
    """為每一季算出「基本面惡化」旗標。

    eps_field:
      - "eps"           台股(無共識)→ 用實際 EPS 連續兩季年減(題目指定的代理)
      - "eps_consensus" 美股(有共識)→ 用共識 EPS 連續兩季年減

    回傳每季 {quarter_end, available_date, eps_bad, gm_bad, ...} 由舊到新。

    ★ 金融業的降級處理:銀行/金控的損益表**沒有「毛利率」這個概念**
      (沒有 GrossProfit / 營業成本科目)。若整檔連一季毛利率都沒有,
      視為「該條件對本業別不適用」,改成只用 EPS 條件判斷可評估性,
      並在每季標 `gm_not_applicable=True` —— 讓報告能明講
      「這檔的策略 B 只有 EPS 出場條件」,而不是靜默地把整檔丟掉、
      也不是假裝它通過了毛利率檢查。
      注意:只有「完全沒有」才降級;部分季缺漏仍維持嚴格(那是資料缺漏,不是不適用)。
    """
    eps_vals = [q.get(eps_field) for q in quarters]
    gm_vals = [q.get("gross_margin") for q in quarters]
    n = P.DETERIORATION_QUARTERS
    gm_na = not any(v is not None for v in gm_vals)

    out: list[dict] = []
    for i, q in enumerate(quarters):
        # 「可評估」= 基本面規則的輸入資料齊備。
        # 用途:策略 A 與 B 必須在**同一個起始日**開跑才公平 —— 若從 B 還沒有財報
        # 可用的年代就開始,B 會在那段期間退化成 A,把兩者差異稀釋掉。
        computable = _yoy_inputs_ready(eps_vals, i, n)
        if not gm_na:
            computable = computable and _qoq_inputs_ready(gm_vals, i, n)
        out.append({
            "quarter_end": q["quarter_end"],
            "available_date": q["available_date"],
            "eps": q.get("eps"),
            "eps_consensus": q.get("eps_consensus"),
            "gross_margin": q.get("gross_margin"),
            "eps_bad": _consecutive_yoy_decline(eps_vals, i, n),
            "gm_bad": False if gm_na else _consecutive_qoq_decline(gm_vals, i, n),
            "gm_not_applicable": gm_na,
            "computable": computable,
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# 美股 trailing PE:近四季實際 EPS 加總(point-in-time)
# ─────────────────────────────────────────────────────────────────────
def ttm_eps_events(quarters: list[dict]) -> list[dict]:
    """回傳 [{available_date, ttm_eps}]:每季財報公布後的最新 TTM EPS。

    TTM = 最近 4 季實際 EPS 加總;可用日 = 第 4 季的財報日(eps_available_date)。
    TTM <= 0(虧損)→ PE 無意義,標記 None,那段期間沒有有效 PE、不能進場。
    """
    out: list[dict] = []
    for i in range(3, len(quarters)):
        window = quarters[i - 3: i + 1]
        vals = [q.get("eps") for q in window]
        if any(v is None for v in vals):
            continue
        s = sum(vals)
        avail = quarters[i].get("eps_available_date") or quarters[i]["available_date"]
        out.append({"available_date": avail, "ttm_eps": s if s > 0 else None})
    out.sort(key=lambda x: x["available_date"])
    return out


# ─────────────────────────────────────────────────────────────────────
# 主建構器
# ─────────────────────────────────────────────────────────────────────
def build_timeline(
    px,                      # PriceSeries
    quarter_flags: list[dict],
    pe_daily: list[dict] | None = None,   # 台股:TWSE 每日本益比 [{date, pe}]
    ttm_events: list[dict] | None = None,  # 美股:TTM EPS 事件
    warmup: int | None = None,
) -> list[dict]:
    """產生逐日狀態。台股用 TWSE 官方 PE;美股用 close_raw / TTM EPS 自算。"""
    warmup = warmup if warmup is not None else P.WARMUP_TRADING_DAYS

    pe_by_date = {r["date"]: r["pe"] for r in (pe_daily or [])}

    # 美股:把 TTM EPS 事件展開成「每日有效的 TTM EPS」(as-of)
    ev_dates = [e["available_date"] for e in (ttm_events or [])]
    ev_vals = [e["ttm_eps"] for e in (ttm_events or [])]

    # 每季旗標 → as-of 查詢用
    q_dates = [q["available_date"] for q in quarter_flags]

    seen_pe: list[float] = []   # 已觀察到的 PE(排序),擴張視窗用
    rows: list[dict] = []

    for i, d in enumerate(px.dates):
        # --- 當日 PE ---
        if pe_daily is not None:
            pe = pe_by_date.get(d)
        else:
            j = bisect.bisect_right(ev_dates, d) - 1   # 只用「已公布」的 TTM EPS
            ttm = ev_vals[j] if j >= 0 else None
            pe = (px.raw[i] / ttm) if (ttm and ttm > 0) else None

        # --- 擴張視窗分位數:先用「今天以前」的樣本算門檻,再把今天加進樣本 ---
        #     (門檻含今日 PE 亦可,但「先算後加」更保守,且避免單點自我影響)
        entry_thr = exit_thr = None
        if len(seen_pe) >= warmup:
            entry_thr = _percentile(seen_pe, P.ENTRY_PCTL)
            exit_thr = _percentile(seen_pe, P.EXIT_PCTL)

        if pe is not None and pe > 0:
            bisect.insort(seen_pe, pe)

        # --- 基本面狀態(as-of:只看已公開的季度)---
        k = bisect.bisect_right(q_dates, d) - 1
        if k >= 0:
            qf = quarter_flags[k]
            eps_bad, gm_bad, latest_q = qf["eps_bad"], qf["gm_bad"], qf["quarter_end"]
        else:
            eps_bad = gm_bad = False
            latest_q = None

        rows.append({
            "date": d,
            "close_raw": px.raw[i],
            "close_adj": px.adj[i],
            "pe": pe,
            "pe_entry_thr": entry_thr,
            "pe_exit_thr": exit_thr,
            "pe_samples": len(seen_pe),
            "tradable": pe is not None and entry_thr is not None,
            "eps_bad": eps_bad,
            "gm_bad": gm_bad,
            "latest_quarter": latest_q,
        })
    return rows
