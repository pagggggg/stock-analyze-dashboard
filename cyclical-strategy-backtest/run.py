"""
階段一主程式(run.py)
=====================
把所有東西串起來,產出階段一報告(reports/phase1_report.md)與逐筆交易 CSV。

流程:
  1) 循環股量化定義(三雄 + 台積電對照)
  2) 樣本內(2010-2017 定參)/ 樣本外(2018-2026 驗證)分別跑:
       左側、右側兩種哲學 × 三雄 → 組合(等權)
       台積電同策略(過擬合檢測)
  3) 對照組:0050 買進持有、各股買進持有、隨機日進場
  4) 四項成功標準判定(每組獨立)
不做任何參數搜尋;門檻全來自 config。
"""
from __future__ import annotations

import csv
from pathlib import Path

import cyclical_def as CD
import metrics as M
from backtest import simulate_side
from features import build_features, load_freight
from util import load_config

ROOT = Path(__file__).resolve().parent
REP = ROOT / "reports"
REP.mkdir(exist_ok=True)


def fmt_pct(x):
    return "n/a" if x is None else f"{x*100:.1f}%"


def fmt_num(x, d=2):
    return "n/a" if x is None else f"{x:.{d}f}"


def side_portfolio_summary(stock_ids, cfg, side, start, end, freight_rows):
    """用指定運價序列(如 BDI)重建特徵並跑某哲學的組合摘要(附錄交叉檢核用)。"""
    fmap = {sid: build_features(sid, freight_rows) for sid in stock_ids}
    equities, pooled = [], []
    for sid in stock_ids:
        res = simulate_side(sid, fmap[sid], cfg, side, start, end)
        equities.append(res["equity"]); pooled.extend(res["trades"])
    port = M.equity_stats(M.combine_equity(equities))
    ps = M.trade_stats(pooled, cfg)
    return port, ps


def run_group(stock_ids, names, features_map, cfg, side, start, end):
    """跑某一哲學(side)在某期間的三檔 + 組合,回傳結果。"""
    per_stock = {}
    equities = []
    pooled_trades = []
    for sid in stock_ids:
        res = simulate_side(sid, features_map[sid], cfg, side, start, end)
        st = M.trade_stats(res["trades"], cfg)
        eq = M.equity_stats(res["equity"], res["idle_pct"])
        per_stock[sid] = {"res": res, "tstat": st, "estat": eq}
        equities.append(res["equity"])
        pooled_trades.extend(res["trades"])
    port_eq = M.combine_equity(equities)
    port_stat = M.equity_stats(port_eq)
    pooled = M.trade_stats(pooled_trades, cfg)
    return {"per_stock": per_stock, "port_eq": port_eq,
            "port_stat": port_stat, "pooled": pooled}


