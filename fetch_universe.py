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
    fetch_month_revenue,
    fetch_price_daily_finmind,
    month_revenue_momentum,
)
from src.river import current_trailing_pe, daily_pe_series, supports_tw_filing_fallback
from src.screener import extract_metrics, load_config
from src.us_data import build_us_record, compute_valuation
from src.valuation_flag import (historical_peg, pe_history_is_compatible,
                                pe_history_stats, pe_source_regressed,
                                tw_pe_source_coverage)

ROOT = Path(__file__).resolve().parent
UNIVERSE_DIR = ROOT / "data/universe"
_RATE_HINTS = ("limit", "402", "free", "requests", "request", "402")


def _bust_cache(stock_id: str, mode: str) -> None:
    """依 refresh 模式刪掉相關快取,強制重抓。
    prices=只刪股價/yfinance(日更新);all=連財報都刪(週更新)。"""
    from src.cache import CACHE_DIR
    price_keys = [f"finmind_price_{stock_id}", f"finmind_pxv_{stock_id}",
                  f"yf_metrics_{stock_id}.TW", f"yf_cov_{stock_id}.TW",
                  f"yf_metrics_{stock_id}", f"yf_cov_{stock_id}"]  # 後兩個給美股 ticker
    fin_keys = [f"finmind_fs_long_{stock_id}", f"finmind_bs_{stock_id}", f"finmind_cf_{stock_id}"]
    keys = price_keys if mode == "prices" else price_keys + fin_keys
    for k in keys:
        f = CACHE_DIR / f"{k}.json"
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass


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


def load_from_universe(cfg: dict) -> list[dict]:
    """改讀『可分析母體』config/universe.yaml 當清單(build_universe.py 產出)。

    台股會再從 taiwan_stock_info 補回產業別(篩選器的負債門檻需要)。
    ★ 同樣支援 --stock-ids / --limit 過濾(否則指定代號會被無視,整份母體被重抓)。
    """
    import yaml
    path = ROOT / "config/universe.yaml"
    if not path.exists():
        raise SystemExit("找不到 config/universe.yaml,請先執行 python build_universe.py --market tw")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    market = cfg["universe"]["market"]
    items = doc.get(market) or (doc.get("twse") if market == "twse" else doc.get("us")) or []

    # --stock-ids:只留指定代號(順序依指定);--limit:只取前 N 檔
    only = [str(x).strip() for x in (cfg["universe"].get("stock_ids") or []) if str(x).strip()]
    if only:
        by_id = {str(s["stock_id"]): s for s in items}
        items = [by_id[i] for i in only if i in by_id]
    lim = cfg["universe"].get("limit") or 0
    if lim:
        items = items[:lim]

    if market == "twse":
        info = _finmind_loader().taiwan_stock_info()
        ind = {str(r["stock_id"]): str(r["industry_category"]) for _, r in info.iterrows()}
        return [{"stock_id": str(s["stock_id"]), "name": s.get("name", s["stock_id"]),
                 "industry": ind.get(str(s["stock_id"]), "")} for s in items]
    return [{"stock_id": s["stock_id"], "name": s.get("name", s["stock_id"]), "industry": ""}
            for s in items]


def _universe_doc() -> dict:
    """讀取母體產物。集中在一處，避免台股/美股/清檔各自解讀不同。"""
    import yaml

    path = ROOT / "config/universe.yaml"
    if not path.exists():
        raise SystemExit("找不到 config/universe.yaml,請先執行 python build_universe.py --market tw")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _sync_universe_files(expected: set[str]) -> list[str]:
    """移除不在母體的舊 JSON，回傳被移除的代號。

    只由完整的 --from-universe 執行呼叫；測試用 --limit/--stock-ids 絕不能清檔。
    這使 data/universe 不再累積已被母體淘汰的股票，避免 screen.py 繼續評估舊檔。
    """
    removed: list[str] = []
    if not UNIVERSE_DIR.exists():
        return removed
    for p in sorted(UNIVERSE_DIR.glob("*.json")):
        if p.stem not in expected:
            p.unlink()
            removed.append(p.stem)
    return removed


