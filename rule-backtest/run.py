"""
主流程 (run.py)
===============
對每一檔標的:
    載入資料 → 建 point-in-time 時間軸 → 跑 A / B / 買進持有 → 算指標 → 產報告

公平性設計(這份回測的成敗關鍵):
  A 與 B **必須在同一個起始日、同一份價格、同一組進場條件**下比較,
  差異才能歸因到「出場規則」。因此起始日取:
      max(PE 分位數暖身完成日, 基本面規則首次可評估日)
  在此之前不交易 —— 否則 B 會在「還沒有財報資料」的年代退化成 A,
  等於偷偷把兩者差異稀釋掉。

用法:
    python3 run.py            # 跑主結果 + 穩健性檢查,產出 rule_backtest.md
    python3 run.py --quick    # 只跑主結果(略過暖身期敏感度),開發時用
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import metrics
import params as P
import sources_tw as TW
import sources_us as US
import strategy
import timeline as TL
from prices import PriceSeries, fetch_prices
from report import write_report


def _first_date_with_threshold(rows: list[dict]) -> str | None:
    for r in rows:
        if r["pe_entry_thr"] is not None:
            return r["date"]
    return None


def _fundamentals_ready_date(qflags: list[dict]) -> str | None:
    for q in qflags:
        if q["computable"]:
            return q["available_date"]
    return None


def load_stock(stock: dict) -> dict:
    """載入單一標的的所有原料 + 資料範圍中繼資料。"""
    px_rows = fetch_prices(stock["yf"])
    px = PriceSeries(px_rows)
    meta = {
        "price_start": px_rows[0]["date"],
        "price_end": px_rows[-1]["date"],
        "price_days": len(px_rows),
    }

    if stock["market"] == "TW":
        quarters = TW.quarterly_fundamentals_tw(stock["code"])
        pe_daily = TW.fetch_pe_daily_tw(stock["code"])
        ttm_events = None
        eps_field = "eps"
        meta.update({
            "pe_source": "TWSE 個股日本益比(BWIBBU)— 交易所官方 trailing PE",
            "pe_start": pe_daily[0]["date"] if pe_daily else None,
            "pe_end": pe_daily[-1]["date"] if pe_daily else None,
            "pe_days": len(pe_daily),
            "fs_source": "FinMind 綜合損益表(季)",
            "eps_basis": "實際 EPS(台股免費資料源無分析師共識,依題目指定改用實際 EPS 連兩季年減)",
            "has_consensus": False,
        })
    else:
        quarters = US.quarterly_fundamentals_us(stock["yf"], stock["cik"])
        pe_daily = None
        ttm_events = TL.ttm_eps_events(quarters)
        eps_field = "eps_consensus"
        n_cons = sum(1 for q in quarters if q.get("eps_consensus") is not None)
        n_gm = sum(1 for q in quarters if q.get("gross_margin") is not None)
        meta.update({
            "pe_source": "自算 trailing PE = 未還原收盤價 / 近四季實際 EPS(adjusted 口徑)",
            "pe_start": ttm_events[0]["available_date"] if ttm_events else None,
            "pe_end": ttm_events[-1]["available_date"] if ttm_events else None,
            "pe_days": None,
            "fs_source": "EPS/共識:yfinance 財報日;毛利率:SEC EDGAR XBRL(Q4 由全年減前三季推導)",
            "eps_basis": f"共識 EPS(可得 {n_cons} 季),毛利率可得 {n_gm} 季",
            "has_consensus": True,
        })

    meta.update({
        "fs_start": quarters[0]["quarter_end"] if quarters else None,
        "fs_end": quarters[-1]["quarter_end"] if quarters else None,
        "fs_quarters": len(quarters),
    })
    return {"px": px, "quarters": quarters, "pe_daily": pe_daily,
            "ttm_events": ttm_events, "eps_field": eps_field, "meta": meta}


def build_rows(data: dict, warmup: int, eps_field: str | None = None) -> tuple[list[dict], str | None]:
    """建時間軸並套用共同起始日;回傳 (rows, analysis_start)。"""
    qflags = TL.build_quarter_flags(data["quarters"], eps_field or data["eps_field"])
    rows = TL.build_timeline(
        data["px"], qflags,
        pe_daily=data["pe_daily"], ttm_events=data["ttm_events"], warmup=warmup,
    )
    warm_date = _first_date_with_threshold(rows)
    fund_date = _fundamentals_ready_date(qflags)
    if warm_date is None or fund_date is None:
        return rows, None
    start = max(warm_date, fund_date)
    for r in rows:               # 起始日之前一律不可交易(A/B 共同起跑線)
        if r["date"] < start:
            r["tradable"] = False
    return rows, start


def _pe_baseline_diag(rows: list[dict], start: str) -> dict:
    """診斷「自身歷史 PE 基準」到底可不可信 —— 這是策略 A/B 共同的前提假設。

    對長期虧損、剛轉盈的公司(如 TSLA),暖身期的 PE 樣本可能全部來自
    「EPS 趨近於 0」的期間 → PE 數百倍,算出來的中位數/百分位根本不是估值基準。
    把樣本組成攤開來讓讀者自己判斷,不要讓垃圾輸入悄悄變成結論。
    """
    import statistics

    pre = [r["pe"] for r in rows if r["pe"] is not None and r["date"] < start]
    period = [r for r in rows if r["date"] >= start]
    valid = [r for r in period if r["pe"] is not None]
    first_valid = next((r["date"] for r in rows if r["pe"] is not None), None)
    return {
        "warmup_n": len(pre),
        "warmup_pe_min": round(min(pre), 1) if pre else None,
        "warmup_pe_max": round(max(pre), 1) if pre else None,
        "warmup_pe_median": round(statistics.median(pre), 1) if pre else None,
        "first_valid_pe_date": first_valid,
        "period_days": len(period),
        "period_valid_pe_days": len(valid),
        "period_no_pe_pct": (1 - len(valid) / len(period)) if period else None,
    }


def run_one(stock: dict, data: dict, warmup: int, eps_field: str | None = None) -> dict:
    rows, start = build_rows(data, warmup, eps_field)
    if start is None:
        return {"error": "資料不足以建立共同起始日(PE 或財報歷史太短)"}

    market = stock["market"]
    res_a = strategy.run_strategy(rows, use_fundamental_exit=False,
                                  use_fundamental_entry_filter=False, market=market)
    res_b = strategy.run_strategy(rows, use_fundamental_exit=True,
                                  use_fundamental_entry_filter=True, market=market)
    # 字面版 B:進場完全同 A(不擋惡化)→ 用來證明賣出訊號會被隔天買回抵銷
    res_b_lit = strategy.run_strategy(rows, use_fundamental_exit=True,
                                      use_fundamental_entry_filter=False, market=market)
    bh_idx = res_a.get("first_tradable_idx") or 0
    res_bh = strategy.run_buy_and_hold(rows, bh_idx, market)

    return {
        "analysis_start": start,
        "analysis_end": rows[-1]["date"],
        "rows_n": len(rows),
        "pe_diag": _pe_baseline_diag(rows, start),
        "A": {"result": res_a, "summary_net": metrics.summarize(res_a, use_net=True),
              "summary_gross": metrics.summarize(res_a, use_net=False),
              "worst": metrics.worst_trade_detail(res_a)},
        "B": {"result": res_b, "summary_net": metrics.summarize(res_b, use_net=True),
              "summary_gross": metrics.summarize(res_b, use_net=False),
              "worst": metrics.worst_trade_detail(res_b)},
        "B_literal": {"result": res_b_lit, "summary_net": metrics.summarize(res_b_lit, use_net=True)},
        "BH": {"result": res_bh, "summary_net": metrics.summarize(res_bh, use_net=True),
               "summary_gross": metrics.summarize(res_bh, use_net=False)},
    }


def main() -> int:
    quick = "--quick" in sys.argv
    out: dict = {"stocks": [], "params": {
        "entry_pctl": P.ENTRY_PCTL, "exit_pctl": P.EXIT_PCTL,
        "deterioration_quarters": P.DETERIORATION_QUARTERS,
        "warmup": P.WARMUP_TRADING_DAYS, "lag": P.EXECUTION_LAG_DAYS,
        "costs": P.COSTS,
    }}

    for stock in P.UNIVERSE:
        print(f"── {stock['code']} {stock['name']} ──")
        try:
            data = load_stock(stock)
        except Exception as e:  # noqa: BLE001
            print(f"   資料載入失敗:{e}")
            out["stocks"].append({**stock, "error": str(e)})
            continue

        entry = {**stock, "meta": data["meta"]}
        main_res = run_one(stock, data, P.WARMUP_TRADING_DAYS)
        entry["main"] = main_res
        # 季度基本面軌跡(給報告做案例佐證用:讓讀者看到訊號當下的實際數字)
        entry["quarters"] = TL.build_quarter_flags(data["quarters"], data["eps_field"])
        if "error" in main_res:
            print(f"   {main_res['error']}")
            out["stocks"].append(entry)
            continue

        print(f"   期間 {main_res['analysis_start']} ~ {main_res['analysis_end']}  "
              f"A:{main_res['A']['summary_net']['n_trades']} 筆 / "
              f"B:{main_res['B']['summary_net']['n_trades']} 筆")

        # 穩健性一:暖身期敏感度(1 年 / 3 年),照實併陳,不挑好看的
        if not quick:
            sens = {}
            for w in P.WARMUP_SENSITIVITY:
                r = run_one(stock, data, w)
                if "error" not in r:
                    sens[w] = {
                        "start": r["analysis_start"],
                        "A": r["A"]["summary_net"],
                        "B": r["B"]["summary_net"],
                    }
            entry["warmup_sensitivity"] = sens

        # 穩健性二:美股用「實際 EPS」取代「共識 EPS」當基本面訊號(與台股同口徑)
        if stock["market"] == "US":
            r = run_one(stock, data, P.WARMUP_TRADING_DAYS, eps_field="eps")
            if "error" not in r:
                entry["eps_variant"] = {"A": r["A"]["summary_net"], "B": r["B"]["summary_net"],
                                        "start": r["analysis_start"]}

        out["stocks"].append(entry)

    # 存原始結果(可複核)
    data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(exist_ok=True)

    def _clean(o):
        """存檔前把逐日淨值曲線拿掉(太大),只留交易明細與指標。"""
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()
                    if k not in ("curve_gross", "curve_net", "dates")}
        if isinstance(o, list):
            return [_clean(x) for x in o]
        return o

    (data_dir / "results.json").write_text(
        json.dumps(_clean(out), ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    write_report(out)
    print("\n完成 → rule_backtest.md(逐筆交易另存 data/trades.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
