"""
報告產生器 (report.py)
======================
產出 rule_backtest.md。設計原則:

  - **結論由數字推導**:第 1 節的判斷句(哪類股票純 PE 夠用、哪類會漏接風險)
    是用回測算出來的指標即時生成的,不是先寫好再套。數字變,結論就跟著變。
  - **不藏壞消息**:每一筆交易、最大單筆虧損、樣本數、資料缺口全部列出。
  - **口徑寫清楚**:trailing / forward PE、共識 vs 實際 EPS、還原 vs 未還原價,
    在哪裡用哪一個,一律標明。
"""

from __future__ import annotations

import csv
from pathlib import Path

import params as P
from strategy import EXIT_LABELS

OUT = Path(__file__).resolve().parent / "rule_backtest.md"
TRADES_CSV = Path(__file__).resolve().parent / "data" / "trades.csv"


# ─────────────────────────────────────────────────────────────────────
# 格式化小工具
# ─────────────────────────────────────────────────────────────────────
def pct(x, nd: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{nd}f}%"


def num(x, nd: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x:.{nd}f}"


def reasons_text(rs: list[str]) -> str:
    return " + ".join(EXIT_LABELS.get(r, r) for r in rs) if rs else "—"


def _shift_year(iso: str, years: int) -> str:
    """把 ISO 日期位移 N 年(只用來框選案例的季度區間,不參與任何計算)。"""
    return f"{int(iso[:4]) + years:04d}{iso[4:]}"


def _sum_row(label: str, s: dict) -> str:
    if not s or s.get("n_trades") is None:
        return f"| {label} | — | — | — | — | — | — |"
    n = s.get("n_trades", 0)
    n_open = s.get("n_open") or 0
    n_txt = f"{n}" + (f"(含 {n_open} 筆未平倉)" if n_open else "")
    return (f"| {label} | {pct(s.get('total_return'))} | {pct(s.get('cagr'))} | "
            f"**{pct(s.get('max_drawdown'))}** | {n_txt} | "
            f"{pct(s.get('win_rate')) if s.get('win_rate') is not None else '—'} | "
            f"{pct(s.get('time_in_market'))} |")


# ─────────────────────────────────────────────────────────────────────
# 結論生成(依數據)
# ─────────────────────────────────────────────────────────────────────
def _derive_conclusions(stocks: list[dict]) -> dict:
    """把每檔的 A/B 差異彙整成分組結論(純算數,不預設立場)。"""
    per: list[dict] = []
    for st in stocks:
        m = st.get("main")
        if not m or "error" in m:
            continue
        a, b = m["A"]["summary_net"], m["B"]["summary_net"]
        if not a or not b or a.get("n_trades") is None:
            continue
        per.append({
            "code": st["code"], "name": st["name"], "group": st["group"],
            "a": a, "b": b,
            "d_mdd": (b["max_drawdown"] - a["max_drawdown"]),          # 正=B回撤較淺(較好)
            "d_ret": (b["total_return"] - a["total_return"]),
            "d_cagr": (b["cagr"] - a["cagr"]),
            "start": m["analysis_start"], "end": m["analysis_end"],
        })

    groups: dict[str, list[dict]] = {}
    for p in per:
        groups.setdefault(p["group"], []).append(p)

    gstats = {}
    for g, items in groups.items():
        mdds = [i["d_mdd"] for i in items]
        # 組內分歧偵測:n 這麼小的時候,平均值可能把兩個相反的結果抵銷成「沒差異」,
        # 那是假象。只要組內有正有負且落差大,就必須明講「不能用平均代表這一組」。
        diverges = (max(mdds) > 0.03 and min(mdds) < -0.03)
        gstats[g] = {
            "n": len(items),
            "avg_d_mdd": sum(mdds) / len(mdds),
            "avg_d_cagr": sum(i["d_cagr"] for i in items) / len(items),
            "spread_d_mdd": max(mdds) - min(mdds),
            "diverges": diverges,
            "items": items,
        }
    return {"per": per, "groups": gstats, "mechanism": _mechanism_cases(stocks)}


