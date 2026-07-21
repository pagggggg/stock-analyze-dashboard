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
    w(f"3. 近 **{L1['ocf_positive']['quarters']}** 季營業現金流為正({L1['ocf_positive']['mode']})")
    fin = "(金融股 %d–%d 排除此條)" % (L1['debt_ratio']['financial_id_min'], L1['debt_ratio']['financial_id_max']) if L1['debt_ratio']['exclude_financial'] else ""
    w(f"4. 負債比 < **{L1['debt_ratio']['max_pct']}%** {fin}")
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

    # 五、誠實說明
    w("## 五、誠實說明")
    w("")
    w("- **資料不足不當通過**:任一條件缺資料(如財報年數不夠、無成交資料)標「⚠️資料不足」,不計為通過。")
    w("- **修正動能**多為「資料不足」屬正常:共識EPS 目前只對觀察清單(`data/consensus/`)累積,"
      "其餘個股尚無共識歷史;依原則此項**僅標記、未納入評分**(等回測驗證後再談加權)。")
    w("- **金融股**(代號 2800–2890)依設定**排除負債比**這條(其高槓桿為業態常態)。")
    w("- 全市場資料由 `fetch_universe.py` 逐檔抓 FinMind 存於 `data/universe/`;"
      "門檻改 `config/screener.yaml` 後重跑 `screen.py` 即可,**毋須重抓**。")
    w("")
    return "\n".join(A)
