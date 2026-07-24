"""
選股篩選報告 (screener_report.py)
=================================
把 screener 的結果組成 reports/screener_result.md:
  一、門檻設定摘要(全部來自 config)
  二、第一層漏斗統計(每條刷掉幾檔、通過幾檔)
  三、第一層通過清單(附第二層四項達標與否 ✅/❌/⚠️資料不足)
  四、精華清單(兩層全過)
  五、誠實說明(資料不足一律不當通過)
"""

from __future__ import annotations

from .screener import L1_LABELS, L2_LABELS, ScreenResult

_SYM = {"pass": "✅", "fail": "❌", "na": "⚠️"}


def _cell(cond) -> str:
    """第二層品質欄位(⑦⑧⑨):數值 + 達標符號。"""
    if cond.status == "na":
        return "⚠️資料不足"
    return f"{cond.detail} {_SYM[cond.status]}"


def _cell_momentum(cond) -> str:
    """⑩修正動能:僅標記方向,不用達標符號(不列入兩層全過判定)。"""
    if cond.status == "na":
        return "⚠️資料不足"
    return cond.detail


def _fv(v, unit: str = "", dp: int = 1) -> str:
    """估值數字格式化(None→—)。"""
    return "—" if v is None else f"{v:,.{dp}f}{unit}"


def _rows(results: list[ScreenResult]) -> str:
    out = []
    for r in results:
        out.append(
            f"| {r.stock_id} | {r.name} | {r.industry} | "
            f"{_cell(r.layer2['q7'])} | {_cell(r.layer2['q8'])} | "
            f"{_cell(r.layer2['q9'])} | {_cell_momentum(r.layer2['q10'])} |"
        )
    return "\n".join(out)


