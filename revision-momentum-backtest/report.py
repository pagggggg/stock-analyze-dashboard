"""
報告產生器 (report.py)
======================
把彙整好的結果寫成 backtest_report.md。原則:**誠實、可檢查、不挑好看的**。
結論(值不值得做成掃描器)是用數據「算出來」的,不是先射箭再畫靶。
"""

from __future__ import annotations


def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%" if isinstance(v, float) else str(v)


def _plain(v) -> str:
    return "—" if v is None else f"{v:.1f}"


def _metric_table(title: str, block_key: str, kind: dict, horizons: list[int]) -> str:
    """輸出某一組(signal/control/is/oos)的 3/6/12 月指標表。"""
    d = kind[block_key]
    lines = [
        f"**{title}**\n",
        "| 持有期 | 樣本數 | 勝率(超額>0) | 平均超額 | 中位數超額 | 平均個股報酬 | 平均基準報酬 | 平均最大回撤 | 最糟回撤 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for m in horizons:
        s = d[m]
        if s.get("n", 0) == 0:
            lines.append(f"| {m} 月 | 0 | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {m} 月 | {s['n']} | {_plain(s['win_rate'])}% | {_pct(s['avg_excess'])} | "
            f"{_pct(s['median_excess'])} | {_pct(s['avg_stock_ret'])} | {_pct(s['avg_bench_ret'])} | "
            f"{_pct(s['avg_max_drawdown'])} | {_pct(s['worst_max_drawdown'])} |"
        )
    return "\n".join(lines) + "\n"


def _edge_table(kind: dict, horizons: list[int]) -> str:
    """訊號 vs 對照:超額報酬差(edge)= 訊號平均超額 − 對照平均超額。"""
    lines = [
        "| 持有期 | 訊號平均超額 | 隨機對照平均超額 | 差值(edge) | 訊號勝率 | 對照勝率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for m in horizons:
        sg = kind["signal"][m]
        ct = kind["control"][m]
        if sg.get("n", 0) == 0 or ct.get("n", 0) == 0:
            lines.append(f"| {m} 月 | — | — | — | — | — |")
            continue
        edge = sg["avg_excess"] - ct["avg_excess"]
        lines.append(
            f"| {m} 月 | {_pct(sg['avg_excess'])} | {_pct(ct['avg_excess'])} | "
            f"**{edge:+.2f}pp** | {_plain(sg['win_rate'])}% | {_plain(ct['win_rate'])}% |"
        )
    return "\n".join(lines) + "\n"


def _score_kind(kind: dict, horizons: list[int]) -> dict:
    """用數據判斷這個訊號有沒有 edge:回傳 {passes, edges, reasons}。"""
    edges = {}
    pos_edge = pos_excess = pos_oos = 0
    considered = 0
    for m in horizons:
        sg = kind["signal"][m]
        ct = kind["control"][m]
        oos = kind["oos"][m]
        if sg.get("n", 0) == 0 or ct.get("n", 0) == 0:
            continue
        considered += 1
        edge = sg["avg_excess"] - ct["avg_excess"]
        edges[m] = edge
        if edge > 0:
            pos_edge += 1
        if sg["avg_excess"] > 0:
            pos_excess += 1
        if oos.get("n", 0) > 0 and oos["avg_excess"] > 0:
            pos_oos += 1
    passes = (
        considered > 0
        and pos_edge >= max(1, considered - 1)      # 幾乎每個持有期都贏對照
        and pos_excess >= max(1, considered - 1)    # 幾乎每個持有期超額為正
        and pos_oos >= 1                            # 至少一個持有期樣本外也為正
    )
    return {"passes": passes, "edges": edges, "considered": considered,
            "pos_edge": pos_edge, "pos_excess": pos_excess, "pos_oos": pos_oos}


KIND_LABEL = {
    "REV_ACCEL": "代理一:月營收 YoY 加速",
    "EPS_SURGE": "代理二:單季 EPS YoY 大增且超前趨勢",
    "ALL": "兩訊號合併(任一觸發)",
}


def build_report(meta: dict, kinds: dict, all_records: list[dict]) -> str:
    H = meta["horizons"]
    p = meta["params"]

    scores = {k: _score_kind(kinds[k], H) for k in kinds}

    # ── 產生「值不值得做掃描器」的總結論(依數據) ──────────────────
    passing = [k for k in ("REV_ACCEL", "EPS_SURGE") if scores[k]["passes"]]
    if passing:
        names = "、".join(KIND_LABEL[k] for k in passing)
        verdict_head = f"✅ 值得做成掃描器(但需帶條件):{names} 在扣掉隨機對照後仍有正向 edge。"
    elif any(scores[k]["pos_edge"] >= 1 and scores[k]["pos_excess"] >= 1 for k in ("REV_ACCEL", "EPS_SURGE")):
        verdict_head = "🟡 邊際、需再驗證:有部分持有期出現正 edge,但穩定度不足以無條件上線掃描器。"
    else:
        verdict_head = "❌ 不建議做成掃描器:扣掉隨機對照後,訊號沒有可靠的超額報酬(多半是大盤 beta)。"

    L: list[str] = []
    a = L.append

    a("# 盈餘修正動能回測報告(revision-momentum-backtest)\n")
    a(f"> 產生時間:{meta['generated_at']} ・ 基準:{meta['benchmark']}(元大台灣50)"
      f" ・ 資料來源:FinMind\n")

    # 0. 假設 + 結論速覽
    a("## 0. 假設與結論速覽\n")
    a("**受檢假設**:分析師共識 EPS 或財測指引被大幅上修的台股,後續 3/6/12 個月"
      "相對 0050 有超額報酬。\n")
    a("由於 FinMind 免費版**沒有分析師共識/財測資料**,改用兩個「實際數字突然轉強」"
      "的**代理訊號**近似「被上修」(定義見第 2 節)。\n")
    a(f"### 一句話結論\n\n{verdict_head}\n")

    # 各 kind 的 6 月結果摘要(速覽)
    a("### 速覽:各訊號 6 個月持有期(對照已扣除)\n")
    a("| 訊號 | 觸發事件數 | 6M 平均超額 | 6M 對照超額 | 6M edge | 6M 勝率 | 樣本外6M超額 |")
    a("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for k in ("REV_ACCEL", "EPS_SURGE", "ALL"):
        kd = kinds[k]
        sg = kd["signal"].get(6, {})
        ct = kd["control"].get(6, {})
        oos = kd["oos"].get(6, {})
        if sg.get("n", 0) and ct.get("n", 0):
            edge = sg["avg_excess"] - ct["avg_excess"]
            oos_txt = _pct(oos["avg_excess"]) if oos.get("n", 0) else "—"
            a(f"| {KIND_LABEL[k]} | {kd['n_events']} | {_pct(sg['avg_excess'])} | "
              f"{_pct(ct['avg_excess'])} | **{edge:+.2f}pp** | {_plain(sg['win_rate'])}% | {oos_txt} |")
        else:
            a(f"| {KIND_LABEL[k]} | {kd['n_events']} | — | — | — | — | — |")
    a("")

    # 1. 資料與樣本
    a("## 1. 資料、樣本與過擬合防護\n")
    a(f"- **股票池**:從全體上市櫃普通股(4 碼、twse+tpex,約 2,100 檔)以固定種子"
      f" `seed={meta['seed']}` **隨機抽樣 {meta['universe_size']} 檔**(無偏代表性樣本;"
      f"隨機對照組亦抽自同一池)。\n")
    a(f"- **實際有資料**:月營收 {meta['n_have_rev']} 檔、EPS {meta['n_have_eps']} 檔、"
      f"日股價 {meta['n_price_ok']} 檔。\n")
    a(f"- **研究時窗**:觸發日介於 {meta['study_start']} ~ {meta['study_end']}"
      f"(確保 12 個月未來報酬都跑得完)。\n")
    a("- **過擬合防護**:\n"
      f"  1. **只有 2 個可調參數**(題目上限 3):月營收加速門檻 `{p['REV_ACCEL_PP']}pp`、"
      f"單季EPS YoY 門檻 `{p['EPS_YOY_PCT']}%`。且**採題目給定預設值,未在樣本內搜尋最佳化**。\n"
      "  2. 持有期 3/6/12 月是**輸出維度、三個全報**,不挑對自己有利的。\n"
      f"  3. **樣本內/樣本外**以 `{meta['is_oos_split']}` 為界分段檢查跨期穩定度。\n"
      "  4. **隨機對照組**扣掉大盤 beta 與「那幾年隨便買都賺」的成分。\n")
    if meta["quota_hit"]:
        a("- ⚠ 本次執行中途撞到 FinMind 免費額度,部分股票資料未取得;"
          "已用實際取得的資料計算(可重跑補齊,快取會續抓)。\n")

    # 2. 訊號定義
    a("## 2. 訊號定義(參數化)與前視偏誤處理\n")
    a("**代理一 REV_ACCEL(月營收動能上修)**\n")
    a(f"- 觸發:當月營收 YoY − 上月營收 YoY ≥ **{p['REV_ACCEL_PP']} 個百分點**,且當月 YoY > 0。\n"
      "- 公開日:該月營收於**次月 10 日**前依法公告 → 以次月 10 日為可交易日。\n")
    a("**代理二 EPS_SURGE(單季盈餘上修)**\n")
    a(f"- 觸發:單季 EPS YoY ≥ **{p['EPS_YOY_PCT']}%**,且去年同季 EPS > 0,"
      "且本季 YoY **超越前四季 YoY 平均**(成長在加速)。\n"
      "- 公開日:採財報**法定申報期限**(Q1→5/15、Q2→8/14、Q3→11/14、Q4→隔年3/31),"
      "不早於實際公布,避免前視。\n")
    a("**進出場口徑(對兩訊號一致)**\n")
    a("- 進場:公開日**之後第一個交易日收盤**(嚴格晚於公開日,杜絕當日前視)。\n"
      "- 出場:進場日 + N 個日曆月後第一個交易日收盤。\n"
      "- 超額報酬 = 個股報酬 − 0050 在**同一組進出場日期**的報酬。\n")

    # 3. 主結果(逐 kind)
    a("## 3. 主結果:勝率 / 平均超額 / 最大回撤 / 樣本數(3/6/12 月)\n")
    for k in ("REV_ACCEL", "EPS_SURGE", "ALL"):
        kd = kinds[k]
        a(f"### 3.{['REV_ACCEL','EPS_SURGE','ALL'].index(k)+1} {KIND_LABEL[k]}"
          f"(觸發事件 {kd['n_events']} 次,涉及 {kd['n_stocks']} 檔)\n")
        a(_metric_table("訊號組", "signal", kd, H))
        a("> 「平均最大回撤」= 每筆交易在持有期內、收盤自波段高點的最深跌幅之平均;"
          "「最糟回撤」= 所有交易裡最深的一筆。\n")

    # 4. 對照組
    a("## 4. 對照組:同期隨機選股(證明不是大盤 beta)\n")
    a(f"每個真實觸發事件,在**同一公開日、同持有期**,自同池隨機抽"
      f" {meta['control_draws']} 檔股票下同樣的單(種子 `{meta['control_seed']}`)。"
      "訊號要真的有 alpha,必須在「edge = 訊號超額 − 對照超額」上穩定為正。\n")
    for k in ("REV_ACCEL", "EPS_SURGE", "ALL"):
        a(f"### 4.{['REV_ACCEL','EPS_SURGE','ALL'].index(k)+1} {KIND_LABEL[k]}\n")
        a(_edge_table(kinds[k], H))

    # 5. 樣本內 / 樣本外
    a("## 5. 樣本內 / 樣本外(跨期穩定度)\n")
    a(f"以 `{meta['is_oos_split']}` 為界。若只在樣本內有效,很可能是那段行情的巧合。\n")
    for k in ("REV_ACCEL", "EPS_SURGE", "ALL"):
        kd = kinds[k]
        a(f"### 5.{['REV_ACCEL','EPS_SURGE','ALL'].index(k)+1} {KIND_LABEL[k]}\n")
        a(_metric_table(f"樣本內(< {meta['is_oos_split']})", "is", kd, H))
        a(_metric_table(f"樣本外(≥ {meta['is_oos_split']})", "oos", kd, H))

    # 6. 失敗案例
    a("## 6. 失敗案例列表(觸發後照跌的)\n")
    _failure_section(a, all_records, H)

    # 7. 樣本清單
    a("## 7. 全樣本清單\n")
    a(f"- 全部 **{len(all_records)}** 筆交易紀錄(股票 × 觸發日 × 持有期 × 後續報酬)已寫出至"
      " `data/samples.csv`(含進出場日期與價格,可逐筆核對)。\n")
    _extremes_section(a, all_records)

    # 8. 限制
    a("## 8. 誠實限制(會影響解讀,務必看)\n")
    a("1. **沒有真正的分析師共識/財測資料**(FinMind 免費版不提供),用「實際數字突然轉強」"
      "當上修代理。這偏向『事後動能』,與『事前共識上修』不完全等價——這是最大的代理誤差。\n"
      "2. **股價未還原權值**(免費版無 `daily_adj`):個股與 0050 都用未還原收盤價,"
      "除息日有跳空缺口。高殖利率個股的報酬會被系統性低估;因對照組與基準同口徑,"
      "超額報酬受到部分抵銷但未完全消除。\n"
      "3. **抽樣而非全市場**:免費版不能單請求抓全市場,故用隨機抽樣代表全市場;"
      "換種子/加大 `UNIVERSE_SIZE` 可提高覆蓋。\n"
      "4. **未計交易成本/流動性/漲跌停**:小型股在觸發後可能無法以收盤價成交。\n"
      "5. **存活者/turnaround 排除**:去年同季 EPS ≤ 0 的個股在代理二被排除,"
      "虧轉盈的爆發個股不在統計內。\n"
      "6. **重疊樣本**:同一時間多檔一起觸發,超額報酬彼此不獨立,統計顯著性會被高估。\n")

    # 9. 最終結論
    a("## 9. 最終結論:值不值得做成掃描器?\n")
    a(f"{verdict_head}\n")
    _final_reasoning(a, kinds, scores, H)

    a("\n---\n*本報告由回測程式自動產生;所有數字可由 `data/samples.csv` 與 `cache/` 內原始"
      "資料重算。僅供研究,非投資建議。*\n")

    return "\n".join(L)


def _failure_section(a, all_records, H):
    fails = [r for r in all_records if r["stock_ret"] < 0]
    if not all_records:
        a("(無交易紀錄)\n")
        return
    # 各持有期失敗比例
    a("**各持有期「觸發後絕對報酬為負」的比例**\n")
    a("| 持有期 | 交易數 | 照跌(絕對<0) | 佔比 | 跑輸0050(超額<0) | 佔比 |")
    a("| --- | ---: | ---: | ---: | ---: | ---: |")
    for m in H:
        recs = [r for r in all_records if r["months"] == m]
        if not recs:
            a(f"| {m} 月 | 0 | 0 | — | 0 | — |")
            continue
        fell = sum(1 for r in recs if r["stock_ret"] < 0)
        lost = sum(1 for r in recs if r["excess_ret"] < 0)
        a(f"| {m} 月 | {len(recs)} | {fell} | {fell/len(recs)*100:.1f}% | "
          f"{lost} | {lost/len(recs)*100:.1f}% |")
    a("")
    # 最慘的 N 筆(不挑好看的:直接秀最爛的)
    worst = sorted(fails, key=lambda x: x["stock_ret"])[:40]
    a(f"**觸發後跌最慘的 40 筆(共 {len(fails)} 筆照跌;完整清單見 `data/samples.csv`)**\n")
    a("| 公開日 | 股票 | 訊號 | 期別 | 訊號值 | 持有 | 個股報酬 | 基準報酬 | 超額 | 最大回撤 |")
    a("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in worst:
        a(f"| {r['available_date']} | {r['stock_id']} | {r['kind']} | {r['period']} | "
          f"{r['metric']} | {r['months']}月 | {_pct(r['stock_ret'])} | {_pct(r['bench_ret'])} | "
          f"{_pct(r['excess_ret'])} | {_pct(r['max_drawdown'])} |")
    a("")


def _extremes_section(a, all_records):
    if not all_records:
        return
    best = sorted(all_records, key=lambda x: x["excess_ret"], reverse=True)[:15]
    a("**超額報酬最高的 15 筆(平衡呈現,非選樣)**\n")
    a("| 公開日 | 股票 | 訊號 | 期別 | 訊號值 | 持有 | 個股報酬 | 基準報酬 | 超額 |")
    a("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for r in best:
        a(f"| {r['available_date']} | {r['stock_id']} | {r['kind']} | {r['period']} | "
          f"{r['metric']} | {r['months']}月 | {_pct(r['stock_ret'])} | {_pct(r['bench_ret'])} | "
          f"{_pct(r['excess_ret'])} |")
    a("")


def _final_reasoning(a, kinds, scores, H):
    a("**逐訊號判讀(依數據):**\n")
    for k in ("REV_ACCEL", "EPS_SURGE"):
        sc = scores[k]
        kd = kinds[k]
        detail = (f"考察 {sc['considered']} 個持有期:超額>對照 {sc['pos_edge']} 個、"
                  f"超額為正 {sc['pos_excess']} 個、樣本外為正 {sc['pos_oos']} 個。")
        if sc["passes"]:
            a(f"- **{KIND_LABEL[k]}:通過。** {detail} 建議做成掃描器,"
              "但務必配合流動性與權值還原再驗證。\n")
        else:
            a(f"- **{KIND_LABEL[k]}:未通過。** {detail} 尚不足以無條件上線。\n")
    a("\n**做成掃描器的前提條件(無論上面結論):**\n"
      "1. 用還原權值股價重算(消除股利拖累)。2. 加流動性下限(排除無法成交的小型股)。"
      "3. 擴大股票池覆蓋率、換種子重測。4. 加計交易成本後 edge 仍要為正。\n")
