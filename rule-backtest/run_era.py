"""
時代穩健性主流程 (run_era.py)
=============================
回答:**A/B/買進持有的結論,是不是只是「成長股大時代」的產物?**

作法:
  1. 樣本擴大到 21 檔,刻意混入 成長 / 成熟 / 循環 / 美股(含 INTC 這種成長變衰退的)
  2. 同一次回測的淨值曲線,依 2008~2016 / 2017~2026 切段分別結算(不分開重跑,持倉自然延續)
  3. 加上等權「組合層級」視角:每日把資金等分給有部位的標的,對照全程等權買進持有

用法:
    .venv/bin/python run_era.py          # 跑完整流程,產出 era_robustness.md
    .venv/bin/python run_era.py --limit 5  # 只跑前 5 檔(開發用)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import era as ERA
import metrics
import params as P
import strategy
import timeline as TL
from report_era import write_era_report
from run import build_rows, load_stock

STRATS = ("A", "B", "BH")


def _run_stock(stock: dict) -> dict | None:
    """跑單一標的的 A/B/BH,回傳含逐日曲線的結果(供組合層級使用)。"""
    data = load_stock(stock)
    rows, start = build_rows(data, P.WARMUP_TRADING_DAYS)
    if start is None:
        return {"error": "資料不足以建立共同起始日(PE 或財報歷史太短)"}

    market = stock["market"]
    res = {
        "A": strategy.run_strategy(rows, use_fundamental_exit=False,
                                   use_fundamental_entry_filter=False, market=market),
        "B": strategy.run_strategy(rows, use_fundamental_exit=True,
                                   use_fundamental_entry_filter=True, market=market),
    }
    bh_idx = res["A"].get("first_tradable_idx") or 0
    res["BH"] = strategy.run_buy_and_hold(rows, bh_idx, market)

    out = {
        "analysis_start": start,
        "analysis_end": rows[-1]["date"],
        "meta": data["meta"],
        "full": {},
        "eras": {},
        "curves": {},
    }
    # ---- 資料完整性:回測期間有多少比例的日子「有有效 PE」----
    # 踩過坑之後加的檢查:TWSE 抓取失敗曾被靜默寫成「該月無 PE」,
    # 使某些股票的 PE 歷史殘缺卻照樣跑出漂亮的回測數字。
    # 覆蓋率低 = 分位數基準不可靠,報告必須攤開它,而不是讓它悄悄影響結論。
    period = [r for r in rows if r["date"] >= start]
    n_pe = sum(1 for r in period if r["pe"] is not None)
    out["pe_coverage"] = (n_pe / len(period)) if period else None
    out["pe_days_in_period"] = n_pe
    out["period_days"] = len(period)
    # 毛利率條件是否對本業別不適用(金融股沒有毛利率概念)→ 報告要標明 B 是降級版
    qf = TL.build_quarter_flags(data["quarters"], data["eps_field"])
    out["gm_not_applicable"] = bool(qf and qf[0].get("gm_not_applicable"))

    for k in STRATS:
        r = res[k]
        out["full"][k] = metrics.summarize(r, use_net=True)
        out["eras"][k] = ERA.slice_all_eras(r, use_net=True)
        s0 = r.get("first_tradable_idx") or 0
        out["curves"][k] = {
            "dates": r["dates"], "curve": r["curve_net"],
            "active": r.get("active"), "first_tradable_idx": s0,
        }

    # B 的「基本面出場」實際被觸發幾次 —— 若為 0,代表 B 與 A 的差異其實
    # 來自「惡化期間不進場」的過濾,而不是賣出訊號本身。這點必須讓報告講清楚。
    fund = [t for t in res["B"]["trades"]
            if any(x in ("EPS_DOWN", "GM_DOWN") for x in (t.get("exit_reasons") or []))]
    out["b_fund_exits"] = len(fund)
    out["b_fund_exit_detail"] = [
        {"entry_date": t.get("entry_date"), "exit_date": t.get("exit_date"),
         "reasons": t.get("exit_reasons"), "ret_net": t.get("ret_net"),
         "quarter": t.get("exit_quarter")}
        for t in fund
    ]
    return out


def _portfolio_block(per_stock_curves: list[dict]) -> dict:
    """對一組標的算等權組合:全期 + 各時代。"""
    pf = ERA.portfolio_equal_weight(per_stock_curves)
    out = {"full": ERA.portfolio_summary(pf), "eras": {}, "n_stocks": len(per_stock_curves)}
    for e in P.ERAS:
        out["eras"][e["key"]] = ERA.portfolio_summary(pf, e)
    return out


def main() -> int:
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    universe = P.UNIVERSE[:limit] if limit else P.UNIVERSE

    out: dict = {
        "params": {
            "entry_pctl": P.ENTRY_PCTL, "exit_pctl": P.EXIT_PCTL,
            "deterioration_quarters": P.DETERIORATION_QUARTERS,
            "warmup": P.WARMUP_TRADING_DAYS, "lag": P.EXECUTION_LAG_DAYS,
            "costs": P.COSTS, "eras": P.ERAS, "era_min_days": P.ERA_MIN_DAYS,
        },
        "stocks": [],
    }
    curves_by_strat: dict[str, list[dict]] = {k: [] for k in STRATS}
    curves_by_group: dict[str, dict[str, list[dict]]] = {}

    for i, stock in enumerate(universe, 1):
        print(f"── [{i}/{len(universe)}] {stock['code']} {stock['name']} ({stock['group']}) ──",
              flush=True)
        try:
            r = _run_stock(stock)
        except Exception as e:  # noqa: BLE001 — 單檔失敗不影響整體,如實記錄
            print(f"   失敗:{e}", flush=True)
            out["stocks"].append({**stock, "error": str(e)})
            continue
        if r is None or "error" in r:
            print(f"   {r.get('error') if r else '未知錯誤'}", flush=True)
            out["stocks"].append({**stock, **(r or {})})
            continue

        entry = {**stock, **{k: v for k, v in r.items() if k != "curves"}}
        out["stocks"].append(entry)

        # 收集曲線供組合層級用
        g = stock["group"]
        curves_by_group.setdefault(g, {k: [] for k in STRATS})
        for k in STRATS:
            c = {**r["curves"][k], "code": stock["code"], "name": stock["name"], "group": g}
            curves_by_strat[k].append(c)
            curves_by_group[g][k].append(c)

        a, b, bh = r["full"]["A"], r["full"]["B"], r["full"]["BH"]
        print(f"   {r['analysis_start']}~{r['analysis_end']}  "
              f"A {a.get('cagr', 0) * 100:5.1f}%/{a.get('max_drawdown', 0) * 100:6.1f}%  "
              f"B {b.get('cagr', 0) * 100:5.1f}%/{b.get('max_drawdown', 0) * 100:6.1f}%  "
              f"BH {bh.get('cagr', 0) * 100:5.1f}%/{bh.get('max_drawdown', 0) * 100:6.1f}%",
              flush=True)

    # 組合層級:全樣本 + 分類型
    print("\n── 組合層級(等權)──", flush=True)
    out["portfolio"] = {k: _portfolio_block(curves_by_strat[k]) for k in STRATS}
    out["portfolio_by_group"] = {
        g: {k: _portfolio_block(v[k]) for k in STRATS} for g, v in curves_by_group.items()
    }
    for k in STRATS:
        f = out["portfolio"][k]["full"]
        print(f"   {k}: 年化 {f.get('cagr', 0) * 100:.1f}%  "
              f"MDD {f.get('max_drawdown', 0) * 100:.1f}%  "
              f"平均持有 {f.get('avg_held', 0):.1f} 檔", flush=True)

    data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "era_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    write_era_report(out)
    print("\n完成 → era_robustness.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
