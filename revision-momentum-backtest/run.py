"""
主流程 (run.py)
===============
串起整條回測管線並產出報告:

  1. 讀股票池 + 逐檔快取的月營收/EPS → 用兩個代理訊號找出所有觸發事件
  2. (盡力)預抓全池日股價 → 建 as-of 價格序列(觸發組與隨機對照組共用)
  3. 每個觸發事件 × 3/6/12 月 → 算未來報酬 / 超額報酬 / 最大回撤
  4. 隨機對照組(同公開日、同持有期、同母體)
  5. 樣本內/樣本外分段、失敗案例、樣本清單 CSV
  6. 產出 backtest_report.md

全程用 cache/,可中斷可續跑;撞到 FinMind 免費額度會優雅降級(用已抓到的資料續算)。
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import params as P
from backtest import PriceSeries, run_trade, summarize
from cache import cache_has
from control import run_control
from finmind_client import (
    FetchError,
    fetch_eps,
    fetch_month_revenue,
    fetch_prices,
    get_universe,
)
from report import build_report
from signals import detect_eps_signals, detect_revenue_signals
from validation import group_by_horizon, split_is_oos

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


# ─────────────────────────────────────────────────────────────────────
# 價格提供者:記憶體內快取 PriceSeries(底層讀 cache/,必要時打 API)
# ─────────────────────────────────────────────────────────────────────
class PriceProvider:
    def __init__(self, cached_only: bool = False):
        self._mem: dict[str, PriceSeries | None] = {}
        self.quota_hit = False
        self.cached_only = cached_only  # True = 只讀 cache/,絕不打 API

    def get(self, stock_id: str) -> PriceSeries | None:
        if stock_id in self._mem:
            return self._mem[stock_id]
        # 只用磁碟快取:沒有就當作沒有(不打 API,方便與背景採集器並行)
        if self.cached_only and not cache_has(f"px_{stock_id}"):
            self._mem[stock_id] = None
            return None
        # 已在磁碟快取 → 直接用;否則(且尚未撞額度)才打 API
        if not cache_has(f"px_{stock_id}") and self.quota_hit:
            self._mem[stock_id] = None
            return None
        try:
            rows = fetch_prices(stock_id, P.PRICE_FETCH_START)
        except FetchError:
            self.quota_hit = True
            self._mem[stock_id] = None
            return None
        ps = PriceSeries(rows) if rows else None
        self._mem[stock_id] = ps
        return ps


def main():
    t0 = time.time()
    cached_only = "--cached-only" in sys.argv
    DATA.mkdir(exist_ok=True)
    universe = get_universe(P.UNIVERSE_SEED, P.UNIVERSE_SIZE)
    print(f"[run] 股票池 {len(universe)} 檔" + ("(cached-only 模式)" if cached_only else ""))

    # ── 1. 逐檔偵測訊號(需要月營收 + EPS 都已快取) ──────────────────
    rev_signals: list[dict] = []
    eps_signals: list[dict] = []
    n_have_rev = n_have_eps = 0
    for sid in universe:
        if cache_has(f"rev_{sid}"):
            rev = fetch_month_revenue(sid, P.REVENUE_FETCH_START)
            if rev:
                n_have_rev += 1
                rev_signals += detect_revenue_signals(sid, rev, P.REV_ACCEL_PP)
        if cache_has(f"eps_{sid}"):
            eps = fetch_eps(sid, P.EPS_FETCH_START)
            if eps:
                n_have_eps += 1
                eps_signals += detect_eps_signals(sid, eps, P.EPS_YOY_PCT)
    print(f"[run] 有月營收資料 {n_have_rev} 檔、有EPS資料 {n_have_eps} 檔")
    print(f"[run] 觸發事件:REV_ACCEL={len(rev_signals)}  EPS_SURGE={len(eps_signals)}")

    # ── 2. 預抓價格(盡力;觸發股 + 全池以供無偏對照) ────────────────
    provider = PriceProvider(cached_only=cached_only)
    bench = provider.get(P.BENCHMARK)
    if bench is None:
        raise SystemExit(f"[run] 無法取得基準 {P.BENCHMARK} 股價,無法計算超額報酬。"
                         + ("(cached-only:請先讓 harvest.py 抓到基準)" if cached_only else ""))

    triggered_ids = {s["stock_id"] for s in rev_signals + eps_signals}
    print(f"[run] 觸發個股 {len(triggered_ids)} 檔;預抓全池股價(對照組用)…")
    price_ok: list[str] = []
    for i, sid in enumerate(universe, 1):
        ps = provider.get(sid)
        if ps is not None and len(ps) > 0:
            price_ok.append(sid)
        if i % 100 == 0:
            print(f"[run]   價格 {i}/{len(universe)} (quota_hit={provider.quota_hit})")
    print(f"[run] 有股價可用 {len(price_ok)} 檔 (quota_hit={provider.quota_hit})")

    # ── 3. 訊號組:每事件 × 3/6/12 月 → 交易紀錄 ──────────────────────
    def build_records(signals: list[dict]) -> list[dict]:
        recs: list[dict] = []
        for sig in signals:
            ps = provider.get(sig["stock_id"])
            if ps is None or len(ps) == 0:
                continue
            for m in P.HOLDING_MONTHS:
                t = run_trade(ps, bench, sig["available_date"], m)
                if t is None:
                    continue
                rec = {**sig, **t}
                recs.append(rec)
        return recs

    rev_records = build_records(rev_signals)
    eps_records = build_records(eps_signals)
    all_records = rev_records + eps_records
    print(f"[run] 交易紀錄:REV={len(rev_records)} EPS={len(eps_records)} 全部={len(all_records)}")

    # ── 4. 對照組(逐 kind 對齊時間分佈) ────────────────────────────
    def control_for(signals: list[dict]) -> dict:
        return run_control(
            signals, P.HOLDING_MONTHS, provider.get, bench,
            price_ok, P.CONTROL_DRAWS_PER_SIGNAL, P.CONTROL_SEED,
        )

    print("[run] 跑對照組…")
    controls = {
        "REV_ACCEL": control_for(rev_signals),
        "EPS_SURGE": control_for(eps_signals),
        "ALL": control_for(rev_signals + eps_signals),
    }

    # ── 5. 彙整每個 kind 的指標(訊號 / 對照 / IS / OOS) ─────────────
    def kind_block(records: list[dict], control: dict, signals: list[dict]) -> dict:
        by_h = group_by_horizon(records, P.HOLDING_MONTHS)
        is_recs, oos_recs = split_is_oos(records, P.IS_OOS_SPLIT)
        by_h_is = group_by_horizon(is_recs, P.HOLDING_MONTHS)
        by_h_oos = group_by_horizon(oos_recs, P.HOLDING_MONTHS)
        return {
            "n_events": len(signals),
            "n_stocks": len({s["stock_id"] for s in signals}),
            "signal": {m: summarize(by_h[m]) for m in P.HOLDING_MONTHS},
            "control": {m: control[m]["summary"] for m in P.HOLDING_MONTHS},
            "is": {m: summarize(by_h_is[m]) for m in P.HOLDING_MONTHS},
            "oos": {m: summarize(by_h_oos[m]) for m in P.HOLDING_MONTHS},
        }

    kinds = {
        "REV_ACCEL": kind_block(rev_records, controls["REV_ACCEL"], rev_signals),
        "EPS_SURGE": kind_block(eps_records, controls["EPS_SURGE"], eps_signals),
        "ALL": kind_block(all_records, controls["ALL"], rev_signals + eps_signals),
    }

    # ── 6. 輸出樣本 CSV(所有交易紀錄:股票、觸發日、後續報酬) ───────
    samples_csv = DATA / "samples.csv"
    fields = ["stock_id", "kind", "period", "available_date", "metric", "yoy_now",
              "months", "entry_date", "exit_date", "entry_close", "exit_close",
              "stock_ret", "bench_ret", "excess_ret", "max_drawdown"]
    with samples_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in sorted(all_records, key=lambda x: (x["available_date"], x["stock_id"], x["months"])):
            w.writerow(r)
    print(f"[run] 已寫出所有樣本 → {samples_csv}")

    # ── 7. 產報告 ────────────────────────────────────────────────────
    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "universe_size": len(universe),
        "n_have_rev": n_have_rev,
        "n_have_eps": n_have_eps,
        "n_price_ok": len(price_ok),
        "quota_hit": provider.quota_hit,
        "benchmark": P.BENCHMARK,
        "params": {"REV_ACCEL_PP": P.REV_ACCEL_PP, "EPS_YOY_PCT": P.EPS_YOY_PCT},
        "study_start": P.STUDY_START, "study_end": P.STUDY_END,
        "is_oos_split": P.IS_OOS_SPLIT,
        "seed": P.UNIVERSE_SEED, "control_seed": P.CONTROL_SEED,
        "control_draws": P.CONTROL_DRAWS_PER_SIGNAL,
        "horizons": P.HOLDING_MONTHS,
    }
    report = build_report(meta, kinds, all_records)
    out = ROOT / "backtest_report.md"
    out.write_text(report, encoding="utf-8")
    print(f"[run] ✔ 報告 → {out}  (總耗時 {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
