"""
選股篩選網頁 (screener_html.py)
===============================
把 screener 結果做成一頁 screener.html(和主網站同風格,離線可開),接到儀表板首頁。
內容:門檻摘要、第一層漏斗、負債比新舊口徑對照、兩層全過精華、第一層通過清單、誠實說明。
"""

from __future__ import annotations

from .dashboard_html import C_CHEAP, C_EXP, C_FAIR, _esc, _note
from .screener import L1_LABELS, L2_LABELS, ScreenResult
from .site_html import _page

_SYM = {"pass": "✅", "fail": "❌", "na": "⚠️"}


def _fv(v, unit: str = "", dp: int = 1) -> str:
    return "—" if v is None else f"{v:,.{dp}f}{unit}"


def _q(cond) -> str:
    if cond.status == "na":
        return "⚠️資料不足"
    return f"{_esc(cond.detail)} {_SYM[cond.status]}"


def _mom(cond) -> str:
    return "⚠️資料不足" if cond.status == "na" else _esc(cond.detail)


def _l2_table(rows: list[ScreenResult]) -> str:
    body = []
    for r in rows:
        body.append(
            "<tr>"
            f"<td>{_esc(r.stock_id)}</td><td>{_esc(r.name)}</td><td>{_esc(r.industry)}</td>"
            f"<td>{_q(r.layer2['q7'])}</td><td>{_q(r.layer2['q8'])}</td>"
            f"<td>{_q(r.layer2['q9'])}</td><td>{_mom(r.layer2['q10'])}</td></tr>"
        )
    return (
        '<div class="table-scroll"><table class="tbl"><thead><tr>'
        "<th>代號</th><th>名稱</th><th>產業</th><th>⑦營收CAGR</th><th>⑧毛利率趨勢</th>"
        "<th>⑨ROE</th><th>⑩修正動能</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table></div>"
    )


