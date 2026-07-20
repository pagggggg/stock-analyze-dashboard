"""
報告輸出 (report.py)
====================
把所有結果組成「一份 markdown 報告」。設計原則:

  1. 先給三行摘要   → 沒時間的人看前三行就好。
  2. 白話註解專有名詞 → 每個財報術語首次出現處,加一行 `> 💡 ...` 白話解釋。
  3. 每個數字都標來源 → 假設清單逐條列出 value + 來源。
  4. 攤開計算鏈      → 「從營收到 EPS」每一步都印出來。

輸出:回傳 markdown 字串 (由 main.py 寫檔)。
"""

from __future__ import annotations

from datetime import datetime

from .models import (
    ConsensusSnapshot,
    DashboardResult,
    EPSScenario,
    ExpectationGap,
    Guidance,
    PEBand,
    QuarterFinancials,
    ValuationResult,
)


# ---- 小工具:數字格式化 ----------------------------------------------
def _n(x: float, d: int = 1) -> str:
    """千分位 + 指定小數位。"""
    return f"{x:,.{d}f}"


def _pct(x: float, d: int = 1) -> str:
    return f"{x:.{d}f}%"


def _signed_pct(x: float, d: int = 1) -> str:
    """帶正負號的百分比 (預期差用)。"""
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.{d}f}%"


