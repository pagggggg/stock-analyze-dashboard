"""
FinMind 資料層(sources_finmind.py)
====================================
逐檔抓四種資料,全部走檔案快取:

  1) fetch_prices()      日收盤價(未還原;免費版無 daily_adj → 已揭露股利拖累)
  2) fetch_financials()  綜合損益表 → 逐季 GrossProfit / Revenue / EPS
                         毛利率 = GrossProfit / Revenue
  3) fetch_pbr()         每日 PBR(taiwan_stock_per_pbr)→ 左側「P/B 近10年百分位」用

前視偏誤處理:
  - 價格、PBR 為當日盤後即可用。
  - 財報用「法定申報期限」當可用日(見 timeline.available_date),保守不早於實際公布。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from cache import cache_get, cache_set

_DL = None


def _load_env() -> None:
    envp = Path(__file__).resolve().parents[1] / ".env"
    if envp.exists():
        for line in envp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _loader():
    global _DL
    if _DL is not None:
        return _DL
    _load_env()
    from FinMind.data import DataLoader

    dl = DataLoader()
    tok = os.environ.get("FINMIND_TOKEN", "").strip()
    if tok:
        try:
            dl.login_by_token(api_token=tok)
        except Exception:
            pass
    _DL = dl
    return _DL


def _safe(fn, retries: int = 5, sleep: float = 0.3):
    last = None
    for attempt in range(retries):
        try:
            df = fn()
            time.sleep(sleep)
            return df
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e).lower()
            if "level is free" in msg:
                return None
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"FinMind 抓取失敗:{last}")


def _detect_corporate_actions(rows: list[dict]) -> list[dict]:
    """偵測分割/減資等公司行為:相鄰交易日收盤比值超出台股 ±10% 漲跌幅甚多者。
    回傳 [{date(事件生效日), ratio=after/before}]。台股日限 ±10%,故 <0.55 或 >1.8
    幾乎不可能是自然波動 → 判為公司行為(分割/減資/大額配股)。"""
    events = []
    for i in range(1, len(rows)):
        p0, p1 = rows[i - 1]["close"], rows[i]["close"]
        if p0 > 0:
            r = p1 / p0
            if r < 0.55 or r > 1.8:
                events.append({"date": rows[i]["date"], "ratio": r,
                               "before": p0, "after": p1})
    return events


# 已人工核對的公司行為(供報告揭露);偵測器應與此一致。
KNOWN_CORPORATE_ACTIONS = {
    "0050": "2025-06 1:4 股票分割",
    "2603": "2022-09 減資(現金退還,股本縮減 → 股價跳升)",
    "2609": "2017-05 減資(股本縮減 → 股價跳升)",
}


def fetch_prices(stock_id: str, start_date: str = "2009-01-01",
                 adjust: bool = True) -> list[dict]:
    """回傳 [{date, close}] 由舊到新。

    adjust=True(預設):對偵測到的分割/減資做「回溯調整」(把事件日之前的價格
    乘上 after/before 比值)使序列連續,避免把公司行為誤當成鉅額漲跌。
    這是總報酬近似(仍未還原現金股利 → 對報酬保守低估,已於報告揭露)。
    """
    key = f"px_{stock_id}"
    c = cache_get(key)
    if c is not None:
        rows = c["data"]
    else:
        dl = _loader()
        df = _safe(lambda: dl.taiwan_stock_daily(stock_id=stock_id, start_date=start_date))
        rows = []
        if df is not None and len(df):
            for _, r in df.iterrows():
                try:
                    cl = float(r["close"])
                except (TypeError, ValueError, KeyError):
                    continue
                if cl == cl and cl > 0:
                    rows.append({"date": str(r["date"]), "close": round(cl, 4)})
        rows.sort(key=lambda x: x["date"])
        cache_set(key, rows)

    if not adjust:
        return rows
    events = _detect_corporate_actions(rows)
    if not events:
        return rows
    adj = [dict(r) for r in rows]
    # 由最新事件往回:事件日之前(不含當日)全部乘 ratio,連續化
    for ev in sorted(events, key=lambda e: e["date"], reverse=True):
        rr = ev["ratio"]
        for r in adj:
            if r["date"] < ev["date"]:
                r["close"] = round(r["close"] * rr, 6)
    return adj


def fetch_month_revenue(stock_id: str, start_date: str = "2009-01-01") -> list[dict]:
    """回傳月營收 [{ry, rm, revenue, avail}] 由舊到新。

    avail = 該月營收的「可公開取得日」= 次月 10 日(台股法定申報期限)。
    例:2021 年 3 月營收 → 2021-04-10 才可用。這樣才不會有前視偏誤。
    """
    key = f"monrev_{stock_id}"
    c = cache_get(key)
    if c is not None:
        return c["data"]
    dl = _loader()
    df = _safe(lambda: dl.taiwan_stock_month_revenue(stock_id=stock_id, start_date=start_date))
    rows = []
    if df is not None and len(df):
        for _, r in df.iterrows():
            try:
                ry = int(r["revenue_year"])
                rm = int(r["revenue_month"])
                rev = float(r["revenue"])
            except (TypeError, ValueError, KeyError):
                continue
            ay, am = (ry + 1, 1) if rm == 12 else (ry, rm + 1)
            rows.append({"ry": ry, "rm": rm, "revenue": rev,
                         "avail": f"{ay:04d}-{am:02d}-10"})
    rows.sort(key=lambda x: (x["ry"], x["rm"]))
    cache_set(key, rows)
    return rows


def corporate_actions(stock_id: str) -> list[dict]:
    """回傳偵測到的公司行為事件(供報告揭露)。"""
    key = f"px_{stock_id}"
    c = cache_get(key)
    rows = c["data"] if c else fetch_prices(stock_id, adjust=False)
    return _detect_corporate_actions(rows)


def fetch_financials(stock_id: str, start_date: str = "2009-01-01") -> list[dict]:
    """回傳逐季 [{date, revenue, gross_profit, eps, gross_margin}] 由舊到新。

    毛利率 = gross_profit / revenue * 100(百分比,pp 為單位)。
    date = 財報所屬季底日(如 2015-03-31 = 2015Q1)。
    """
    key = f"fin_{stock_id}"
    c = cache_get(key)
    if c is not None:
        return c["data"]
    dl = _loader()
    df = _safe(lambda: dl.taiwan_stock_financial_statement(stock_id=stock_id, start_date=start_date))
    by_date: dict[str, dict] = {}
    if df is not None and len(df):
        for _, r in df.iterrows():
            d = str(r["date"])
            t = str(r["type"])
            try:
                v = float(r["value"])
            except (TypeError, ValueError, KeyError):
                continue
            slot = by_date.setdefault(d, {"date": d, "revenue": None, "gross_profit": None, "eps": None})
            if t == "Revenue":
                slot["revenue"] = v
            elif t == "GrossProfit":
                slot["gross_profit"] = v
            elif t == "EPS":
                slot["eps"] = v
    rows = []
    for d in sorted(by_date):
        s = by_date[d]
        rev = s["revenue"]
        gp = s["gross_profit"]
        gm = (gp / rev * 100.0) if (rev and gp is not None and rev != 0) else None
        s["gross_margin"] = round(gm, 4) if gm is not None else None
        rows.append(s)
    cache_set(key, rows)
    return rows


def fetch_pbr(stock_id: str, start_date: str = "2009-01-01") -> list[dict]:
    """回傳 [{date, pbr}] 由舊到新(每日)。"""
    key = f"pbr_{stock_id}"
    c = cache_get(key)
    if c is not None:
        return c["data"]
    dl = _loader()
    df = _safe(lambda: dl.taiwan_stock_per_pbr(stock_id=stock_id, start_date=start_date))
    rows = []
    if df is not None and len(df):
        for _, r in df.iterrows():
            try:
                p = float(r["PBR"])
            except (TypeError, ValueError, KeyError):
                continue
            if p == p and p > 0:
                rows.append({"date": str(r["date"]), "pbr": round(p, 4)})
    rows.sort(key=lambda x: x["date"])
    cache_set(key, rows)
    return rows
