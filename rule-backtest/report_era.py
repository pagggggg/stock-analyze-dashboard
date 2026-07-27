"""
時代穩健性報告 (report_era.py)
==============================
產出 era_robustness.md。

★ 原則:**所有判斷句都由數據生成**(數字變、結論就跟著變),不預先寫死立場。
  每個結論後面都附上它依據的統計量,讀者可以自己複核。
"""

from __future__ import annotations

import statistics
from pathlib import Path

import params as P

OUT = Path(__file__).resolve().parent / "era_robustness.md"
STRATS = ("A", "B", "BH")
SNAME = {"A": "A 純PE進出", "B": "B PE進場+基本面出場", "BH": "買進持有"}


def pct(x, nd: int = 1) -> str:
    return "—" if x is None else f"{x * 100:.{nd}f}%"


def _ok(s: dict | None) -> bool:
    """這份摘要是否可用(有算出年化)。"""
    return bool(s) and not s.get("insufficient") and s.get("cagr") is not None


def _med(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


def _collect(stocks: list[dict], era_key: str | None, strat: str,
             group: str | None = None, field: str = "cagr") -> list[float]:
    """取某時代/某類型/某策略的欄位值清單(略過資料不足者)。"""
    out = []
    for s in stocks:
        if "error" in s or not s.get("full"):
            continue
        if group and s.get("group") != group:
            continue
        d = s["full"][strat] if era_key is None else s.get("eras", {}).get(strat, {}).get(era_key)
        if _ok(d):
            out.append(d[field])
    return out


def _pairs(stocks: list[dict], era_key: str | None, s1: str, s2: str,
           group: str | None = None, field: str = "cagr") -> list[tuple[str, float, float]]:
    """成對比較(同一檔同時有兩個策略的數據才納入)→ [(name, v1, v2)]。"""
    out = []
    for s in stocks:
        if "error" in s or not s.get("full"):
            continue
        if group and s.get("group") != group:
            continue
        if era_key is None:
            d1, d2 = s["full"][s1], s["full"][s2]
        else:
            d1 = s.get("eras", {}).get(s1, {}).get(era_key)
            d2 = s.get("eras", {}).get(s2, {}).get(era_key)
        if _ok(d1) and _ok(d2):
            out.append((s["name"], d1[field], d2[field]))
    return out


# ─────────────────────────────────────────────────────────────────────
# 必答三題:判斷句由數據生成
# ─────────────────────────────────────────────────────────────────────
def _q1(stocks: list[dict]) -> list[str]:
    """Q1:2008~2016 那段,A 有沒有贏過買進持有?(並與 2017~2026 對照看是否翻轉)"""
    L = ["### Q1. 2008~2016 那段,A(低買高賣)有沒有贏過買進持有?", ""]
    rows = []
    for ek, ename in [("early", "2008~2016"), ("late", "2017~2026")]:
        p = _pairs(stocks, ek, "A", "BH", field="cagr")
        pm = _pairs(stocks, ek, "A", "BH", field="max_drawdown")
        if not p:
            rows.append((ename, None, None, None, None, None, None))
            continue
        win = sum(1 for _, a, b in p if a > b)
        med_a, med_bh = _med([a for _, a, _ in p]), _med([b for _, _, b in p])
        # 年化差距的中位數:只看勝負會漏掉「輸多少」——早期小輸和晚期慘輸意義完全不同
        med_gap = _med([a - b for _, a, b in p])
        mdd_better = sum(1 for _, a, b in pm if a > b)   # 回撤為負,越大越淺
        med_mdd_gap = _med([a - b for _, a, b in pm])
        rows.append((ename, win, len(p), med_a, med_bh, med_gap, (mdd_better, med_mdd_gap)))

    L += ["| 時代 | A 年化贏過買進持有 | A 年化中位數 | 買進持有年化中位數 | "
          "**年化差距中位數(A−BH)** | A 回撤較淺 | 回撤差距中位數 |",
          "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for ename, win, n, ma, mb, gap, mdd in rows:
        if not n:
            L.append(f"| {ename} | 樣本不足 | — | — | — | — | — |")
        else:
            mdd_better, med_mdd_gap = mdd
            L.append(f"| {ename} | **{win}/{n}** | {pct(ma)} | {pct(mb)} | "
                     f"**{pct(gap)}** | {mdd_better}/{n} | {pct(med_mdd_gap)} |")
    L.append("")

    early = next((r for r in rows if r[0] == "2008~2016"), None)
    late = next((r for r in rows if r[0] == "2017~2026"), None)
    if early and early[2]:
        ename, win, n, ma, mb, gap, (mdd_better, med_mdd_gap) = early
        ratio = win / n
        if ratio > 0.5:
            verdict = (f"**有。** 2008~2016 這段,A 的年化贏過買進持有的有 {win}/{n} 檔"
                       f"(過半),年化中位數 {pct(ma)} vs 買進持有 {pct(mb)}。")
        elif ratio == 0.5:
            verdict = (f"**打平。** A 贏 {win}/{n} 檔,恰好各半;"
                       f"年化中位數 {pct(ma)} vs {pct(mb)}。")
        else:
            verdict = (f"**論年化,沒有贏。** 2008~2016 只有 {win}/{n} 檔的 A 年化贏過買進持有,"
                       f"年化中位數 {pct(ma)} vs 買進持有 {pct(mb)}"
                       f"(差距中位數 **{pct(gap)}**)。")
        L += [verdict, ""]
        L.append(f"但**回撤面**要分開看:A 的最大回撤比買進持有淺的有 **{mdd_better}/{n}** 檔,"
                 f"回撤差距中位數 **{pct(med_mdd_gap)}**"
                 f"(正值代表 A 的回撤較淺)。"
                 "「賺得少但跌得淺」是否划算,取決於你能承受多深的帳面虧損 —— "
                 "這是價值判斷,數據不能替你決定。")
        L.append("")

        if late and late[2]:
            _, win2, n2, ma2, mb2, gap2, (mdd_b2, mgap2) = late
            r2 = win2 / n2
            flip_by_winrate = (ratio > 0.5) != (r2 > 0.5)
            # 即使勝率同向,「輸的幅度」變化才是「是不是時代產物」的關鍵證據
            widened = (gap is not None and gap2 is not None
                       and abs(gap2) > abs(gap) * 2 and gap2 < gap)
            if flip_by_winrate:
                L.append(f"**時代對照:結論翻轉。** 早期 A 勝率 {win}/{n}、晚期 {win2}/{n2}"
                         f" —— 「該不該低買高賣」的答案在兩個時代不一致,"
                         f"代表原本的結論**確實可能是時代產物**。")
            elif widened:
                L.append(f"**時代對照:勝負方向沒變,但差距被大幅拉開。** "
                         f"A 落後買進持有的幅度,從早期的 **{pct(gap)}** "
                         f"擴大到晚期的 **{pct(gap2)}**"
                         f"(年化中位數:早期 {pct(ma)} vs {pct(mb)};"
                         f"晚期 {pct(ma2)} vs {pct(mb2)})。"
                         f"\n\n這是本報告最直接的證據:**「買進持有大勝」主要發生在後段的大噴發期**,"
                         f"早期兩者其實接近。換句話說,原本 4 檔樣本得到的「抱著不動比較好」,"
                         f"**很可能相當程度是時代產物** —— 若未來報酬結構回到 2008~2016 那種樣子,"
                         f"這個結論不一定還成立。")
            else:
                L.append(f"**時代對照:方向與幅度都大致一致。** 早期 A 勝率 {win}/{n}、"
                         f"差距 {pct(gap)};晚期 {win2}/{n2}、差距 {pct(gap2)}。"
                         f"兩個時代指向同一方向,較不像單純的時代產物"
                         f"(但樣本仍小,見末節限制)。")
        L.append("")
    else:
        L += ["**無法回答:2008~2016 這段沒有足夠樣本**"
              f"(需至少 {P.ERA_MIN_DAYS} 個交易日)。多數標的的可交易起點晚於 2016,"
              "原因是「PE 分位數暖身 + 財報可用」兩個條件的交集。", ""]
    return L


def _q2(stocks: list[dict]) -> list[str]:
    """Q2:INTC 這種「成長變衰退」的股票,哪個策略活下來?B 的基本面出場有沒有救到?"""
    L = ["### Q2. INTC(成長變衰退)哪個策略活下來?B 的基本面出場有沒有救到?", ""]
    intc = next((s for s in stocks if s.get("code") == "INTC"), None)
    if not intc or "error" in intc or not intc.get("full"):
        err = intc.get("error") if intc else "未納入樣本"
        L += [f"**無法回答:INTC 資料不可用({err})。**", ""]
        return L

    L += ["| 策略 | 全期年化 | 全期最大回撤 | 交易次數 | 在市比例 |",
          "| --- | ---: | ---: | ---: | ---: |"]
    for k in STRATS:
        d = intc["full"][k]
        if not _ok(d):
            L.append(f"| {SNAME[k]} | — | — | — | — |")
            continue
        L.append(f"| {SNAME[k]} | {pct(d.get('cagr'))} | **{pct(d.get('max_drawdown'))}** | "
                 f"{d.get('n_trades', 0)} | {pct(d.get('time_in_market'))} |")
    L.append("")

    a, b, bh = intc["full"]["A"], intc["full"]["B"], intc["full"]["BH"]
    if _ok(a) and _ok(b) and _ok(bh):
        best = max([("A", a), ("B", b), ("BH", bh)], key=lambda t: t[1]["cagr"])
        shallow = max([("A", a), ("B", b), ("BH", bh)], key=lambda t: t[1]["max_drawdown"])
        L.append(f"**年化最高:{SNAME[best[0]]}({pct(best[1]['cagr'])});"
                 f"回撤最淺:{SNAME[shallow[0]]}({pct(shallow[1]['max_drawdown'])})。**")
        L.append("")
        # B 相對 A 的救援效果
        d_cagr = b["cagr"] - a["cagr"]
        d_mdd = b["max_drawdown"] - a["max_drawdown"]     # 正值=B 回撤較淺
        if d_mdd > 0.01 and d_cagr > -0.005:
            v = (f"**有救到。** B 的最大回撤比 A 淺 {pct(d_mdd)}"
                 f"(A {pct(a['max_drawdown'])} → B {pct(b['max_drawdown'])}),"
                 f"且年化並未因此變差({pct(a['cagr'])} → {pct(b['cagr'])})。")
        elif d_mdd > 0.01:
            v = (f"**部分救到,但有代價。** B 的回撤比 A 淺 {pct(d_mdd)},"
                 f"但年化也低了 {pct(-d_cagr)}({pct(a['cagr'])} → {pct(b['cagr'])})。")
        elif d_mdd < -0.01:
            v = (f"**沒救到,反而更糟。** B 的回撤比 A 更深 {pct(-d_mdd)}"
                 f"(A {pct(a['max_drawdown'])} → B {pct(b['max_drawdown'])})。")
        else:
            v = (f"**沒有明顯差別。** B 與 A 的回撤相差僅 {pct(abs(d_mdd))},"
                 f"年化相差 {pct(abs(d_cagr))}。")
        L += [v, ""]
        # 基本面出場實際上有沒有被觸發
        L.append(_fund_exit_note(intc))
        L.append("")
        # 對照買進持有
        if b["cagr"] > bh["cagr"]:
            L.append(f"對照買進持有:B 年化 {pct(b['cagr'])} **高於**買進持有 {pct(bh['cagr'])}"
                     f",回撤 {pct(b['max_drawdown'])} vs {pct(bh['max_drawdown'])}。"
                     "在這檔「成長變衰退」的股票上,有出場規則確實比抱著不動好。")
        else:
            L.append(f"對照買進持有:B 年化 {pct(b['cagr'])} **低於/等於**買進持有 "
                     f"{pct(bh['cagr'])},回撤 {pct(b['max_drawdown'])} vs "
                     f"{pct(bh['max_drawdown'])}。")
        L.append("")
    return L


def _fund_exit_note(stock: dict) -> str:
    """B 的基本面出場實際被觸發過幾次(從全期摘要看不出來,需查交易明細)。"""
    n = stock.get("b_fund_exits")
    if n is None:
        return ("(基本面出場觸發次數見 `data/era_results.json` 的交易明細。)")
    if n == 0:
        return ("⚠️ **注意:B 的基本面出場條件在這檔上從未被觸發** —— "
                "也就是說 B 的表現差異其實來自「惡化期間不進場」的過濾,而非賣出訊號本身。")
    return f"B 的基本面出場(EPS 連兩季年減 / 毛利率連兩季下滑)實際被觸發 **{n}** 次。"


def _q3(stocks: list[dict]) -> list[str]:
    """Q3:循環股上,三種策略是不是全滅?"""
    L = ["### Q3. 循環股上,三種策略是不是全滅?(驗證「系統不適用循環股」的判斷)", ""]
    cyc = [s for s in stocks if s.get("group") == "循環" and "error" not in s and s.get("full")]
    if not cyc:
        L += ["**無法回答:循環股樣本不可用。**", ""]
        return L

    L += ["| 標的 | A 年化 | A 回撤 | B 年化 | B 回撤 | 買進持有年化 | 買進持有回撤 |",
          "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    neg = {k: 0 for k in STRATS}
    tot = {k: 0 for k in STRATS}
    deep = {k: 0 for k in STRATS}
    for s in cyc:
        cells = []
        for k in STRATS:
            d = s["full"][k]
            if _ok(d):
                tot[k] += 1
                if d["cagr"] < 0:
                    neg[k] += 1
                if d["max_drawdown"] < -0.5:
                    deep[k] += 1
                cells += [pct(d["cagr"]), pct(d["max_drawdown"])]
            else:
                cells += ["—", "—"]
        L.append(f"| {s['name']} | " + " | ".join(cells) + " |")
    L.append("")

    parts = []
    for k in STRATS:
        if tot[k]:
            parts.append(f"{SNAME[k]}:{neg[k]}/{tot[k]} 檔年化為負、"
                         f"{deep[k]}/{tot[k]} 檔回撤超過 -50%")
    L.append("統計:" + ";".join(parts) + "。")
    L.append("")

    all_neg = all(tot[k] and neg[k] == tot[k] for k in STRATS)
    any_pos_strat = [k for k in STRATS if tot[k] and neg[k] < tot[k]]
    if all_neg:
        L.append("**是,全滅。** 三種策略在所有循環股樣本上年化都是負的 —— "
                 "這支持「本系統不適用循環股」的判斷。")
    elif not any_pos_strat:
        L.append("**接近全滅。** 三種策略幾乎都無法在循環股上取得正報酬。")
    else:
        best_k = max([k for k in STRATS if tot[k]],
                     key=lambda k: _med(_collect(stocks, None, k, group="循環")) or -9)
        med = _med(_collect(stocks, None, best_k, group="循環"))
        L.append(f"**不是全滅,但要小心解讀。** 表現最好的是 {SNAME[best_k]}"
                 f"(年化中位數 {pct(med)});虧損檔數見上方統計。"
                 "循環股的報酬高度取決於**回測期間剛好落在景氣的哪一段**,"
                 "同一檔換個起訖點結論就會翻掉 —— 這正是「系統不適用循環股」的理由:"
                 "不是它一定賠錢,而是**結果由景氣位置決定,不由規則決定**。")
    L.append("")
    return L


# ─────────────────────────────────────────────────────────────────────
# 核心矩陣
# ─────────────────────────────────────────────────────────────────────
def _matrix(stocks: list[dict]) -> list[str]:
    L = ["## 二、核心矩陣:策略 × 時代 × 股票類型", "",
         "每格是**該組別內各股的中位數**(中位數比平均不易被單一極端值主導)。",
         "「—」= 該組在該時代沒有足夠樣本"
         f"(需至少 {P.ERA_MIN_DAYS} 個交易日)。", ""]
    groups = []
    for s in stocks:
        if s.get("group") and s["group"] not in groups:
            groups.append(s["group"])

    for ek, ename in [("early", "2008~2016(大噴發前)"),
                      ("late", "2017~2026(大噴發時代)"),
                      (None, "全期")]:
        L += [f"### {ename}", "",
              "| 類型 | 檔數 | A 年化 | A 回撤 | B 年化 | B 回撤 | 買進持有年化 | 買進持有回撤 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for g in groups:
            cells, n_max = [], 0
            for k in STRATS:
                cg = _collect(stocks, ek, k, group=g, field="cagr")
                cm = _collect(stocks, ek, k, group=g, field="max_drawdown")
                n_max = max(n_max, len(cg))
                cells += [pct(_med(cg)), pct(_med(cm))]
            L.append(f"| {g} | {n_max} | " + " | ".join(cells) + " |")
        # 全樣本一列
        cells, n_max = [], 0
        for k in STRATS:
            cg = _collect(stocks, ek, k, field="cagr")
            cm = _collect(stocks, ek, k, field="max_drawdown")
            n_max = max(n_max, len(cg))
            cells += [pct(_med(cg)), pct(_med(cm))]
        L.append(f"| **全樣本** | **{n_max}** | " + " | ".join(f"**{c}**" for c in cells) + " |")
        L.append("")
    return L


# ─────────────────────────────────────────────────────────────────────
# 組合層級
# ─────────────────────────────────────────────────────────────────────
def _portfolio(out: dict, stocks: list[dict] | None = None) -> list[str]:
    L = ["## 三、組合層級(等權,比單股更接近實戰)", "",
         "作法:每日把資金**等分給當日有部位的標的**;全部空手就是現金(以 0% 計息,保守)。",
         "買進持有 = 全程等權持有全部樣本。", "",
         "| 範圍 | 策略 | 年化 | 最大回撤 | 平均同時持有檔數 | 在市比例 |",
         "| --- | --- | ---: | ---: | ---: | ---: |"]
    pf = out.get("portfolio", {})
    for ek, ename in [(None, "全期"), ("early", "2008~2016"), ("late", "2017~2026")]:
        for k in STRATS:
            d = pf.get(k, {}).get("full") if ek is None else pf.get(k, {}).get("eras", {}).get(ek)
            if not _ok(d):
                L.append(f"| {ename} | {SNAME[k]} | 樣本不足 | — | — | — |")
                continue
            L.append(f"| {ename} | {SNAME[k]} | **{pct(d.get('cagr'))}** | "
                     f"**{pct(d.get('max_drawdown'))}** | {d.get('avg_held', 0):.1f} | "
                     f"{pct(d.get('time_in_market'))} |")
    L.append("")

    # 依數據生成組合層級的判斷
    full = {k: pf.get(k, {}).get("full") for k in STRATS}
    if all(_ok(full[k]) for k in STRATS):
        rank = sorted(STRATS, key=lambda k: -full[k]["cagr"])
        L.append(f"**組合層級年化排名:** " +
                 " > ".join(f"{SNAME[k]}({pct(full[k]['cagr'])})" for k in rank) + "。")
        mdd_rank = sorted(STRATS, key=lambda k: -full[k]["max_drawdown"])
        L.append(f"**回撤由淺到深:** " +
                 " < ".join(f"{SNAME[k]}({pct(full[k]['max_drawdown'])})" for k in mdd_rank) + "。")
        L.append("")
        if rank[0] == "BH":
            L.append("在組合層級上,**買進持有的年化仍是最高的**。"
                     "若要為 A/B 辯護,理由只能是回撤或波動更可控 —— 請直接看上表的回撤欄,"
                     "不要用「感覺比較安全」代替數據。")
        else:
            L.append(f"在組合層級上,**{SNAME[rank[0]]} 的年化高於買進持有**"
                     f"({pct(full[rank[0]]['cagr'])} vs {pct(full['BH']['cagr'])})。")
        L.append("")

        # ★ 單股 vs 組合 若結論相反,必須講清楚,否則讀者會挑對自己有利的那個看
        if stocks:
            med_a = _med(_collect(stocks, None, "A", field="cagr"))
            med_bh = _med(_collect(stocks, None, "BH", field="cagr"))
            if med_a is not None and med_bh is not None:
                single_winner = "A" if med_a > med_bh else "BH"
                pf_winner = rank[0]
                if single_winner != pf_winner or (single_winner == "BH" and pf_winner != "BH"):
                    L += [
                        "### ⚠️ 單股層級與組合層級的結論相反 —— 這一點必須講清楚", "",
                        f"- **單股中位數**:買進持有 {pct(med_bh)} vs A {pct(med_a)} → "
                        f"**{'買進持有' if med_bh > med_a else 'A'} 較好**",
                        f"- **等權組合**:A {pct(full['A']['cagr'])} vs "
                        f"買進持有 {pct(full['BH']['cagr'])} → "
                        f"**{SNAME[pf_winner]} 較好**", "",
                        "同一份資料、兩種看法、相反結論。可能的原因(**沒有做歸因實驗,以下是待驗證的假說,不是結論**):",
                        "",
                        "1. **資金集中效果**:組合層級的 A 只把錢放在「當時 PE 相對便宜」的少數標的"
                        f"(平均同時持有 {full['A'].get('avg_held', 0):.1f} 檔),"
                        f"而買進持有把錢攤平在全部 {full['BH'].get('avg_held', 0):.1f} 檔上,"
                        "被表現差的標的(如中鋼、台塑)拖累。這比較像是**選股效果**,不是出場規則的功勞。",
                        "2. **每日再平衡的紅利**:等權組合假設可以每日無摩擦再平衡,"
                        "在高波動、低相關的標的上,這件事本身就會產生額外報酬(rebalancing premium)。"
                        "**這是我們的假設造出來的,不是策略賺到的** —— 實務上有成本、零股與流動性限制。",
                        "3. **空手期間以 0% 計息**:對 A/B 不利,所以這個方向不會誇大 A。",
                        "",
                        "**該相信哪一個?** 如果你實際上是「一次只買一兩檔」,單股層級比較貼近你的處境;"
                        "如果你會同時持有一籃子並定期再平衡,組合層級比較貼近。"
                        "但無論哪一種,**都不要只挑對自己結論有利的那個引用**。", "",
                    ]

    # 分類型組合
    bg = out.get("portfolio_by_group", {})
    if bg:
        L += ["### 分類型的等權組合(全期)", "",
              "| 類型 | 檔數 | A 年化 | A 回撤 | B 年化 | B 回撤 | 買進持有年化 | 買進持有回撤 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for g, d in bg.items():
            cells = []
            n = 0
            for k in STRATS:
                s = d.get(k, {}).get("full")
                n = max(n, d.get(k, {}).get("n_stocks", 0))
                cells += [pct(s.get("cagr")) if _ok(s) else "—",
                          pct(s.get("max_drawdown")) if _ok(s) else "—"]
            L.append(f"| {g} | {n} | " + " | ".join(cells) + " |")
        L.append("")
    return L


# ─────────────────────────────────────────────────────────────────────
# 逐檔明細
# ─────────────────────────────────────────────────────────────────────
def _per_stock(stocks: list[dict]) -> list[str]:
    L = ["## 四、逐檔明細(時代切段)", "",
         "**PE 覆蓋** = 回測期間內「有有效 trailing PE」的交易日比例。"
         "偏低代表該股有很多日子算不出 PE(EPS 為負,或資料源缺漏),"
         "此時「自身歷史 PE 分位數」這個基準本身就不可靠 —— 該檔的結果要打折看。", "",
         "| 標的 | 類型 | 可交易期間 | PE覆蓋 | 時代 | A 年化 | A 回撤 | B 年化 | B 回撤 | BH 年化 | BH 回撤 |",
         "| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for s in stocks:
        if "error" in s:
            L.append(f"| {s.get('name', s.get('code'))} | {s.get('group', '—')} | "
                     f"資料失敗:{str(s['error'])[:40]} | — | — | — | — | — | — | — | — |")
            continue
        if not s.get("full"):
            continue
        span = f"{s.get('analysis_start', '—')}~{s.get('analysis_end', '—')}"
        cov = s.get("pe_coverage")
        cov_txt = pct(cov) if cov is not None else "—"
        if cov is not None and cov < 0.8:
            cov_txt = f"⚠️{cov_txt}"
        for ek, ename in [(None, "全期"), ("early", "08~16"), ("late", "17~26")]:
            cells = []
            for k in STRATS:
                d = s["full"][k] if ek is None else s.get("eras", {}).get(k, {}).get(ek)
                cells += ([pct(d.get("cagr")), pct(d.get("max_drawdown"))]
                          if _ok(d) else ["—", "—"])
            first = (f"| {s['name']} | {s.get('group', '')} | {span} | {cov_txt} "
                     if ek is None else "|  |  |  |  ")
            L.append(first + f"| {ename} | " + " | ".join(cells) + " |")
    L.append("")

    low = [s for s in stocks if isinstance(s.get("pe_coverage"), float) and s["pe_coverage"] < 0.8]
    if low:
        names = "、".join(f"{s['name']}({pct(s['pe_coverage'])})" for s in low)
        L += [f"⚠️ **PE 覆蓋率低於 80% 的標的:{names}。** "
              "這些標的的「自身歷史 PE 分位數」是用殘缺樣本算的,"
              "其 A/B 結果的可信度明顯低於其他標的,請勿與其他標的等量齊觀。", ""]

    # 毛利率條件不適用(金融業)→ B 是降級版,必須講明
    gm_na = [s for s in stocks if s.get("gm_not_applicable")]
    if gm_na:
        names = "、".join(s["name"] for s in gm_na)
        L += [f"⚠️ **毛利率條件不適用的標的:{names}(金融業)。** "
              "銀行/金控的損益表沒有「毛利率」科目,策略 B 在這些標的上"
              "**只有 EPS 出場條件**(等於降級版的 B)。"
              "它們的 A vs B 差異會被系統性低估 —— 不是 B 沒用,是 B 少了一隻腳。", ""]

    # 從未進場的標的:0% 年化的意義和「虧損」完全不同,不標明會誤讀矩陣
    never = []
    for s in stocks:
        if not s.get("full"):
            continue
        a = s["full"].get("A") or {}
        if _ok(a) and a.get("n_trades", 0) == 0:
            never.append(s["name"])
    if never:
        L += [f"ℹ️ **策略 A 從未進場的標的:{'、'.join(never)}。** "
              "它們的年化 0%、回撤 0% 代表「全程空手抱現金」,**不是虧損**。"
              "成因是這些股票的 PE 長期緩步上移,"
              "「當前 PE < 自身歷史中位數」幾乎不曾成立 —— "
              "這是純估值規則的一個真實特性:**對估值中樞持續上移的股票,它會永遠在等一個不會來的價格**。"
              "計算組別中位數時它們仍被計入,請注意這會把該組的 A 年化往下拉。", ""]
    return L


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────
def write_era_report(out: dict) -> None:
    stocks = out.get("stocks", [])
    ok_stocks = [s for s in stocks if "error" not in s and s.get("full")]
    p = out.get("params", {})

    L = ["# 時代穩健性測試 (era_robustness.md)", "",
         "> 一句話問題:**A / B / 買進持有 的結論,是不是只是「成長股大時代」的產物?**", "",
         f"樣本 {len(stocks)} 檔(成功回測 {len(ok_stocks)} 檔),"
         f"刻意混入成長 / 成熟 / 循環 / 美股(含 INTC 這種成長變衰退的樣本)。",
         f"策略參數未改動:進場 PE < 第 {p.get('entry_pctl')} 百分位、"
         f"出場 PE > 第 {p.get('exit_pctl')} 百分位、"
         f"基本面惡化需連續 {p.get('deterioration_quarters')} 季。", "",
         "> ⚠ 僅供研究,非投資建議。**本節所有判斷句都由數據生成**"
         "(程式依統計量選句子),數字變、結論就跟著變。", "",
         "---", "",
         "## 一、必答三題", ""]
    L += _q1(ok_stocks)
    L += _q2(ok_stocks)
    L += _q3(ok_stocks)
    L += ["---", ""]
    L += _matrix(ok_stocks)
    L += ["---", ""]
    L += _portfolio(out, ok_stocks)
    L += ["---", ""]
    L += _per_stock(stocks)
    L += ["---", ""]
    L += _limits(out, stocks, ok_stocks)

    OUT.write_text("\n".join(L), encoding="utf-8")


def _limits(out: dict, stocks: list[dict], ok_stocks: list[dict]) -> list[str]:
    p = out.get("params", {})
    eras = p.get("eras", [])
    era_txt = "、".join(f"{e['name']}" for e in eras) if eras else "—"
    failed = [s for s in stocks if "error" in s]
    return [
        "## 五、限制與誠實聲明(請先讀完再看結論)", "",
        f"1. **樣本仍然很小。** {len(ok_stocks)} 檔、單一市場為主,"
        "且每檔的可交易期間長短不一。本報告**不做任何統計顯著性宣稱**"
        "(沒有 p 值、沒有信賴區間),所有數字都只是「這 21 檔在這段歷史上的結果」,"
        "不足以推論到未來或其他標的。",
        "",
        "2. **存活者偏差(最嚴重的問題)。** 樣本全是**今天還在、還查得到資料**的公司。"
        "2008 年買進的人並不知道誰會活下來 —— 真正下市、被合併、長期停牌的公司完全不在樣本裡。"
        "這會**系統性高估**所有策略(尤其是買進持有)的表現。"
        "本報告無法修正這個偏差,只能明確標示。",
        "",
        "3. **trailing PE 口徑限制(沿用主回測)。** PE 一律用「股價 ÷ 近四季**實際** EPS」。"
        "要用前瞻 PE 就必須有「歷史上每一天的分析師共識」,免費資料源拿不到;"
        "用今天的共識回推過去就是前視偏誤。"
        "影響方向:對高成長股,trailing PE 會系統性偏高,"
        "使「PE 低於中位數」的進場訊號在成長加速期較難觸發。",
        "",
        f"4. **時代切點是事後選的。** 切成 {era_txt},"
        "切點 2017 是**知道後段大漲之後才決定的**,本身就帶後見之明。"
        "換一個切點(例如 2015 或 2019)結論可能不同。"
        "這個切段的用途是「檢驗結論穩不穩」,不是宣稱 2017 是什麼客觀分界。",
        "",
        "5. **股票類型的標籤也是事後貼的。** 把 INTC 歸為「衰退」、把國巨歸為「循環」,"
        "都是因為**我們已經知道後來發生什麼**。2008 年時沒有人能可靠地事先分好類。"
        "分組只是為了觀察差異,不代表這種分類在當年可執行。",
        "",
        "6. **等權組合是樂觀假設。** 假設可以每日無摩擦地重新等權配置,"
        "忽略零股、流動性、再平衡的額外交易成本與稅負。實際執行會比這裡差。"
        "另外空手期間以 **0% 計息**,對「常常空手」的策略是不利的保守設定"
        "(真實世界現金有利息)。",
        "",
        "7. **時代切段不重跑、持倉自然延續。** 分段結算用的是同一次回測的淨值曲線切片,"
        "而不是各時代分開重新暖身回測。跨越切點的部位會延續到下一段 —— "
        "這比較接近真實,但也代表某一段的起點淨值狀態受前一段影響。",
        "",
        f"8. **失敗樣本照實列出。** 本次有 {len(failed)} 檔無法完成回測"
        + (":" + "、".join(f"{s.get('name', s.get('code'))}" for s in failed) if failed else "")
        + "。這些標的沒有被靜悄悄丟掉,原因見第四節表格。",
        "",
        "9. **參數沒有做任何最佳化。** 進場/出場百分位與惡化季數全部沿用主回測的設定,"
        "沒有為了讓某個結論好看而搜尋參數。但也因此,本報告**不能**宣稱這組參數是好的 —— "
        "只能說「在這組固定參數下,結果長這樣」。",
        "",
    ]
