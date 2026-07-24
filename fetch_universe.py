"""
全市場資料抓取 (fetch_universe.py)
==================================
把台股(預設上市 twse)全市場的財務/股價「一次抓好、存本地」,供 screener 反覆讀取。
FinMind 免費版有頻率限制且不能一次抓全市場,所以這裡是「逐檔抓 + 檔案快取 + 可續跑」。

用法:
    python fetch_universe.py                      # 依 config/screener.yaml 抓全市場
    python fetch_universe.py --limit 30           # 只抓前 30 檔(測試)
    python fetch_universe.py --stock-ids 2330,2454 # 只抓指定幾檔

特性:
    - 可續跑:本地 data/universe/<代號>.json 若在 refetch_after_days 內就跳過。
    - 省請求:deep_fetch_only_liquid=true 時,未通過流動性門檻者不深抓財報。
    - 禮貌節流:每檔之間 sleep;遇疑似限流訊息會暫停後重試一次。
    - 一檔失敗不中斷:錯誤記進該檔 json 的 errors,繼續下一檔。

★ 只抓公開市場數據,無持倉/交易紀錄。首次全量較久(免費額度下可能數小時,
  建議設 FINMIND_TOKEN 提高額度);之後 screener 讀本地,毋須再連網。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

from src.data_layer import (
    _finmind_loader,
    fetch_balance_pivot,
    fetch_cashflow_pivot,
    fetch_daily_price_value,
    fetch_income_pivot,
    fetch_price_daily_finmind,
)
from src.river import daily_pe_series
from src.screener import extract_metrics, load_config
from src.us_data import build_us_record, compute_valuation
from src.valuation_flag import pe_history_stats

ROOT = Path(__file__).resolve().parent
UNIVERSE_DIR = ROOT / "data/universe"
_RATE_HINTS = ("limit", "402", "free", "requests", "request", "402")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_stock_list(cfg: dict) -> list[dict]:
    """由 FinMind taiwan_stock_info 取清單,依 config 過濾市場/普通股/指定/限量。"""
    import re

    dl = _finmind_loader()
    info = dl.taiwan_stock_info()
    market = cfg["universe"]["market"]
    only_common = cfg["universe"].get("only_common_stock", True)
    seen: dict[str, dict] = {}
    for _, r in info.iterrows():
        if str(r["type"]) != market:
            continue
        sid = str(r["stock_id"]).strip()
        if only_common and not re.fullmatch(r"[1-9]\d{3}", sid):  # 4碼普通股(排 ETF 00xx/權證)
            continue
        if sid in seen:
            continue
        seen[sid] = {"stock_id": sid, "name": str(r["stock_name"]),
                     "industry": str(r["industry_category"])}
    lst = list(seen.values())
    ids = [str(x) for x in (cfg["universe"].get("stock_ids") or [])]
    if ids:
        lst = [s for s in lst if s["stock_id"] in set(ids)]
    limit = cfg["universe"].get("limit") or 0
    if limit > 0:
        lst = lst[:limit]
    return lst


def _fresh(path: Path, days: int) -> bool:
    if not path.exists():
        return False
    try:
        f = json.loads(path.read_text(encoding="utf-8")).get("fetched")
        return f is not None and (date.today() - date.fromisoformat(f)).days <= days
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def _retry(fn, cfg, tag, errors):
    """呼叫 fn();遇疑似限流訊息暫停一次再試,其它錯誤記錄後回 None。"""
    for attempt in (1, 2):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if attempt == 1 and any(h in msg for h in _RATE_HINTS):
                pause = cfg["fetch"].get("rate_limit_pause_seconds", 90)
                print(f"      ! 疑似限流({tag}),暫停 {pause}s 後重試…")
                time.sleep(pause)
                continue
            errors.append(f"{tag}:{e}")
            return None


def build_and_save(stock: dict, cfg: dict) -> dict:
    sid = stock["stock_id"]
    rec: dict = {"stock_id": sid, "name": stock["name"], "industry": stock["industry"],
                 "market": cfg["universe"]["market"], "currency": "TWD",
                 "fetched": date.today().isoformat(), "errors": []}
    errors = rec["errors"]

    # --- 流動性(近 N 日均成交金額)---
    look = (date.today() - timedelta(days=cfg["fetch"]["price_lookback_days"])).isoformat()
    days = cfg["layer1"]["liquidity"]["days"]
    liquid = True
    pv = _retry(lambda: fetch_daily_price_value(sid, start_date=look), cfg, "price", errors)
    if pv:
        rows = sorted(pv[0], key=lambda x: x["date"])
        last = rows[-days:]
        rec["liq_avg_value"] = (sum(r["value"] for r in last) / len(last)) if last else None
        rec["liq_days"] = len(last)
        rec["price_last"] = rows[-1]["close"]
        rec["price_date"] = rows[-1]["date"]
        if (cfg["fetch"].get("deep_fetch_only_liquid", True)
                and (rec["liq_avg_value"] or 0) <= cfg["layer1"]["liquidity"]["min_avg_value"]):
            liquid = False

    # --- 財報(不夠流動就不深抓,省請求)---
    if liquid:
        start = cfg["fetch"]["financial_start"]
        inc = _retry(lambda: fetch_income_pivot(sid, start_date=start), cfg, "income", errors)
        bal = _retry(lambda: fetch_balance_pivot(sid, start_date=start), cfg, "balance", errors)
        cf = _retry(lambda: fetch_cashflow_pivot(sid, start_date=start), cfg, "cashflow", errors)
        if inc:
            rec.update(extract_metrics(inc[0], bal[0] if bal else {}, cf[0] if cf else {}))
        # 估值檢查(僅參考;yfinance,best-effort)+ 估值旗標用的個股近N年PE分布
        if cfg["fetch"].get("valuation", True):
            rec["valuation"] = compute_valuation(f"{sid}.TW", rec.get("price_last"))
            if inc:
                try:
                    px_long = fetch_price_daily_finmind(sid)[0]      # ~10 年日收盤(有快取)
                    pe_ser = daily_pe_series(px_long, inc[0])
                    fpe = (rec.get("valuation") or {}).get("forward_pe")
                    rec["pe_hist"] = pe_history_stats(
                        pe_ser, fpe, years=cfg["valuation_flag"]["pe_history_years"])
                except Exception as e:  # noqa: BLE001
                    errors.append(f"pe_hist:{e}")
    else:
        rec["skipped_financials"] = True

    _save(rec)
    return rec


def _save(rec: dict) -> None:
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
    (UNIVERSE_DIR / f"{rec['stock_id']}.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def run(args) -> None:
    _load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    # CLI 覆寫
    if args.limit:
        cfg["universe"]["limit"] = args.limit
    if args.stock_ids:
        cfg["universe"]["stock_ids"] = args.stock_ids.split(",")

    stocks = load_stock_list(cfg)
    print(f"觀察宇宙:{cfg['universe']['market']} 共 {len(stocks)} 檔"
          f"(token={'有' if os.getenv('FINMIND_TOKEN') else '匿名'})")
    refetch_days = cfg["fetch"].get("refetch_after_days", 3)
    sleep_s = cfg["fetch"].get("sleep_seconds", 0.6)

    done = skipped = liquid_deep = 0
    for i, s in enumerate(stocks, 1):
        path = UNIVERSE_DIR / f"{s['stock_id']}.json"
        if _fresh(path, refetch_days):
            skipped += 1
            continue
        rec = build_and_save(s, cfg)
        done += 1
        if not rec.get("skipped_financials") and "annual" in rec:
            liquid_deep += 1
        tag = "深抓" if not rec.get("skipped_financials") else "僅流動性"
        liq = rec.get("liq_avg_value")
        print(f"[{i}/{len(stocks)}] {s['stock_id']} {s['name']}"
              f"（{tag}；均額 {liq/1e8:,.2f}億）" if liq else
              f"[{i}/{len(stocks)}] {s['stock_id']} {s['name']}（{tag}）"
              + (f"　! {len(rec['errors'])} err" if rec["errors"] else ""))
        time.sleep(sleep_s)

    print("─" * 56)
    print(f"完成:新抓 {done}、沿用本地 {skipped}、其中深抓財報 {liquid_deep};"
          f"本地資料夾 {UNIVERSE_DIR}")

    # ---- 額外美股(yfinance,測試用)----
    us = cfg["universe"].get("extra_us") or []
    if us:
        print(f"美股測試({len(us)} 檔,yfinance):")
        for j, ticker in enumerate(us, 1):
            path = UNIVERSE_DIR / f"{ticker}.json"
            if _fresh(path, refetch_days):
                print(f"  [{j}/{len(us)}] {ticker} 沿用本地")
                continue
            rec = build_us_record(str(ticker), str(ticker), cfg)
            _save(rec)
            val = (rec.get("valuation") or {}).get("forward_pe")
            print(f"  [{j}/{len(us)}] {ticker}（{rec.get('industry','')}）"
                  + (f"　前瞻PE {val:.0f}x" if val else "")
                  + (f"　! {len(rec['errors'])} err" if rec["errors"] else ""))
            time.sleep(sleep_s)


def main() -> None:
    p = argparse.ArgumentParser(description="台股全市場資料抓取(存本地,供選股篩選器)")
    p.add_argument("--config", default=str(ROOT / "config/screener.yaml"))
    p.add_argument("--limit", type=int, default=0, help="只抓前 N 檔(測試)")
    p.add_argument("--stock-ids", default="", help="只抓指定代號,逗號分隔(測試)")
    run(p.parse_args())


if __name__ == "__main__":
    main()