def _fresh(path: Path, days: int, pe_years: int = 5) -> bool:
    if not path.exists():
        return False
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        # schema 變更也視為不新鮮。目前歷史位階必須保存 trailing 口徑；
        # 舊檔的 percentile 是 forward 對 trailing，不能因 fetched 日期新就沿用。
        ph = rec.get("pe_hist") or {}
        if rec.get("pe_refresh_error") or not pe_history_is_compatible(
                ph, rec.get("market", "twse"), rec.get("price_date"), pe_years):
            return False
        f = rec.get("fetched")
        return f is not None and (date.today() - date.fromisoformat(f)).days <= days
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def _tw_pe_source_error(price_rows: list[dict], income: dict,
                        current_date: str | None, years: int) -> str | None:
    """Reject malformed/truncated FinMind inputs before honest insufficiency handling."""
    coverage = tw_pe_source_coverage(price_rows, income, years)
    if coverage["price_n"] < 60:
        return "price_history_truncated"
    if not coverage["eps_n"]:
        return "income_eps_invalid"

    as_of = date.fromisoformat(current_date or coverage["price_end"])
    if date.fromisoformat(coverage["price_end"]) < as_of - timedelta(days=7):
        return "price_history_stale"
    try:
        cutoff = as_of.replace(year=as_of.year - years)
    except ValueError:
        cutoff = as_of.replace(year=as_of.year - years, day=28)
    price_start = date.fromisoformat(coverage["price_start"])
    eps_start = date.fromisoformat(coverage["eps_start"])
    if price_start <= cutoff - timedelta(days=365) and eps_start > cutoff + timedelta(days=270):
        return "income_history_truncated"
    return None


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
    inc = None

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
            # 歷史PEG:不依賴分析師共識(無覆蓋股也算得出),口徑與前瞻PEG 不同,分開存
            try:
                rec["hist_peg"] = historical_peg(
                    rec.get("annual") or {}, rec.get("price_last"),
                    years=cfg["valuation_flag"].get("pe_history_years", 5))
            except Exception as e:  # noqa: BLE001
                errors.append(f"hist_peg:{e}")
            if inc:
                try:
                    px_long = fetch_price_daily_finmind(sid)[0]      # ~10 年日收盤(有快取)
                    latest = max(px_long, key=lambda x: x["date"])
                    if not rec.get("price_date") or latest["date"] > rec["price_date"]:
                        rec["price_last"], rec["price_date"] = latest["close"], latest["date"]
                    source_error = _tw_pe_source_error(
                        px_long, inc[0], rec.get("price_date"),
                        cfg["valuation_flag"]["pe_history_years"])
                    if source_error:
                        raise ValueError(source_error)
                    fallback_ok = supports_tw_filing_fallback(stock["name"])
                    pe_ser = daily_pe_series(px_long, inc[0], fallback_ok)
                    current_tpe, current_date = current_trailing_pe(
                        px_long, inc[0], fallback_ok, rec.get("price_last"), rec.get("price_date"))
                    reason = None if fallback_ok else "unsupported_foreign_issuer_filing_deadline"
                    rec["pe_hist"] = pe_history_stats(
                        pe_ser, current_tpe, years=cfg["valuation_flag"]["pe_history_years"],
                        current_date=current_date, market="twse", insufficient_reason=reason,
                        source_coverage=tw_pe_source_coverage(
                            px_long, inc[0], cfg["valuation_flag"]["pe_history_years"]))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"pe_hist:{e}")
                    rec["pe_refresh_error"] = f"calculation_error:{type(e).__name__}"

        # --- 月營收動能(台股每月10日前公告;不依賴分析師覆蓋,近全市場都有)---
        if cfg["fetch"].get("month_revenue", True):
            try:
                mrows = fetch_month_revenue(sid, start_date=cfg["fetch"].get(
                    "month_revenue_start", "2021-01-01"))[0]
                rec["mrev"] = month_revenue_momentum(
                    mrows, recent=cfg["fetch"].get("month_revenue_recent", 3))
            except Exception as e:  # noqa: BLE001
                errors.append(f"month_revenue:{e}")
    else:
        rec["skipped_financials"] = True

    if cfg["fetch"].get("valuation", True) and "pe_hist" not in rec:
        if not liquid and rec.get("price_date"):
            rec["pe_hist"] = pe_history_stats(
                [], None, years=cfg["valuation_flag"]["pe_history_years"],
                current_date=rec["price_date"], market="twse",
                insufficient_reason="financials_not_fetched",
                source_coverage={"price_start": rec["price_date"],
                                 "price_end": rec["price_date"], "price_n": rec.get("liq_days", 0),
                                 "eps_start": None, "eps_end": None, "eps_n": 0})
        else:
            rec.setdefault("pe_refresh_error",
                           "price_fetch_error" if not rec.get("price_date") else "income_fetch_error")

    _save(rec)
    return rec