def build_screener_report(results, funnel, cfg, generated: str, universe_desc: str) -> str:
    A = []
    w = A.append
    L1 = cfg["layer1"]
    L2 = cfg["layer2"]

    w("# 台股兩層選股篩選結果(screener_result.md)")
    w("")
    w(f"> 產生時間:{generated}　|　{universe_desc}")
    w("")
    w("> ⚠️ 只用**公開市場數據**做資格/品質研究,**無任何持倉或交易紀錄**。"
      "本表僅供**縮小研究範圍**,**非買進清單**;所有數字請回原始財報核實,不構成投資建議。")
    w("")

    # 一、門檻設定
    w("## 一、門檻設定(全部來自 config/screener.yaml,可自行調整)")
    w("")
    w("**第一層 資格篩選(6 條全過才進池):**")
    w("")
    w(f"1. 上市滿 **{L1['listed_years']['min']}** 年")
    w(f"2. 近 **{L1['eps_positive']['years']}** 年至少 **{L1['eps_positive']['min_positive_years']}** 年 EPS 為正")
    w(f"3. 近 **{L1['ocf_positive']['years']}** 年(≈12季)**累積 OCF 為正**,且至少 "
      f"**{L1['ocf_positive']['min_positive_years']}** 年全年 OCF 為正(看長期,濾單季波動)")
    fin = "(金融股 %d–%d 排除此條)" % (L1['debt_ratio']['financial_id_min'], L1['debt_ratio']['financial_id_max']) if L1['debt_ratio']['exclude_financial'] else ""
    dov = L1['debt_ratio'].get('industry_overrides') or {}
    ov_txt = "、".join(f"{k} <{v:.0f}%" for k, v in dov.items())
    w(f"4. **有息負債比**(短期借款+長期借款+應付公司債 ÷ 總資產)< **{L1['debt_ratio']['default_max_pct']:.0f}%**"
      f"(預設);產業覆寫:{ov_txt} {fin}")
    w(f"5. 近 **{L1['liquidity']['days']}** 日日均成交金額 > **{L1['liquidity']['min_avg_value']/1e8:.2f} 億**")
    w(f"6. 有最新財報(距今 ≤ **{L1['latest_report']['max_age_days']}** 天)")
    w("")
    w("**第二層 品質篩選(通過第一層者中標記,不淘汰):**")
    w("")
    w(f"7. 近 **{L2['revenue_cagr']['years']}** 年營收 CAGR > **{L2['revenue_cagr']['min_pct']}%**")
    w(f"8. 近 **{L2['gross_margin_trend']['years']}** 年毛利率斜率 ≥ **{L2['gross_margin_trend']['min_slope']}**(持平或上升)")
    w(f"9. 近 **{L2['roe']['years']}** 年 ROE 平均 > **{L2['roe']['min_avg_pct']}%**")
    w(f"10. 盈餘修正動能:近期共識EPS 上修(僅標記,來源 {L2['momentum']['source']})")
    w("")

    # 二、漏斗統計
    total = funnel["total"]
    w("## 二、第一層漏斗統計(每條各刷掉多少)")
    w("")
    w(f"- 進入評估的股票數:**{total}**")
    w(f"- **通過第一層(6 條全過):{funnel['layer1_pass']}** 檔")
    w(f"- **兩層全過(精華):{funnel['both_pass']}** 檔")
    w("")
    w("| 第一層條件 | 通過 | 未通過 | 資料不足 |")
    w("| --- | ---: | ---: | ---: |")
    for k, label in L1_LABELS.items():
        c = funnel[k]
        w(f"| {label} | {c['pass']} | {c['fail']} | {c['na']} |")
    w("")
    w("> 註:各條為**獨立評估**(一檔可能同時卡多條);「通過第一層」才是 6 條同時成立。"
      "「資料不足」代表該條缺資料無法判斷,**一律不當通過**。")
    w("")

    # 負債比新舊口徑對照(驗證修正1/2)
    w("### 負債比口徑對照(新:有息負債比 vs 舊:總負債比)")
    w("")
    dr = [r for r in results if r.metrics.get("ib_ratio") is not None]
    dr.sort(key=lambda r: r.stock_id)
    if dr:
        w("| 代號 | 名稱 | 產業 | 有息負債比(新) | 產業門檻 | ④判定 | 原總負債比(舊) | 差 |")
        w("| --- | --- | --- | ---: | ---: | :--: | ---: | ---: |")
        for r in dr:
            ib = r.metrics["ib_ratio"]
            tot = r.metrics.get("total_ratio")
            thr = r.metrics.get("debt_thr")
            mk = {"pass": "✅", "fail": "❌", "na": "⚠️"}[r.layer1["c4"].status]
            star = "" if r.metrics.get("has_ib_items", True) else "＊"
            tot_s = f"{tot:.1f}%" if tot is not None else "—"
            diff_s = f"−{tot - ib:.1f}pp" if tot is not None else "—"
            w(f"| {r.stock_id} | {r.name} | {r.industry} | {ib:.1f}%{star} | <{thr:.0f}% | {mk} | {tot_s} | {diff_s} |")
        w("")
        w("> 有息負債比 =(短期借款+長期借款+應付公司債)÷ 總資產(FinMind 未單列「一年內到期長期負債」,"
          "多已含在短期借款);`＊`=該公司查無借款科目,視為 0。"
          "對照可見:**代工/fabless 因『應付帳款』被舊口徑(總負債比)灌水**,新口徑才反映真實財務槓桿。")
    else:
        w("_(尚無可計算負債比的資料。)_")
    w("")

    # 三、第一層通過清單
    passers = [r for r in results if r.layer1_pass]
    passers.sort(key=lambda r: (not r.both_pass, r.stock_id))  # 兩層全過的排前面
    w(f"## 三、通過第一層清單({len(passers)} 檔,附第二層四項達標與否)")
    w("")
    if not passers:
        w("_(目前沒有股票通過第一層。可能是本地資料尚少,或門檻較嚴。)_")
    else:
        w("| 代號 | 名稱 | 產業 | ⑦營收CAGR | ⑧毛利率趨勢 | ⑨ROE | ⑩修正動能 |")
        w("| --- | --- | --- | --- | --- | --- | --- |")
        w(_rows(passers))
        w("")
        w("> ⑦⑧⑨ 為品質門檻:✅ 達標 / ❌ 未達標 / ⚠️資料不足。"
          "⑩ 修正動能**僅標記方向**(不列入「兩層全過」判定,依原則等回測驗證後才加權)。"
          "第二層一律**只標記不淘汰**。")
    w("")

    # 四、精華清單
    essence = [r for r in results if r.both_pass]
    essence.sort(key=lambda r: r.stock_id)
    w(f"## 四、精華清單:兩層全過({len(essence)} 檔)")
    w("")
    w("> 「兩層全過」= 第一層 6 條 **＋** 第二層 ⑦⑧⑨ 三項品質門檻全達標;"
      "⑩修正動能**不列入**此判定(僅標記)。")
    w("")
    if not essence:
        w("_(目前沒有股票兩層全過。第二層 ⑦⑧⑨ 任一項為 ❌ 或 ⚠️資料不足 都不算全過——刻意從嚴。)_")
    else:
        w("| 代號 | 名稱 | 產業 | ⑦營收CAGR | ⑧毛利率趨勢 | ⑨ROE | ⑩修正動能 |")
        w("| --- | --- | --- | --- | --- | --- | --- |")
        w(_rows(essence))
        w("")
        w("> ⚠️ 兩層全過 ≠ 買進訊號;僅代表「資格乾淨且品質指標同時達標」,"
          "仍須看產業循環、估值(見主儀表板河流圖/四指標)與最新財報再判斷。")
    w("")

    # 五、估值檢查(僅供參考)
    w("## 五、估值檢查(僅供參考,篩選器不以估值淘汰)")
    w("")
    w("> ⚠️ **此欄僅供參考**:兩層篩選只看資格與品質,**不以估值高低淘汰任何標的**。"
      "估值貴/便宜是「買點」問題,請自行結合下列指標與主儀表板判斷。")
    w("")
    val = [r for r in results
           if any(r.metrics.get(k) is not None for k in ("forward_pe", "peg", "fcf_yield"))]
    val.sort(key=lambda r: (r.market != "us", r.stock_id))
    if val:
        w("| 代號 | 名稱 | 市場 | 前瞻PE | PEG | FCF Yield |")
        w("| --- | --- | --- | ---: | ---: | ---: |")
        for r in val:
            m = "美股" if r.market == "us" else "台股"
            w(f"| {r.stock_id} | {r.name} | {m} | {_fv(r.metrics.get('forward_pe'), 'x')} | "
              f"{_fv(r.metrics.get('peg'), '', 2)} | {_fv(r.metrics.get('fcf_yield'), '%')} |")
        w("")
        w("> 前瞻PE=現價÷今年共識EPS;PEG=前瞻PE÷盈餘成長率;FCF Yield=近4季自由現金流÷市值。"
          "來源 yfinance;台股分析師共識覆蓋率較低,缺者顯示「—」。")
    else:
        w("_(尚無估值資料。)_")
    w("")

    # 六、美股測試標的:逐條 + 估值評語
    us_res = [r for r in results if r.market == "us"]
    if us_res:
        w("## 六、美股測試標的:逐條檢視 + 估值評語")
        w("")
        for r in us_res:
            l1pass = sum(1 for c in r.layer1.values() if c.status == "pass")
            w(f"### {r.stock_id}（{r.industry}）")
            w("")
            w(f"**第一層 6 條:通過 {l1pass}/6**" + ("　✅ 全數通過" if r.layer1_pass else ""))
            w("")
            for k, label in L1_LABELS.items():
                c = r.layer1[k]
                w(f"- {label}:{c.mark}　{c.detail}")
            w("")
            w(f"**第二層品質(⑦⑧⑨):**{'✅ 全達標' if r.layer2_pass else '未全達標'}")
            w("")
            for k in ("q7", "q8", "q9"):
                c = r.layer2[k]
                w(f"- {L2_LABELS[k]}:{c.mark}　{c.detail}")
            w(f"- {L2_LABELS['q10']}(僅標記):{r.layer2['q10'].detail}")
            w("")
            fpe = r.metrics.get("forward_pe")
            peg = r.metrics.get("peg")
            fy = r.metrics.get("fcf_yield")
            w(f"**估值檢查(僅參考):** 前瞻PE {_fv(fpe, 'x')}、PEG {_fv(peg, '', 2)}、"
              f"FCF Yield {_fv(fy, '%')}")
            w("")
            verd = []
            if fpe is not None:
                verd.append("前瞻PE 極高" if fpe > 40 else "前瞻PE 偏高" if fpe > 25 else "前瞻PE 尚屬合理")
            if fy is not None:
                verd.append("FCF殖利率偏低" if fy < 2 else "FCF殖利率尚可")
            if peg is not None:
                verd.append(f"PEG {peg:.2f}(>2 偏貴)" if peg > 2 else f"PEG {peg:.2f}")
            vtxt = "、".join(verd) if verd else "估值資料不足"
            l1_block = [L1_LABELS[k].split(" ")[0] for k, c in r.layer1.items() if c.status != "pass"]
            block_txt = f"(卡 {'、'.join(l1_block)})" if l1_block else ""
            pass_txt = "**通過第一層資格**" if r.layer1_pass else f"**未通過第一層**{block_txt}"
            qual_txt = "、品質 ⑦⑧⑨ 全達標" if r.layer2_pass else "、品質 ⑦⑧⑨ 未全達標"
            w(f"> **結論**:{r.name} {pass_txt}{qual_txt}。**若加入估值判斷**:{vtxt}"
              "——成長預期多已反映在股價。本篩選器**刻意不以估值淘汰**,故仍照資格/品質列出;"
              "是否買進需自行結合估值與成長延續性(可回主儀表板看河流圖/四指標)。")
            w("")

    # 七、誠實說明
    w("## 七、誠實說明")
    w("")
    w("- **資料不足不當通過**:任一條件缺資料(如財報年數不夠、無成交資料)標「⚠️資料不足」,不計為通過。")
    w("- **修正動能**多為「資料不足」屬正常:共識EPS 目前只對觀察清單(`data/consensus/`)累積,"
      "其餘個股尚無共識歷史;依原則此項**僅標記、未納入評分**(等回測驗證後再談加權)。")
    w("- **金融股**(代號 2800–2890)依設定**排除負債比**這條(其高槓桿為業態常態)。")
    w("- 全市場資料由 `fetch_universe.py` 逐檔抓 FinMind 存於 `data/universe/`;"
      "門檻改 `config/screener.yaml` 後重跑 `screen.py` 即可,**毋須重抓**。")
    w("")
    return "\n".join(A)
