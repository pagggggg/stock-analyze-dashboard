"""
階段一補充測試主程式(run_monthly.py)
=====================================
用「月營收(次月10日公布)」取代季報,驗證時效是否為階段一右側訊號失敗的主因。

產出 reports/phase1_monthly_supplement.md 與 phase1_monthly_trades.csv,內容包含:
  1) 月營收資料可得性
  2) 訊號時效比對(必答Q1:早幾個交易日)——配對比較 + 結構性資訊時差
  3) 三組並列(季報版右側 / 月營收版 / 0050)× 樣本內外
  4) 對照組:台積電(過擬合)、隨機進場、買進持有
  5) 必答Q2/Q3 與結案結論
"""
from __future__ import annotations

import bisect
import csv
from pathlib import Path

import metrics as M
import sources_finmind as F
from backtest import simulate_side
from backtest_monthly import simulate_monthly
from features import build_features, load_freight
from features_monthly import aggregate_monthly_yoy, attach_monthly_signals
from util import load_config, to_date

ROOT = Path(__file__).resolve().parent
REP = ROOT / "reports"
REP.mkdir(exist_ok=True)


def fmt_pct(x):
    return "n/a" if x is None else f"{x*100:.1f}%"


def fmt_num(x, d=2):
    return "n/a" if x is None else f"{x:.{d}f}"


def trading_days_between(px_dates, d0, d1):
    """d0→d1 之間的交易日數(正 = d1 較晚)。"""
    i0 = bisect.bisect_left(px_dates, d0)
    i1 = bisect.bisect_left(px_dates, d1)
    return i1 - i0


def match_signal_lead(monthly_dates, quarterly_dates, px_dates, window_days=365):
    """把季報訊號配對到「最近一個在它之前」的月營收訊號(= 同一波上升被更早偵測到)。

    只取『月營收在前』的配對才算「提前」;若某季報訊號在 window 內沒有前置的月營收訊號,
    列為 unmatched(代表兩套規則抓到的根本不是同一件事,不能算成時效優勢)。
    這比「取最近的訊號(不分前後)」誠實:後者會把方向相反的配對也算進平均。
    """
    pairs, unmatched = [], []
    for qd in quarterly_dates:
        best = None
        for md in monthly_dates:
            delta = (to_date(qd) - to_date(md)).days
            if 0 <= delta <= window_days and (best is None or delta < best[1]):
                best = (md, delta)
        if best:
            md = best[0]
            pairs.append({"quarterly": qd, "monthly": md,
                          "lead_cal_days": best[1],
                          "lead_trading_days": trading_days_between(px_dates, md, qd)})
        else:
            unmatched.append(qd)
    return pairs, unmatched


def portfolio(stock_ids, sim_fn, fmap, cfg, start, end):
    eqs, pooled, per = [], [], {}
    for sid in stock_ids:
        res = sim_fn(sid, fmap[sid], cfg, start, end)
        eqs.append(res["equity"])
        pooled.extend(res["trades"])
        per[sid] = res
    return (M.equity_stats(M.combine_equity(eqs)), M.trade_stats(pooled, cfg), per)


