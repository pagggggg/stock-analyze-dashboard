"""
逐檔抓「月營收 + 單季EPS」到快取 (fetch_data.py)
================================================
這是整個回測最慢的一步(逐檔打 FinMind)。設計成可中斷、可續跑:
每檔抓完就寫快取,撞到免費額度(QuotaExceeded)就優雅停下並回報進度,
已抓的都留在 cache/,重跑會直接沿用、只補沒抓到的。

股價(prices)不在這裡抓——只有「真的觸發訊號」的股票才需要股價,
留到 run.py 針對觸發清單去抓,省很多請求。
"""

from __future__ import annotations

import sys
import time

import params as P
from finmind_client import (
    FetchError,
    fetch_eps,
    fetch_month_revenue,
    get_universe,
)
from cache import cache_has


def main() -> int:
    universe = get_universe(P.UNIVERSE_SEED, P.UNIVERSE_SIZE)
    total = len(universe)
    print(f"[fetch] 股票池:{total} 檔(seed={P.UNIVERSE_SEED}, size={P.UNIVERSE_SIZE})", flush=True)

    done = 0
    t0 = time.time()
    try:
        for i, sid in enumerate(universe, 1):
            # 兩個資料集都已在快取就跳過(續跑時很快)
            if cache_has(f"rev_{sid}") and cache_has(f"eps_{sid}"):
                done += 1
                continue
            fetch_month_revenue(sid, P.REVENUE_FETCH_START)
            fetch_eps(sid, P.EPS_FETCH_START)
            done += 1
            if i % 25 == 0 or i == total:
                rate = i / max(time.time() - t0, 1e-6)
                print(f"[fetch] {i}/{total}  ({rate:.1f} 檔/秒)", flush=True)
    except FetchError as e:
        print(f"[fetch] ⚠ 撞到 FinMind 免費額度/暫時錯誤,已優雅停下。已完成 {done}/{total}。", flush=True)
        print(f"[fetch]   訊息:{str(e)[:120]}", flush=True)
        print("[fetch]   已抓的都在 cache/,稍後重跑會自動續抓剩下的。", flush=True)
        return 2

    print(f"[fetch] ✔ 完成 {done}/{total},耗時 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
