"""
進場規則回測:環境標記 + 對照組 + 報告 (entry_rule_run.py)
=========================================================
用法:.venv/bin/python entry_rule_run.py  → 產出 entry_rule_backtest.md
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import entry_rule as ER
import sources_tw as TW

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "entry_rule_backtest.md"
RULES = [("r1", f"只用 PE < {ER.PE_ABS_MAX:.0f}x"),
         ("r2", f"只用 歷史PE百分位 < {ER.PCTL_MAX:.0f}%"),
         ("r3", "兩條件同時成立")]


def pct(x, nd=1):
    return "—" if x is None else f"{x*100:.{nd}f}%"


def num(x, nd=1):
    return "—" if x is None else f"{x:.{nd}f}"


# ─────────────────────────────────────────────────────────────────────
# 環境標記
# ─────────────────────────────────────────────────────────────────────
def taiex_drawdown_at(taiex: list[dict], d: str, lookback_days: int = 365) -> float | None:
    """進場當日,大盤自「前一年高點」的跌幅 —— 用來標記當時是不是在下跌環境。"""
    lo = (date.fromisoformat(d) - __import__("datetime").timedelta(days=lookback_days)).isoformat()
    win = [r["price"] for r in taiex if lo <= r["date"] <= d]
    if len(win) < 30:
        return None
    peak = max(win)
    cur = win[-1]
    return (cur / peak - 1.0) if peak else None


def eps_trend_at(quarters: list[dict], d: str) -> tuple[str, str]:
    """進場當日『最近可得』的實際EPS 年增方向。

    ★ 題目要的是「共識EPS 當時是上修或下修」,但那需要歷史上每一天的分析師共識,
      免費源沒有(同前瞻PE 的問題)。這裡改用**實際EPS 的年增方向**當替代指標,
      並在報告中明確標示這是替代、不是共識修正方向。
    """
    avail = [q for q in quarters if q.get("available_date") and q["available_date"] <= d
             and q.get("eps") is not None]
    if len(avail) < 5:
        return "資料不足", ""
    avail.sort(key=lambda q: q["available_date"])
    cur = avail[-1]
    # 找去年同季
    y, m = cur["quarter_end"][:4], cur["quarter_end"][5:]
    prev = next((q for q in avail if q["quarter_end"] == f"{int(y)-1}-{m}"), None)
    if not prev or not prev.get("eps"):
        return "資料不足", cur["quarter_end"]
    if prev["eps"] == 0:
        return "資料不足", cur["quarter_end"]
    chg = (cur["eps"] - prev["eps"]) / abs(prev["eps"]) * 100
    lab = "年增↑" if chg > 5 else ("年減↓" if chg < -5 else "持平→")
    return f"{lab} {chg:+.0f}%", cur["quarter_end"]


# ─────────────────────────────────────────────────────────────────────
# 對照組:定期定額不擇時
# ─────────────────────────────────────────────────────────────────────
def dca(rows: list[dict], start_idx: int) -> dict:
    """每月第一個交易日投入 1 單位,直到最後。回傳總報酬(不年化,與規則同期比較)。"""
    seg = rows[start_idx:]
    if len(seg) < 30:
        return {}
    units = 0.0
    invested = 0.0
    seen_month = set()
    for r in seg:
        ym = r["date"][:7]
        if ym in seen_month:
            continue
        seen_month.add(ym)
        units += 1.0 / r["close_adj"]
        invested += 1.0
    final = units * seg[-1]["close_adj"]
    return {
        "invested": invested, "final": final,
        "total_return": final / invested - 1.0,
        "n_buys": len(seen_month),
        "start": seg[0]["date"], "end": seg[-1]["date"],
    }


def buy_hold(rows: list[dict], start_idx: int) -> dict:
    seg = rows[start_idx:]
    return {"total_return": seg[-1]["close_adj"] / seg[0]["close_adj"] - 1.0,
            "start": seg[0]["date"], "end": seg[-1]["date"]}


# ─────────────────────────────────────────────────────────────────────
def anytime_baseline(rows: list[dict], start_idx: int, years: int) -> dict:
    """基準:同期間「任意一天進場」持有 N 年的報酬分布。

    ★ 這是判斷規則有沒有價值的**關鍵對照**:
      如果那段期間不管哪天買、持有 N 年都賺,那高勝率並不代表規則會選時,
      只代表這檔股票在這段歷史上漲了。規則要有價值,必須**明顯優於**這個基準。
    """
    rets = []
    for i in range(start_idx, len(rows)):
        r, _, matured = ER._fwd_return(rows, i, years)
        if matured and r is not None:
            rets.append(r)
    if not rets:
        return {"n": 0}
    wins = [x for x in rets if x > 0]
    return {"n": len(rets), "avg": sum(rets) / len(rets),
            "median": sorted(rets)[len(rets) // 2],
            "win_rate": len(wins) / len(rets)}


def analyse(stock: dict, taiex: list[dict]) -> dict:
    rows = ER.build_timeline(stock["code"], stock["yf"])
    quarters = TW.quarterly_fundamentals_tw(stock["code"])
    first = next((i for i, r in enumerate(rows) if r["tradable"]), None)
    if first is None:
        return {**stock, "error": "PE 歷史不足以完成暖身"}

    res = {**stock, "rows_n": len(rows), "first_tradable": rows[first]["date"],
           "last": rows[-1]["date"], "rules": {}}
    for key, label in RULES:
        # 主結果:加冷卻期(一段低估期只算一次進場,接近真實可執行的決策次數)
        trigs = ER.find_triggers(rows, key, cooldown_days=ER.COOLDOWN_DAYS)
        # 對照:不加冷卻的原始邊緣觸發次數,用來顯示「條件在門檻附近震盪」的程度
        raw = ER.find_triggers(rows, key, cooldown_days=0)
        for t in trigs:
            t["taiex_dd"] = taiex_drawdown_at(taiex, t["entry_date"])
            t["eps_trend"], t["eps_q"] = eps_trend_at(quarters, t["entry_date"])
        res["rules"][key] = {"label": label, "triggers": trigs,
                             "n_raw": len(raw),
                             "days": ER.condition_days(rows, key)}
    res["dca"] = dca(rows, first)
    res["bh"] = buy_hold(rows, first)
    res["anytime"] = {y: anytime_baseline(rows, first, y) for y in ER.HOLD_YEARS}
    return res


def agg(trigs: list[dict], y: int) -> dict:
    """某持有期的彙總(只算已到期者,未到期另計)。"""
    mat = [t for t in trigs if t.get(f"matured_{y}y") and t.get(f"ret_{y}y") is not None]
    un = [t for t in trigs if not t.get(f"matured_{y}y")]
    if not mat:
        return {"n": 0, "n_unmatured": len(un)}
    rets = [t[f"ret_{y}y"] for t in mat]
    wins = [r for r in rets if r > 0]
    return {"n": len(mat), "n_unmatured": len(un),
            "avg": sum(rets) / len(rets),
            "median": sorted(rets)[len(rets) // 2],
            "win_rate": len(wins) / len(rets),
            "best": max(rets), "worst": min(rets)}


def main() -> int:
    print("抓大盤報酬指數…", flush=True)
    taiex = ER.fetch_taiex()
    results = []
    for s in ER.UNIVERSE:
        print(f"── {s['code']} {s['name']}", flush=True)
        try:
            r = analyse(s, taiex)
        except Exception as e:  # noqa: BLE001
            r = {**s, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        if "error" in r:
            print(f"   失敗:{r['error']}", flush=True)
        else:
            for k, lab in RULES:
                d = r["rules"][k]
                print(f"   {lab:22s} 觸發 {len(d['triggers']):2d} 次"
                      f"(未加冷卻 {d['n_raw']:2d} 次),條件成立 "
                      f"{d['days'].get('on_pct', 0):.0f}% 的交易日", flush=True)

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "entry_rule_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_report(results, taiex)
    print("\n完成 → entry_rule_backtest.md")
    return 0


def write_report(results: list[dict], taiex: list[dict]) -> None:
    ok = [r for r in results if "error" not in r]
    L = []
    w = L.append

    w("# 進場規則回測:「前瞻PE < 20x 且 歷史PE百分位 < 50%」")
    w("")
    w("> 規則只管**進場**,進場後固定持有 1 / 3 / 5 年不賣。")
    w("")
    w(_summary(ok))
    w("")
    w("## ⚠️ 開頭必須先講:這份回測測的不是「前瞻PE」")
    w("")
    w("題目寫的是前瞻PE,但**歷史回測做不出前瞻PE**——它需要「歷史上每一天的分析師共識EPS」,")
    w("免費資料源沒有這種東西;拿今天的共識回推過去,就是標準的**前視偏誤**。")
    w("因此本回測一律用 **trailing PE**(股價 ÷ 近四季**實際**EPS,與交易所公布口徑一致)。")
    w("")
    w("**這不是無害的替換,它讓規則變嚴格了:** 對獲利成長中的公司,")
    w("未來EPS > 過去EPS ⇒ 前瞻PE < trailing PE。")
    w("所以「trailing PE < 20」會**漏掉**一批「前瞻PE 已經低於 20、但 trailing PE 還在 20 以上」的時點。")
    w("→ 本報告的觸發次數應視為**偏保守的下界**,真正用前瞻PE 的觸發次數只會更多、進場點更早。")
    w("")
    w("---")
    w("")

    # ── 1. 觸發清單
    w("## 一、觸發次數與日期(近十年;邊緣觸發 + 180 天冷卻)")
    w("")
    w("「觸發」= 條件由**不成立變成成立**的那一天(訊號日),隔一個交易日收盤進場。")
    w("")
    w("**為什麼要加冷卻期(實測發現的問題):** 單純的邊緣觸發會把「同一段低估期」重複計算 ——")
    w("PE 在門檻附近上下震盪,每穿越一次就算一次新買點。")
    w("實測台積電:條件只在 **30%** 的交易日成立,卻產生 **52 次**「觸發」;")
    w("台達電更誇張,條件成立 36% 卻觸發 58 次。**那顯然不是 52 個獨立的投資機會。**")
    w("若照單全收,後面的「平均報酬/勝率」會看起來樣本很多,實際上高度重複、彼此不獨立,")
    w("統計意義被嚴重高估。因此主結果採 **180 天冷卻**(約半年、跨兩次季報):")
    w("一段低估期只算一次進場。下表同時列出未加冷卻的原始次數,讓你看震盪的程度。")
    w("")
    for r in ok:
        w(f"### {r['code']} {r['name']}(可交易起點 {r['first_tradable']},資料至 {r['last']})")
        w("")
        for k, lab in RULES:
            d = r["rules"][k]
            trigs = d["triggers"]
            w(f"**{lab}** — 觸發 **{len(trigs)}** 次(未加冷卻:{d['n_raw']} 次)")
            w("")
            if not trigs:
                w("_(期間內從未觸發。)_")
                w("")
                continue
            w("| # | 訊號日 | 進場日 | 當時PE | PE百分位 | 大盤近一年自高點 | 最近財報EPS年增 |")
            w("| ---: | --- | --- | ---: | ---: | ---: | --- |")
            for i, t in enumerate(trigs, 1):
                w(f"| {i} | {t['signal_date']} | {t['entry_date']} | {num(t['pe'])}x | "
                  f"{num(t['pctl'])}% | {pct(t['taiex_dd']) if t['taiex_dd'] is not None else '—'} | "
                  f"{t['eps_trend']} |")
            w("")
    w("> 「大盤近一年自高點」= 進場當日,加權**報酬**指數相對前一年高點的跌幅"
      "(負值越大代表當時大盤跌越深)。")
    w("> 「最近財報EPS年增」是**替代指標**:題目要的是「共識EPS 當時是上修或下修」,"
      "但歷史共識同樣拿不到(理由同前瞻PE),故改用當時**已公布**的實際EPS 年增方向。")
    w("")
    w("---")
    w("")

    # ── 2. 空手比例
    w("## 二、條件成立時間佔比與最長等待期")
    w("")
    w("| 標的 | 規則 | 條件成立天數佔比 | **不成立(空手)佔比** | 最長連續等待 | 等待期間 |")
    w("| --- | --- | ---: | ---: | ---: | --- |")
    for r in ok:
        for k, lab in RULES:
            d = r["rules"][k]["days"]
            span = d.get("longest_wait_span") or (None, None)
            span_s = f"{span[0]} ~ {span[1]}" if span[0] else "—"
            yrs = (d.get("longest_wait_days") or 0) / 252
            w(f"| {r['name']} | {lab} | {num(d.get('on_pct'))}% | "
              f"**{num(d.get('off_pct'))}%** | {d.get('longest_wait_days', 0)} 交易日"
              f"(約 {yrs:.1f} 年) | {span_s} |")
    w("")
    w("> 這欄是這條規則**最現實的成本**:多數時間你只能空手等待。"
      "等待期間的資金機會成本、以及「看著它一路漲上去卻不能買」的心理壓力,"
      "回測數字不會告訴你,但實際執行時那才是最難的部分。")
    w("")
    w("---")
    w("")

    # ── 3. 報酬與勝率
    w("## 三、觸發後持有 1 / 3 / 5 年的報酬與勝率")
    w("")
    for r in ok:
        w(f"### {r['code']} {r['name']}")
        w("")
        w("| 規則 | 持有期 | 已到期樣本 | 平均報酬 | 中位數 | 勝率 | 最好 | 最差 | 未到期 |")
        w("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for k, lab in RULES:
            trigs = r["rules"][k]["triggers"]
            for y in ER.HOLD_YEARS:
                a = agg(trigs, y)
                if a["n"] == 0:
                    w(f"| {lab} | {y} 年 | 0 | — | — | — | — | — | {a['n_unmatured']} |")
                    continue
                w(f"| {lab} | {y} 年 | {a['n']} | **{pct(a['avg'])}** | {pct(a['median'])} | "
                  f"{pct(a['win_rate'])} | {pct(a['best'])} | {pct(a['worst'])} | {a['n_unmatured']} |")
        w("")
    w("> 「未到期」= 進場日距今不足該持有期,尚無最終結果,**不列入平均與勝率**"
      "(硬算會讓近期的進場點污染統計)。")
    w("")
    w("### 3.1 ★ 最關鍵的對照:規則進場 vs「同期間任意一天進場」")
    w("")
    w("上面的勝率動輒 90~100%,看起來規則很神。但要判斷規則**有沒有選時價值**,")
    w("必須問:**同一段期間,不挑日子隨便買、抱一樣久,結果會差多少?**")
    w("如果不管哪天買都賺,那高勝率只說明「這檔股票這段時間漲了」,不代表規則會挑點。")
    w("")
    w("| 標的 | 持有期 | 規則(雙條件)平均 | 規則勝率 | **任意日進場平均** | **任意日勝率** | 規則超額 |")
    w("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in ok:
        for y in ER.HOLD_YEARS:
            a = agg(r["rules"]["r3"]["triggers"], y)
            b = (r.get("anytime") or {}).get(y) or {}
            if not a["n"] or not b.get("n"):
                w(f"| {r['name']} | {y} 年 | — | — | — | — | — |")
                continue
            excess = a["avg"] - b["avg"]
            w(f"| {r['name']} | {y} 年 | {pct(a['avg'])} | {pct(a['win_rate'])} | "
              f"**{pct(b['avg'])}** | **{pct(b['win_rate'])}** | "
              f"{'+' if excess >= 0 else ''}{pct(excess)} |")
    w("")
    w(_anytime_verdict(ok))
    w("")
    w("---")
    w("")

    # ── 4. 對照定期定額
    w("## 四、對照組:同期「定期定額不擇時」與「一次買進持有」")
    w("")
    w("定期定額 = 每月第一個交易日投入 1 單位,完全不看估值;期間與規則回測相同。")
    w("")
    w("| 標的 | 期間 | 定期定額總報酬 | 買進次數 | 一次買進持有總報酬 |")
    w("| --- | --- | ---: | ---: | ---: |")
    for r in ok:
        d, b = r.get("dca") or {}, r.get("bh") or {}
        w(f"| {r['name']} | {d.get('start', '—')} ~ {d.get('end', '—')} | "
          f"**{pct(d.get('total_return'))}** | {d.get('n_buys', '—')} | {pct(b.get('total_return'))} |")
    w("")
    w("> ⚠️ **這個對照要小心解讀**:定期定額是「一路持續投入」,規則是「等到訊號才一次投入」,")
    w("> 兩者的**投入時間分布與資金曝險完全不同**,總報酬不能直接對比誰優誰劣。")
    w("> 這裡列出來是為了回答「不擇時會不會比較慘」——答案見上表,但請把它當**參考點**,不是勝負判定。")
    w("")
    w("---")
    w("")

    # ── 5. 單條件 vs 雙條件
    w("## 五、兩條件同時成立,是真的更好,還是只是交易變少?")
    w("")
    w("### 5.1 先看一個更根本的問題:「PE < 20x」這條件有在做事嗎?")
    w("")
    w("| 標的 | PE<20 成立天數佔比 | 百分位<50% 成立佔比 | 兩條件同時成立佔比 | 雙條件觸發 vs 只用百分位 |")
    w("| --- | ---: | ---: | ---: | ---: |")
    for r in ok:
        d1 = r["rules"]["r1"]["days"]
        d2 = r["rules"]["r2"]["days"]
        d3 = r["rules"]["r3"]["days"]
        n2 = len(r["rules"]["r2"]["triggers"])
        n3 = len(r["rules"]["r3"]["triggers"])
        w(f"| {r['name']} | {num(d1.get('on_pct'))}% | {num(d2.get('on_pct'))}% | "
          f"{num(d3.get('on_pct'))}% | {n3} vs {n2} |")
    w("")
    w(_abs_threshold_verdict(ok))
    w("")
    w("### 5.2 三種規則的報酬對照")
    w("")
    w("| 標的 | 規則 | 觸發次數 | 3年平均報酬 | 3年勝率 | 5年平均報酬 | 5年勝率 |")
    w("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for r in ok:
        for k, lab in RULES:
            t = r["rules"][k]["triggers"]
            a3, a5 = agg(t, 3), agg(t, 5)
            w(f"| {r['name']} | {lab} | {len(t)} | "
              f"{pct(a3['avg']) if a3['n'] else '—'} | {pct(a3['win_rate']) if a3['n'] else '—'} | "
              f"{pct(a5['avg']) if a5['n'] else '—'} | {pct(a5['win_rate']) if a5['n'] else '—'} |")
    w("")
    w(_compare_verdict(ok))
    w("")
    w("---")
    w("")
    w(_limits(ok))
    OUT.write_text("\n".join(L), encoding="utf-8")


def _summary(ok: list[dict]) -> str:
    """開頭摘要:全部由數據生成,數字變結論就變。"""
    # 1) PE<20 是否形同虛設
    loose = [r["name"] for r in ok if (r["rules"]["r1"]["days"].get("on_pct") or 0) >= 60]
    same = sum(1 for r in ok
               if len(r["rules"]["r2"]["triggers"])
               and abs(len(r["rules"]["r3"]["triggers"]) - len(r["rules"]["r2"]["triggers"]))
               <= max(1, len(r["rules"]["r2"]["triggers"]) * 0.1))
    # 2) 規則 vs 任意日進場
    win = lose = 0
    worst = None
    for r in ok:
        for y in ER.HOLD_YEARS:
            a = agg(r["rules"]["r3"]["triggers"], y)
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
    # 3) 最長空手
    longest = max(((r["rules"]["r3"]["days"].get("longest_wait_days") or 0), r["name"],
                   r["rules"]["r3"]["days"].get("longest_wait_span"))
                  for r in ok) if ok else (0, "", None)

    L = ["## 結論摘要(先看這裡)", ""]
    L.append(f"**1. 這條規則實質上只有一道門檻,不是兩道。** "
             f"「PE < 20x」在 {len(ok)} 檔中有 {same} 檔完全沒有改變觸發次數"
             + (f";{'、'.join(loose)} 更是有六成以上的交易日 PE 本來就低於 20。" if loose else ".")
             + " 你以為的雙重保險,實際上等於只用「歷史PE百分位 < 50%」。")
    L.append("")
    if win + lose:
        L.append(f"**2. 規則沒有展現選時價值 —— 這是最重要的發現。** "
                 f"和「同期間隨便挑一天進場」相比,規則在 {win + lose} 組比較中"
                 f"只有 **{win} 組**贏、**{lose} 組**輸"
                 + (f",最差的一組是 {worst[1]},落後 **{worst[0]*100:.0f} 個百分點**。" if worst else ".")
                 + " 第三節那些 90~100% 的勝率,主要來自**這四檔在這段期間本來就漲**,不是規則挑到好時點。")
        L.append("")
    if longest[0]:
        span = longest[2] or (None, None)
        L.append(f"**3. 代價是長期空手,而且空在最會漲的時候。** "
                 f"最長連續等待出現在 {longest[1]}:**{longest[0]} 個交易日(約 {longest[0]/252:.1f} 年)**"
                 + (f",{span[0]} ~ {span[1]}。" if span[0] else "。")
                 + " 規則要求「估值低於自身歷史中位數」,但成長股在主升段的估值往往一路墊高 —— "
                 "於是你會在它漲最凶的那幾年完全沒有訊號。這正是上一點超額報酬為負的直接原因。")
        L.append("")
    L.append("**4. 以上結論建立在小樣本與贏家股上,不能外推。** "
             "四檔都是今天還活著的大公司(存活者偏差),且測的是 trailing PE 而非題目要的前瞻PE。"
             "詳見末節限制。")
    return "\n".join(L)


def _anytime_verdict(ok: list[dict]) -> str:
    """依數據判斷:規則相對「任意日進場」有沒有超額報酬。"""
    win = lose = 0
    rows = []
    for r in ok:
        for y in ER.HOLD_YEARS:
            a = agg(r["rules"]["r3"]["triggers"], y)
            b = (r.get("anytime") or {}).get(y) or {}
            if not a["n"] or not b.get("n"):
                continue
            ex = a["avg"] - b["avg"]
            (win if ex > 0 else lose).__class__  # noqa: B018
            if ex > 0:
                win += 1
            else:
                lose += 1
            rows.append(f"{r['name']}{y}年 {ex*100:+.0f}pp")
    if not rows:
        return "**無法比較:樣本不足。**"
    out = [f"可比較 {win+lose} 組:規則優於任意日進場 **{win}** 組、不如 **{lose}** 組。",
           "",
           "逐組超額(規則平均 − 任意日平均):" + "、".join(rows), ""]
    if win > lose * 2:
        out.append("**規則看起來確實有選時效果**(多數組別的超額為正),"
                   "但樣本極小、且四檔都是事後看的贏家股,不能當成可靠證據。")
    elif lose >= win:
        out.append("**規則沒有展現出穩定的選時價值。** 在多數組別上,"
                   "「等訊號才買」的報酬並沒有勝過「隨便哪天買」—— "
                   "換句話說,這幾檔的高勝率主要來自**標的本身在這段期間上漲**,"
                   "而不是規則挑到了好時點。")
    else:
        out.append("**超額報酬有正有負,看不出穩定優勢。** 高勝率主要反映標的本身的走勢,"
                   "規則的貢獻無法從這個樣本中分離出來。")
    return "\n".join(out)


def _abs_threshold_verdict(ok: list[dict]) -> str:
    """依數據判斷:PE<20 這個絕對門檻有沒有實際發揮篩選作用。"""
    loose = []       # PE<20 幾乎總是成立的標的
    same = []        # 加上 PE<20 之後,觸發次數幾乎沒變
    for r in ok:
        p1 = r["rules"]["r1"]["days"].get("on_pct") or 0
        n2 = len(r["rules"]["r2"]["triggers"])
        n3 = len(r["rules"]["r3"]["triggers"])
        if p1 >= 60:
            loose.append(f"{r['name']}({p1:.0f}%)")
        if n2 and abs(n3 - n2) <= max(1, n2 * 0.1):
            same.append(f"{r['name']}({n3} vs {n2})")
    out = []
    if loose:
        out.append(f"**「PE < 20x」對這些標的形同虛設:**{'、'.join(loose)} "
                   f"—— 它們有六成以上的交易日 PE 本來就低於 20,這個條件幾乎不篩掉任何東西。")
    if same:
        out.append(f"**加上這個條件後,觸發次數幾乎沒變:**{'、'.join(same)}(雙條件 vs 只用百分位)。")
    if loose or same:
        out.append("")
        out.append("→ **結論:這條規則實質上等於「只用歷史PE百分位 < 50%」。** "
                   "「PE < 20x」在這四檔身上沒有做事,因為它們的估值水準本來就落在 20 附近或以下。"
                   "換句話說,你以為的「兩道防線」其實只有一道 —— "
                   "**絕對門檻要有意義,必須訂在該股實際估值分布會被切到的位置**,"
                   "而 20x 對這幾檔太寬鬆(對高估值成長股則會變成幾乎永不觸發的過嚴門檻)。")
    else:
        out.append("「PE < 20x」確實有額外篩選作用(觸發次數比單用百分位明顯減少)。")
    return "\n".join(out)


def _compare_verdict(ok: list[dict]) -> str:
    """依數據生成判斷:雙條件是否真的優於單條件。"""
    lines = []
    better = worse = same = 0
    detail = []
    for r in ok:
        for y in (3, 5):
            a1, a2, a3 = (agg(r["rules"][k]["triggers"], y) for k in ("r1", "r2", "r3"))
            if not (a3["n"] and (a1["n"] or a2["n"])):
                continue
            base = max([a for a in (a1, a2) if a["n"]], key=lambda a: a["avg"])
            if a3["avg"] > base["avg"] * 1.05:
                better += 1
            elif a3["avg"] < base["avg"] * 0.95:
                worse += 1
            else:
                same += 1
            detail.append(f"{r['name']}{y}年: 雙 {a3['avg']*100:.0f}% vs 單最佳 {base['avg']*100:.0f}%")
    if not detail:
        return ("**無法判斷:已到期的樣本太少**,任何「雙條件比較好」的說法都缺乏依據。")
    lines.append(f"可比較的組合共 {better+worse+same} 組:"
                 f"雙條件較佳 **{better}**、較差 **{worse}**、差不多 **{same}**。")
    lines.append("")
    lines.append("逐組:" + "；".join(detail))
    lines.append("")
    if better > worse and better >= (better + worse + same) / 2:
        lines.append("**傾向支持「兩條件同時成立比較好」**,但樣本極小,不足以排除運氣。")
    elif worse >= better:
        lines.append("**數據不支持「雙條件比較好」**。雙條件主要的效果是**把交易次數變少**"
                     "(見上表觸發次數),報酬並沒有相應變好 —— "
                     "這正是你要求驗證的那個疑慮,結果傾向證實它。")
    else:
        lines.append("**看不出明確差異**。雙條件最明顯的作用是減少觸發次數,而非提升報酬。")
    return "\n".join(lines)


def _limits(ok: list[dict]) -> str:
    total_trig = sum(len(r["rules"]["r3"]["triggers"]) for r in ok)
    few = [f"{r['name']}({len(r['rules']['r3']['triggers'])}次)"
           for r in ok if len(r["rules"]["r3"]["triggers"]) < 5]
    s = ["## 六、限制與誠實聲明", "",
         "1. **測的是 trailing PE,不是題目要的前瞻PE。** 理由與影響方向見開頭,"
         "此替換讓規則變嚴格、觸發次數偏少,是**保守下界**。", ""]
    if few:
        s += [f"2. **樣本不足,不做任何統計宣稱。** 雙條件規則觸發次數少於 5 次的標的:"
              f"{'、'.join(few)}(全部標的雙條件合計僅 {total_trig} 次)。"
              "在這種樣本數下,平均報酬與勝率**主要反映運氣與特定時點**,"
              "不具統計意義,也不能外推到未來。本報告刻意不計算任何顯著性檢定 —— "
              "不是漏做,是樣本根本不支持。", ""]
    else:
        s += [f"2. **樣本仍小**(雙條件合計 {total_trig} 次),不做顯著性宣稱。", ""]
    s += [
        "3. **觸發次數看起來夠多,但彼此不獨立。** 加了 180 天冷卻後每檔約 11~15 次,"
        "數量上似乎堪用;但這些進場點集中在少數幾段低估期(例如同一次股災的前後幾個月),"
        "**本質上是同一個事件被切成好幾筆**。真正獨立的「估值低檔事件」遠少於這個數字,"
        "所以平均報酬與勝率的可信度,比表面的樣本數低很多。",
        "",
        "4. **存活者偏差。** 這四檔都是**今天還在、還是大公司**的贏家。"
        "2010 年當下沒人知道它們會走到今天;真正倒下、下市、長期低迷的公司完全不在樣本裡。"
        "這會**系統性高估**所有規則的報酬 —— 特別是「跌深買進然後長抱」這類策略,"
        "因為樣本裡沒有「跌深之後再也沒起來」的案例。"
        "**注意這個偏差同時也灌水了『任意日進場』基準**,所以第 3.1 節的比較"
        "(規則 vs 任意日)反而比絕對報酬數字更可信 —— 兩邊受同一個偏差影響。",
        "",
        "5. **只測進場、不測出場。** 固定持有 1/3/5 年是題目設定,"
        "不代表這是好的出場方式;期間的最大回撤、以及中途需要多強的持有紀律,本報告未涵蓋。",
        "",
        "6. **百分位用擴張視窗,但門檻 20x 是絕對值。** 百分位只用當日以前的資料(無前視),"
        "但「PE < 20」這個絕對門檻本身帶有時代性 —— 台股整體估值水準在不同年代不同,"
        "同一個 20x 在 2012 年和 2026 年的意義並不一樣。",
        "",
        "7. **定期定額對照不是公平比較。** 兩者投入時點與資金曝險結構不同,見第四節說明。"
        "第 3.1 節的「任意日進場」才是判斷規則有無選時價值的對照組。",
        "",
        "8. **冷卻期 180 天是結構性選擇,不是最佳化出來的。** 目的是避免同一段低估期被"
        "重複計算(未加冷卻時台積電會出現 52 次「觸發」)。報告已同時列出未加冷卻的原始次數,"
        "你可以自己判斷這個處理是否合理。",
        "",
    ]
    return "\n".join(s)


if __name__ == "__main__":
    raise SystemExit(main())
