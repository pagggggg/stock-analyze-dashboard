"""
對照組 (control.py)
===================
證明訊號有效、不是單純大盤 beta 或「那幾年隨便買都賺」:
對每一個真實觸發事件,在**同一個公開日、同樣的持有期**,從**同一個股票池**隨機
抽 CONTROL_DRAWS_PER_SIGNAL 檔股票下同樣的單,算它們的超額報酬。

因為對照組與訊號組:同時間、同基準(0050)、同持有期、同母體 → 唯一差別就是
「有沒有訊號」。若訊號組的超額明顯優於這個隨機對照,才算訊號真的有 alpha。
"""

from __future__ import annotations

import random

from backtest import run_trade, summarize


def run_control(
    signals: list[dict],
    months_list: list[int],
    price_provider,          # callable: stock_id -> PriceSeries | None
    bench_px,
    universe: list[str],
    draws: int,
    seed: int,
) -> dict:
    """回傳 {months: {"trades": [...], "summary": {...}}}。

    對每個訊號事件抽 `draws` 檔隨機股(排除訊號本身那檔),在同公開日下單。
    """
    rng = random.Random(seed)
    trades_by_m: dict[int, list[dict]] = {m: [] for m in months_list}

    for sig in signals:
        avail = sig["available_date"]
        for _ in range(draws):
            # 隨機抽一檔(最多試 5 次抽到有價格資料、且非訊號本身的股票)
            picked = None
            for _try in range(5):
                cand = rng.choice(universe)
                if cand == sig["stock_id"]:
                    continue
                px = price_provider(cand)
                if px is not None and len(px) > 0:
                    picked = px
                    break
            if picked is None:
                continue
            for m in months_list:
                t = run_trade(picked, bench_px, avail, m)
                if t is not None:
                    trades_by_m[m].append(t)

    return {m: {"trades": trades_by_m[m], "summary": summarize(trades_by_m[m])} for m in months_list}