def build_screener_page(results, funnel, cfg, generated: str) -> str:
    L1 = cfg["layer1"]
    A = []
    w = A.append

    w('<div class="wrap">')
    w('<header>')
    w('<div><a class="back" href="index.html">← 回總表</a></div>')
    w("<h1>兩層選股篩選器(台股全市場)</h1>")
    deep = sum(1 for r in results)
    w(f'<div class="meta">更新時間 {generated}　|　評估 {deep} 檔　|　'
      f'通過第一層 <b>{funnel["layer1_pass"]}</b>　兩層全過 <b>{funnel["both_pass"]}</b></div>')
    w('<div class="table-warn">📌 只用公開數據做資格/品質研究,<b>無持倉/交易紀錄</b>;'
      '本表僅供<b>縮小研究範圍,非買進清單</b>。門檻全在 <code>config/screener.yaml</code>。</div>')
    w("</header>")

    # 精華清單(置頂 highlight)
    essence = sorted([r for r in results if r.both_pass], key=lambda r: r.stock_id)
    w("<section>")
    w(f"<h2>★ 精華清單:兩層全過（{len(essence)} 檔）</h2>")
    if essence:
        w(_l2_table(essence))
        w(_note("「兩層全過」= 第一層 6 條 ＋ 第二層 ⑦⑧⑨ 三項品質門檻全達標;"
                "⑩修正動能僅標記、不列入判定。<b>兩層全過 ≠ 買進訊號</b>,仍須看估值與最新財報。"))
    else:
        w('<div class="stream-empty">目前沒有股票兩層全過(⑦⑧⑨ 任一未達標或資料不足都不算)。</div>')
    w("</section>")

    # 漏斗
    w("<section>")
    w("<h2>第一層漏斗統計(每條各刷掉多少)</h2>")
    w('<div class="table-scroll"><table class="tbl"><thead><tr>'
      "<th>第一層條件</th><th>通過</th><th>未通過</th><th>資料不足</th></tr></thead><tbody>")
    for k, label in L1_LABELS.items():
        c = funnel[k]
        w(f"<tr><td>{_esc(label)}</td><td class='num'>{c['pass']}</td>"
          f"<td class='num'>{c['fail']}</td><td class='num'>{c['na']}</td></tr>")
    w("</tbody></table></div>")
    w(_note("各條為<b>獨立評估</b>(一檔可能同時卡多條);「通過第一層」才是 6 條同時成立。"
            "「資料不足」一律<b>不當通過</b>。"))
    w("</section>")

    # 負債比對照
    dr = sorted([r for r in results if r.metrics.get("ib_ratio") is not None],
                key=lambda r: r.stock_id)
    w("<section>")
    w("<h2>負債比口徑對照(新:有息負債比 vs 舊:總負債比)</h2>")
    if dr:
        w('<div class="table-scroll"><table class="tbl"><thead><tr>'
          "<th>代號</th><th>名稱</th><th>產業</th><th>有息負債比(新)</th><th>門檻</th>"
          "<th>④</th><th>原總負債比(舊)</th><th>差</th></tr></thead><tbody>")
        for r in dr:
            ib = r.metrics["ib_ratio"]
            tot = r.metrics.get("total_ratio")
            thr = r.metrics.get("debt_thr")
            mk = _SYM[r.layer1["c4"].status]
            star = "" if r.metrics.get("has_ib_items", True) else "＊"
            tot_s = f"{tot:.1f}%" if tot is not None else "—"
            diff = f"−{tot - ib:.1f}pp" if tot is not None else "—"
            w(f"<tr><td>{_esc(r.stock_id)}</td><td>{_esc(r.name)}</td><td>{_esc(r.industry)}</td>"
              f"<td class='num'>{ib:.1f}%{star}</td><td class='num'>&lt;{thr:.0f}%</td>"
              f"<td>{mk}</td><td class='num'>{tot_s}</td><td class='num'>{diff}</td></tr>")
        w("</tbody></table></div>")
        w(_note("有息負債比 =(短期借款+長期借款+應付公司債)÷ 總資產。"
                "<b>代工/fabless 因『應付帳款』被舊口徑灌水</b>,新口徑才反映真實財務槓桿。"))
    w("</section>")

    # 第一層通過清單
    passers = [r for r in results if r.layer1_pass]
    passers.sort(key=lambda r: (not r.both_pass, r.stock_id))
    w("<section>")
    w(f"<h2>通過第一層清單({len(passers)} 檔,附第二層四項)</h2>")
    if passers:
        w(_l2_table(passers))
        w(_note("⑦⑧⑨:✅達標／❌未達標／⚠️資料不足;⑩僅標記方向。第二層<b>只標記不淘汰</b>。"))
    else:
        w('<div class="stream-empty">目前沒有股票通過第一層。</div>')
    w("</section>")

    # 估值檢查(僅供參考)
    val = [r for r in results
           if any(r.metrics.get(k) is not None for k in ("forward_pe", "peg", "fcf_yield"))]
    val.sort(key=lambda r: (r.market != "us", r.stock_id))
    w("<section>")
    w("<h2>估值檢查(僅供參考,不以估值淘汰)</h2>")
    if val:
        w('<div class="table-scroll"><table class="tbl"><thead><tr>'
          "<th>代號</th><th>名稱</th><th>市場</th><th>前瞻PE</th><th>PEG</th>"
          "<th>FCF Yield</th></tr></thead><tbody>")
        for r in val:
            m = "美股" if r.market == "us" else "台股"
            w(f"<tr><td>{_esc(r.stock_id)}</td><td>{_esc(r.name)}</td><td>{m}</td>"
              f"<td class='num'>{_fv(r.metrics.get('forward_pe'), 'x')}</td>"
              f"<td class='num'>{_fv(r.metrics.get('peg'), '', 2)}</td>"
              f"<td class='num'>{_fv(r.metrics.get('fcf_yield'), '%')}</td></tr>")
        w("</tbody></table></div>")
    w(_note("兩層篩選<b>不以估值淘汰</b>;此欄僅供『買點』參考。"
            "前瞻PE=現價÷今年共識EPS、PEG=前瞻PE÷成長率、FCF Yield=近4季自由現金流÷市值(yfinance)。"))
    w("</section>")

    # 美股測試標的:逐條 + 估值評語
    us_res = [r for r in results if r.market == "us"]
    if us_res:
        w("<section>")
        w("<h2>美股測試標的:逐條檢視 + 估值評語</h2>")
        for r in us_res:
            l1pass = sum(1 for c in r.layer1.values() if c.status == "pass")
            w(f"<h3 style='margin:8px 0 4px'>{_esc(r.stock_id)}（{_esc(r.industry)}）</h3>")
            w(f"<p><b>第一層 6 條:通過 {l1pass}/6</b>"
              f"{'　✅ 全數通過' if r.layer1_pass else ''}</p><ul>")
            for k, label in L1_LABELS.items():
                c = r.layer1[k]
                w(f"<li>{_esc(label)}:{_SYM[c.status]}　{_esc(c.detail)}</li>")
            w("</ul>")
            w(f"<p><b>第二層品質(⑦⑧⑨):</b>{'✅ 全達標' if r.layer2_pass else '未全達標'}</p><ul>")
            for k in ("q7", "q8", "q9"):
                c = r.layer2[k]
                w(f"<li>{_esc(L2_LABELS[k])}:{_SYM[c.status]}　{_esc(c.detail)}</li>")
            w(f"<li>{_esc(L2_LABELS['q10'])}(僅標記):{_esc(r.layer2['q10'].detail)}</li></ul>")
            fpe = r.metrics.get("forward_pe")
            peg = r.metrics.get("peg")
            fy = r.metrics.get("fcf_yield")
            w(f"<p><b>估值檢查(僅參考):</b> 前瞻PE {_fv(fpe, 'x')}、PEG {_fv(peg, '', 2)}、"
              f"FCF Yield {_fv(fy, '%')}</p>")
            verd = []
            if fpe is not None:
                verd.append("前瞻PE 極高" if fpe > 40 else "前瞻PE 偏高" if fpe > 25 else "前瞻PE 尚屬合理")
            if fy is not None:
                verd.append("FCF殖利率偏低" if fy < 2 else "FCF殖利率尚可")
            if peg is not None:
                verd.append(f"PEG {peg:.2f}(>2 偏貴)" if peg > 2 else f"PEG {peg:.2f}")
            vtxt = "、".join(verd) if verd else "估值資料不足"
            block = [L1_LABELS[k].split(" ")[0] for k, c in r.layer1.items() if c.status != "pass"]
            btxt = f"(卡 {'、'.join(block)})" if block else ""
            passtxt = "通過第一層資格" if r.layer1_pass else f"未通過第一層{btxt}"
            qual = "、品質 ⑦⑧⑨ 全達標" if r.layer2_pass else "、品質 ⑦⑧⑨ 未全達標"
            w(_note(f"<b>結論</b>:{_esc(r.name)} {passtxt}{qual}。<b>若加入估值判斷</b>:{vtxt}"
                    "——成長預期多已反映在股價。篩選器<b>刻意不以估值淘汰</b>,"
                    "是否買進需自行結合估值與成長延續性。"))
        w("</section>")

    w('<footer><div><a class="back" href="index.html">← 回總表</a>　|　'
      "資料:FinMind(台股)+ yfinance(美股/估值);門檻見 config/screener.yaml,不構成投資建議。</div></footer>")
    w("</div>")
    return _page("兩層選股篩選器", "\n".join(A), plotly=False)