def _save(rec: dict) -> None:
    """寫入紀錄,但**絕不用殘缺資料覆蓋既有的完整資料**。

    為什麼需要這個保護:build_and_save 對每個區塊都是 best-effort —— 抓不到就記進
    errors 繼續跑。若當次遇到 FinMind 額度用盡(雲端 CI 首次執行沒有本地快取時很容易發生),
    財報區塊會整批抓不到,rec 就少了 annual/annual_bs/… ,直接寫檔會把 repo 裡
    原本完整的 240 檔資料**洗成殘缺**,而且不會報錯 —— 這正是先前 TWSE 空快取那類
    「靜默資料損壞」的翻版。
    作法:逐區塊比對,新的抓不到就沿用舊值,並在 errors 註記「沿用前次資料」。
    """
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
    path = UNIVERSE_DIR / f"{rec['stock_id']}.json"

    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = None
        if isinstance(old, dict):
            new_ph = rec.get("pe_hist") or {}
            old_ph = old.get("pe_hist") or {}
            if not rec.get("pe_refresh_error") and pe_source_regressed(old_ph, new_ph):
                rec["pe_refresh_error"] = "unexpected_pe_history_regression"
                rec.pop("pe_hist", None)
            if rec.get("pe_refresh_error"):
                rec.pop("pe_hist", None)            # preserve old snapshot, but block commit/deploy
            kept = []
            # 這些區塊「有比沒有好」:新的缺、舊的有 → 保留舊的
            for k in ("annual", "annual_bs", "annual_ocf", "latest_bs", "ocf_q",
                      "pe_hist", "valuation", "hist_peg", "mrev",
                      "first_report", "latest_report",
                      "price_last", "price_date", "liq_avg_value", "liq_days"):
                if not rec.get(k) and old.get(k):
                    rec[k] = old[k]
                    kept.append(k)
            if kept:
                rec.setdefault("errors", []).append(
                    "本次抓取缺漏,沿用前次資料:" + "、".join(kept))
                rec["partial_update"] = True

    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def run(args) -> None:
    _load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    # CLI 覆寫
    if args.limit:
        cfg["universe"]["limit"] = args.limit
    if args.stock_ids:
        cfg["universe"]["stock_ids"] = args.stock_ids.split(",")

    stocks = load_from_universe(cfg) if args.from_universe else load_stock_list(cfg)
    src = "母體 universe.yaml" if args.from_universe else "全市場"
    print(f"觀察宇宙:{cfg['universe']['market']} 共 {len(stocks)} 檔"
          f"(來源 {src};token={'有' if os.getenv('FINMIND_TOKEN') else '匿名'})")
    refetch_days = cfg["fetch"].get("refetch_after_days", 3)
    sleep_s = cfg["fetch"].get("sleep_seconds", 0.6)

    done = skipped = liquid_deep = 0
    for i, s in enumerate(stocks, 1):
        sid = s["stock_id"]
        path = UNIVERSE_DIR / f"{sid}.json"
        if args.refresh:
            _bust_cache(sid, args.refresh)          # 強制重抓(日=只股價/yf,週=連財報)
        elif _fresh(path, refetch_days, cfg["valuation_flag"]["pe_history_years"]):
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

    # ---- 額外美股(yfinance)----
    # --from-universe 時以 config/universe.yaml 的 us 清單為唯一真相；
    # 非母體模式才沿用 screener.yaml 的 extra_us 測試清單。
    if args.from_universe:
        us = [str(x["stock_id"]) for x in (_universe_doc().get("us") or [])]
    else:
        us = [str(x) for x in (cfg["universe"].get("extra_us") or [])]
    if us:
        print(f"美股測試({len(us)} 檔,yfinance):")
        for j, ticker in enumerate(us, 1):
            path = UNIVERSE_DIR / f"{ticker}.json"
            if args.refresh:
                _bust_cache(str(ticker), args.refresh)
            elif _fresh(path, refetch_days, cfg["valuation_flag"]["pe_history_years"]):
                print(f"  [{j}/{len(us)}] {ticker} 沿用本地")
                continue
            rec = build_us_record(str(ticker), str(ticker), cfg)
            _save(rec)
            val = (rec.get("valuation") or {}).get("forward_pe")
            print(f"  [{j}/{len(us)}] {ticker}（{rec.get('industry','')}）"
                  + (f"　前瞻PE {val:.0f}x" if val else "")
                  + (f"　! {len(rec['errors'])} err" if rec["errors"] else ""))
            time.sleep(sleep_s)

    # 完整母體執行才同步刪除舊檔。先抓後刪，避免抓取中斷時先破壞既有資料。
    if args.from_universe and not args.stock_ids and not args.limit:
        doc = _universe_doc()
        expected = {str(x["stock_id"]) for k in ("twse", "us") for x in (doc.get(k) or [])}
        removed = _sync_universe_files(expected)
        if removed:
            print(f"母體清理:移除 {len(removed)} 個已不在 universe.yaml 的舊檔:{','.join(removed)}")


def main() -> None:
    p = argparse.ArgumentParser(description="台股全市場資料抓取(存本地,供選股篩選器)")
    p.add_argument("--config", default=str(ROOT / "config/screener.yaml"))
    p.add_argument("--limit", type=int, default=0, help="只抓前 N 檔(測試)")
    p.add_argument("--stock-ids", default="", help="只抓指定代號,逗號分隔(測試)")
    p.add_argument("--from-universe", action="store_true",
                   help="改讀 config/universe.yaml(可分析母體)當清單,而非全市場")
    p.add_argument("--refresh", choices=["", "prices", "all"], default="",
                   help="強制重抓:prices=只股價+yfinance(日更新);all=連財報(週更新)")
    run(p.parse_args())


if __name__ == "__main__":
    main()