def main():
    cfg = load_config()
    targets = cfg["universe"]["phase1_targets"]
    stock_ids = [t["id"] for t in targets]
    names = {t["id"]: t["name"] for t in targets}
    tsmc = cfg["universe"]["controls"]["overfit"]["id"]
    bench = cfg["universe"]["controls"]["benchmark"]["id"]
    names[tsmc] = cfg["universe"]["controls"]["overfit"]["name"]
    names[bench] = cfg["universe"]["controls"]["benchmark"]["name"]

    freight = load_freight(cfg)
    fr_label = cfg["freight"][cfg["freight"]["primary"]]["label"]
    cov_end = cfg["freight"][cfg["freight"]["primary"]]["coverage_end"]

    # 特徵(全期建一次,回測時再切期間)
    feat_ids = stock_ids + [tsmc]
    features_map = {sid: build_features(sid, freight) for sid in feat_ids}

    periods = {
        "樣本內 2010–2017 (定參)": cfg["periods"]["in_sample"],
        "樣本外 2018–2026 (驗證)": cfg["periods"]["out_sample"],
    }

    lines = []
    P = lines.append
    P("# 階段一報告:海運貨櫃三雄 循環股系統化操作回測\n")
    P(f"標的:長榮2603、陽明2609、萬海2615　|　領先指標(主):{fr_label}")
    P(f"貨櫃指數免費歷史涵蓋至 **{cov_end}**;樣本外 2025-04→2026-07 **無貨櫃指數覆蓋**"
      "(該段左/右側運價訊號無法生成,已如實反映在訊號次數上;BDI 交叉檢核見附錄)。\n")
    P("> 防過擬合:門檻與各組參數全寫在 `config.yaml`,未做任何參數搜尋;"
      "樣本內定參、樣本外只驗證。手續費 %.3f%%/單邊、賣出稅 %.2f%%。\n"
      % (cfg["assumptions"]["fee_bps"]/100, cfg["assumptions"]["tax_bps"]/100))

    # ---- 1) 循環股定義 ----
    P("## 1. 循環股量化定義(三取二;數值供人工複核)\n")
    P("| 股票 | 季數 | 毛利率std(pp) | A>5 | EPS最大年減% | B>50 | 營收YoY振幅(pp) | C>60 | 命中 | 判定 |")
    P("|---|--:|--:|:-:|--:|:-:|--:|:-:|:-:|:-:|")
    for r in CD.classify_table(feat_ids, cfg):
        P(f"| {r['stock_id']} {names.get(r['stock_id'],'')} | {r['n_quarters_used']} | "
          f"{fmt_num(r['gross_margin_std_pp'])} | {'✓' if r['A_margin_std_gt5'] else '·'} | "
          f"{fmt_num(r['worst_eps_yoy_drop_pct'])} | {'✓' if r['B_eps_drop_gt50'] else '·'} | "
          f"{fmt_num(r['revenue_yoy_range_pp'])} | {'✓' if r['C_rev_range_gt60'] else '·'} | "
          f"{r['hits']} | {'**循環股**' if r['is_cyclical'] else '非循環'} |")
    P("\n台積電作為**過擬合檢測**:量化上被判為非循環股;若策略在它身上也『有效』,"
      "代表抓到的是大盤 beta 而非循環特性。\n")

    all_results = {}
    ledger_rows = []
    section = 2
    period_items = list(periods.items())
    for pname, (start, end) in period_items:
        P(f"## {section}. {pname}\n")
        section += 1
        # 0050 基準
        b_eq = M.buy_hold_equity(bench, start, end)
        b_stat = M.equity_stats(b_eq)
        P(f"**績效基準 0050 買進持有**:年化 {fmt_pct(b_stat['annualized'])}、"
          f"總報酬 {fmt_pct(b_stat['total_return'])}、最大回撤 {fmt_pct(b_stat['mdd'])}\n")

        for side, label in [("left", "左側(供給面:運價P20+P/B近10年P10,分4批)"),
                            ("right", "右側(需求面:運價連4週回升+毛利連2季升+營收YoY轉正,分3批)")]:
            g = run_group(stock_ids, names, features_map, cfg, side, start, end)
            # 台積電同策略
            t_res = simulate_side(tsmc, features_map[tsmc], cfg, side, start, end)
            t_ts = M.trade_stats(t_res["trades"], cfg)
            t_es = M.equity_stats(t_res["equity"], t_res["idle_pct"])
            all_results[(pname, side)] = {"g": g, "tsmc": {"ts": t_ts, "es": t_es},
                                          "bench": b_stat}

            P(f"### {label}\n")
            ps = g["pooled"]
            pst = g["port_stat"]
            P(f"- **組合(等權三雄)**:年化 **{fmt_pct(pst['annualized'])}**、"
              f"總報酬 {fmt_pct(pst['total_return'])}、最大回撤 {fmt_pct(pst['mdd'])}")
            P(f"- 訊號次數(三檔合計)**{ps['n_signals']}**、假訊號率 "
              f"**{fmt_pct(ps['false_signal_ratio'])}**、勝率 {fmt_pct(ps['win_rate'])}、"
              f"盈虧比 {fmt_num(ps['profit_factor'])}、平均持有 "
              f"{fmt_num(ps['avg_hold_days'],0)} 天、出場原因 {ps['exit_reasons']}")
            # 每檔
            P("\n| 股票 | 訊號 | 假訊號率 | 勝率 | 盈虧比 | 平均持有(天) | 空手% | 年化 | 總報酬 | MDD | 買進持有年化 |")
            P("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
            for sid in stock_ids:
                d = g["per_stock"][sid]
                ts, es = d["tstat"], d["estat"]
                bh = M.equity_stats(M.buy_hold_equity(sid, start, end))
                P(f"| {sid} {names[sid]} | {ts['n_signals']} | {fmt_pct(ts['false_signal_ratio'])} | "
                  f"{fmt_pct(ts['win_rate'])} | {fmt_num(ts['profit_factor'])} | "
                  f"{fmt_num(ts['avg_hold_days'],0)} | {fmt_pct(es['idle_pct']/100 if es['idle_pct'] is not None else None)} | "
                  f"{fmt_pct(es['annualized'])} | {fmt_pct(es['total_return'])} | {fmt_pct(es['mdd'])} | "
                  f"{fmt_pct(bh['annualized'])} |")
                # 逐筆明細寫 CSV
                for t in d["res"]["trades"]:
                    ledger_rows.append({
                        "period": pname, "side": side, "stock": f"{sid}{names[sid]}",
                        "signal_date": t["signal_date"],
                        "entry": ";".join(f"{l['date']}@{l['price']}" for l in t["entry_legs"]),
                        "exit": ";".join(f"{l['date']}@{l['price']}({l['reason']})" for l in t["exit_legs"]),
                        "exit_reason": t["exit_reason"],
                        "return": round(t["return"], 4),
                        "hold_days": t["hold_days"],
                        "false_signal": t["false_signal"],
                    })
            # 台積電過擬合對照
            P(f"\n- 過擬合對照 **台積電同策略**:訊號 {t_ts['n_signals']}、"
              f"假訊號率 {fmt_pct(t_ts['false_signal_ratio'])}、年化 {fmt_pct(t_es['annualized'])}、"
              f"MDD {fmt_pct(t_es['mdd'])} "
              f"→ 若此處也『有效』,策略疑似抓大盤 beta 而非循環特性。")
            # 隨機基準(以組合平均持有天數)
            rb = M.random_baseline(stock_ids[0], start, end,
                                   ps['avg_hold_days'], cfg["assumptions"]["random_baseline_runs"],
                                   cfg["assumptions"]["random_seed"])
            if rb:
                P(f"- 隨機日進場基準(以 {names[stock_ids[0]]}、持有~{rb['hold_days_used']}天、"
                  f"{rb['runs']}次):年化中位數 {fmt_pct(rb['median_annualized'])} "
                  f"(P25 {fmt_pct(rb['p25'])} / P75 {fmt_pct(rb['p75'])})")
            P("")

    # ---- 3) 四項成功標準判定 ----
    P("## 4. 四項成功標準判定(以樣本外為主;任一不過即該組不可行)\n")
    sc = cfg["success_criteria"]
    P(f"門檻:①假訊號率<{fmt_pct(sc['false_signal_ratio_max'])} ②樣本外不崩 "
      f"③年化勝0050且MDD可接受 ④每組訊號≥{sc['min_signals_per_group']}"
      f"(某組假訊號率>{fmt_pct(sc['group_infeasible_false_signal'])}直接判不可行)\n")
    P("| 期間 | 哲學 | 訊號數 | 假訊號率 | 組合年化 | 0050年化 | MDD | ①<40% | ④≥5 | ③勝0050 | 該組結論 |")
    P("|---|---|--:|--:|--:|--:|--:|:-:|:-:|:-:|:-:|")
    verdicts = {}
    for (pname, side), r in all_results.items():
        ps = r["g"]["pooled"]; pst = r["g"]["port_stat"]; bst = r["bench"]
        fsr = ps["false_signal_ratio"]
        c1 = fsr is not None and fsr < sc["false_signal_ratio_max"]
        c4 = ps["n_signals"] >= sc["min_signals_per_group"]
        c3 = (pst["annualized"] is not None and bst["annualized"] is not None
              and pst["annualized"] > bst["annualized"])
        infeasible = fsr is not None and fsr > sc["group_infeasible_false_signal"]
        if not c4:
            concl = "樣本不足,不下結論"
        elif infeasible:
            concl = "**不可行**(假訊號率>50%)"
        elif c1 and c3:
            concl = "通過(此二項)"
        else:
            concl = "未通過"
        verdicts[(pname, side)] = concl
        sidelabel = "左側" if side == "left" else "右側"
        P(f"| {pname} | {sidelabel} | {ps['n_signals']} | {fmt_pct(fsr)} | "
          f"{fmt_pct(pst['annualized'])} | {fmt_pct(bst['annualized'])} | {fmt_pct(pst['mdd'])} | "
          f"{'✓' if c1 else '✗'} | {'✓' if c4 else '✗'} | {'✓' if c3 else '✗'} | {concl} |")

    P("\n### 總結論")
    P("- 「樣本外表現不崩」需人工複核上表樣本外年化是否為正且 MDD 未顯著劣於買進持有。")
    P("- 只要任一必要條件不過,即宣告該哲學不可行,**不得調參補救**。")
    P("- 台積電對照若與三雄同樣『有效』,代表訊號抓到的是大盤 beta,循環特性存疑。")

    # ---- 5) 附錄 A:BDI 交叉檢核(完整覆蓋至 2026,填補貨櫃指數缺口)----
    import sources_finmind as SF
    import sources_freight as G
    bdi_rows = G.fetch_freight(cfg["freight"]["bdi"]["pair_id"])
    P("\n## 5. 附錄 A:BDI 交叉檢核(運價指標改用 BDI,完整覆蓋 2009→2026)\n")
    P("主指標(貨櫃)免費歷史只到 2025-03,樣本外最後約16個月無訊號;"
      "此處改用 **BDI(乾散貨,完整至2026-07)** 作為運價指標重跑,"
      "檢查『補上缺口期』與『換一個運價來源』後結論是否翻轉(BDI 為交叉檢核,非主訊號)。\n")
    P("| 期間 | 哲學 | 訊號數 | 假訊號率 | 組合年化 | 0050年化 | MDD |")
    P("|---|---|--:|--:|--:|--:|--:|")
    for pname, (start, end) in period_items:
        b_ann = M.equity_stats(M.buy_hold_equity(bench, start, end))["annualized"]
        for side in ("left", "right"):
            port, ps = side_portfolio_summary(stock_ids, cfg, side, start, end, bdi_rows)
            P(f"| {pname} | {'左側' if side=='left' else '右側'} | {ps['n_signals']} | "
              f"{fmt_pct(ps['false_signal_ratio'])} | {fmt_pct(port['annualized'])} | "
              f"{fmt_pct(b_ann)} | {fmt_pct(port['mdd'])} |")
    P("\n> BDI 版若與貨櫃版結論一致(皆無法四項全過),代表結論對『運價來源選擇』穩健,"
      "而非某一指標的偶然。\n")

    # ---- 6) 附錄 B:資料處理與公司行為揭露 ----
    P("## 6. 附錄 B:資料來源、公司行為與已知偏誤揭露\n")
    P("- **價格**:FinMind 未還原日收盤(免費版無還原權值)→ 已對**分割/減資**做回溯調整"
      "使序列連續;但**現金股利未還原**,對報酬為**保守低估**(尤其高股息的海運股)。")
    P("- 偵測並回溯調整的公司行為(相鄰交易日比值超出台股±10%甚多):")
    for sid in stock_ids + [bench, tsmc]:
        evs = SF.corporate_actions(sid)
        note = SF.KNOWN_CORPORATE_ACTIONS.get(sid, "")
        if evs:
            e = evs[0]
            P(f"  - {sid} {names.get(sid,'')}:{e['date']} 比值 {e['ratio']:.3f}"
              f"({e['before']}→{e['after']}){'　' + note if note else ''}")
    P("- **運價指數**:investing.com 歷史 API(週線);貨櫃 metadata 被 Cloudflare 擋,"
      "SCFI vs CCFI 精確標籤無法程式確認,已於 config 標示為『貨櫃運價類』。")
    P("- **樣本外貨櫃缺口**:2025-04→2026-07 無貨櫃指數 → 該段左/右側運價訊號無法生成;"
      "同期 0050 買進持有仍計入其大漲,對『勝過0050』的比較**對策略不利但誠實**。")
    P("- **倖存者偏差**:國巨(2327)為構想來源,不在階段一樣本;已下市循環股亦不在樣本,"
      "會高估存活者結果 → 明確標示。")
    P("- **idle 現金不計息**:空手期報酬以 0 計,對策略保守。")

    (REP / "phase1_report.md").write_text("\n".join(lines), encoding="utf-8")
    with (REP / "phase1_trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(ledger_rows[0].keys()) if ledger_rows else
                           ["period","side","stock","signal_date","entry","exit","exit_reason","return","hold_days","false_signal"])
        w.writeheader()
        w.writerows(ledger_rows)
    print(f"報告已產出:{REP/'phase1_report.md'}")
    print(f"逐筆交易:{REP/'phase1_trades.csv'}({len(ledger_rows)} 筆)")


if __name__ == "__main__":
    main()
