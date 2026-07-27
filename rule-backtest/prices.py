"""
股價層 (prices.py)
==================
台股、美股一律用 yfinance,兩個口徑刻意分開,理由寫清楚:

  close_raw  = 未還原股利的收盤價(仍還原分割)
               → **只拿來算 PE**。PE 的分子必須是市場真實報價,
                 用還原股利價會把 PE 系統性壓低,失真。

  close_adj  = 還原股利與分割的收盤價(total return)
               → **只拿來算報酬與回撤**。這才是投資人實際拿到的績效;
                 忽略股利會低估台股報酬(2330/2308 殖利率不低)。

  兩個策略 A/B 都吃同一份價格,比較基礎一致,不會偏袒任何一方。
"""

from __future__ import annotations

import bisect

from cache import cache_get, cache_set
import params as P


def fetch_prices(yf_ticker: str) -> list[dict]:
    """回傳 [{date, close_raw, close_adj}](由舊到新)。快取 24 小時。"""
    key = f"px_{yf_ticker}"
    cached = cache_get(key, ttl_seconds=24 * 3600)
    if cached is not None:
        return cached["data"]

    import yfinance as yf

    t = yf.Ticker(yf_ticker)
    # auto_adjust=False → 同時拿到 Close(未還原股利)與 Adj Close(全還原)
    h = t.history(start=P.PRICE_START, auto_adjust=False)
    if h is None or len(h) == 0:
        raise RuntimeError(f"yfinance 未回傳 {yf_ticker} 股價")

    rows: list[dict] = []
    for ts, r in h.iterrows():
        c_raw = r.get("Close")
        c_adj = r.get("Adj Close", c_raw)
        if c_raw != c_raw or c_raw is None or c_raw <= 0:  # 濾 NaN / 非正值
            continue
        if c_adj != c_adj or c_adj is None or c_adj <= 0:
            c_adj = c_raw
        rows.append({
            "date": str(ts)[:10],
            "close_raw": round(float(c_raw), 4),
            "close_adj": round(float(c_adj), 6),
        })
    rows.sort(key=lambda x: x["date"])
    cache_set(key, rows)
    return rows


class PriceSeries:
    """日收盤序列,支援 as-of 查詢(ISO 日期字串的字典序 = 時間序,可直接二分)。"""

    def __init__(self, rows: list[dict]):
        self.dates = [r["date"] for r in rows]
        self.raw = [r["close_raw"] for r in rows]
        self.adj = [r["close_adj"] for r in rows]
        self._idx = {d: i for i, d in enumerate(self.dates)}

    def __len__(self) -> int:
        return len(self.dates)

    def index_of(self, iso: str) -> int | None:
        return self._idx.get(iso)

    def asof_index(self, iso: str) -> int | None:
        """date <= iso 的最後一個索引。"""
        i = bisect.bisect_right(self.dates, iso) - 1
        return i if i >= 0 else None

    def first_index_after(self, iso: str) -> int | None:
        """date 嚴格大於 iso 的第一個索引。"""
        i = bisect.bisect_right(self.dates, iso)
        return i if i < len(self.dates) else None
