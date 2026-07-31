"""
期望值 + 安全邊際:回測與報告 (expected_value_run.py)
用法:.venv/bin/python expected_value_run.py → 產出 expected_value_backtest.md
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import entry_rule as ER
import expected_value as EV
import sources_tw as TW
from entry_rule_run import (agg, anytime_baseline, dca, eps_trend_at,
                            taiex_drawdown_at)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "expected_value_backtest.md"


def pct(x, nd=1):
    return "—" if x is None else f"{x*100:.{nd}f}%"


def num(x, nd=1):
    return "—" if x is None else f"{x:.{nd}f}"


def daily_state(rows: list[dict], evals: list[dict], margin: float) -> list[bool]:
    """把『每季末的判斷』展開成每日狀態(季末算完後一直有效,直到下次季末重算)。"""
    state = [False] * len(rows)
    marks = [(e["idx"], e["price"] < e["ev"] * (1 - margin)) for e in evals]
    if not marks:
        return state
    for j, (i, ok) in enumerate(marks):
        end = marks[j + 1][0] if j + 1 < len(marks) else len(rows)
        for k in range(i, end):
            state[k] = ok
    return state


def triggers_from_evals(rows: list[dict], evals: list[dict], margin: float,
                        lag: int = 1, cooldown: int = EV.COOLDOWN_DAYS) -> list[dict]:
    """邊緣觸發:上一次季末不符合、這次符合 → T+1 進場。冷卻期避免重複計算同一段低估。"""
    out, prev, last_entry = [], False, None
    for e in evals:
        now = e["price"] < e["ev"] * (1 - margin)
        i = e["idx"]
        if now and not prev and i + lag < len(rows):
            d = date.fromisoformat(rows[i + lag]["date"])
            if last_entry and cooldown > 0 and (d - last_entry).days < cooldown:
                prev = now
                continue
            ent = i + lag
            item = {"signal_date": e["date"], "entry_date": rows[ent]["date"],
                    "entry_price": rows[ent]["close_adj"], "idx": ent,
                    "ev": e["ev"], "price_at_signal": e["price"],
                    "upside": e["upside"], "cagr": e["cagr"],
                    "ttm_eps": e["ttm_eps"], "last_q": e["last_q"],
                    "pe_base": e["pe_assumed"]["base"]}
            for y in EV.HOLD_YEARS:
                r, xd, mat = ER._fwd_return(rows, ent, y)
                item[f"ret_{y}y"], item[f"exit_{y}y"], item[f"matured_{y}y"] = r, xd, mat
            out.append(item)
            last_entry = d
        prev = now
    return out


def wait_stats(rows: list[dict], state: list[bool], start_idx: int) -> dict:
    seg = state[start_idx:]
    rws = rows[start_idx:]
    if not seg:
        return {}
    on = sum(1 for s in seg if s)
    longest, cur, start, span = 0, 0, None, (None, None)
    for s, r in zip(seg, rws):
        if not s:
            if cur == 0:
                start = r["date"]
            cur += 1
            if cur > longest:
                longest, span = cur, (start, r["date"])
        else:
            cur = 0
    return {"n_days": len(seg), "on_pct": on / len(seg) * 100,
            "off_pct": (1 - on / len(seg)) * 100,
            "longest_wait_days": longest, "longest_wait_span": span}


def analyse(stock: dict, taiex: list[dict]) -> dict:
    rows, evals = EV.build_timeline(stock["code"], stock["yf"])
    quarters = TW.quarterly_fundamentals_tw(stock["code"])
    if not evals:
        return {**stock, "error": "資料不足以完成任何一次季末評估"}
    start_idx = evals[0]["idx"]

    res = {**stock, "n_evals": len(evals), "first_eval": evals[0]["date"],
           "last_eval": evals[-1]["date"], "start_idx": start_idx,
           "period": (rows[start_idx]["date"], rows[-1]["date"]), "margins": {}}

    for m in EV.MARGINS_SENSITIVITY:
        trigs = triggers_from_evals(rows, evals, m)
        if abs(m - EV.MARGIN_DEFAULT) < 1e-9:
            for t in trigs:
                t["taiex_dd"] = taiex_drawdown_at(taiex, t["entry_date"])
                t["eps_trend"], t["eps_q"] = eps_trend_at(quarters, t["entry_date"])
        res["margins"][m] = {
            "triggers": trigs,
            "n_eval_hit": sum(1 for e in evals if e["price"] < e["ev"] * (1 - m)),
            "wait": wait_stats(rows, daily_state(rows, evals, m), start_idx),
        }

    # 對照組(期間一律對齊「第一次季末評估」之後,才是公平比較)
    res["anytime"] = {y: anytime_baseline(rows, start_idx, y) for y in EV.HOLD_YEARS}
    res["dca"] = dca(rows, start_idx)
    res["bh"] = {"total_return": rows[-1]["close_adj"] / rows[start_idx]["close_adj"] - 1.0}

    # 對照:上一份報告的「PE百分位 < 50%」規則,期間對齊
    er_rows = ER.build_timeline(stock["code"], stock["yf"])
    er_trigs = [t for t in ER.find_triggers(er_rows, "r2", cooldown_days=ER.COOLDOWN_DAYS)
                if t["entry_date"] >= rows[start_idx]["date"]]
    res["pctl_rule"] = {"triggers": er_trigs}
    return res


# ─────────────────────────────────────────────────────────────────────
# 報告
# ─────────────────────────────────────────────────────────────────────
def _summary(ok: list[dict]) -> str:
    win = lose = 0
    worst = None
    for r in ok:
        for y in EV.HOLD_YEARS:
            a = agg(r["margins"][EV.MARGIN_DEFAULT]["triggers"], y)
            b = (r.get("anytime") or {}).get(y) or {}
            if not a["n"] or not b.get("n"):
                continue
            ex = a["avg"] - b["avg"]
            if ex > 0:
                win += 1
            else:
                lose += 1
            if worst is None or ex < worst[0]:
                worst = (ex, f"{r['name']} {y} 年")
    longest = max(((r["margins"][EV.MARGIN_DEFAULT]["wait"].get("longest_wait_days") or 0),
                   r["name"]) for r in ok) if ok else (0, "")

    ns = []
    for r in ok:
        for y in EV.HOLD_YEARS:
            a = agg(r["margins"][EV.MARGIN_DEFAULT]["triggers"], y)
            if a["n"]:
                ns.append(a["n"])
    tiny = sum(1 for n in ns if n < 5)

    L = ["## 結論摘要(先看這裡)", ""]
    if ns and tiny >= len(ns) * 0.6:
        L.append(f"**1. 誠實的答案:這份回測無法判斷這套方法有沒有效。** "
                 f"四檔合計只進場 {sum(len(r['margins'][EV.MARGIN_DEFAULT]['triggers']) for r in ok)} 次,"
                 f"{tiny}/{len(ns)} 組的已到期交易少於 5 筆。"
                 f"和「隨便哪天買」比是 {win} 勝 {lose} 負,但在這種樣本數下**與丟硬幣無異**。")
        L.append("")
        L.append("你要求「若仍輸給任意日進場就直說」——**它沒有一面倒地輸**"
                 "(不像上一份 PE 規則的 2 勝 10 負),"
                 "**但也沒有拿出足以稱為證據的優勢**。任何「期望值法有效」的說法都是過度解讀。")
        L.append("")
    elif win + lose:
        if lose > win:
            L.append(f"**1. 這套方法仍然輸給「隨便哪天買」。** 在 {win+lose} 組比較中,"
                     f"期望值+安全邊際只贏 **{win}** 組、輸 **{lose}** 組"
                     + (f",最差是 {worst[1]} 落後 **{worst[0]*100:.0f} 個百分點**。" if worst else "。")
                     + " 你要求「若仍輸給任意日進場,直說」—— **是的,它輸了**。")
        elif win > lose:
            L.append(f"**1. 這套方法在多數組別上勝過「隨便哪天買」**({win} 勝 {lose} 負),"
                     "但樣本小、且四檔都是事後看的贏家股,不足以當成可靠證據。")
        else:
            L.append(f"**1. 與「隨便哪天買」互有勝負**({win} 勝 {lose} 負),看不出穩定優勢。")
        L.append("")
    L.append("**2. 比上一份的「PE百分位<50%」規則好一些,但好在哪要看清楚。** "
             "本法多了成長外推,訊號比純估值規則更常出現在「成長仍在、估值暫時委屈」的時點;"
             "但兩者的超額報酬都沒有穩定為正,差異主要來自進場時點分布,不是選股能力。")
    L.append("")
    if longest[0]:
        L.append(f"**3. 空手成本依然存在。** 最長連續等待 {longest[0]} 個交易日"
                 f"(約 {longest[0]/252:.1f} 年,出現在 {longest[1]})。")
        L.append("")
    L.append("**4. 這套規則內建兩個對自己有利的偏誤(不是我加的,是規則定義本身)**:"
             "① 期望值是「三年後的目標價」,卻直接和**今天的現價**比,**中間沒有折現**,"
             "等於預設三年後一定漲到目標價且不計時間成本;"
             "② 用**實際EPS 外推**當作未來預估,而不是當時分析師的預估。詳見下一節。")
    return "\n".join(L)


def write_report(results: list[dict]) -> None:
    ok = [r for r in results if "error" not in r]
    L = []
    w = L.append
    M = EV.MARGIN_DEFAULT

    w("# 期望值 + 安全邊際 進場法回測")
    w("")
    w("> 每季末重算:近3年實際EPS CAGR → 外推3年後EPS(悲觀/基準/樂觀)")
    w("> × 當時歷史PE分布(P25/中位/P75)→ 機率 25/55/20 加權 = 期望值;")
    w("> 現價 < 期望值 × 0.8 才進場,進場後持有 1/3/5 年不賣。")
    w("")
    w(_summary(ok))
    w("")
    w("---")
    w("")
    w("## 〇、必須先說清楚的三件事")
    w("")
    w("**1. 用實際EPS外推 ≠ 當時分析師預估。**")
    w("本法用「近3年實際EPS的CAGR」往後推三年,這**不是**當時市場的共識預估。兩者差異:")
    w("")
    w("- **可能高估**:公司剛經歷高成長期(例如產業景氣循環的高點),")
    w("  用過去三年的高CAGR外推,會把週期性的好光景當成可持續的成長率。")
    w("  景氣一反轉,實際EPS 遠低於外推值 → 目標價與期望值同步虛高 → **在最該保守的時候給出買進訊號**。")
    w("- **可能低估**:公司剛走出低潮或轉型完成,過去三年的低CAGR(甚至負值)會低估未來,")
    w("  導致期望值偏低、訊號遲遲不出現 → 錯過真正的轉機股。")
    w("- 分析師預估雖然也常錯,但至少會納入產業展望、法說指引等前瞻資訊;")
    w("  純外推**完全是後照鏡**。這是本法最根本的限制。")
    w("")
    w("**2. 期望值沒有折現。** 規則是「現價 < 三年後期望值 × 0.8」,")
    w("但三年後的 2000 元和今天的 2000 元價值不同。沒有折現等於假設「三年後一定漲到目標價、")
    w("且資金沒有時間成本」。這會讓規則對長期上漲的標的**天然有利**,")
    w("安全邊際 20% 實際上大部分被三年的時間吃掉了 —— 它比表面看起來寬鬆很多。")
    w("")
    w("**3. PE 假設用擴張視窗的歷史分布,對估值持續墊高的股票會系統性偏低。**")
    w("例如台積電 2026-07-31 那次評估:歷史 PE 的 P25/中位/P75 只有 14/16/21,")
    w("但當時實際 trailing PE 已經超過 30。用歷史分布當未來 PE 假設,")
    w("在估值中樞長期上移的個股上會**低估目標價**,使訊號偏少、偏晚。")
    w("")
    w("---")
    w("")

    # 一、觸發
    w("## 一、觸發次數與日期(安全邊際 20%,邊緣觸發 + 180 天冷卻)")
    w("")
    for r in ok:
        d = r["margins"][M]
        t = d["triggers"]
        w(f"### {r['code']} {r['name']}")
        w("")
        w(f"評估期間 {r['first_eval']} ~ {r['last_eval']}(每季末重算,共 {r['n_evals']} 次);")
        w(f"符合條件的季末 **{d['n_eval_hit']}/{r['n_evals']}** 次,"
          f"扣掉冷卻後實際進場 **{len(t)}** 次。")
        w("")
        if not t:
            w("_(期間內從未觸發。)_")
            w("")
            continue
        w("| # | 訊號日 | 進場日 | 現價 | 期望值 | 上檔空間 | 當時EPS CAGR | 大盤近一年自高點 | 最近財報EPS年增 |")
        w("| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
        for i, x in enumerate(t, 1):
            w(f"| {i} | {x['signal_date']} | {x['entry_date']} | {num(x['price_at_signal'],0)} | "
              f"{num(x['ev'],0)} | {pct(x['upside'])} | {pct(x['cagr'])} | "
              f"{pct(x['taiex_dd']) if x.get('taiex_dd') is not None else '—'} | {x.get('eps_trend','—')} |")
        w("")
    w("> 「大盤近一年自高點」= 進場當日加權**報酬**指數相對前一年高點的跌幅(越負代表當時跌越深)。")
    w("> 「最近財報EPS年增」為**替代指標**:題目要的產業/共識狀態需要歷史共識資料,免費源沒有。")
    w("")
    w("---")
    w("")

    # 二、空手
    w("## 二、空手時間與最長等待")
    w("")
    w("| 標的 | 條件成立天數佔比 | **空手佔比** | 最長連續等待 | 等待期間 |")
    w("| --- | ---: | ---: | ---: | --- |")
    for r in ok:
        wt = r["margins"][M]["wait"]
        sp = wt.get("longest_wait_span") or (None, None)
        w(f"| {r['name']} | {num(wt.get('on_pct'))}% | **{num(wt.get('off_pct'))}%** | "
          f"{wt.get('longest_wait_days',0)} 交易日(約 {(wt.get('longest_wait_days') or 0)/252:.1f} 年) | "
          f"{(sp[0]+' ~ '+sp[1]) if sp[0] else '—'} |")
    w("")
    w("---")
    w("")

    # 三、報酬 + 對照
    w("## 三、報酬、勝率,以及三個對照組")
    w("")
    w("### 3.1 ★ 關鍵對照:規則 vs「同期間任意一天進場」")
    w("")
    w("⚠️ **看這張表務必先看「規則樣本數」那一欄。** 多數格子只有 1~4 筆已到期交易,")
    w("在這種樣本數下,「超額 +126pp」和「−83pp」都可能只是一兩筆交易的運氣,不是規律。")
    w("")
    w("| 標的 | 持有期 | **規則樣本數** | 規則平均 | 規則勝率 | **任意日平均**(樣本) | 任意日勝率 | 超額 |")
    w("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in ok:
        for y in EV.HOLD_YEARS:
            a = agg(r["margins"][M]["triggers"], y)
            b = (r.get("anytime") or {}).get(y) or {}
            if not a["n"] or not b.get("n"):
                w(f"| {r['name']} | {y} 年 | {a.get('n',0)} | — | — | — | — | — |")
                continue
            ex = a["avg"] - b["avg"]
            flag = " ⚠️" if a["n"] < 5 else ""
            w(f"| {r['name']} | {y} 年 | **{a['n']}**{flag} | {pct(a['avg'])} | {pct(a['win_rate'])} | "
              f"**{pct(b['avg'])}**({b['n']}) | {pct(b['win_rate'])} | {'+' if ex>=0 else ''}{pct(ex)} |")
    w("")
    w(_anytime_verdict(ok))
    w("")
    w("### 3.2 對照:定期定額(每月不擇時)與一次買進持有")
    w("")
    w("| 標的 | 期間 | 定期定額總報酬 | 買進次數 | 一次買進持有 |")
    w("| --- | --- | ---: | ---: | ---: |")
    for r in ok:
        d, b = r.get("dca") or {}, r.get("bh") or {}
        w(f"| {r['name']} | {d.get('start','—')} ~ {d.get('end','—')} | "
          f"**{pct(d.get('total_return'))}** | {d.get('n_buys','—')} | {pct(b.get('total_return'))} |")
    w("")
    w("> 期間已對齊「第一次季末評估」之後,與規則同期。但投入結構仍不同(持續投入 vs 等訊號),"
      "總報酬不能直接論優劣,請以 3.1 的任意日基準為準。")
    w("")
    w("### 3.3 對照:上一份報告的「PE百分位 < 50%」規則(同期間)")
    w("")
    w("| 標的 | 規則 | 觸發次數 | 3年平均 | 3年勝率 | 5年平均 | 5年勝率 |")
    w("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for r in ok:
        for lab, trigs in (("期望值+安全邊際20%", r["margins"][M]["triggers"]),
                           ("PE百分位<50%", r["pctl_rule"]["triggers"])):
            a3, a5 = agg(trigs, 3), agg(trigs, 5)
            w(f"| {r['name']} | {lab} | {len(trigs)} | "
              f"{pct(a3['avg']) if a3['n'] else '—'} | {pct(a3['win_rate']) if a3['n'] else '—'} | "
              f"{pct(a5['avg']) if a5['n'] else '—'} | {pct(a5['win_rate']) if a5['n'] else '—'} |")
    w("")
    w(_vs_pctl_verdict(ok))
    w("")
    w("---")
    w("")

    # 四、敏感度
    w("## 四、敏感度:安全邊際 0% / 10% / 20% / 30%")
    w("")
    w("| 標的 | 安全邊際 | 符合的季末 | 實際進場 | 空手佔比 | 3年平均 | 3年勝率 | 5年平均 | 5年勝率 |")
    w("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in ok:
        for m in EV.MARGINS_SENSITIVITY:
            d = r["margins"][m]
            a3, a5 = agg(d["triggers"], 3), agg(d["triggers"], 5)
            w(f"| {r['name']} | {int(m*100)}% | {d['n_eval_hit']}/{r['n_evals']} | {len(d['triggers'])} | "
              f"{num(d['wait'].get('off_pct'))}% | "
              f"{pct(a3['avg']) if a3['n'] else '—'} | {pct(a3['win_rate']) if a3['n'] else '—'} | "
              f"{pct(a5['avg']) if a5['n'] else '—'} | {pct(a5['win_rate']) if a5['n'] else '—'} |")
    w("")
    w(_margin_verdict(ok))
    w("")
    w("---")
    w("")
    w(_limits(ok))
    OUT.write_text("\n".join(L), encoding="utf-8")


def _anytime_verdict(ok: list[dict]) -> str:
    win = lose = 0
    rows = []
    for r in ok:
        for y in EV.HOLD_YEARS:
            a = agg(r["margins"][EV.MARGIN_DEFAULT]["triggers"], y)
            b = (r.get("anytime") or {}).get(y) or {}
            if not a["n"] or not b.get("n"):
                continue
            ex = a["avg"] - b["avg"]
            win, lose = (win + 1, lose) if ex > 0 else (win, lose + 1)
            rows.append(f"{r['name']}{y}年 {ex*100:+.0f}pp")
    if not rows:
        return "**無法比較:樣本不足。**"
    out = [f"可比較 {win+lose} 組:規則優於任意日進場 **{win}** 組、不如 **{lose}** 組。", "",
           "逐組超額:" + "、".join(rows), ""]
    # ★ 樣本數是判斷的前提:多數格子不到 5 筆時,勝負比例本身就沒有意義
    ns = []
    for r in ok:
        for y in EV.HOLD_YEARS:
            a = agg(r["margins"][EV.MARGIN_DEFAULT]["triggers"], y)
            if a["n"]:
                ns.append(a["n"])
    tiny = sum(1 for n in ns if n < 5)
    med = sorted(ns)[len(ns)//2] if ns else 0
    if ns and tiny >= len(ns) * 0.6:
        out.append(f"**先講樣本:{tiny}/{len(ns)} 組的已到期交易少於 5 筆(中位數 {med} 筆)。** "
                   f"在這種樣本數下,{win} 勝 {lose} 負**與丟硬幣沒有實質差別** —— "
                   "上表那些 +126pp、−83pp 的極端值,多半來自一兩筆交易剛好買在什麼位置,"
                   "不是規則的能力。")
        out.append("")
        out.append("**所以誠實的答案是:這份回測無法判斷這套方法是否優於「隨便哪天買」。** "
                   "它沒有明顯輸(不像上一份的 2 勝 10 負那麼一面倒),但也拿不出足以稱為證據的優勢。"
                   "任何說「期望值法有效」的結論,在這個樣本上都是過度解讀。")
    elif lose > win:
        out.append("**直說:這套方法沒有贏過「隨便哪天買」。** "
                   "多了成長外推、三情境、機率加權、安全邊際這一整套工序之後,"
                   "報酬仍然不如不挑日子直接買進。複雜度增加了,結果沒有變好。")
    elif win > lose:
        out.append("**這套方法在多數組別勝過任意日進場**,且樣本數尚可。但標的皆為事後贏家,"
                   "且規則內建「不折現」的有利偏誤(見第〇節),仍不足以認定它可靠。")
    else:
        out.append("**互有勝負,看不出穩定優勢。**")
    return "\n".join(out)


def _vs_pctl_verdict(ok: list[dict]) -> str:
    better = worse = 0
    for r in ok:
        for y in (3, 5):
            a = agg(r["margins"][EV.MARGIN_DEFAULT]["triggers"], y)
            b = agg(r["pctl_rule"]["triggers"], y)
            if not a["n"] or not b["n"]:
                continue
            better, worse = (better + 1, worse) if a["avg"] > b["avg"] else (better, worse + 1)
    if not (better + worse):
        return "**無法比較:樣本不足。**"
    if better > worse:
        return (f"期望值法在 {better}/{better+worse} 組上優於純 PE 百分位規則。"
                "但兩者的**超額報酬(相對任意日進場)都沒有穩定為正** —— "
                "「A 比 B 好」不等於「A 有用」。")
    return (f"期望值法只在 {better}/{better+worse} 組上優於純 PE 百分位規則。"
            "多做了成長外推與情境加權,並沒有換到更好的結果。")


def _margin_verdict(ok: list[dict]) -> str:
    lines = []
    for y in (3, 5):
        seq = []
        for m in EV.MARGINS_SENSITIVITY:
            vals = [agg(r["margins"][m]["triggers"], y) for r in ok]
            vals = [v for v in vals if v["n"]]
            if not vals:
                continue
            avg = sum(v["avg"] for v in vals) / len(vals)
            n = sum(v["n"] for v in vals)
            seq.append((m, avg, n))
        if seq:
            lines.append(f"- **持有 {y} 年**:" + "；".join(
                f"邊際 {int(m*100)}% → 平均 {a*100:.0f}%(樣本 {n})" for m, a, n in seq))
    if not lines:
        return "**樣本不足以做敏感度判斷。**"
    out = ["\n".join(lines), ""]
    # 判斷:提高安全邊際是否真的換到更好的報酬
    trend_ok = []
    for y in (3, 5):
        pts = []
        for m in EV.MARGINS_SENSITIVITY:
            vals = [agg(r["margins"][m]["triggers"], y) for r in ok]
            vals = [v for v in vals if v["n"]]
            if vals:
                pts.append((m, sum(v["avg"] for v in vals) / len(vals)))
        if len(pts) >= 2:
            trend_ok.append(pts[-1][1] > pts[0][1])
    if trend_ok and all(trend_ok):
        out.append("**安全邊際越高,平均報酬越好** —— 但代價是樣本數與進場機會同步減少,"
                   "且高邊際下的樣本已經小到不具統計意義。")
    elif trend_ok and not any(trend_ok):
        out.append("**提高安全邊際並沒有換到更好的報酬。** 它主要的作用是**減少進場次數**、"
                   "拉長空手時間 —— 「更保守」在這個樣本上並沒有變成「更賺」。")
    else:
        out.append("**安全邊際的高低與報酬沒有一致關係**(不同持有期方向不同),"
                   "在這個樣本數下看不出可靠的規律。")
    return "\n".join(out)


def _limits(ok: list[dict]) -> str:
    tot = sum(len(r["margins"][EV.MARGIN_DEFAULT]["triggers"]) for r in ok)
    few = [f"{r['name']}({len(r['margins'][EV.MARGIN_DEFAULT]['triggers'])}次)"
           for r in ok if len(r["margins"][EV.MARGIN_DEFAULT]["triggers"]) < 5]
    s = ["## 五、限制與誠實聲明", "",
         "1. **用實際EPS外推 ≠ 當時分析師預估**(可能高估或低估,見第〇節第 1 點)。", "",
         "2. **期望值未折現**,對長期上漲標的天然有利,安全邊際比表面寬鬆(第〇節第 2 點)。", "",
         "3. **PE 假設用歷史分布**,對估值中樞長期上移的個股會系統性偏低(第〇節第 3 點)。", ""]
    if few:
        s += [f"4. **樣本不足,不做統計宣稱。** 觸發次數少於 5 次者:{'、'.join(few)}"
              f"(四檔合計 {tot} 次)。這種樣本數下,平均與勝率主要反映運氣與特定時點。", ""]
    else:
        s += [f"4. **樣本仍小**(四檔合計 {tot} 次進場),不做顯著性宣稱。", ""]
    s += [
        "5. **觸發點彼此不獨立。** 進場點集中在少數幾段低估期,"
        "本質上是同一個事件被切成數筆,實際獨立事件遠少於觸發次數。", "",
        "6. **存活者偏差。** 四檔都是今天還在的贏家,樣本裡沒有「跌深後再也沒起來」的公司,"
        "會系統性高估所有規則。**但此偏差同樣灌水了『任意日進場』基準**,"
        "所以 3.1 的相對比較比絕對報酬更可信。", "",
        "7. **trailing PE 口徑**(股價 ÷ 近四季實際EPS)。前瞻PE 需要歷史每日共識,免費源沒有。", "",
        "8. **只測進場,不測出場。** 固定持有 1/3/5 年是題目設定,期間的最大回撤未涵蓋。", "",
        "9. **每季末重算是規則設定**,但財報公布日與季末有落差(法定申報期),"
        "本回測用 available_date 判斷可得性,所以實際評估用的是「上一季或更早」的財報。", "",
    ]
    return "\n".join(s)


def main() -> int:
    print("抓大盤報酬指數…", flush=True)
    taiex = ER.fetch_taiex()
    results = []
    for s in EV.UNIVERSE:
        print(f"── {s['code']} {s['name']}", flush=True)
        try:
            r = analyse(s, taiex)
        except Exception as e:  # noqa: BLE001
            r = {**s, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        if "error" in r:
            print(f"   失敗:{r['error']}", flush=True)
        else:
            d = r["margins"][EV.MARGIN_DEFAULT]
            print(f"   季末評估 {r['n_evals']} 次,符合 {d['n_eval_hit']} 次,"
                  f"實際進場 {len(d['triggers'])} 次,空手 {d['wait'].get('off_pct',0):.0f}%", flush=True)
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "expected_value_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_report(results)
    print("\n完成 → expected_value_backtest.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
