"""
主程式 (main.py)
================
把資料層 → 指引 → EPS 試算 → 估值 → 預期差 → 報告 串成一條龍。

用法:
    # 手動模式 (預設,讀 config + data 底下的檔案)
    python main.py

    # 指定檔案路徑
    python main.py --config config/assumptions.yaml \
                   --financials data/financials_manual.csv \
                   --pe data/pe_history.csv \
                   --out reports/report.md

    # 自動抓取 (FinMind 財報 + TWSE 本益比;抓不到自動退回手動 CSV)
    python main.py --data-mode auto

流程刻意「先手動、再自動」:預設就是手動,保證跑得動;
加 --data-mode auto 才嘗試自動抓取(FinMind 近8季財報 + TWSE 近10年本益比),
任一步失敗都會自動退回對應的手動 CSV。首次 auto 會較慢(TWSE 逐月抓),
之後有快取(cache/)幾乎不再連網。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from src.data_layer import (
    compute_historical_averages,
    fetch_balance_pivot,
    fetch_cashflow_pivot,
    fetch_current_price_twse,
    fetch_financials_auto,
    fetch_income_pivot,
    fetch_pe_history_twse,
    fetch_price_daily_finmind,
    fetch_yfinance_metrics,
    load_consensus_history,
    load_financials_csv,
    load_pe_history_csv,
    merge_supplement,
    record_consensus_history,
    save_financials_csv,
    save_pe_history_csv,
    trailing_eps,
    validate_against_csv,
)
from src.dashboard_html import build_html_dashboard
from src.eps_calc import backtest_against_actuals, calculate_scenarios
from src.expectation import compute_all_gaps
from src.fcf_quality import build_fcf_quality
from src.guidance import load_guidance, load_raw_config
from src.metrics import build_dashboard
from src.models import ConsensusSnapshot, SourcedValue
from src.report import build_report
from src.river import build_pe_river
from src.valuation import build_valuation


# 專案根目錄 (main.py 所在資料夾),讓相對路徑不受「在哪執行」影響
ROOT = Path(__file__).resolve().parent


def _parse_consensus(raw: dict) -> tuple[SourcedValue | None, SourcedValue | None]:
    """從原始 config 取出單季 / 全年 共識 EPS (沒填就回 None)。"""
    c = raw.get("consensus") or {}

    def pick(node):
        if not node or "value" not in node:
            return None
        return SourcedValue(
            value=float(node["value"]),
            source=node.get("source", "(未標註來源)"),
            note=node.get("note", ""),
        )

    return pick(c.get("eps_quarter")), pick(c.get("eps_annual"))


def run(args) -> None:
    # ---- 1. 讀指引 / 假設 -------------------------------------------
    guidance = load_guidance(args.config)
    raw = load_raw_config(args.config)
    consensus_q, consensus_a = _parse_consensus(raw)
    annualize_method = (raw.get("valuation") or {}).get("annualize_method", "ttm")

    print(f"[1/8] 已載入指引:{guidance.quarter_label}")

    # ---- 2. 取得財務歷史 (先自動、失敗退回手動) ---------------------
    data_mode = "手動 CSV"
    quarters = None
    validation_warnings = None  # 只有 auto 模式才會做 API vs CSV 驗證
    if args.data_mode == "auto":
        try:
            quarters = fetch_financials_auto(last_n=8)
            data_mode = "自動抓取 (FinMind + TWSE)"
            # 存一份下來,方便你檢查抓到什麼、必要時微調後改用手動模式
            auto_csv = ROOT / "data/financials_auto.csv"
            save_financials_csv(quarters, auto_csv)
            # 驗證:API 近8季 vs 原本手動 CSV,差異 > 2% 列警告
            validation_warnings = validate_against_csv(quarters, args.financials, threshold_pct=2.0)
            print(f"[2/8] FinMind 抓取 {len(quarters)} 季 → {auto_csv.name};"
                  f"驗證警告 {len(validation_warnings)} 筆")
        except Exception as e:  # noqa: BLE001 — 任何失敗都退回手動,確保跑得動
            print(f"[2/8] FinMind 抓取失敗({e}),退回手動 CSV")
    if quarters is None:
        quarters = load_financials_csv(args.financials)
        print(f"[2/8] 已讀取手動財務 CSV:{len(quarters)} 季")

    # 合併「補充檔」:補上 API 尚未收錄、但已公布的最新季度(如剛開完法說的當季)
    # backtest/hist_avg 只用「真正財報實際值」(quarters);
    # trailing 年化與資料層明細用「含補充」的 quarters_all。
    supplement_path = ROOT / "data/financials_supplement.csv"
    quarters_all, supplement_labels = merge_supplement(quarters, supplement_path)
    if supplement_labels:
        print(f"[2/8] 已合併補充季度:{'、'.join(sorted(supplement_labels))}")

    hist_avg = compute_historical_averages(quarters)
    trailing_sum, trailing_qs = trailing_eps(quarters_all, n=3, before_quarter=guidance.quarter_label)

    # ---- 3. EPS 三情境試算 -----------------------------------------
    scenarios = calculate_scenarios(guidance)
    print(f"[3/8] EPS 試算完成:中性單季 EPS = {scenarios['中性'].eps_quarter:.2f}")

    # ---- 4. 估值 (先年化再乘本益比) --------------------------------
    pe_band = None
    if args.data_mode == "auto":
        try:
            pe_band, pe_year_rows = fetch_pe_history_twse(stock_id="2330", years=10)
            save_pe_history_csv(pe_year_rows, ROOT / "data/pe_history_auto.csv", pe_band.source)
            print(f"[4/8] TWSE 本益比:{len(pe_year_rows)} 年 → pe_history_auto.csv")
        except Exception as e:  # noqa: BLE001 — 抓不到退回手動 pe CSV
            print(f"[4/8] TWSE 本益比抓取失敗({e}),退回手動 pe CSV")
    if pe_band is None:
        pe_band = load_pe_history_csv(args.pe)

    valuation = build_valuation(
        scenarios, pe_band, method=annualize_method, trailing_eps_sum=trailing_sum
    )
    print(f"[4/8] 估值完成:中性年化 EPS = {scenarios['中性'].eps_annualized:.2f}，"
          f"本益比 {pe_band.pe_low}~{pe_band.pe_high}x")

    # ---- 5. 現價(config 指定 or auto 抓 TWSE 收盤)-------------------
    current_price = None  # (價格, 來源)
    cp_cfg = (raw.get("valuation") or {}).get("current_price")
    if cp_cfg and cp_cfg.get("value"):
        current_price = (float(cp_cfg["value"]), cp_cfg.get("source", "手動填入"))
        print(f"[5/8] 現價(config):{current_price[0]}")
    elif args.data_mode == "auto":
        try:
            pr, pdate, psrc = fetch_current_price_twse(stock_id="2330")
            current_price = (pr, psrc)
            print(f"[5/8] 現價(TWSE):{pr} @ {pdate}")
        except Exception as e:  # noqa: BLE001
            print(f"[5/8] 現價抓取失敗({e}),略過現價相關")

    # ---- 6. yfinance 儀表板原料 + 分析師共識 EPS ----------------------
    yf_metrics = None
    yf_date = ""
    if args.data_mode == "auto":
        try:
            yf_metrics, yf_date = fetch_yfinance_metrics("2330.TW")
            print(f"[6/8] yfinance 指標 OK(抓取 {yf_date})")
        except Exception as e:  # noqa: BLE001
            print(f"[6/8] yfinance 抓取失敗({e}),改用 config 手填")

    dash_cfg = raw.get("dashboard") or {}
    # 共識 EPS:yfinance 優先,config fallback
    eps_q0 = eps_y0 = eps_y1 = None
    n_analysts = None
    consensus_src = ""
    if yf_metrics and yf_metrics.get("eps_y0"):
        eps_q0 = yf_metrics.get("eps_q0")
        eps_y0 = yf_metrics.get("eps_y0")
        eps_y1 = yf_metrics.get("eps_y1")
        n_analysts = yf_metrics.get("n_y0")
        consensus_src = f"yfinance 分析師共識 (抓取 {yf_date})"
    else:
        eps_y0 = dash_cfg.get("consensus_eps_2026")
        eps_y1 = dash_cfg.get("consensus_eps_2027")
        if eps_y0 or eps_y1:
            consensus_src = dash_cfg.get("source", "config 手填")

    # 盈餘成長率(PEG 用):config growth_pct 優先,否則用 2026/2027 共識算
    growth_pct = None
    growth_source = "(無成長率,PEG 無法計算)"
    if dash_cfg.get("growth_pct") is not None:
        growth_pct = float(dash_cfg["growth_pct"])
        growth_source = f"config 手填 {growth_pct:.1f}%"
    elif eps_y0 and eps_y1 and float(eps_y0) != 0:
        growth_pct = (float(eps_y1) - float(eps_y0)) / float(eps_y0) * 100
        growth_source = f"共識 2027 {float(eps_y1):.1f} vs 2026 {float(eps_y0):.1f} → {growth_pct:.1f}%"

    # 預期差用的共識:config 手填優先(consensus_q/a),否則用 yfinance(當季0q / 今年0y)
    if consensus_q is None and eps_q0:
        consensus_q = SourcedValue(float(eps_q0), f"{consensus_src}・當季(0q)")
    if consensus_a is None and eps_y0:
        consensus_a = SourcedValue(float(eps_y0), f"{consensus_src}・今年FY(0y)")

    # ---- 7. 預期差 + 估值儀表板 + 共識監控 ----------------------------
    gaps = compute_all_gaps(scenarios, consensus_q, consensus_a)
    print(f"[7/8] 預期差:{len(gaps)} 個口徑")

    dashboard = None
    if current_price:
        dashboard = build_dashboard(
            price=current_price[0],
            ann_eps=scenarios["中性"].eps_annualized,
            shares_bn=guidance.shares_bn.value,
            pe_band=pe_band,
            yf=yf_metrics,
            growth_pct=growth_pct,
            growth_source=growth_source,
        )
        have = len([m for m in dashboard.metrics if m.value is not None])
        print(f"[7/8] 估值儀表板:{have}/4 指標有值")

    # 共識監控:每次執行記錄一列,並取「上一筆」比較上修/下修/持平
    consensus_snapshot = None
    if eps_y0 is not None or eps_y1 is not None:
        hist_path = ROOT / "data/consensus_history.csv"
        prev = record_consensus_history(
            hist_path,
            float(eps_y0) if eps_y0 is not None else None,
            float(eps_y1) if eps_y1 is not None else None,
            round(growth_pct, 2) if growth_pct is not None else None,
            consensus_src,
        )

        def _pf(row, k):
            try:
                return float(row.get(k)) if row and row.get(k) not in (None, "") else None
            except (TypeError, ValueError):
                return None

        consensus_snapshot = ConsensusSnapshot(
            as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
            eps_q0=float(eps_q0) if eps_q0 is not None else None,
            eps_y0=float(eps_y0) if eps_y0 is not None else None,
            eps_y1=float(eps_y1) if eps_y1 is not None else None,
            growth_pct=growth_pct, n_analysts=n_analysts, source=consensus_src,
            prev_eps_y0=_pf(prev, "eps_y0"), prev_eps_y1=_pf(prev, "eps_y1"),
            prev_as_of=(prev.get("datetime", "") if prev else ""),
        )
        print(f"[7/8] 共識監控:2026 {consensus_snapshot.y0_change}")

    # ---- 8. 模型回測 + 報告 ------------------------------------------
    backtest = backtest_against_actuals(quarters)
    report_md = build_report(
        guidance=guidance,
        scenarios=scenarios,
        valuation=valuation,
        gaps=gaps,
        backtest=backtest,
        hist_avg=hist_avg,
        trailing_info=(trailing_sum, trailing_qs),
        data_mode=data_mode,
        financials=quarters_all,
        validation_warnings=validation_warnings,
        current_price=current_price,
        supplement_labels=supplement_labels,
        dashboard=dashboard,
        consensus_snapshot=consensus_snapshot,
    )

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_md, encoding="utf-8")
    print(f"[8/8] 報告已輸出:{out_path}")

    # ---- 9. 視覺化 HTML 儀表板 (--html) ------------------------------
    # 單一 HTML 檔(plotly.js 內嵌,離線可開)。河流圖 / FCF 品質需 auto 模式的
    # 長區間財報與日股價;抓不到就以「資料不足」佔位,其餘圖照樣產出。
    if args.html:
        consensus_rows = load_consensus_history(ROOT / "data/consensus_history.csv")
        river_series = None
        fcf_result = None
        inc_piv = None
        if args.data_mode == "auto":
            try:
                inc_piv, _ = fetch_income_pivot("2330")
            except Exception as e:  # noqa: BLE001
                print(f"[HTML] 長區間財報抓取失敗({e}),河流圖/FCF 將略過")
            # 河流圖:長區間 EPS(TTM)+ 日股價 + 本益比河道
            try:
                if inc_piv:
                    price_rows, _ = fetch_price_daily_finmind("2330")
                    cp = current_price[0] if current_price else None
                    river_series = build_pe_river(price_rows, inc_piv, pe_band, current_price=cp)
                    print(f"[HTML] 河流圖:{len(river_series.dates)} 個月頻點")
            except Exception as e:  # noqa: BLE001
                print(f"[HTML] 河流圖略過({e})")
            # FCF 品質:資產負債(存貨/應收)+ 現金流(OCF/Capex)+ 損益(營收/COGS)
            try:
                if inc_piv:
                    bal_piv, _ = fetch_balance_pivot("2330")
                    cf_piv, _ = fetch_cashflow_pivot("2330")
                    fcf_result = build_fcf_quality(inc_piv, bal_piv, cf_piv)
                    print(f"[HTML] FCF 品質:{len(fcf_result.years)} 個完整年度、"
                          f"{len(fcf_result.signals)} 個燈號")
            except Exception as e:  # noqa: BLE001
                print(f"[HTML] FCF 品質略過({e})")

        html = build_html_dashboard(
            guidance=guidance,
            scenarios=scenarios,
            valuation=valuation,
            data_mode=data_mode,
            dashboard=dashboard,
            river=river_series,
            quarters=quarters_all,
            consensus_rows=consensus_rows,
            fcf=fcf_result,
            current_price=current_price,
        )
        html_path = ROOT / "reports/dashboard.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        print(f"[HTML] 視覺化儀表板已輸出:{html_path}(瀏覽器直接開,免架 server)")


def main() -> None:
    p = argparse.ArgumentParser(description="台積電 EPS 試算與估值工具")
    p.add_argument("--config", default=str(ROOT / "config/assumptions.yaml"),
                   help="假設檔 (YAML)")
    p.add_argument("--financials", default=str(ROOT / "data/financials_manual.csv"),
                   help="近8季財務數據 CSV")
    p.add_argument("--pe", default=str(ROOT / "data/pe_history.csv"),
                   help="近10年本益比 CSV")
    p.add_argument("--out", default=str(ROOT / "reports/report.md"),
                   help="報告輸出路徑")
    p.add_argument("--data-mode", choices=["manual", "auto"], default="manual",
                   help="manual=只讀CSV;auto=FinMind財報+TWSE本益比,失敗退回CSV")
    p.add_argument("--html", action="store_true",
                   help="額外產出視覺化儀表板 reports/dashboard.html(單一HTML,離線可開)")
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