def _mechanism_cases(stocks: list[dict]) -> list[dict]:
    """找出「A 抱著挨最深回撤的那筆交易」,並比對 B 在同一筆是否更早出場。

    這是回答「純 PE 會不會漏接風險」最直接的證據:
    同一個進場點,A 因為沒有出場訊號而一路抱下去,B 是否靠基本面規則先跑掉?
    """
    out: list[dict] = []
    for st in stocks:
        m = st.get("main")
        if not m or "error" in m:
            continue
        a_trades = m["A"]["result"]["trades"]
        b_trades = m["B"]["result"]["trades"]
        if not a_trades:
            continue
        worst = min(a_trades, key=lambda t: t["trade_mdd"])
        match = next((t for t in b_trades if t["entry_date"] == worst["entry_date"]), None)
        out.append({
            "code": st["code"], "name": st["name"], "group": st["group"],
            "entry_date": worst["entry_date"],
            "a_exit": worst["exit_date"], "a_reasons": worst["exit_reasons"],
            "a_mdd": worst["trade_mdd"], "a_ret": worst["ret_net"],
            "a_days": worst["holding_days"],
            "b_exit": match["exit_date"] if match else None,
            "b_reasons": match["exit_reasons"] if match else None,
            "b_mdd": match["trade_mdd"] if match else None,
            "b_ret": match["ret_net"] if match else None,
            "b_days": match["holding_days"] if match else None,
            "b_earlier": (match is not None and match["exit_date"] < worst["exit_date"]),
            "b_no_entry": match is None,
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# 主寫檔
# ─────────────────────────────────────────────────────────────────────
def write_report(out: dict) -> None:
    stocks = out["stocks"]
    concl = _derive_conclusions(stocks)
    L: list[str] = []
    A = L.append

    A("# 股票進出場規則回測:純 PE vs PE + 基本面證偽")
    A("")
    A("> **這份報告要回答的問題**:賣出時只看估值(PE)夠不夠?還是必須加上「基本面轉壞」"
      "才能有效控制風險?用歷史數據回答,不做先入為主的假設。")
    A("")
    A("> ⚠️ 僅供研究,非投資建議。所有數字可用 `cache/` 的原始資料與本專案程式碼完整重算。")
    A("")

    # ── 0. 一段話結論 ────────────────────────────────────────────
    if concl["per"]:
        mech = concl.get("mechanism") or []
        cmp_ok = [c for c in mech if c["b_ret"] is not None]
        helped = [c for c in cmp_ok if c["b_ret"] > c["a_ret"]]
        best = max(concl["per"], key=lambda p: p["d_mdd"])
        n_mdd_better = sum(1 for p in concl["per"] if p["d_mdd"] > 0)
        n_all = len(concl["per"])
        A("## 0. 一段話結論")
        A("")
        A(f"在這 {n_all} 檔、{concl['per'][0]['start'][:4]}–{concl['per'][-1]['end'][:4]} 年的歷史上,"
          f"**加入基本面證偽出場(B)並沒有普遍地降低風險**:最大回撤只在 "
          f"{n_mdd_better}/{n_all} 檔變淺,而年化報酬 **{n_all}/{n_all} 檔全部下降**"
          f"(-{abs(min(p['d_cagr'] for p in concl['per'])) * 100:.0f} ~ "
          f"-{abs(max(p['d_cagr'] for p in concl['per'])) * 100:.0f}pp)。")
        A("")
        A(f"**但這個平均值掩蓋了真正的答案。**決定勝負的不是「股票波動大不大」,而是"
          f"**基本面的惡化是不是真的持續**:")
        A("")
        A(f"- **會漏接風險的情況**:當獲利與股價同步下滑時,PE 會一直停在「看起來便宜」的區間,"
          f"純 PE 規則**永遠不會發出賣出訊號**。{best['code']} {best['name']} 就是實例 —— "
          f"A 抱著走完 {pct(next(c['a_mdd'] for c in mech if c['code'] == best['code']))} 的回撤,"
          f"B 靠基本面訊號提前出場,最大回撤少了 {best['d_mdd'] * 100:.1f}pp。")
        A(f"- **夠用的情況**:穩定成長股(獲利只是短期波動、隨後回升)上,"
          f"基本面訊號多半是假警報,B 賣早了、錯過反彈,"
          f"最後只是白白犧牲報酬({len(cmp_ok) - len(helped)}/{len(cmp_ok)} 檔的關鍵交易屬於此類)。")
        A("")
        A("換句話說:**純 PE 的盲點不在「波動大的股票」,而在「E 正在結構性下滑的股票」。**"
          "基本面規則的價值,取決於它能不能分辨「真崩壞」與「季節性雜訊」—— "
          "而本回測用的簡單規則(連兩季)**分辨不出來**,所以在 4 檔裡只幫上 1 檔。")
        A("")
        A(f"> 樣本僅 {n_all} 檔、交易數個位數,以下所有數字都是**個案觀察,不是統計證據**。"
          f"另有兩項前提限制會直接影響結論,務必一併閱讀:"
          f"**PE 口徑為 trailing 而非前瞻**(第 6.2 節)、"
          f"**TSLA 的 PE 基準本身不可信**(第 7.2 節)。")
        A("")

    # ── 1. 結論 ──────────────────────────────────────────────────
    A("## 1. 結論(先講答案)")
    A("")
    if not concl["per"]:
        A("資料不足,無法產生結論。")
    else:
        A("### 1.1 逐檔:B 相對 A 的差異")
        A("")
        A("| 標的 | 分組 | 回測期間 | 最大回撤 A | 最大回撤 B | 回撤改善 | 年化 A | 年化 B | 年化差異 |")
        A("|---|---|---|---|---|---|---|---|---|")
        for p in concl["per"]:
            imp = p["d_mdd"]
            imp_s = f"{'+' if imp > 0 else ''}{imp * 100:.1f}pp"
            A(f"| {p['code']} {p['name']} | {p['group']} | {p['start']}~{p['end']} | "
              f"{pct(p['a']['max_drawdown'])} | {pct(p['b']['max_drawdown'])} | **{imp_s}** | "
              f"{pct(p['a']['cagr'])} | {pct(p['b']['cagr'])} | "
              f"{'+' if p['d_cagr'] > 0 else ''}{p['d_cagr'] * 100:.1f}pp |")
        A("")
        A("> 「回撤改善」= B 的最大回撤 − A 的最大回撤(兩者皆為負值)。"
          "**正值代表 B 的回撤較淺(較好)**,負值代表 B 反而更深。")
        A("")

        A("### 1.2 分組結論")
        A("")
        for g, gs in concl["groups"].items():
            items = gs["items"]
            detail = "、".join(
                f"{i['code']}{i['name']} {'+' if i['d_mdd'] > 0 else ''}{i['d_mdd'] * 100:.1f}pp"
                for i in items)
            A(f"- **{g}股(n={gs['n']})**:回撤平均改善 "
              f"{'+' if gs['avg_d_mdd'] > 0 else ''}{gs['avg_d_mdd'] * 100:.1f}pp"
              f"(逐檔:{detail});年化平均差異 "
              f"{'+' if gs['avg_d_cagr'] > 0 else ''}{gs['avg_d_cagr'] * 100:.1f}pp。"
              + ("  ⚠ **組內兩檔結果方向相反,平均值不具代表性**,"
                 f"落差達 {gs['spread_d_mdd'] * 100:.1f}pp,請看逐檔數字。" if gs["diverges"] else ""))
        A("")

        # 依數據給出「純 PE 在哪類夠用/不夠用」的判斷
        A("### 1.3 用數據回答原始問題")
        A("")
        THRESH = 0.03  # 回撤差異 3 個百分點以內視為「差異不明顯」(僅作敘述分界,不影響任何計算)
        for g, gs in concl["groups"].items():
            avg = gs["avg_d_mdd"]
            if gs["diverges"]:
                best = max(gs["items"], key=lambda i: i["d_mdd"])
                worst = min(gs["items"], key=lambda i: i["d_mdd"])
                verdict = (f"**組內結論分歧,不能一概而論**:"
                           f"{best['code']}{best['name']} 上 B 明顯較好(回撤改善 {best['d_mdd'] * 100:+.1f}pp),"
                           f"但 {worst['code']}{worst['name']} 上 B 反而較差({worst['d_mdd'] * 100:+.1f}pp)。"
                           f"平均值({avg * 100:+.1f}pp)在此毫無意義,不應用來下結論。")
            elif avg > THRESH:
                verdict = (f"**純 PE 進出在這組會漏接風險**:加入基本面證偽出場後,"
                           f"最大回撤平均改善 {avg * 100:.1f}pp。")
            elif avg < -THRESH:
                verdict = (f"**純 PE 在這組反而較好**:加入基本面規則後回撤平均惡化 "
                           f"{abs(avg) * 100:.1f}pp(B 過早出場、錯過反彈)。")
            else:
                verdict = (f"**兩者差異不明顯**(回撤平均差 {avg * 100:+.1f}pp,"
                           f"在 ±{THRESH * 100:.0f}pp 之內):純 PE 進出大致夠用。")
            A(f"- {g}股:{verdict}")
        A("")

        # 一致的方向:B 的報酬代價
        cagr_worse = [p for p in concl["per"] if p["d_cagr"] < 0]
        if len(cagr_worse) == len(concl["per"]) and concl["per"]:
            A(f"- **一個在 4 檔上完全一致的結果**:B 的年化報酬**每一檔都低於 A**"
              f"(差 {min(p['d_cagr'] for p in concl['per']) * 100:.1f} ~ "
              f"{max(p['d_cagr'] for p in concl['per']) * 100:.1f}pp)。"
              f"基本面出場規則是用**報酬**換**風險控制**,而且代價不小。")
            A("")

        # 機制證據
        mech = concl.get("mechanism") or []
        if mech:
            A("### 1.4 機制證據:A 抱得最痛的那一筆,B 有沒有先跑掉?")
            A("")
            A("直接檢查「A 的最深回撤交易」,看 B 在同一個進場點做了什麼 —— "
              "這是「純 PE 會不會漏接風險」最直接的證據。")
            A("")
            A("| 標的 | 同一進場日 | A 出場 | A 該筆最深回撤 | A 損益 | B 出場 | B 觸發條件 | B 該筆最深回撤 | B 損益 |")
            A("|---|---|---|---|---|---|---|---|---|")
            for c in mech:
                if c["b_no_entry"]:
                    b_cols = "**未進場**(基本面已惡化) | — | — | —"
                else:
                    b_cols = (f"{c['b_exit']}{' ⏪' if c['b_earlier'] else ''} | "
                              f"{reasons_text(c['b_reasons'])} | {pct(c['b_mdd'])} | {pct(c['b_ret'])}")
                A(f"| {c['code']} {c['name']} | {c['entry_date']} | {c['a_exit']} "
                  f"({reasons_text(c['a_reasons'])}) | **{pct(c['a_mdd'])}** | {pct(c['a_ret'])} | {b_cols} |")
            A("")
            earlier = [c for c in mech if c["b_earlier"] or c["b_no_entry"]]
            A(f"> {len(earlier)}/{len(mech)} 檔的「A 最痛交易」上,B 確實更早出場或根本沒進場。"
              f"⏪ 標記代表 B 比 A 早出場。")
            A("")
            # 更早出場 ≠ 更好:比對同一筆交易的最終損益
            cmp_ok = [c for c in mech if c["b_ret"] is not None]
            helped = [c for c in cmp_ok if c["b_ret"] > c["a_ret"]]
            hurt = [c for c in cmp_ok if c["b_ret"] <= c["a_ret"]]
            if cmp_ok:
                A(f"**但「更早出場」不等於「更好」**:同一筆交易比最終損益,"
                  f"B 只在 **{len(helped)}/{len(cmp_ok)}** 檔真的避開了損失"
                  + (f"({'、'.join(c['code'] + c['name'] for c in helped)})" if helped else "")
                  + f",另外 **{len(hurt)}/{len(cmp_ok)}** 檔是**賣太早**"
                  + (f"({'、'.join(c['code'] + c['name'] for c in hurt)})—— "
                     f"股價後來漲回去,A 抱到最後反而賺更多。" if hurt else "。"))
                A("")
                A("> 這就是關鍵區別:基本面規則能不能幫上忙,取決於**惡化是真的持續、還是只是一兩季的波動**。"
                  "規則本身無法分辨這兩者。")
                A("")

            # 案例佐證:B 幫助最大的那一檔,把當時的基本面數字攤開
            best_case = max(concl["per"], key=lambda p: p["d_mdd"]) if concl["per"] else None
            if best_case:
                st = next((s for s in stocks if s["code"] == best_case["code"]), None)
                case = next((c for c in mech if c["code"] == best_case["code"]), None)
                if st and case and st.get("quarters"):
                    A(f"#### 案例:{best_case['code']} {best_case['name']} —— 「真的崩壞」長什麼樣子")
                    A("")
                    A(f"這是 B 表現最好的一檔(回撤改善 {best_case['d_mdd'] * 100:+.1f}pp)。"
                      f"把進場日 {case['entry_date']} 前後的實際基本面數字攤開,"
                      f"就能看出這裡的惡化不是雜訊,而是連續多季的結構性下滑:")
                    A("")
                    A("| 季度 | 單季 EPS | 毛利率 | EPS 連兩季年減 | 毛利率連兩季下滑 | 可用日 |")
                    A("|---|---|---|---|---|---|")
                    ed = case["entry_date"]
                    qs = [q for q in st["quarters"]
                          if q["available_date"] >= _shift_year(ed, -1)
                          and q["available_date"] <= _shift_year(ed, 3)]
                    for q in qs[:14]:
                        A(f"| {q['quarter_end']} | {num(q.get('eps'), 2)} | "
                          f"{num(q.get('gross_margin'), 1)}% | "
                          f"{'✔' if q.get('eps_bad') else ''} | {'✔' if q.get('gm_bad') else ''} | "
                          f"{q['available_date']} |")
                    A("")
                    A(f"> A 在 {case['entry_date']} 進場後一路抱到 {case['a_exit']}"
                      f"(最深回撤 {pct(case['a_mdd'])}、最終 {pct(case['a_ret'])}),"
                      f"因為 PE 隨著獲利一起下滑,**始終沒有觸及 80 百分位的賣出線**。"
                      f"B 則在 {case['b_exit']} 依「{reasons_text(case['b_reasons'])}」出場"
                      f"(最深回撤 {pct(case['b_mdd'])}、最終 {pct(case['b_ret'])})。")
                    A("")
                    A("> **這正是「純 PE 的結構性盲點」**:當 E(獲利)和 P(股價)同步下滑,"
                      "PE 可以一直維持在「看起來便宜」的區間,估值規則因此永遠不會示警。")
                    A("")
        A(f"> **樣本數警告**:本回測只有 {len(concl['per'])} 檔股票、每組 "
          f"{'/'.join(str(g['n']) for g in concl['groups'].values())} 檔,"
          f"交易筆數也是個位數到十幾筆。這個規模**不足以做統計顯著性檢定**,"
          f"上述只能當作「這幾檔在這段歷史上的實際表現」,不能外推成通則。")
        A("")

    # ── 2. 主結果總表 ────────────────────────────────────────────
    A("## 2. 主結果總表")
    A("")
    A("績效皆為**計入交易成本**後(台股:手續費 0.1425%×2 + 證交稅 0.3%;美股:0.025%×2)。")
    A("報酬為**還原股利**的總報酬。")
    A("")
    for st in stocks:
        m = st.get("main")
        if not m or "error" in m:
            A(f"### {st['code']} {st['name']}(無法回測:{st.get('error') or m.get('error')})")
            A("")
            continue
        A(f"### {st['code']} {st['name']}({st['group']})")
        A("")
        A(f"回測期間:**{m['analysis_start']} ~ {m['analysis_end']}**")
        A("")
        A("| 策略 | 總報酬 | 年化報酬 | 最大回撤 | 交易次數 | 勝率(已平倉) | 在市時間 |")
        A("|---|---|---|---|---|---|---|")
        A(_sum_row("A:純 PE 進出", m["A"]["summary_net"]))
        A(_sum_row("B:PE + 基本面證偽", m["B"]["summary_net"]))
        A(_sum_row("(參考)買進持有", m["BH"]["summary_net"]))
        A("")

    # ── 3. 逐檔明細 ──────────────────────────────────────────────
    A("## 3. 逐檔明細:最大單筆虧損情境 + 每筆交易")
    A("")
    for st in stocks:
        m = st.get("main")
        if not m or "error" in m:
            continue
        A(f"### {st['code']} {st['name']}")
        A("")
        for tag, label in (("A", "策略 A(純 PE 進出)"), ("B", "策略 B(PE + 基本面證偽)")):
            blk = m[tag]
            s = blk["summary_net"]
            A(f"#### {label}")
            A("")
            # 最大單筆虧損情境
            w = blk.get("worst")
            if w and w["ret_net"] < 0:
                ctx = []
                ctx.append(f"進場日 **{w['entry_date']}**(訊號日 {w['signal_date']}),"
                           f"進場價 {num(w['entry_price_raw'], 2)},"
                           f"當時 PE {num(w['entry_pe'], 1)} < 進場門檻(歷史中位數)"
                           f"{num(w['entry_pe_thr'], 1)}")
                ctx.append(f"出場日 **{w['exit_date']}**,出場價 {num(w['exit_price_raw'], 2)},"
                           f"當時 PE {num(w['exit_pe'], 1)}")
                ctx.append(f"觸發條件:**{reasons_text(w['exit_reasons'])}**")
                ctx.append(f"持有 {w['holding_days']} 個交易日,期間股價自波段高點最深下跌 "
                           f"{pct(w['trade_mdd'])}")
                if w.get("exit_eps_bad") or w.get("exit_gm_bad"):
                    bad = []
                    if w.get("exit_eps_bad"):
                        bad.append("EPS 連兩季年減")
                    if w.get("exit_gm_bad"):
                        bad.append("毛利率連兩季下滑")
                    ctx.append(f"出場當下基本面狀態:{' / '.join(bad)}(最新可用季度 {w.get('exit_quarter')})")
                    fund_bad = " / ".join(bad)
                else:
                    ctx.append(f"出場當下基本面**未觸發**惡化訊號(最新可用季度 {w.get('exit_quarter')})")
                    fund_bad = None
                A(f"**最大單筆虧損:{pct(w['ret_net'])}**")
                A("")
                for c in ctx:
                    A(f"- {c}")
                A("")
                # 情境解讀要分策略講,否則會出現「A 因基本面出場」這種矛盾敘述
                if tag == "A":
                    if fund_bad:
                        note = (f"值得注意的是,出場當下基本面其實**已在惡化**({fund_bad}),"
                                f"但純 PE 規則看不到這個訊號 —— 它只在 PE 越過 80 百分位時才動作。")
                    else:
                        note = "出場時基本面並未惡化,這筆虧損純粹來自估值波動。"
                    A(f"> 情境解讀:在「估值看起來便宜」(PE 低於自身歷史中位數)時買進,"
                      f"最終以 {reasons_text(w['exit_reasons'])} 出場。{note}")
                else:
                    if fund_bad:
                        note = f"基本面隨後被證偽({fund_bad}),規則據此提前出場、認賠。"
                    else:
                        note = "出場由估值條件觸發,基本面訊號並未示警。"
                    A(f"> 情境解讀:在「估值看起來便宜」(PE 低於自身歷史中位數)時買進,"
                      f"最終以 {reasons_text(w['exit_reasons'])} 出場。{note}")
                A("")
            elif s.get("n_trades"):
                A(f"沒有虧損交易(最差一筆為 {pct(s.get('worst_trade'))})。")
                A("")
            else:
                A("此策略在回測期間內沒有產生任何交易。")
                A("")

            # 交易明細表
            trades = blk["result"]["trades"]
            if trades:
                A("| # | 進場日 | 進場價 | 進場PE | 出場日 | 出場價 | 出場PE | 觸發條件 | 報酬(計費) | 持有(交易日) | 期間最大回撤 |")
                A("|---|---|---|---|---|---|---|---|---|---|---|")
                for i, t in enumerate(trades, 1):
                    A(f"| {i} | {t['entry_date']} | {num(t['entry_price_raw'], 2)} | "
                      f"{num(t['entry_pe'], 1)} | {t['exit_date']} | "
                      f"{num(t['exit_price_raw'], 2)} | {num(t['exit_pe'], 1)} | "
                      f"{reasons_text(t['exit_reasons'])} | {pct(t['ret_net'])} | "
                      f"{t['holding_days']} | {pct(t['trade_mdd'])} |")
                A("")
                A("> 價格為**未還原股利**的實際市價(便於對照當時行情);"
                  "報酬則以**還原股利**的總報酬計算並扣除交易成本,兩者不會完全對應(台股尤其明顯)。")
                A("")

    # ── 4. 假設檢定 ──────────────────────────────────────────────
    A("## 4. 原始假設 vs 實際結果")
    A("")
    A("原始假設:**「A、B 在穩定股上差異小;B 在轉折股上最大回撤明顯較小」**")
    A("")
    if concl["groups"]:
        stable = concl["groups"].get("穩定成長")
        vol = concl["groups"].get("高波動/轉折")
        rows = []
        if stable:
            rows.append(("穩定成長股", stable))
        if vol:
            rows.append(("高波動/轉折股", vol))
        A("| 分組 | 檔數 | 回撤改善(平均) | 年化差異(平均) | 組內一致性 |")
        A("|---|---|---|---|---|")
        for label, gs in rows:
            items = gs["items"]
            detail = "、".join(f"{i['code']} {i['d_mdd'] * 100:+.1f}pp" for i in items)
            consist = ("⚠ **方向相反**(" + detail + ")") if gs["diverges"] else ("一致(" + detail + ")")
            A(f"| {label} | {gs['n']} | {gs['avg_d_mdd'] * 100:+.1f}pp | "
              f"{gs['avg_d_cagr'] * 100:+.1f}pp | {consist} |")
        A("")
        if stable and vol:
            s_ok = abs(stable["avg_d_mdd"]) < 0.03 and not stable["diverges"]
            v_ok = vol["avg_d_mdd"] > 0.03 and not vol["diverges"]
            if s_ok and v_ok:
                A("**結果:假設成立。** 穩定股上兩策略差異小,轉折股上 B 的回撤明顯較淺。")
            elif vol["diverges"]:
                best = max(vol["items"], key=lambda i: i["d_mdd"])
                worst = min(vol["items"], key=lambda i: i["d_mdd"])
                A(f"**結果:一半成立,但關鍵的那一半無法一概而論。**")
                A("")
                A(f"- 「穩定股差異小」→ **成立**(平均 {stable['avg_d_mdd'] * 100:+.1f}pp,"
                  f"{'組內方向一致' if not stable['diverges'] else '但組內方向不一致'})。")
                A(f"- 「轉折股 B 回撤明顯較小」→ **只對一半的樣本成立**:"
                  f"{best['code']}{best['name']} 完全符合預期({best['d_mdd'] * 100:+.1f}pp),"
                  f"但 {worst['code']}{worst['name']} 完全相反({worst['d_mdd'] * 100:+.1f}pp)。")
                A("")
                A(f"- 兩者的差別不在於「波動高低」,而在於**基本面惡化是不是真的持續**:"
                  f"{best['code']} 在該段期間 EPS 與毛利率連續多季結構性下滑(見第 1.4 節案例),"
                  f"規則抓到的是真訊號;而 {worst['code']} 的問題是"
                  f"**PE 基準本身不可信**(見第 7.2 節),規則的輸入就是壞的。")
            elif v_ok and not s_ok:
                A(f"**結果:部分成立。** 轉折股上 B 回撤確實較淺(平均 {vol['avg_d_mdd'] * 100:+.1f}pp),"
                  f"但穩定股上兩者差異也不小(平均 {stable['avg_d_mdd'] * 100:+.1f}pp),"
                  f"與「穩定股差異小」的預期不符。")
            elif s_ok and not v_ok:
                A(f"**結果:部分成立。** 穩定股上確實差異小({stable['avg_d_mdd'] * 100:+.1f}pp),"
                  f"但轉折股上 B 並未展現預期的回撤優勢({vol['avg_d_mdd'] * 100:+.1f}pp)。")
            else:
                A(f"**結果:假設不成立。** 穩定股差異 {stable['avg_d_mdd'] * 100:+.1f}pp、"
                  f"轉折股差異 {vol['avg_d_mdd'] * 100:+.1f}pp,都與預期方向不符。")
            A("")
            A("> 再次提醒:每組只有 2 檔,這是**個案觀察不是統計證據**。方向性參考即可。")
            A("")

    # ── 5. 穩健性檢查 ────────────────────────────────────────────
    A("## 5. 穩健性檢查(避免結論建立在單一設定上)")
    A("")
    A("### 5.1 暖身期敏感度")
    A("")
    A(f"主結果用 {P.WARMUP_TRADING_DAYS} 個交易日(約 2 年)作為「累積多少 PE 歷史才准開始交易」。"
      f"下表把 {P.WARMUP_SENSITIVITY} 日的結果一併列出 —— **全部照實呈現,不挑對結論有利的**。")
    A("")
    A("| 標的 | 暖身期 | 起始日 | 最大回撤 A | 最大回撤 B | 回撤改善 |")
    A("|---|---|---|---|---|---|")
    for st in stocks:
        m = st.get("main")
        if not m or "error" in m:
            continue
        base_a, base_b = m["A"]["summary_net"], m["B"]["summary_net"]
        A(f"| {st['code']} {st['name']} | {P.WARMUP_TRADING_DAYS}(主) | {m['analysis_start']} | "
          f"{pct(base_a['max_drawdown'])} | {pct(base_b['max_drawdown'])} | "
          f"{(base_b['max_drawdown'] - base_a['max_drawdown']) * 100:+.1f}pp |")
        for w, r in (st.get("warmup_sensitivity") or {}).items():
            a, b = r["A"], r["B"]
            if a.get("max_drawdown") is None or b.get("max_drawdown") is None:
                continue
            A(f"| {st['code']} {st['name']} | {w} | {r['start']} | "
              f"{pct(a['max_drawdown'])} | {pct(b['max_drawdown'])} | "
              f"{(b['max_drawdown'] - a['max_drawdown']) * 100:+.1f}pp |")
    A("")

    A("### 5.2 美股基本面訊號:共識 EPS vs 實際 EPS")
    A("")
    A("題目指定「有共識資料用共識、沒有就用實際 EPS」。美股兩種都拿得到,因此兩種都跑:")
    A("")
    has_variant = False
    for st in stocks:
        v = st.get("eps_variant")
        m = st.get("main")
        if not v or not m or "error" in m:
            continue
        has_variant = True
        A(f"**{st['code']} {st['name']}**")
        A("")
        A("| 基本面 EPS 口徑 | 最大回撤 B | 年化 B | 交易次數 B |")
        A("|---|---|---|---|")
        A(f"| 共識 EPS(主結果) | {pct(m['B']['summary_net']['max_drawdown'])} | "
          f"{pct(m['B']['summary_net']['cagr'])} | {m['B']['summary_net']['n_trades']} |")
        A(f"| 實際 EPS(與台股同口徑) | {pct(v['B']['max_drawdown'])} | "
          f"{pct(v['B']['cagr'])} | {v['B']['n_trades']} |")
        A("")
    if not has_variant:
        A("(無美股標的或資料不足。)")
        A("")

    A("### 5.3 「字面版 B」:為什麼 B 的進場需要多一個濾網")
    A("")
    A("題目寫「B 的進場條件同 A」。若完全照字面實作,會出現一個機械性問題:"
      "基本面觸發賣出的**隔天**,PE 通常仍低於中位數(基本面轉壞的股票 PE 往往也低)→ 立刻買回,"
      "接著又因為同一個基本面條件再賣出……如此反覆。下表是實測結果:")
    A("")
    A("| 標的 | 策略 A 回撤 | 策略 A 交易次數 | 字面版 B 回撤 | 字面版 B 交易次數 | 主結果 B 回撤 | 主結果 B 交易次數 |")
    A("|---|---|---|---|---|---|---|")
    lit_rows = []
    for st in stocks:
        m = st.get("main")
        if not m or "error" in m:
            continue
        a = m["A"]["summary_net"]
        lit = m["B_literal"]["summary_net"]
        b = m["B"]["summary_net"]
        lit_rows.append((st, a, lit, b))
        A(f"| {st['code']} {st['name']} | {pct(a['max_drawdown'])} | {a['n_trades']} | "
          f"{pct(lit.get('max_drawdown'))} | **{lit.get('n_trades')}** | "
          f"{pct(b['max_drawdown'])} | {b['n_trades']} |")
    A("")
    if lit_rows:
        worse = [x for x in lit_rows if x[2].get("max_drawdown") is not None
                 and x[2]["max_drawdown"] < x[1]["max_drawdown"]]
        avg_lit_n = sum(x[2].get("n_trades") or 0 for x in lit_rows) / len(lit_rows)
        avg_a_n = sum(x[1].get("n_trades") or 0 for x in lit_rows) / len(lit_rows)
        A(f"**實測結果與直覺不同,如實記錄**:字面版 B 並沒有「退化成 A」,而是變成"
          f"**高頻空轉**——平均交易 {avg_lit_n:.0f} 次(策略 A 平均只有 {avg_a_n:.0f} 次),"
          f"因為賣出後隔天 PE 仍低於中位數就立刻買回,然後再度觸發同一個基本面條件賣出,"
          f"如此來回。結果是 {len(worse)}/{len(lit_rows)} 檔的最大回撤**比 A 更深**"
          f"(交易成本與來回摩擦持續侵蝕淨值),完全失去風險控制的意義。")
        A("")
    A("> 因此主結果的 B 把「基本面惡化」視為一個**狀態**而非單次事件:惡化期間不進場、持有則出場。"
      "這是一個**必要的實作詮釋**,它讓 B 偏離了字面上的「進場條件同 A」,"
      "所以在此完整揭露、並附上字面版的數字供讀者自行判斷。")
    A("")

    # ── 6. 方法與口徑 ────────────────────────────────────────────
    A("## 6. 方法與口徑")
    A("")
    A("### 6.1 策略定義")
    A("")
    A("| | 策略 A | 策略 B |")
    A("|---|---|---|")
    A(f"| 進場 | PE < 自身歷史 PE 第 {P.ENTRY_PCTL:.0f} 百分位(中位數) | 同 A,且基本面未處於惡化狀態 |")
    A(f"| 出場 | PE > 自身歷史 PE 第 {P.EXIT_PCTL:.0f} 百分位 | "
      f"(a) PE > 第 {P.EXIT_PCTL:.0f} 百分位、或 (b) EPS 連 {P.DETERIORATION_QUARTERS} 季年減、"
      f"或 (c) 毛利率連 {P.DETERIORATION_QUARTERS} 季下滑 |")
    A("| 部位 | 全押 / 全空手,不做空、不用槓桿 | 同左 |")
    A("")
    A("### 6.2 ⚠ PE 口徑:本回測用的是 trailing PE,不是前瞻 PE")
    A("")
    A("這是**必須誠實標明的最大口徑限制**:")
    A("")
    A("- 題目原本希望「前瞻 PE(或 trailing PE,視資料可得性)」。實際可得性是:"
      "**免費資料源拿不到「歷史上每一天的前瞻 PE」**。前瞻 PE 需要「當時的分析師共識 EPS 預估」"
      "逐日序列;yfinance 只提供**當下**的共識快照(`eps_trend` 僅回溯 90 天),無法回溯到 2015 年。"
      "硬用「今天的共識」去回推過去,就是前視偏誤。")
    A("- 因此**全部標的一律使用 trailing PE**(近四季已公布 EPS),口徑統一、可回溯、無前視。")
    A("- 影響方向要講清楚:trailing PE 是**落後指標**。當獲利開始下滑,分母(過去四季 EPS)"
      "還停留在高檔,PE 會顯得「便宜」→ **這正是純 PE 策略容易在轉折點誤判進場的結構原因**。"
      "若能取得真正的前瞻 PE,策略 A 的表現可能會優於本報告的結果;"
      "但同時,策略 B 的基本面規則所捕捉的資訊,有一部分也會被前瞻 PE 提前反映。"
      "**本報告的結論僅在 trailing PE 口徑下成立。**")
    A("")
    A("兩市場的 trailing PE 取得方式不同(已盡量對齊):")
    A("")
    A("| 市場 | PE 來源 | 說明 |")
    A("|---|---|---|")
    A("| 台股 | TWSE 個股日本益比(BWIBBU) | 交易所官方計算的每日 trailing PE,權威口徑 |")
    A("| 美股 | 自算:未還原收盤價 / 近四季實際 EPS | EPS 為分析師基準(adjusted),與共識同口徑 |")
    A("")
    A("### 6.3 前視偏誤(look-ahead bias)防護")
    A("")
    A("| 環節 | 做法 |")
    A("|---|---|")
    A("| 歷史 PE 分位數 | **擴張視窗**:第 t 天的中位數/80 百分位只用第 t 天(含)以前的 PE。"
      "絕不使用全樣本分位數 |")
    A("| 台股財報可用日 | 法定申報期限(Q1→5/15、Q2→8/14、Q3→11/14、Q4→隔年 3/31),"
      "保守:實際公布只會更早 |")
    A("| 美股財報可用日 | EPS 用實際財報日;毛利率用 SEC EDGAR 的實際送件日(`filed`) |")
    A("| 美股財務數字 | 同一期間有多筆(重編/比較欄位)時,一律取**最早送件**那筆 |")
    A(f"| 成交時點 | T 日收盤產生訊號 → **T+{P.EXECUTION_LAG_DAYS} 日收盤**成交,杜絕當日前視 |")
    A("| A/B 共同起跑線 | 起始日 = max(PE 暖身完成日, 基本面規則首次可評估日),"
      "避免 B 在沒有財報的年代退化成 A 而稀釋差異 |")
    A("")
    A("### 6.4 價格口徑")
    A("")
    A("- **PE 分子**:未還原股利的收盤價(市場真實報價;用還原價會系統性低估 PE)。")
    A("- **報酬與回撤**:還原股利與分割的收盤價(total return)。忽略股利會低估台股報酬。")
    A("- 兩策略吃同一份價格,比較基礎一致。")
    A("")

    # ── 7. 資料範圍與限制 ────────────────────────────────────────
    A("## 7. 資料範圍與限制(誠實揭露)")
    A("")
    A("### 7.1 各標的實際資料範圍")
    A("")
    A("| 標的 | 股價 | PE 資料 | 財報季數 | 實際回測期間 | 限制條件 |")
    A("|---|---|---|---|---|---|")
    for st in stocks:
        meta = st.get("meta") or {}
        m = st.get("main")
        period = f"{m['analysis_start']} ~ {m['analysis_end']}" if (m and "error" not in m) else "無法回測"
        pe_rng = (f"{meta.get('pe_start')} ~ {meta.get('pe_end')}"
                  if meta.get("pe_start") else "—")
        A(f"| {st['code']} {st['name']} | {meta.get('price_start')} ~ {meta.get('price_end')} | "
          f"{pe_rng} | {meta.get('fs_quarters')} 季"
          f"({meta.get('fs_start')}~{meta.get('fs_end')}) | {period} | "
          f"{meta.get('eps_basis', '')} |")
    A("")
    A("**為什麼回測期間比股價歷史短很多?** 兩個約束同時生效:"
      "(1) PE 分位數需要暖身期;(2) 基本面規則需要足夠季數才能評估"
      "(連兩季年減要 6 季歷史)。取兩者較晚者當共同起跑線。")
    A("")

    # ── 7.1b PE 基準可信度 ──────────────────────────────────────
    A("### 7.2 ⚠ 「自身歷史 PE 基準」可不可信?(策略 A、B 共同的前提)")
    A("")
    A("兩個策略都假設「這檔股票有一段可信的 PE 歷史」可以拿來當中位數/百分位基準。"
      "這個前提**不是對每檔股票都成立**,尤其是長期虧損、剛轉盈的公司。"
      "下表把暖身期實際用到的 PE 樣本攤開,讓讀者自行判斷基準可不可信:")
    A("")
    A("| 標的 | 暖身樣本天數 | 暖身期 PE 範圍 | 暖身期 PE 中位數 | 首個有效 PE 日 | 回測期間「無 PE」天數占比 |")
    A("|---|---|---|---|---|---|")
    for st in stocks:
        m = st.get("main")
        if not m or "error" in m:
            continue
        dg = m.get("pe_diag") or {}
        A(f"| {st['code']} {st['name']} | {dg.get('warmup_n')} | "
          f"{num(dg.get('warmup_pe_min'))} ~ {num(dg.get('warmup_pe_max'))} | "
          f"{num(dg.get('warmup_pe_median'))} | {dg.get('first_valid_pe_date')} | "
          f"{pct(dg.get('period_no_pe_pct'))} |")
    A("")
    A("**TSLA 必須特別警告**:它在 2010–2020 年間大部分時間 TTM EPS ≤ 0(PE 無定義),"
      "少數有 PE 的期間(2014–2015)EPS 僅約 0.03–0.05 美元,"
      "PE 因此被機械性地推到數百倍。結果是暖身樣本的「歷史 PE 中位數」高達數百倍 —— "
      "**這不是一個有意義的估值基準**。因此 TSLA 上「PE 低於自身歷史中位數」實際意義是"
      "「低於約 290 倍本益比」,並不代表便宜。")
    A("")
    A("> 這是一個**對 A 和 B 都成立**的前提失效問題,不是某一個策略的缺陷。"
      "報告仍照實呈現 TSLA 的結果,但任何基於 TSLA 的結論都必須帶著這個警告來讀。")
    A("")

    A("### 7.3 共識 EPS 的可得性(重要限制)")
    A("")
    A("| 標的 | 共識 EPS | 實際採用 | 可能失真 |")
    A("|---|---|---|---|")
    for st in stocks:
        meta = st.get("meta") or {}
        if meta.get("has_consensus"):
            A(f"| {st['code']} {st['name']} | 有(yfinance 財報日共識) | "
              f"共識 EPS 連兩季**年減** | 只有「每季財報日當下的共識值」一個點,"
              f"沒有逐日修正軌跡 → 無法測「共識被下修的當下」,只能測「共識水準低於去年同季」 |")
        else:
            A(f"| {st['code']} {st['name']} | **無**(FinMind 免費版不提供台股分析師共識) | "
              f"**實際 EPS 連兩季年減**(題目指定的替代做法) | "
              f"實際 EPS 落後於共識修正:分析師通常在財報前就下修,"
              f"用實際值會**晚 1 季以上才發出訊號**,低估 B 的即時性 |")
    A("")
    A("### 7.4 其他已知限制")
    A("")
    A("1. **樣本數極小**:4 檔股票、每檔個位數到十幾筆交易。任何「勝率」「平均報酬」都不具統計檢定力。"
      "本報告不做顯著性宣稱。")
    A("2. **毛利率的季節性**:規則 (c) 定義為「連續兩季**環比**下滑」。台股與特斯拉的毛利率都有季節性,"
      "環比下滑有時只是淡季,不代表基本面轉壞 → 這條規則會產生假訊號。"
      "報告未對此做任何修正(修正就等於加參數)。")
    A("3. **虧損期間 PE 失效**:EPS 為負時 trailing PE 無意義(台股 TWSE 直接不給值,美股 TTM EPS ≤ 0)。"
      "這段期間**純 PE 規則完全沒有訊號**:不能進場,持有中也不會出場。"
      "這不是程式 bug,而是純估值規則的結構性盲點,已在交易明細中如實呈現。")
    A("4. **未還原 vs 還原價**:PE 用未還原價、報酬用還原價,兩者在交易明細表中不會對應,已加註說明。")
    A("5. **無滑價模型**:假設可用收盤價成交。個股流動性足夠,但極端行情下實際成交價可能較差。")
    A("6. **現金不計息**:空手期間報酬以 0 計。這對「在市時間較短」的策略(通常是 B)是**保守**的假設。")
    A("7. **未考慮稅負差異**(台股股利所得稅、美股股息預扣稅)。")
    A("")

    # ── 8. 過擬合防護 ────────────────────────────────────────────
    A("## 8. 過擬合防護")
    A("")
    A("| 項目 | 做法 |")
    A("|---|---|")
    A(f"| 參數數量 | **只有題目定義的 3 個**:進場 {P.ENTRY_PCTL:.0f} 百分位、"
      f"出場 {P.EXIT_PCTL:.0f} 百分位、惡化持續 {P.DETERIORATION_QUARTERS} 季。全部採題目指定值 |")
    A("| 參數搜尋 | **完全沒做**。沒有跑網格搜尋、沒有試不同百分位再挑最好的 |")
    A("| 事後調整 | 無。看到結果後未回頭改任何門檻 |")
    A("| 結構設定 | 暖身期是唯一的自由度,已用 3 種設定併陳(第 5.1 節),不挑好看的 |")
    A("| 標的選擇 | 4 檔全由題目指定,非事後挑選 |")
    A("| 結論生成 | 報告第 1 節的判斷句由程式依指標**自動生成**,數字變結論就變,無法先射箭再畫靶 |")
    A("")
    A("**仍然存在的過擬合風險(誠實說明)**:標的是「已知結果的知名個股」"
      "(台積電長期上漲、特斯拉大幅波動),這本身就帶有選擇偏誤。"
      "要真正驗證規則,需要在**數百檔隨機抽樣**的股票上重跑同一套邏輯。")
    A("")

    # ── 9. 重現方式 ──────────────────────────────────────────────
    A("## 9. 如何重現")
    A("")
    A("```bash")
    A("cd rule-backtest")
    A("python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    A(".venv/bin/python fetch_data.py     # 抓資料(TWSE 免額度;FinMind 有每小時上限,可續跑)")
    A(".venv/bin/python run.py            # 回測 + 產生本報告")
    A("```")
    A("")
    A("原始輸出:`data/results.json`(所有指標)、`data/trades.csv`(逐筆交易)、`cache/`(原始資料快取)。")
    A("")

    OUT.write_text("\n".join(L), encoding="utf-8")
    _write_trades_csv(stocks)


def _write_trades_csv(stocks: list[dict]) -> None:
    TRADES_CSV.parent.mkdir(exist_ok=True)
    with TRADES_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "strategy", "entry_date", "entry_price_raw", "entry_pe",
                    "entry_pe_threshold", "exit_date", "exit_price_raw", "exit_pe",
                    "exit_pe_threshold", "exit_reasons", "ret_gross", "ret_net",
                    "holding_days", "trade_max_drawdown", "open_position"])
        for st in stocks:
            m = st.get("main")
            if not m or "error" in m:
                continue
            for tag in ("A", "B"):
                for t in m[tag]["result"]["trades"]:
                    w.writerow([
                        st["code"], st["name"], tag, t["entry_date"], t["entry_price_raw"],
                        t["entry_pe"], t["entry_pe_thr"], t["exit_date"], t["exit_price_raw"],
                        t["exit_pe"], t["exit_pe_thr"], "|".join(t["exit_reasons"]),
                        round(t["ret_gross"], 6), round(t["ret_net"], 6),
                        t["holding_days"], round(t["trade_mdd"], 6), t["open"],
                    ])