# ======================================================================
# 主函式
# ======================================================================
def build_report(
    guidance: Guidance,
    scenarios: dict[str, EPSScenario],
    valuation: ValuationResult,
    gaps: list[ExpectationGap],
    backtest: list[dict],
    hist_avg: dict | None,
    trailing_info: tuple[float, list[str]] | None,
    data_mode: str,
    financials: list[QuarterFinancials] | None = None,
    validation_warnings: list[dict] | None = None,
    current_price: tuple[float, str] | None = None,
    supplement_labels: set[str] | None = None,
    dashboard: DashboardResult | None = None,
    consensus_snapshot: ConsensusSnapshot | None = None,
) -> str:
    """組出完整 markdown 報告字串。"""
    lines: list[str] = []
    A = lines.append  # 縮寫,少打字

    neutral = scenarios["中性"]
    opt = scenarios["樂觀"]
    pes = scenarios["悲觀"]
    supplement_labels = supplement_labels or set()

    ann = neutral.eps_annualized                       # 中性年化 EPS
    pb = valuation.pe_band
    mid_price = valuation.price_matrix["中性"]["mid"]   # 估值中樞
    lo_price = valuation.price_matrix["悲觀"]["low"]
    hi_price = valuation.price_matrix["樂觀"]["high"]

    # 現價相關(有抓到才算)
    price = market_pe = price_vs_center = premium = None
    price_src = ""
    if current_price:
        price, price_src = current_price
        market_pe = price / ann if ann else 0.0
        price_vs_center = (price - mid_price) / price * 100.0 if price else 0.0
        premium = (market_pe - pb.pe_mid) / pb.pe_mid * 100.0 if pb.pe_mid else 0.0

    # ---- 標題 --------------------------------------------------------
    A(f"# 台積電 (2330.TW) EPS 試算與估值報告 — {guidance.quarter_label}")
    A("")
    A(f"> 產生時間:{datetime.now().strftime('%Y-%m-%d %H:%M')}　|　"
      f"數據模式:**{data_mode}**")
    A("")
    A("> ⚠️ 本報告為個人試算工具產出,所有數字請對照原始來源核實,不構成投資建議。")
    A("")

    # ---- 🎯 三行摘要 -------------------------------------------------
    A("## 🎯 三行摘要(沒時間就看這裡)")
    A("")
    A(f"1. **我估 {guidance.quarter_label} 單季 EPS ≈ NT$ {_n(neutral.eps_quarter, 2)}**"
      f"(悲觀 {_n(pes.eps_quarter, 2)} ~ 樂觀 {_n(opt.eps_quarter, 2)})。")
    A(f"2. **合理股價中樞約 NT$ {_n(mid_price, 0)}**"
      f"(全區間 NT$ {_n(lo_price, 0)} ~ {_n(hi_price, 0)};= 年化EPS × 歷史本益比)。")
    if price is not None:
        pd_word = "溢價" if premium >= 0 else "折價"
        A(f"3. **目前股價 NT$ {_n(price, 0)},隱含本益比 {_n(market_pe, 1)}x**"
          f"(vs 歷史中樞 {_n(pb.pe_mid, 1)}x → {pd_word} {_signed_pct(premium)})。")
    else:
        A("3. **目前股價:未取得**(可在 config 的 `valuation.current_price` 填入,或用 `--data-mode auto` 自動抓)。")
    A("")
    A("> 💡 **EPS(每股盈餘)**=公司稅後淨利 ÷ 股數,白話就是「每一股幫你賺多少錢」,單位台幣/股。")
    A(">")
    A("> 💡 **本益比(PE)**=股價 ÷ EPS,市場願意為每 1 元盈餘付幾元;數字越高=市場越願意給溢價(通常反映成長期待)。")
    A(">")
    A("> 💡 **估值中樞**=最可能的合理股價中間值,這裡用「中性情境的年化EPS × 歷史平均本益比」估算。")
    A("")

    # ---- 一句話結論 --------------------------------------------------
    A("## 一、結論速覽")
    A("")
    A(f"- **中性情境單季 EPS:NT$ {_n(neutral.eps_quarter, 2)}**　"
      f"(悲觀 {_n(pes.eps_quarter, 2)} ~ 樂觀 {_n(opt.eps_quarter, 2)})")
    A(f"- **中性情境年化 EPS:NT$ {_n(ann, 2)}**　"
      f"(年化方式:{valuation.annualize_method})")
    A("> 💡 **年化**=把「單季」EPS 換算成「一整年」,才能拿去和以「年」為基礎的本益比相乘。")
    A(f"- **估值中樞:NT$ {_n(mid_price, 0)}**　"
      f"(全矩陣區間 {_n(lo_price, 0)} ~ {_n(hi_price, 0)})")
    if price is not None:
        pd_word = "溢價(偏貴)" if premium >= 0 else "折價(偏便宜)"
        A(f"- **現價 NT$ {_n(price, 0)}**　→ 相對估值中樞 {_signed_pct(price_vs_center)}"
          f",市場本益比 {_n(market_pe, 1)}x vs 歷史中樞 {_n(pb.pe_mid, 1)}x(**{pd_word}**)")
    if gaps:
        for g in gaps:
            direction = "高於" if g.diff_pct >= 0 else "低於"
            caveat = "" if g.scope.startswith("單季") else "　_(年化口徑,詳見預期差節提醒)_"
            A(f"- **預期差 ({g.scope}):我的試算 {direction}共識 "
              f"{_signed_pct(g.diff_pct)}**　"
              f"(我 {_n(g.my_eps, 2)} vs 共識 {_n(g.consensus_eps.value, 2)}){caveat}")
    # 儀表板重點:PEG(若有)
    if dashboard:
        peg = next((m for m in dashboard.metrics if m.key == "peg"), None)
        if peg and peg.value is not None:
            A(f"- **PEG:{peg.value:.2f}({peg.verdict})**　"
              f"→ 綜合『貴不貴』與『成長』的一眼指標(詳見估值儀表板)")
    # 共識監控:最重要的訊號放結論(功能4)
    if consensus_snapshot and consensus_snapshot.eps_y0 is not None:
        cs = consensus_snapshot
        A(f"- **📡 共識監控(最該盯的訊號):2026 全年共識 EPS {_n(cs.eps_y0, 2)}"
          f",較上次記錄【{cs.y0_change}】**(詳見共識監控節)")
    A("")

    # ---- 假設清單 (逐條可檢查) --------------------------------------
    A("## 二、假設清單(逐條檢查,每個數字都標來源)")
    A("")
    A("### A. 法說會指引(公司官方)")
    A("")
    A("| 項目 | 數值 | 來源 |")
    A("| --- | --- | --- |")
    A(f"| 季營收(十億美元) | {_n(guidance.revenue_usd.low, 2)} ~ "
      f"{_n(guidance.revenue_usd.high, 2)} | {guidance.revenue_usd.source} |")
    A(f"| 毛利率 | {_pct(guidance.gross_margin.low)} ~ "
      f"{_pct(guidance.gross_margin.high)} | {guidance.gross_margin.source} |")
    A(f"| 匯率(1美元=?台幣) | {_n(guidance.fx_usdtwd.value, 2)} | "
      f"{guidance.fx_usdtwd.source} |")
    A("")
    A("> 💡 **毛利率**=(營收 − 生產成本)÷ 營收;台積電製程越先進、良率越高,毛利率越高。")
    A("")
    A("### B. 模型假設(指引未提供,採歷史平均或自行假設)")
    A("")
    A("| 項目 | 數值 | 來源 |")
    A("| --- | --- | --- |")
    A(f"| 營業費用率 | {_pct(guidance.opex_ratio.value)} | {guidance.opex_ratio.source} |")
    A(f"| 有效稅率 | {_pct(guidance.tax_rate.value)} | {guidance.tax_rate.source} |")
    A(f"| 業外收支佔營收比 | {_pct(guidance.non_op_ratio.value)} | {guidance.non_op_ratio.source} |")
    A(f"| 流通股數(十億股) | {_n(guidance.shares_bn.value, 2)} | {guidance.shares_bn.source} |")
    A("")
    A("> 💡 **營業費用率**=營業費用(研發+管理+行銷)÷ 營收;毛利扣掉它,才是本業賺的「營業利益」。")
    A(">")
    A("> 💡 **業外收支(業外損益)**=本業以外的損益,台積電主要是利息與投資收益,通常小幅加分。")
    A(">")
    A("> 💡 **有效稅率**=實際繳的所得稅 ÷ 稅前淨利;2026 起受全球最低稅負(約15%)影響而略升。")
    A("")
    if hist_avg:
        A(f"> 📌 近 {len(hist_avg['quarters_used'])} 季歷史平均參考"
          f"({'、'.join(hist_avg['quarters_used'])}):"
          f"毛利率 {_pct(hist_avg['gross_margin'])}、"
          f"營業費用率 {_pct(hist_avg['opex_ratio'])}、"
          f"稅率 {_pct(hist_avg['tax_rate'])}、"
          f"業外比 {_pct(hist_avg['non_op_ratio'])}。")
        A("")

    # ---- 三情境定義 --------------------------------------------------
    A("### C. 三情境如何設定")
    A("")
    A("| 情境 | 營收(十億美元) | 毛利率 | 說明 |")
    A("| --- | --- | --- | --- |")
    A(f"| 樂觀 | {_n(opt.revenue_usd_bn, 2)} | {_pct(opt.gross_margin_pct)} | 營收高標 × 毛利率高標 |")
    A(f"| 中性 | {_n(neutral.revenue_usd_bn, 2)} | {_pct(neutral.gross_margin_pct)} | 區間中點 |")
    A(f"| 悲觀 | {_n(pes.revenue_usd_bn, 2)} | {_pct(pes.gross_margin_pct)} | 營收低標 × 毛利率低標 |")
    A("")
    A("> 匯率、營業費用率、稅率、業外比、股數三情境相同(僅營收與毛利率隨指引區間變動)。")
    A("")

    # ---- EPS 試算 (攤開計算鏈) --------------------------------------
    A("## 三、EPS 三情境試算")
    A("")
    A("### 結果總表")
    A("")
    A("| 損益項目(十億台幣) | 樂觀 | 中性 | 悲觀 |")
    A("| --- | ---: | ---: | ---: |")
    A(f"| 營收(美元→台幣) | {_n(opt.revenue_twd_bn)} | {_n(neutral.revenue_twd_bn)} | {_n(pes.revenue_twd_bn)} |")
    A(f"| ─ 毛利 | {_n(opt.gross_profit_twd_bn)} | {_n(neutral.gross_profit_twd_bn)} | {_n(pes.gross_profit_twd_bn)} |")
    A(f"| ─ 營業費用 | {_n(opt.opex_twd_bn)} | {_n(neutral.opex_twd_bn)} | {_n(pes.opex_twd_bn)} |")
    A(f"| = 營業利益 | {_n(opt.operating_income_twd_bn)} | {_n(neutral.operating_income_twd_bn)} | {_n(pes.operating_income_twd_bn)} |")
    A(f"| + 業外收支 | {_n(opt.non_op_twd_bn)} | {_n(neutral.non_op_twd_bn)} | {_n(pes.non_op_twd_bn)} |")
    A(f"| = 稅前淨利 | {_n(opt.pretax_income_twd_bn)} | {_n(neutral.pretax_income_twd_bn)} | {_n(pes.pretax_income_twd_bn)} |")
    A(f"| = 稅後淨利 | {_n(opt.net_income_twd_bn)} | {_n(neutral.net_income_twd_bn)} | {_n(pes.net_income_twd_bn)} |")
    A(f"| **單季 EPS(台幣)** | **{_n(opt.eps_quarter, 2)}** | **{_n(neutral.eps_quarter, 2)}** | **{_n(pes.eps_quarter, 2)}** |")
    A("")
    A("> 💡 **營業利益**=毛利 − 營業費用,代表「本業」實際賺的錢(還沒加業外、扣稅)。"
      "營業利益 ÷ 營收 = 營業淨利率,正好對得上法說的營益率展望。")
    A("")

    # 中性情境逐步展開,示範計算邏輯
    A("### 中性情境計算鏈(示範,方便逐步核對)")
    A("")
    A("```text")
    A(f"營收(美元) {_n(neutral.revenue_usd_bn, 2)} × 匯率 {_n(neutral.fx_usdtwd, 2)}"
      f"                 = 營收(台幣)   {_n(neutral.revenue_twd_bn)}")
    A(f"營收(台幣) {_n(neutral.revenue_twd_bn)} × 毛利率 {_pct(neutral.gross_margin_pct)}"
      f"          = 毛利         {_n(neutral.gross_profit_twd_bn)}")
    A(f"營收(台幣) {_n(neutral.revenue_twd_bn)} × 營業費用率 {_pct(neutral.opex_ratio_pct)}"
      f"      = 營業費用     {_n(neutral.opex_twd_bn)}")
    A(f"毛利 − 營業費用"
      f"                                = 營業利益     {_n(neutral.operating_income_twd_bn)}")
    A(f"營收(台幣) × 業外比 {_pct(neutral.non_op_ratio_pct)}"
      f"                    = 業外收支     {_n(neutral.non_op_twd_bn)}")
    A(f"營業利益 + 業外收支"
      f"                            = 稅前淨利     {_n(neutral.pretax_income_twd_bn)}")
    A(f"稅前淨利 × (1 − 稅率 {_pct(neutral.tax_rate_pct)})"
      f"                 = 稅後淨利     {_n(neutral.net_income_twd_bn)}")
    A(f"稅後淨利 {_n(neutral.net_income_twd_bn)} ÷ 股數 {_n(neutral.shares_bn, 2)}"
      f"          = 單季 EPS     {_n(neutral.eps_quarter, 2)}")
    A("```")
    A("")

    # ---- 估值 --------------------------------------------------------
    A("## 四、估值價格矩陣")
    A("")
    A("> 💡 **TTM(滾動12個月)**=最近 4 季加總。這裡用「最近3季實際 + 本季試算」湊成一整年,"
      "比「單季×4」更貼近真實(避免淡旺季失真)。")
    A("")
    A(f"- 年化 EPS 方式:**{valuation.annualize_method}**")
    if trailing_info and valuation.annualize_method.startswith("TTM"):
        total, qs = trailing_info
        note = ""
        used_supp = [q for q in qs if q in supplement_labels]
        if used_supp:
            note = f"　_(含補充季 {'、'.join(used_supp)}:法說實際+推算,非 API 值)_"
        A(f"- 前 3 季實際 EPS 加總:NT$ {_n(total, 2)}"
          f"({' + '.join(qs)}){note}")
    A(f"- 本益比區間({pb.years_covered}):"
      f"低 **{_n(pb.pe_low, 1)}x** / 中 **{_n(pb.pe_mid, 1)}x** / 高 **{_n(pb.pe_high, 1)}x**"
      f"　來源:{pb.source}")
    A("")
    A("### 價格矩陣(NT$ / 股)= 年化 EPS × 本益比")
    A("")
    A("| 情境(年化EPS) | 低本益比 | 中本益比 | 高本益比 |")
    A("| --- | ---: | ---: | ---: |")
    for name in ("樂觀", "中性", "悲觀"):
        sc = scenarios[name]
        pm = valuation.price_matrix[name]
        A(f"| {name}(EPS {_n(sc.eps_annualized, 2)}) | "
          f"{_n(pm['low'], 0)} | {_n(pm['mid'], 0)} | {_n(pm['high'], 0)} |")
    A("")

    # ---- 現價 vs 估值 (NEW) -----------------------------------------
    A("## 五、現價 vs 估值(市場現在買貴還是買便宜?)")
    A("")
    A("> 💡 **溢價 / 折價**=市場現在給的本益比,比歷史常態「高」就是溢價(偏貴)、「低」就是折價(偏便宜)。")
    A("")
    if price is None:
        A("_(未取得現價,略過。用 `--data-mode auto` 自動抓 TWSE 收盤,或在 config 填 `valuation.current_price`。)_")
    else:
        vs_word = "貴" if price_vs_center >= 0 else "便宜"
        pd_word = "溢價" if premium >= 0 else "折價"
        A("| 指標 | 數值 | 說明 |")
        A("| --- | ---: | --- |")
        A(f"| 目前股價 | NT$ {_n(price, 0)} | {price_src} |")
        A(f"| 我的估值中樞(中性) | NT$ {_n(mid_price, 0)} | 中性年化EPS {_n(ann, 2)} × 歷史中樞PE {_n(pb.pe_mid, 1)}x |")
        A(f"| 現價 vs 中樞 | {_signed_pct(price_vs_center)} | 現價比我的合理中樞**{vs_word}** {_pct(abs(price_vs_center))} |")
        A(f"| 市場給的本益比 | {_n(market_pe, 1)}x | 現價 {_n(price, 0)} ÷ 年化EPS {_n(ann, 2)} |")
        A(f"| 歷史中樞本益比 | {_n(pb.pe_mid, 1)}x | 近10年每日本益比平均({pb.years_covered}) |")
        A(f"| 溢價 / 折價 | {_signed_pct(premium)} | 市場PE 比歷史中樞**{'高' if premium >= 0 else '低'}** {_pct(abs(premium))} → **{pd_word}** |")
        A("")
        # 白話結論
        if premium >= 0:
            A(f"**白話結論**:市場現在用約 **{_n(market_pe, 1)} 倍**本益比買台積電,"
              f"比近10年常態的 {_n(pb.pe_mid, 1)} 倍**高出約 {_pct(abs(premium), 0)}**,屬於**溢價**——"
              "代表市場願意為它的成長多付一點錢。若你認為成長能延續,溢價有其道理;"
              "若擔心景氣或競爭,這段溢價就是潛在的回檔空間。")
        else:
            A(f"**白話結論**:市場現在用約 **{_n(market_pe, 1)} 倍**本益比買台積電,"
              f"比近10年常態的 {_n(pb.pe_mid, 1)} 倍**低約 {_pct(abs(premium), 0)}**,屬於**折價**——"
              "市場對它相對保守。若基本面沒有惡化,折價可能是機會;但也要留意市場是否在反映某種擔憂。")
        A("")
        A("> ⚠️ 口徑提醒:這裡的本益比用「年化EPS(含本季試算)」= 前瞻本益比;"
          "歷史區間是 TWSE 以「過去4季實際」計算。成長期的前瞻PE 通常會比歷史trailing PE 低一些,"
          "兩者非完全同口徑,**看相對高低與趨勢即可,別當精準門檻**。")
    A("")

    # ---- 六、估值儀表板 (功能1) ------------------------------------
    A("## 六、估值儀表板(多指標,全部即時連動現價)")
    A("")
    if dashboard is None:
        A("_(未取得現價,無法計算即時指標。用 `--data-mode auto`,或在 config 填 `valuation.current_price`。)_")
        A("")
    else:
        d = dashboard
        A(f"> 現價 NT$ {_n(d.price, 0)}　市值 {d.market_cap_bn / 1000:,.1f} 兆　"
          f"年化EPS {_n(d.ann_eps, 2)}(以下 4 個指標都會隨『現價』每天連動)")
        A("")
        A("| 指標 | 目前值 | 判讀 | 這在衡量什麼 | 歷史/參考區間 |")
        A("| --- | ---: | :--: | --- | --- |")
        for m in d.metrics:
            A(f"| **{m.name}** | {m.display} | {m.verdict} | {m.measures} | {m.reference} |")
        A("")
        A("**算式攤開(核對用):**")
        A("")
        for m in d.metrics:
            if m.value is not None:
                A(f"- {m.name}:{m.formula}")
            else:
                A(f"- {m.name}:_資料不足_")
        A("")
        A("> 💡 **PEG**=前瞻PE ÷ 盈餘成長率,把「貴」和「成長」合起來看:1 附近算合理,<1 難得便宜,>2 偏貴。")
        A(">")
        A("> 💡 **FCF Yield(自由現金流殖利率)**=公司一年賺到的「可自由運用現金」÷ 市值;越高=用現價買越划算。")
        A(">")
        A("> 💡 **EV/EBITDA**=(市值+負債−現金)÷ 息前稅前折舊攤銷前獲利;把負債與現金也算進來,較能跨公司比。")
        A("")

    # ---- 七、判讀速查表 (功能3) ------------------------------------
    A("## 七、判讀速查表(單一指標不下結論,務必交叉看)")
    A("")
    if dashboard is None:
        A("_(需先有即時指標才能判讀。)_")
    else:
        A("| 指標 | 目前值 | 便宜 / 合理 / 貴 門檻 | 白話判讀 |")
        A("| --- | ---: | --- | :--: |")
        for m in dashboard.metrics:
            A(f"| {m.name} | {m.display} | {m.thresholds} | 目前 **{m.verdict}** |")
        A("")
        A("> ⚠️ **務必交叉看,別用單一指標下結論**:例如前瞻PE 看起來貴,但若成長夠快(PEG 反而合理),"
          "貴得可能有道理;FCF Yield 偏低要看是不是正在大幅擴產(資本支出高)。"
          "上面門檻是經驗法則、非鐵律,請搭配基本面與產業循環一起判斷。")
    A("")

    # ---- 八、數字如何連動 (功能2) ----------------------------------
    A("## 八、數字如何連動(哪些是雜訊、哪些是訊號)")
    A("")
    A("```text")
    A("【雜訊級|每天都在變】────────────────────────")
    A("  現價(TWSE 收盤,每日變動)")
    A("     ├─→ 前瞻PE    = 現價 ÷ 年化EPS")
    A("     ├─→ PEG       = 前瞻PE ÷ 成長率")
    A("     ├─→ FCF Yield = 近4季FCF ÷ 市值(市值 = 現價 × 股數)")
    A("     └─→ EV/EBITDA = 企業價值 ÷ EBITDA(企業價值含市值)")
    A("  ⇒ 這些每天都在動,多屬『雜訊』,別因單日漲跌就下結論。")
    A("")
    A("【訊號級|季度或事件才變】──────────────────")
    A("  法說指引(每季)─→ 我的EPS試算 ─→ 年化EPS ─┐")
    A("                                              ├─→ 前瞻PE、PEG")
    A("  共識EPS(季/事件)─→ 盈餘成長率 ────────────┘  (PEG 專屬)")
    A("  財報(每季)─→ 近4季FCF / EBITDA / 負債·現金 ─→ FCF Yield、EV/EBITDA")
    A("  ⇒ 這些變動才是『訊號』,其中【共識EPS 上修/下修】最該盯(見下一節)。")
    A("")
    A("【哪個指標被誰牽動】──────────────────────")
    A("  前瞻PE    ← 現價(日) + 年化EPS(季)")
    A("  PEG       ← 現價(日) + 年化EPS(季) + 共識EPS成長(季/事件)")
    A("  FCF Yield ← 現價(日) + 近4季FCF(季)")
    A("  EV/EBITDA ← 現價(日) + EBITDA·負債·現金(季)")
    A("```")
    A("")

    # ---- 九、共識EPS監控 (功能4) -----------------------------------
    A("## 九、共識EPS監控(最重要的訊號:上修還是下修?)")
    A("")
    A("> 💡 為什麼最重要:股價短期跟著情緒亂跳,但**分析師共識EPS 的『上修/下修』代表基本面預期真的在變**,"
      "比每天的股價波動更值得盯。每次執行都會把共識記到 `data/consensus_history.csv`,方便長期追蹤。")
    A("")
    if consensus_snapshot is None:
        A("_(未取得共識EPS。auto 模式會用 yfinance 抓;或在 config 的 `dashboard` 手填。)_")
    else:
        cs = consensus_snapshot
        A("| 項目 | 本次 | 上次記錄 | 變化 |")
        A("| --- | ---: | ---: | :--: |")
        A(f"| 2026(今年FY)共識EPS | {_n(cs.eps_y0, 2) if cs.eps_y0 else 'N/A'} | "
          f"{_n(cs.prev_eps_y0, 2) if cs.prev_eps_y0 else '—'} | **{cs.y0_change}** |")
        A(f"| 2027(明年FY)共識EPS | {_n(cs.eps_y1, 2) if cs.eps_y1 else 'N/A'} | "
          f"{_n(cs.prev_eps_y1, 2) if cs.prev_eps_y1 else '—'} | **{cs.y1_change}** |")
        if cs.eps_q0:
            A(f"| 當季(0q)共識EPS | {_n(cs.eps_q0, 2)} | — | (供單季預期差對照) |")
        A("")
        growth_txt = f"{cs.growth_pct:.1f}%" if cs.growth_pct is not None else "N/A"
        A(f"- 由此算出**盈餘成長率(2027 vs 2026)= {growth_txt}**,正是 PEG 的分母。")
        A(f"- 本次來源:{cs.source};分析師家數(今年):{cs.n_analysts or '—'}。")
        if cs.prev_as_of:
            A(f"- 上次記錄時間:{cs.prev_as_of}。")
        A("")
        A("> 📌 解讀:共識**上修**=市場對基本面更樂觀(常有股價支撐);**下修**=預期轉弱(留意風險);"
          "**持平**=暫無新訊號。這一格若變動,通常比股價本身更值得你花時間研究背後原因。")
    A("")

    # ---- 預期差 ------------------------------------------------------
    A("## 十、預期差(我的試算 vs 分析師共識)")
    A("")
    A("> 💡 **預期差**=我的試算 EPS 和「分析師共識」的差距%;> 0 代表我比市場樂觀(潛在超預期),< 0 代表比市場保守。")
    A("")
    if gaps:
        A("| 口徑 | 我的試算(中性) | 分析師共識 | 差距 | 共識來源 |")
        A("| --- | ---: | ---: | ---: | --- |")
        for g in gaps:
            A(f"| {g.scope} | {_n(g.my_eps, 2)} | {_n(g.consensus_eps.value, 2)} | "
              f"{_signed_pct(g.diff_pct)}(NT$ {_n(g.diff_abs, 2)}) | {g.consensus_eps.source} |")
        A("")
        A("> 差距 > 0:我比市場樂觀(潛在超預期);< 0:我比市場保守(潛在下修)。")
        A(">")
        A("> ⚠️ **口徑提醒**:單季是最乾淨的對照。全年那列的「我的試算」是用"
          f"**{valuation.annualize_method}**年化而來,和分析師的『完整會計年度(FY)』"
          "口徑不完全一致,差距會混入季節性與期間落差,請斟酌解讀。")
    else:
        A("_(未提供分析師共識,略過。可在 config 的 `consensus` 區塊填入 3Q26 共識 EPS。)_")
    A("")

    # ---- 模型回測 ----------------------------------------------------
    if backtest:
        A("## 十一、模型回測(同一套公式重算歷史,對照財報實際 EPS)")
        A("")
        A("> 💡 **回測誤差**=拿同一套公式去「重算過去幾季」,再和財報實際 EPS 比對的平均差距;越小代表這套公式越可信。")
        A("")
        A("用意:檢查這套公式套在過去幾季會不會系統性高估/低估,幫你判斷試算可信度。")
        A("")
        A("| 季度 | 模型回推 EPS | 財報實際 EPS | 差異 | 差異% |")
        A("| --- | ---: | ---: | ---: | ---: |")
        for b in backtest:
            A(f"| {b['quarter']} | {_n(b['model_eps'], 2)} | {_n(b['reported_eps'], 2)} | "
              f"{_n(b['diff'], 2)} | {_signed_pct(b['diff_pct'])} |")
        avg_abs = sum(abs(b["diff_pct"]) for b in backtest) / len(backtest)
        A("")
        A(f"> 平均絕對誤差:**{_pct(avg_abs)}**。誤差主要來自業外收支與稅率的季度波動。"
          "(回測只用『財報實際值』季度,不含補充推算季,以免自我印證。)")
        A("")

    # ---- 資料層明細 + 驗證 ------------------------------------------
    if financials:
        A("## 十二、資料層明細與驗證(近8季財務,每筆標來源)")
        A("")
        A("| 季度 | 營收(十億) | 毛利率 | 營業費用率 | 稅率 | 業外比 | 股數(十億) | EPS | 來源 |")
        A("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        for q in financials:
            tag = "🔸" if q.quarter in supplement_labels else ""
            A(f"| {tag}{q.quarter} | {_n(q.revenue_twd_bn)} | {_pct(q.gross_margin_pct)} | "
              f"{_pct(q.opex_ratio_pct)} | {_pct(q.tax_rate_pct)} | {_pct(q.non_op_ratio_pct)} | "
              f"{_n(q.shares_bn, 2)} | {_n(q.reported_eps, 2)} | {q.source} |")
        A("")
        if supplement_labels:
            A(f"> 🔸 標記者為「補充季」({'、'.join(sorted(supplement_labels))}):"
              "API 尚未收錄、由法說實際數 + 稅/業外假設推算,僅供年化用;正式財報出爐後請更新。")
            A("")

        # 驗證區:API vs 原始手動 CSV
        A("### 驗證:API 數字 vs 我原本的手動 CSV(差異 > 2% 就示警)")
        A("")
        if validation_warnings is None:
            A("_(此為手動模式,未做 API 對照。用 `--data-mode auto` 才會自動比對。)_")
        elif not validation_warnings:
            A("✅ 重疊季度中,API 與原始 CSV 的每個欄位差異皆 **≤ 2%**,可放心採用。")
        else:
            A(f"⚠️ 以下 **{len(validation_warnings)}** 筆欄位,API 與你原始 CSV 差異 > 2%,"
              "請人工核對哪個為準(通常 API 為實際財報值,原 CSV 若為手填近似值就更新它):")
            A("")
            A("| 季度 | 欄位 | 我的 CSV | API 值 | 差異 |")
            A("| --- | --- | ---: | ---: | ---: |")
            for w in validation_warnings:
                A(f"| {w['quarter']} | {w['field']} | {_n(w['csv'], 2)} | "
                  f"{_n(w['api'], 2)} | {_signed_pct(w['diff_pct'])} |")
        A("")

    # ---- 資料來源彙整 ------------------------------------------------
    A("## 十三、來源彙整與免責")
    A("")
    A(f"- 數據模式:**{data_mode}**。")
    if price is not None:
        A(f"- 現價來源:{price_src}。")
    A("- 估值儀表板(PEG成長率/FCF/EBITDA/負債現金)來源:yfinance;共識EPS 見「第九節」。")
    A("- 指引與假設來源見「第二節 假設清單」每一列。")
    A("- 近8季財務數據來源見「第十二節」每一季(FinMind / 手動 CSV / 補充推算,含抓取日期)。")
    A("- 本益比區間來源見「第四節」(TWSE 個股日本益比 / 手動 CSV,含抓取日期)。")
    A("- 本工具僅為個人試算,數字可能過時或有誤,**請務必回到原始出處核對**,不構成投資建議。")
    A("")

    return "\n".join(lines)
