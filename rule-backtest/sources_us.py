"""
美股資料層 (sources_us.py)
==========================
美股要湊齊「季 EPS + 共識 EPS + 毛利率 + trailing PE」,單一來源都不夠,
所以用兩個互補來源(實測後選的,不是隨便挑):

  1. yfinance `get_earnings_dates()` —— 每季「共識 EPS 估計」與「實際公布 EPS」,
     可回溯到 2010 年,且**自帶財報日**(= 資訊可用日,前視偏誤處理最精準)。
     這是本專案唯一拿得到「歷史共識 EPS」的地方,策略 B 的 (b) 條件靠它。
     口徑:分析師基準(adjusted / non-GAAP),估計與實際同口徑,可直接比較。

  2. SEC EDGAR XBRL companyconcept —— 季毛利率(GrossProfit / Revenues),
     回溯到 2010 年,且每筆都有 `filed`(實際送件日)= 精準可用日。

  ⚠ 實測踩到的兩個坑(已處理,寫在這裡免得後人再踩):
     a. EDGAR 的 10-K **只標整年、不標 Q4** → Q4 必須用「全年 − 前三季」推導。
     b. 同一期間會有多筆(後續財報的比較欄位/重編)→ 一律取 **最早送件那筆**,
        才是「當時真正看得到的數字」(point-in-time,不用未來的重編值)。

  ⚠ 為什麼不用 yfinance 的 quarterly_income_stmt 抓毛利率?
     實測只回傳 5 季,長度完全不夠回測(EDGAR 有 66 季)。
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date

from cache import cache_get, cache_set

_SEC_UA = {"User-Agent": "rule-backtest research script (contact: local research use)"}


# ─────────────────────────────────────────────────────────────────────
# SEC EDGAR:季毛利率
# ─────────────────────────────────────────────────────────────────────
def _sec_get(url: str) -> dict:
    req = urllib.request.Request(url, headers=_SEC_UA)
    with urllib.request.urlopen(req, timeout=40) as f:
        return json.loads(f.read().decode())


def _duration_days(u: dict) -> int:
    return (date.fromisoformat(u["end"]) - date.fromisoformat(u["start"])).days


def _sec_concept_first_filed(cik: str, tag: str) -> tuple[dict, dict]:
    """抓某個 XBRL 科目 → (季度事實, 年度事實),各自「同期間只留最早送件那筆」。

    回傳 {期末日: {"val": 數值, "filed": 送件日}}。
    季度 = 期間長度 80–100 天;年度 = 350–380 天。
    """
    key = f"sec_{cik}_{tag}"
    cached = cache_get(key, ttl_seconds=7 * 24 * 3600)
    if cached is not None:
        q, a = cached["data"]["q"], cached["data"]["a"]
        return q, a

    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
    try:
        d = _sec_get(url)
    except Exception:  # noqa: BLE001 — 該科目不存在就當空
        cache_set(key, {"q": {}, "a": {}})
        return {}, {}

    units = d.get("units", {}).get("USD") or d.get("units", {}).get("USD/shares") or []
    q: dict[str, dict] = {}
    a: dict[str, dict] = {}
    # 先照 (期末, 送件日) 排序 → setdefault 就會保留「最早送件」那筆
    for u in sorted(units, key=lambda x: (x.get("end", ""), x.get("filed", ""))):
        if not u.get("start") or not u.get("end"):
            continue
        dd = _duration_days(u)
        rec = {"val": float(u["val"]), "filed": u["filed"]}
        if 80 <= dd <= 100:
            q.setdefault(u["end"], rec)
        elif 350 <= dd <= 380:
            a.setdefault(u["end"], rec)
    cache_set(key, {"q": q, "a": a})
    return q, a


def quarterly_gross_margin_us(cik: str) -> dict[str, dict]:
    """回傳 {季末日: {"gross_margin": %, "available_date": 送件日}}。

    Q4 用「全年 − 前三季」推導(EDGAR 的 10-K 不單獨標 Q4)。
    可用日取毛利與營收兩者送件日的較晚者(兩個數字都到齊才算得出毛利率)。

    ★ 營收要**合併多個 XBRL 科目**,不能只在「完全查無」時才換一個:
      實測 AAPL 的 us-gaap:Revenues 只有 8 季(2018 之後改用
      RevenueFromContractWithCustomerExcludingAssessedTax)、INTC 的 Revenues 是 0 季。
      舊寫法 `if not rq:` 只在完全空時 fallback,於是 AAPL 永遠只有 8 季營收 →
      毛利率算不出來 → 整檔被判「資料不足」而消失。
    """
    gq, ga = _sec_concept_first_filed(cik, "GrossProfit")

    rq: dict[str, dict] = {}
    ra: dict[str, dict] = {}
    for tag in ("Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet"):
        q, a = _sec_concept_first_filed(cik, tag)
        for k, v in q.items():
            rq.setdefault(k, v)       # 先到者優先(Revenues 為主,其餘補洞)
        for k, v in a.items():
            ra.setdefault(k, v)

    out: dict[str, dict] = {}
    for qend in sorted(set(gq) & set(rq)):
        rev = rq[qend]["val"]
        if not rev:
            continue
        out[qend] = {
            "gross_margin": round(gq[qend]["val"] / rev * 100.0, 3),
            "available_date": max(gq[qend]["filed"], rq[qend]["filed"]),
        }

    # Q4 = 全年 − (Q1+Q2+Q3)
    for fy in sorted(set(ga) & set(ra)):
        if not fy.endswith("12-31") or fy in out:
            continue
        y = fy[:4]
        q123 = [f"{y}-03-31", f"{y}-06-30", f"{y}-09-30"]
        if not all(x in gq and x in rq for x in q123):
            continue
        g4 = ga[fy]["val"] - sum(gq[x]["val"] for x in q123)
        r4 = ra[fy]["val"] - sum(rq[x]["val"] for x in q123)
        if not r4:
            continue
        out[fy] = {
            "gross_margin": round(g4 / r4 * 100.0, 3),
            "available_date": max(ga[fy]["filed"], ra[fy]["filed"]),
        }
    return out


# ─────────────────────────────────────────────────────────────────────
# yfinance:季共識 EPS + 實際 EPS(自帶財報日)
# ─────────────────────────────────────────────────────────────────────
def _prev_quarter_end(iso: str) -> str:
    """財報日 → 它所報告的那一季季末(= 財報日之前最近的季末)。

    例:2024-04-23 公布的是 2024-03-31 那季;2025-01-29 公布的是 2024-12-31 那季。
    """
    y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    ends = [(y - 1, 12, 31), (y, 3, 31), (y, 6, 30), (y, 9, 30), (y, 12, 31)]
    best = None
    for (ey, em, ed) in ends:
        cand = f"{ey:04d}-{em:02d}-{ed:02d}"
        if cand < iso:
            best = cand
    return best or f"{y - 1}-12-31"


def quarterly_eps_us(ticker: str) -> list[dict]:
    """回傳每季 [{quarter_end, available_date, eps_actual, eps_consensus}](由舊到新)。

    - available_date = 實際財報日(yfinance 提供)→ 前視偏誤處理最精準。
    - eps_consensus  = 財報日前的分析師共識(該季預估)。
    - eps_actual     = 實際公布 EPS(與共識同為 adjusted 口徑)。
    """
    key = f"us_eps_{ticker}"
    cached = cache_get(key, ttl_seconds=24 * 3600)
    if cached is not None:
        return cached["data"]

    import yfinance as yf

    ed = yf.Ticker(ticker).get_earnings_dates(limit=100)  # Yahoo 上限為 100
    if ed is None or len(ed) == 0:
        raise RuntimeError(f"yfinance 未回傳 {ticker} 財報日資料")
    ed = ed.sort_index()

    rows: list[dict] = []
    for ts, r in ed.iterrows():
        iso = str(ts)[:10]
        est = r.get("EPS Estimate")
        act = r.get("Reported EPS")
        est = float(est) if est == est and est is not None else None  # est==est 濾 NaN
        act = float(act) if act == act and act is not None else None
        if est is None and act is None:
            continue
        rows.append({
            "quarter_end": _prev_quarter_end(iso),
            "available_date": iso,
            "eps_actual": act,
            "eps_consensus": est,
        })
    # 同一季若有多筆(重複財報日)只留最早那筆
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(r["quarter_end"], r)
    out = [seen[q] for q in sorted(seen)]
    cache_set(key, out)
    return out


def _match_quarter(gm: dict[str, dict], q: str, tol_days: int = 12) -> dict | None:
    """把 yfinance 的季末日對應到 SEC 的季末日,允許數日誤差。

    ★ 為什麼需要容差:AAPL / INTC / KO 這類公司的財年結束在「某個星期六」,
      SEC 記的是 2026-06-27,而 yfinance 給的是 2026-06-30 —— 精確比對永遠對不上,
      結果毛利率整欄變成 None(實測 AAPL/INTC 只剩 4 季、KO 只剩 3 季),
      整檔因此被判「資料不足」而從樣本中消失。
      季與季相隔約 90 天,12 天容差不可能誤配到相鄰季。
    """
    if q in gm:
        return gm[q]
    try:
        qd = date.fromisoformat(q)
    except ValueError:
        return None
    best, best_d = None, None
    for k, v in gm.items():
        try:
            d = abs((date.fromisoformat(k) - qd).days)
        except ValueError:
            continue
        if d <= tol_days and (best_d is None or d < best_d):
            best, best_d = v, d
    return best


def quarterly_fundamentals_us(ticker: str, cik: str) -> list[dict]:
    """合併 EPS(yfinance)與毛利率(SEC EDGAR)成統一格式的季序列。

    輸出每季:{quarter_end, available_date, eps, eps_consensus, gross_margin}
      - eps            = 實際 EPS(adjusted 口徑)
      - eps_consensus  = 該季共識 EPS
      - gross_margin   = SEC XBRL 推算
      - available_date = 兩個來源可用日的**較晚者**(兩者都到齊才算完整一季)
    """
    eps_rows = quarterly_eps_us(ticker)
    gm = quarterly_gross_margin_us(cik)

    out: list[dict] = []
    for r in eps_rows:
        q = r["quarter_end"]
        g = _match_quarter(gm, q)
        # 毛利率可能還沒送件(財報日通常早於 10-Q 送件日)→ 可用日取較晚者
        avail = r["available_date"]
        if g:
            avail = max(avail, g["available_date"])
        out.append({
            "quarter_end": q,
            "available_date": avail,
            "eps_available_date": r["available_date"],
            "eps": r["eps_actual"],
            "eps_consensus": r["eps_consensus"],
            "gross_margin": g["gross_margin"] if g else None,
            "eps_source": "actual+consensus",
        })
    return out