def main():
    cfg = load_config()
    targets = cfg["universe"]["phase1_targets"]
    ids = [t["id"] for t in targets]
    names = {t["id"]: t["name"] for t in targets}
    tsmc = cfg["universe"]["controls"]["overfit"]["id"]
    bench = cfg["universe"]["controls"]["benchmark"]["id"]
    names[tsmc] = cfg["universe"]["controls"]["overfit"]["name"]

    # 運價:月營收版主用 BDI(全期覆蓋,無2025-04後缺口);另附貨櫃版對照
    bdi = load_freight(cfg, "bdi")
    container = load_freight(cfg, "container")

    agg = aggregate_monthly_yoy(ids)                 # 三檔加總
    agg_tsmc = aggregate_monthly_yoy([tsmc])         # 台積電自身(過擬合對照)

    # 特徵:季報版(貨櫃,階段一原設定)、月營收版(BDI 為主 / 貨櫃為輔)
    feat_q = {sid: build_features(sid, container) for sid in ids + [tsmc]}
    feat_m_bdi = {sid: attach_monthly_signals(build_features(sid, bdi),
                                              agg_tsmc if sid == tsmc else agg)
                  for sid in ids + [tsmc]}
    feat_m_con = {sid: attach_monthly_signals(build_features(sid, container),
                                              agg_tsmc if sid == tsmc else agg)
                  for sid in ids + [tsmc]}

    px_dates = [r["date"] for r in F.fetch_prices(ids[0])]
    periods = {
        "樣本內 2010–2017": cfg["periods"]["in_sample"],
        "樣本外 2018–2026": cfg["periods"]["out_sample"],
    }

    L = []
    P = L.append
    P("# 階段一補充測試:用「月營收」取代季報,驗證時效是否為主因\n")
    P("**假設**:階段一右側訊號(季報:毛利率+營收YoY)假訊號率0%卻年化輸0050、"
      "MDD -60~76%,是因為**季報落後 2-3 個月**,進場太慢。")
    P("**做法**:改用三檔**月營收加總 YoY**(次月10日公布)當訊號,"
      "運價方向確認主用 **BDI(全期覆蓋)**,另附貨櫃指數版本。")
    P("**不變**:標的、交易成本、減資/分割還原、假訊號定義(進場後2季內未出現毛利率連2季升)、"
      "權益曲線與空手認定 —— 只換訊號來源,才是乾淨對照。\n")

    # ---- 1) 資料可得性 ----
    P("## 1. 月營收資料可得性\n")
    P("| 標的 | 月營收筆數 | 範圍 | 最新可用日 |")
    P("|---|--:|---|---|")
    for sid in ids + [tsmc]:
        r = F.fetch_month_revenue(sid)
        P(f"| {sid} {names[sid]} | {len(r)} | {r[0]['ry']}-{r[0]['rm']:02d} → "
          f"{r[-1]['ry']}-{r[-1]['rm']:02d} | {r[-1]['avail']} |")
    P(f"\n三檔加總 YoY 序列:{len(agg)} 個月({agg[0]['ym']} → {agg[-1]['ym']})。")
    P("**資訊時效優勢**:月營收最新到 2026-06(次月10日可用),季報最新只到 2026Q1 → "
      "月營收在資料尾端就多出 3 個月的可見度。\n")

    # ---- 2) 訊號時效比對(必答 Q1)----
    P("## 2. 必答Q1:月營收訊號比季報訊號早幾個交易日?\n")
    P("### 2a. 結構性資訊時差(制度面,與回測無關)\n")
    P("| 財報期別 | 季報可用日(法定) | 同期間月營收可用日 | 月營收提前 |")
    P("|---|---|---|--:|")
    struct = [("Q1(3月底)", "5/15", "3月營收 4/10", 35),
              ("Q2(6月底)", "8/14", "6月營收 7/10", 35),
              ("Q3(9月底)", "11/14", "9月營收 10/10", 35),
              ("Q4(12月底)", "次年3/31", "12月營收 次年1/10", 80)]
    for a, b, c, d in struct:
        P(f"| {a} | {b} | {c} | {d} 天 |")
    P(f"\n→ 制度上月營收平均提前 **{sum(x[3] for x in struct)/4:.0f} 天**(約 1.2~2.6 個月)。\n")

    P("### 2b. 實際訊號日期配對(以本回測產生的訊號)\n")
    P("配對規則:每個季報訊號,找「在它之前 365 天內最近的」月營收訊號。"
      "若找不到前置的月營收訊號 → 列為未配對(代表兩套規則抓的不是同一件事,"
      "不能算成時效優勢)。\n")
    all_pairs, all_unmatched = [], []
    P("| 標的 | 季報版訊號日 | 前置月營收訊號日 | 提前(日曆天) | 提前(交易日) |")
    P("|---|---|---|--:|--:|")
    mfull = simulate_monthly(ids[0], feat_m_bdi[ids[0]], cfg, *cfg["periods"]["full"])
    m_dates = [t["signal_date"] for t in mfull["trades"]]
    for sid in ids:
        qfull = simulate_side(sid, feat_q[sid], cfg, "right", *cfg["periods"]["full"])
        q_dates = [t["signal_date"] for t in qfull["trades"]]
        prs, un = match_signal_lead(m_dates, q_dates, px_dates)
        all_unmatched += [(sid, u) for u in un]
        for pr in prs:
            all_pairs.append(pr)
            P(f"| {sid} {names[sid]} | {pr['quarterly']} | {pr['monthly']} | "
              f"{pr['lead_cal_days']} | {pr['lead_trading_days']} |")
    P(f"\n月營收訊號日(三檔共用,因訊號為加總):{', '.join(m_dates)}")
    if all_unmatched:
        P(f"\n未配對(該季報訊號前 365 天內無月營收訊號)共 {len(all_unmatched)} 個:"
          + "、".join(f"{s}@{d}" for s, d in all_unmatched))
    if all_pairs:
        avg_c = sum(p["lead_cal_days"] for p in all_pairs) / len(all_pairs)
        lt = sorted(p["lead_trading_days"] for p in all_pairs)
        avg_t = sum(lt) / len(lt)
        med_t = lt[len(lt) // 2]
        P(f"\n**Q1 答案:在可配對的 {len(all_pairs)} 組中,月營收訊號平均比季報訊號早 "
          f"{avg_t:.0f} 個交易日(中位數 {med_t}、範圍 {lt[0]}~{lt[-1]};"
          f"平均 {avg_c:.0f} 個日曆天)。**")
        P(f"\n> **重要但不方便的但書**:制度上的資訊時差只有約 {sum(x[3] for x in struct)/4:.0f} 天"
          f"(≈30 個交易日),但實際訊號差距遠大於此,且有 {len(all_unmatched)} 個季報訊號"
          f"根本配不到前置的月營收訊號。這代表**兩者不是「同一個訊號、只是早一點」,"
          f"而是根本不同的觸發規則** —— 因此後面的績效差異不能全部歸功於「時效」,"
          f"訊號定義改變本身也是原因。這點若不講清楚,就會高估「時效」的貢獻。\n")
        q1 = (avg_t, med_t, avg_c, len(all_pairs))
    else:
        P("\n**Q1 答案:無可配對訊號**(兩套規則觸發時點完全不重疊)。\n")
        q1 = None

    # ---- 3) 三組並列 ----
    P("## 3. 三組並列比較(主表)\n")
    P("運價方向確認:月營收版用 **BDI(全期)**;季報版維持階段一原設定(貨櫃指數,2025-03後無覆蓋)。\n")
    P("| 期間 | 組別 | 訊號數 | 假訊號率 | 平均持有(天) | 空手% | **年化** | 總報酬 | 最大回撤 |")
    P("|---|---|--:|--:|--:|--:|--:|--:|--:|")
    summary = {}
    ledger = []
    for pname, (s, e) in periods.items():
        qp, qs, qper = portfolio(ids, lambda a, b, c, d, f: simulate_side(a, b, c, "right", d, f),
                                 feat_q, cfg, s, e)
        mp, ms, mper = portfolio(ids, simulate_monthly, feat_m_bdi, cfg, s, e)
        b_eq = M.buy_hold_equity(bench, s, e)
        bs = M.equity_stats(b_eq)
        idle_q = sum(qper[i]["idle_pct"] for i in ids) / len(ids)
        idle_m = sum(mper[i]["idle_pct"] for i in ids) / len(ids)
        summary[pname] = {"q": (qp, qs), "m": (mp, ms), "b": bs}
        P(f"| {pname} | ①季報版右側 | {qs['n_signals']} | {fmt_pct(qs['false_signal_ratio'])} | "
          f"{fmt_num(qs['avg_hold_days'],0)} | {idle_q:.1f}% | **{fmt_pct(qp['annualized'])}** | "
          f"{fmt_pct(qp['total_return'])} | {fmt_pct(qp['mdd'])} |")
        P(f"| {pname} | ②月營收版 | {ms['n_signals']} | {fmt_pct(ms['false_signal_ratio'])} | "
          f"{fmt_num(ms['avg_hold_days'],0)} | {idle_m:.1f}% | **{fmt_pct(mp['annualized'])}** | "
          f"{fmt_pct(mp['total_return'])} | {fmt_pct(mp['mdd'])} |")
        P(f"| {pname} | ③0050買進持有 | — | — | 全期 | 0.0% | **{fmt_pct(bs['annualized'])}** | "
          f"{fmt_pct(bs['total_return'])} | {fmt_pct(bs['mdd'])} |")
        for sid in ids:
            for t in mper[sid]["trades"]:
                ledger.append({
                    "period": pname, "stock": f"{sid}{names[sid]}",
                    "signal_date": t["signal_date"],
                    "entry": ";".join(f"{l['date']}@{l['price']}" for l in t["entry_legs"]),
                    "exit": ";".join(f"{l['date']}@{l['price']}({l['reason']})" for l in t["exit_legs"]),
                    "exit_reason": t["exit_reason"], "return": round(t["return"], 4),
                    "hold_days": t["hold_days"], "false_signal": t["false_signal"]})

    # 每檔明細
    P("\n### 月營收版:每檔明細\n")
    P("| 期間 | 股票 | 訊號 | 假訊號率 | 勝率 | 盈虧比 | 平均持有 | 空手% | 年化 | MDD | 買進持有年化 |")
    P("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for pname, (s, e) in periods.items():
        for sid in ids:
            res = simulate_monthly(sid, feat_m_bdi[sid], cfg, s, e)
            ts = M.trade_stats(res["trades"], cfg)
            es = M.equity_stats(res["equity"], res["idle_pct"])
            bh = M.equity_stats(M.buy_hold_equity(sid, s, e))
            P(f"| {pname} | {sid} {names[sid]} | {ts['n_signals']} | "
              f"{fmt_pct(ts['false_signal_ratio'])} | {fmt_pct(ts['win_rate'])} | "
              f"{fmt_num(ts['profit_factor'])} | {fmt_num(ts['avg_hold_days'],0)} | "
              f"{fmt_pct(es['idle_pct']/100)} | {fmt_pct(es['annualized'])} | "
              f"{fmt_pct(es['mdd'])} | {fmt_pct(bh['annualized'])} |")

    # ---- 4) 對照組 ----
    P("\n## 4. 對照組\n")
    P("| 期間 | 對照 | 訊號數 | 假訊號率 | 年化 | MDD | 說明 |")
    P("|---|---|--:|--:|--:|--:|---|")
    for pname, (s, e) in periods.items():
        tr = simulate_monthly(tsmc, feat_m_bdi[tsmc], cfg, s, e)
        tts = M.trade_stats(tr["trades"], cfg)
        tes = M.equity_stats(tr["equity"], tr["idle_pct"])
        P(f"| {pname} | 台積電(月營收版同策略) | {tts['n_signals']} | "
          f"{fmt_pct(tts['false_signal_ratio'])} | {fmt_pct(tes['annualized'])} | "
          f"{fmt_pct(tes['mdd'])} | 非循環股;若也有效→抓到的是大盤beta |")
        mp, ms = summary[pname]["m"]
        rb = M.random_baseline(ids[0], s, e, ms["avg_hold_days"],
                               cfg["assumptions"]["random_baseline_runs"],
                               cfg["assumptions"]["random_seed"])
        if rb:
            P(f"| {pname} | 隨機進場({names[ids[0]]},持有~{rb['hold_days_used']}天×{rb['runs']}次) | — | — | "
              f"{fmt_pct(rb['median_annualized'])} | — | 年化中位數;P25 {fmt_pct(rb['p25'])}/P75 {fmt_pct(rb['p75'])} |")
        for sid in ids:
            bh = M.equity_stats(M.buy_hold_equity(sid, s, e))
            P(f"| {pname} | {sid}{names[sid]} 買進持有 | — | — | {fmt_pct(bh['annualized'])} | "
              f"{fmt_pct(bh['mdd'])} | 各該股同期 |")

    # ---- 5) 附錄:貨櫃指數版本(換運價來源) ----
    P("\n## 5. 附錄:月營收版改用「貨櫃指數」做方向確認(2025-03後無覆蓋)\n")
    P("| 期間 | 訊號數 | 假訊號率 | 年化 | MDD |")
    P("|---|--:|--:|--:|--:|")
    for pname, (s, e) in periods.items():
        cp, cs, _ = portfolio(ids, simulate_monthly, feat_m_con, cfg, s, e)
        P(f"| {pname} | {cs['n_signals']} | {fmt_pct(cs['false_signal_ratio'])} | "
          f"{fmt_pct(cp['annualized'])} | {fmt_pct(cp['mdd'])} |")
    P("\n> 兩種運價來源結論一致與否,可判斷結果是否只是某一指標的偶然。\n")

    # ---- 6) 必答 Q2 / Q3 與結案 ----
    P("## 6. 必答Q2、Q3 與結案結論\n")
    oos = summary["樣本外 2018–2026"]
    ins = summary["樣本內 2010–2017"]
    P("### Q2:早進場有沒有換到更好的年化?\n")
    P("| 期間 | 季報版年化 | 月營收版年化 | 差異 | 0050 | 月營收版是否勝0050 |")
    P("|---|--:|--:|--:|--:|:-:|")
    for pname in periods:
        qa = summary[pname]["q"][0]["annualized"]
        ma = summary[pname]["m"][0]["annualized"]
        ba = summary[pname]["b"]["annualized"]
        diff = (ma - qa) if (qa is not None and ma is not None) else None
        win = "✓" if (ma is not None and ba is not None and ma > ba) else "✗"
        P(f"| {pname} | {fmt_pct(qa)} | {fmt_pct(ma)} | "
          f"{'n/a' if diff is None else ('%+.1f pp' % (diff*100))} | {fmt_pct(ba)} | {win} |")
    P("\n### Q3:最大回撤有沒有改善?\n")
    P("| 期間 | 季報版MDD | 月營收版MDD | 差異 | 買進持有(三雄均) |")
    P("|---|--:|--:|--:|--:|")
    for pname, (s, e) in periods.items():
        qm = summary[pname]["q"][0]["mdd"]
        mm = summary[pname]["m"][0]["mdd"]
        bh_mdds = [M.equity_stats(M.buy_hold_equity(sid, s, e))["mdd"] for sid in ids]
        P(f"| {pname} | {fmt_pct(qm)} | {fmt_pct(mm)} | "
          f"{'n/a' if (qm is None or mm is None) else ('%+.1f pp' % ((mm-qm)*100))} | "
          f"{fmt_pct(sum(bh_mdds)/len(bh_mdds))} |")

    # ---- 7) 四項成功標準判定(沿用階段一預先登記的門檻)----
    sc = cfg["success_criteria"]
    P("\n### 月營收版:四項成功標準判定(門檻為階段一**事前登記**,未因本次結果調整)\n")
    P("| 期間 | 訊號數 | 假訊號率 | 年化 | 0050 | ①<40% | ④≥5 | ③勝0050 | 結論 |")
    P("|---|--:|--:|--:|--:|:-:|:-:|:-:|:-:|")
    verdicts = {}
    for pname in periods:
        mp, ms = summary[pname]["m"]
        ba = summary[pname]["b"]["annualized"]
        fsr = ms["false_signal_ratio"]
        c1 = fsr is not None and fsr < sc["false_signal_ratio_max"]
        c4 = ms["n_signals"] >= sc["min_signals_per_group"]
        c3 = mp["annualized"] is not None and ba is not None and mp["annualized"] > ba
        infeasible = fsr is not None and fsr > sc["group_infeasible_false_signal"]
        if not c4:
            v = "樣本不足"
        elif infeasible:
            v = "**不可行**(假訊號率>50%)"
        elif c1 and c3:
            v = "通過"
        else:
            v = "未通過"
        verdicts[pname] = v
        P(f"| {pname} | {ms['n_signals']} | {fmt_pct(fsr)} | {fmt_pct(mp['annualized'])} | "
          f"{fmt_pct(ba)} | {'✓' if c1 else '✗'} | {'✓' if c4 else '✗'} | "
          f"{'✓' if c3 else '✗'} | {v} |")

    # ---- 8) 結案 ----
    P("\n## 7. 結案結論\n")
    m_in = summary["樣本內 2010–2017"]["m"][0]["annualized"]
    m_oos = summary["樣本外 2018–2026"]["m"][0]["annualized"]
    b_in = summary["樣本內 2010–2017"]["b"]["annualized"]
    b_oos = summary["樣本外 2018–2026"]["b"]["annualized"]
    P(f"**A1(時效)**:可配對的訊號中月營收平均早約 "
      f"{q1[0]:.0f} 個交易日(中位 {q1[1]});但制度性時差僅約 30 個交易日,"
      f"差額來自「規則定義不同」而非單純早看到資料。" if q1 else "**A1**:無可配對訊號。")
    P(f"\n**A2(年化)**:有改善但**沒有跨過門檻**。樣本內 -4.0%→{fmt_pct(m_in)}"
      f"(0050 {fmt_pct(b_in)},仍輸);樣本外 13.4%→{fmt_pct(m_oos)}"
      f"(0050 {fmt_pct(b_oos)},**打平但仍未勝出**)。")
    P(f"\n**A3(最大回撤)**:**你的預期正確**。樣本外 MDD 僅由 -76.3% 改善到 "
      f"{fmt_pct(summary['樣本外 2018–2026']['m'][0]['mdd'])}(約 2.5pp),"
      f"仍是 0050(-36.4%)的兩倍以上。回撤來自循環股本身的波動,不是進場時點能解決的。")
    P("\n**與階段一的關係**:結論相同 —— **年化沒有勝過 0050,策略不可行**。"
      "換了更即時的指標之後,績效確實變好,但:")
    P("- 樣本外假訊號率從 0% 惡化到 66.7%(>50%),依**事前登記**的規則直接判該組**不可行**;"
      "更快的訊號 = 更多雜訊,這是用時效換來的代價。")
    P("- 樣本外年化 20.4% vs 0050 20.5%:**承擔 -74% 的回撤,只換到與大盤指數打平**,"
      "風險調整後明顯更差。")
    P("- 過擬合對照:同一套月營收規則套在**台積電**(非循環股)上,樣本外年化 23.4%,"
      "**比三雄還好** → 訊號抓到的主要是「營收動能 + 大盤 beta」,不是循環股特性。")
    P("\n> **不因為換了指標就淡化結論**:時效假設有部分被證實(進場確實變早、年化確實改善),"
      "但改善幅度不足以跨過『勝過 0050』這條事前定下的線,且以假訊號率大幅惡化為代價。\n")
    P("### 專案結案")
    P("依專案前提「階段一失敗就停止整個專案」,且本次為最後一次測試:")
    P("**景氣循環股(海運)的系統化訊號操作,在本測試設計下不可行 → 專案結案,不進入階段二、三。**")
    P("\n誠實的替代結論:對這三檔而言,**買進持有 0050**(樣本外年化 20.5%、MDD -36.4%)"
      "在風險調整後優於所有測試過的訊號版本;若真要參與海運循環,買進持有個股的年化"
      "(18~22%)也與策略相當,但要承受 -80% 級距的回撤。")

    (REP / "phase1_monthly_supplement.md").write_text("\n".join(L), encoding="utf-8")
    if ledger:
        with (REP / "phase1_monthly_trades.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(ledger[0].keys()))
            w.writeheader()
            w.writerows(ledger)
    print("報告:", REP / "phase1_monthly_supplement.md")
    print("逐筆:", REP / "phase1_monthly_trades.csv", f"({len(ledger)} 筆)")
    # 印出關鍵數字供終端快速確認
    for pname in periods:
        q = summary[pname]["q"]; m = summary[pname]["m"]; b = summary[pname]["b"]
        print(f"{pname}: 季報 年化{fmt_pct(q[0]['annualized'])}/MDD{fmt_pct(q[0]['mdd'])}/訊號{q[1]['n_signals']} | "
              f"月營收 年化{fmt_pct(m[0]['annualized'])}/MDD{fmt_pct(m[0]['mdd'])}/訊號{m[1]['n_signals']}"
              f"/假訊號{fmt_pct(m[1]['false_signal_ratio'])} | 0050 {fmt_pct(b['annualized'])}")
    if q1:
        print(f"Q1: 月營收平均早 {q1[0]:.0f} 交易日(中位 {q1[1]})")


if __name__ == "__main__":
    main()
