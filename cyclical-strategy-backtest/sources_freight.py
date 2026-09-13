"""
運價指數資料層(sources_freight.py)
===================================
從 investing.com 的歷史資料 API 抓「週線」運價指數:

  - 貨櫃運價指數(container):pair_id 940796 / 940798,2009 → 2025-03(免費歷史上限)
  - BDI 波羅的海乾散貨:pair_id 940793,2009 → 2026-07(完整)

只有 /api/financialdata/historical 這個端點免 Cloudflare 封鎖;metadata 端點會 403,
故精確名稱(SCFI vs CCFI)無法程式化確認,已在 config 與報告誠實標示。

回傳 [{date, close}] 由舊到新;date 為該週日期(週更)。
"""
from __future__ import annotations

import time

import requests

from cache import cache_get, cache_set

_HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.investing.com/",
    "domain-id": "www",
}
_SESS = requests.Session()


def fetch_freight(pair_id: int, start_date: str = "2009-01-01",
                  end_date: str = "2026-08-01") -> list[dict]:
    """回傳 [{date(YYYY-MM-DD), close}] 由舊到新。週線。"""
    key = f"freight_{pair_id}"
    c = cache_get(key)
    if c is not None:
        return c["data"]
    url = (
        f"https://api.investing.com/api/financialdata/historical/{pair_id}"
        f"?start-date={start_date}&end-date={end_date}"
        f"&time-frame=Weekly&add-missing-rows=false"
    )
    data = None
    last = None
    for attempt in range(6):
        try:
            r = _SESS.get(url, headers=_HDR, timeout=30)
            if r.status_code == 200:
                data = r.json().get("data", [])
                break
            last = f"status {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2.0 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"investing.com 抓取失敗 pair_id={pair_id}:{last}")
    rows = []
    for x in data:
        try:
            ts = x["rowDateTimestamp"][:10]  # "2026-07-26T00:00:00Z" -> date
            close = float(x["last_closeRaw"])
        except (KeyError, TypeError, ValueError):
            continue
        if close == close and close > 0:
            rows.append({"date": ts, "close": round(close, 4)})
    rows.sort(key=lambda x: x["date"])
    cache_set(key, rows)
    return rows
