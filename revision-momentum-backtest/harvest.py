"""
資料採集器 (harvest.py)
=======================
FinMind 免費版有「每小時請求上限」,一次抓不完整個股票池。這支程式設計成
**跨越多個限流視窗、無人值守**地把資料補齊:撞到上限就睡一下再續,已抓的都在
cache/,永不重抓。優先序:

  Phase 0：基準 0050 日股價(必要)
  Phase A：已經有「月營收+EPS」的股票 → 補其日股價(補完就能跑完整報告)
  Phase B：尚未有基本面的股票 → 補月營收+EPS+日股價(擴大覆蓋、提升穩健度)

跑法:`nohup python3 harvest.py > harvest.log 2>&1 &`,然後定期看 harvest.log。
"""

from __future__ import annotations

import sys
import time

import params as P
from cache import cache_has
from finmind_client import (
    FetchError,
    fetch_eps,
    fetch_month_revenue,
    fetch_prices,
    get_universe,
)

SLEEP_ON_QUOTA = 90   # 撞到上限/暫時錯誤先睡 90 秒再試(視窗會滾動釋放,較短間隔可及早取用)
MAX_WALL_HOURS = 8    # 最長跑 8 小時,避免無限掛著


def _do(fn, label: str) -> bool:
    """執行一次抓取;撞上限/網路錯誤就睡、回 False(讓外層重試同一項)。成功回 True。"""
    try:
        fn()
        return True
    except FetchError as e:
        print(f"[harvest] ⚠ {label} 稍後重試({type(e).__name__});睡 {SLEEP_ON_QUOTA}s …", flush=True)
        time.sleep(SLEEP_ON_QUOTA)
        return False


def main() -> int:
    t0 = time.time()
    uni = get_universe(P.UNIVERSE_SEED, P.UNIVERSE_SIZE)

    # Phase 0:基準
    while not cache_has(f"px_{P.BENCHMARK}"):
        if time.time() - t0 > MAX_WALL_HOURS * 3600:
            return 1
        if _do(lambda: fetch_prices(P.BENCHMARK, P.PRICE_FETCH_START), f"基準{P.BENCHMARK}"):
            print(f"[harvest] Phase0 基準 {P.BENCHMARK} 股價 ✔", flush=True)

    def phase_a_targets():
        return [s for s in uni if cache_has(f"rev_{s}") and cache_has(f"eps_{s}") and not cache_has(f"px_{s}")]

    def phase_b_targets():
        return [s for s in uni if not (cache_has(f"rev_{s}") and cache_has(f"eps_{s}"))]

    # Phase A:補價格
    while True:
        if time.time() - t0 > MAX_WALL_HOURS * 3600:
            print("[harvest] 達時間上限,停。", flush=True); return 0
        todo = phase_a_targets()
        if not todo:
            break
        sid = todo[0]
        if _do(lambda: fetch_prices(sid, P.PRICE_FETCH_START), f"px {sid}"):
            done = sum(1 for s in uni if cache_has(f"px_{s}"))
            if done % 10 == 0:
                print(f"[harvest] PhaseA 價格進度:{done} 檔已有價格", flush=True)
    print(f"[harvest] ✔ PhaseA 完成:所有有基本面的股票都有價格了 "
          f"(耗時 {(time.time()-t0)/60:.0f} 分)", flush=True)

    # Phase B:擴大覆蓋
    while True:
        if time.time() - t0 > MAX_WALL_HOURS * 3600:
            print("[harvest] 達時間上限,停。", flush=True); return 0
        todo = phase_b_targets()
        if not todo:
            break
        sid = todo[0]
        ok = _do(lambda: (fetch_month_revenue(sid, P.REVENUE_FETCH_START),
                          fetch_eps(sid, P.EPS_FETCH_START),
                          fetch_prices(sid, P.PRICE_FETCH_START)), f"full {sid}")
        if ok:
            have = sum(1 for s in uni if cache_has(f"rev_{s}") and cache_has(f"eps_{s}"))
            if have % 10 == 0:
                print(f"[harvest] PhaseB 覆蓋:{have}/{len(uni)} 檔有基本面", flush=True)

    print(f"[harvest] ✔ 全部完成,耗時 {(time.time()-t0)/60:.0f} 分", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
