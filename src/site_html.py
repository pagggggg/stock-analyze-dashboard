"""
網站產生器 (site_html.py)
=========================
把整份觀察清單組成一個「可遠端存取的靜態網站」(多頁,離線也能開):

  index.html          三層儀表板首頁
    第一層:頂端狀態燈(綠/黃/紅)
    第二層:訊號流水(共識上下修 / FCF 燈變色 / 估值門檻跨越;不放股價雜訊)
    掃描總表:所有股票四指標一覽,可點欄位排序,標便宜/合理/貴 + 盈餘修正動能欄
  stock_<id>.html     第三層:個股詳情(四指標卡 + 河流圖 + FCF三燈 + FCF雙線 + EPS走勢 + 共識折線)
  plotly.min.js       圖表函式庫(本地一份,所有頁共用 → 離線可開、不重複下載)
  style.css           共用樣式

★ 全站僅用公開市場數據做估值研究,無任何持倉 / 交易紀錄;掃描總表非買進清單。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

# 沿用單股儀表板的圖表/卡片/燈號/配色,避免重工
from .dashboard_html import (
    C_BLUE,
    C_CHEAP,
    C_EXP,
    C_FAIR,
    C_NA,
    C_PRICEY,
    _CSS as BASE_CSS,
    _cards_html,
    _esc,
    _fig_consensus,
    _fig_div,
    _fig_fcf_dual,
    _fig_river,
    _layout,
    _lights_html,
    _n,
    _note,
    _placeholder,
    _VERDICT_COLOR,
)

_MOM = {
    "up": (C_CHEAP, "↑ 上修"),
    "down": (C_EXP, "↓ 下修"),
    "flat": (C_FAIR, "— 持平"),
    "na": (C_NA, "—"),
}
_STATUS = {
    "green": ("#16a34a", "🟢 無訊號級變化", "觀察清單目前沒有需要注意的基本面訊號。"),
    "yellow": ("#eab308", "🟡 有共識異動 / FCF 燈變色", "有『訊號級』變化,詳見下方訊號流水。"),
    "red": ("#dc2626", "🔴 有股票跨越估值門檻", "有股票前瞻PE 判讀等級改變,詳見下方訊號流水。"),
}


# ======================================================================
# 個股詳情頁的 EPS 走勢圖(相容「有/無」法說三情境)
# ======================================================================
def _fig_eps_site(quarters, scenarios, quarter_label: str) -> str:
    actual = quarters[-8:] if len(quarters) > 8 else quarters
    ax = [q.quarter for q in actual]
    ay = [round(q.reported_eps, 2) for q in actual]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=ax, y=ay, name="實際 EPS", marker_color=C_BLUE,
        text=[f"{v:.1f}" for v in ay], textposition="outside",
        hovertemplate="%{x}<br>實際 EPS %{y:.2f}<extra></extra>",
    ))
    if scenarios:
        pes = scenarios["悲觀"].eps_quarter
        neu = scenarios["中性"].eps_quarter
        opt = scenarios["樂觀"].eps_quarter
        ex = [f"{quarter_label}悲觀E", f"{quarter_label}中性E", f"{quarter_label}樂觀E"]
        ey = [round(pes, 2), round(neu, 2), round(opt, 2)]
        fig.add_trace(go.Bar(
            x=ex, y=ey, name=f"{quarter_label} 試算",
            marker_color=[C_EXP, C_BLUE, C_CHEAP], marker_pattern_shape="/",
            text=[f"{v:.1f}" for v in ey], textposition="outside",
            hovertemplate="%{x}<br>試算 EPS %{y:.2f}<extra></extra>",
        ))
    fig.update_layout(barmode="group", bargap=0.25)
    fig.update_yaxes(title_text="單季 EPS (NT$)")
    fig.update_xaxes(type="category")
    return _fig_div(_layout(fig, height=360))


# ======================================================================
# 掃描總表
# ======================================================================
def _fmt(v, unit="", dp=1):
    if v is None:
        return "N/A", "nan"
    return (f"{v:,.{dp}f}{unit}", f"{v}")


def _metric_cell(a, key, unit, dp=1):
    """回傳 (顯示HTML, data-sort 值),值依判讀著色。"""
    m = a.metric(key)
    if not m or m.value is None:
        return '<td class="num" data-sort="nan">N/A</td>'
    color = _VERDICT_COLOR.get(m.verdict, C_NA)
    txt = m.display
    return (f'<td class="num" data-sort="{m.value}">'
            f'<span style="color:{color};font-weight:700">{_esc(txt)}</span>'
            f'<span class="verdict">{_esc(m.verdict)}</span></td>')


def _scan_table(rows: list[tuple]) -> str:
    """rows: list of (analysis, momentum_dir, momentum_pct)。"""
    body = []
    for a, mdir, mpct in rows:
        price_txt = _n(a.price, 0) if a.price else "N/A"
        price_sort = a.price if a.price else "nan"
        mcolor, mlabel = _MOM.get(mdir, _MOM["na"])
        mtxt = mlabel + (f" {mpct:+.1f}%" if mpct not in (None, 0.0) and mdir in ("up", "down") else "")
        msort = mpct if (mpct is not None and mdir in ("up", "down")) else "nan"
        detail = (f'<a href="stock_{a.stock_id}.html">{_esc(a.name)}</a>'
                  if a.ok else _esc(a.name))
        body.append(
            "<tr>"
            f'<td data-sort="{a.stock_id}">{_esc(a.stock_id)}</td>'
            f'<td class="name">{detail}</td>'
            f'<td class="num" data-sort="{price_sort}">{price_txt}</td>'
            f'{_metric_cell(a, "forward_pe", "x")}'
            f'{_metric_cell(a, "peg", "", 2)}'
            f'{_metric_cell(a, "fcf_yield", "%")}'
            f'{_metric_cell(a, "ev_ebitda", "x")}'
            f'<td class="num" data-sort="{msort}"><span style="color:{mcolor};font-weight:700">{_esc(mtxt)}</span></td>'
            "</tr>"
        )
    heads = [
        ("代號", 0), ("名稱", 1), ("現價", 2), ("前瞻PE", 3), ("PEG", 4),
        ("FCF Yield", 5), ("EV/EBITDA", 6), ("盈餘修正動能", 7),
    ]
    th = "".join(f'<th onclick="sortTable({i})">{_esc(h)} ⇅</th>' for h, i in heads)
    return (
        '<table id="scan" data-dir="asc"><thead><tr>'
        f"{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


# ======================================================================
# 第二層:訊號流水
# ======================================================================
def _signal_stream(log_rows: list[dict], first_run: bool) -> str:
    if first_run:
        return ('<div class="stream-empty">首次建立基準快照。'
                '從<b>下一次每日重跑</b>起,共識上下修 / FCF 燈變色 / 估值門檻跨越會出現在這裡。</div>')
    if not log_rows:
        return ('<div class="stream-empty">目前沒有訊號事件。'
                '每日重跑後,只要有共識上下修、FCF 燈變色或估值門檻跨越,就會即時列在這裡'
                '(<b>股價漲跌不算訊號,不會出現</b>)。</div>')
    items = []
    lv_color = {"red": C_EXP, "yellow": "#eab308"}
    for r in log_rows:
        c = lv_color.get(r.get("level", ""), C_FAIR)
        items.append(
            '<div class="stream-item">'
            f'<span class="dot" style="background:{c}"></span>'
            f'<span class="stream-date">{_esc(r.get("date", ""))}</span>'
            f'<span class="stream-stock">{_esc(r.get("stock_id", ""))} {_esc(r.get("name", ""))}</span>'
            f'<span class="stream-msg">{_esc(r.get("message", ""))}</span>'
            "</div>"
        )
    return f'<div class="stream">{"".join(items)}</div>'


# ======================================================================
# 首頁 index.html
# ======================================================================
def build_index_html(
    rows: list[tuple],
    status: str,
    events: list,
    first_run: bool,
    log_rows: list[dict],
    generated: str,
) -> str:
    scolor, stitle, sdesc = _STATUS.get(status, _STATUS["green"])
    n_red = sum(1 for e in events if e.level == "red")
    n_yellow = sum(1 for e in events if e.level == "yellow")
    count_txt = ""
    if not first_run:
        count_txt = f'　本次:<b style="color:{C_EXP}">紅 {n_red}</b>・<b style="color:#b59000">黃 {n_yellow}</b>'

    banner = (
        f'<div class="status" style="background:{scolor}">'
        f'<div class="status-title">{_esc(stitle)}</div>'
        f'<div class="status-desc">{_esc(sdesc)}{count_txt}</div>'
        "</div>"
    )

    table = _scan_table(rows)
    stream = _signal_stream(log_rows, first_run)

    body = f"""
<div class="wrap">
  <header>
    <h1>個人選股分析儀表板</h1>
    <div class="meta">更新時間 {generated}　|　觀察清單 {len(rows)} 檔　|　資料:FinMind + yfinance(公開市場數據)</div>
    <div class="warn">⚠️ 全站僅為<b>公開數據估值研究</b>,無任何持倉或交易紀錄;所有數字請回原始來源核實,<b>不構成投資建議</b>。</div>
  </header>

  <div class="layer-tag">第一層 · 狀態燈</div>
  {banner}

  <div class="layer-tag">第二層 · 訊號流水(只看訊號,不看股價雜訊)</div>
  <section>
    {stream}
    {_note('這裡只收<b>基本面訊號</b>:共識EPS 上/下修、FCF 品質燈變色、前瞻PE 跨越估值門檻。'
           '<b>股價每日漲跌屬雜訊,刻意不列</b>——真正該花時間研究的是這些訊號背後的原因。')}
  </section>

  <div class="layer-tag">掃描總表 · 縮小研究範圍用</div>
  <section>
    <div class="table-warn">📌 本表僅供<b>縮小研究範圍</b>,<b>非買進清單</b>。點欄位標題可排序;顏色為估值判讀(綠便宜/灰合理/橘偏貴/紅貴),僅為經驗法則。</div>
    <div class="table-scroll">{table}</div>
    {_note('<b>前瞻PE</b>=現價÷今年共識EPS;<b>PEG</b>=前瞻PE÷盈餘成長率;'
           '<b>FCF Yield</b>=近4季自由現金流÷市值;<b>EV/EBITDA</b>=(市值+負債−現金)÷近4季EBITDA。'
           '<b>盈餘修正動能</b>僅<b>標記</b>近期共識被上/下修的方向,<b>目前不納入評分</b>'
           '(依原則,等回測驗證後才考慮加權重)。點名稱進個股詳情看河流圖與 FCF 品質。')}
  </section>

  <footer>
    <div>資料來源:財報/資產負債/現金流/日股價 FinMind、分析師共識EPS/FCF/EV 元件 yfinance。</div>
    <div>本工具僅為個人估值研究,數字可能過時或有誤,請務必回原始出處核對,不構成投資建議。</div>
  </footer>
</div>
{_SORT_JS}
"""
    return _page(f"個人選股分析儀表板", body, plotly=False)


# ======================================================================
# 個股詳情 stock_<id>.html
# ======================================================================
def build_detail_html(a, generated: str) -> str:
    # 頂部小摘要
    parts = []
    if a.price:
        parts.append(f"現價 <b>NT$ {_n(a.price, 0)}</b>（{_esc(a.price_date)}）")
    if a.eps_y0:
        g = f"，成長 {a.growth_pct:+.1f}%" if a.growth_pct is not None else ""
        parts.append(f"今年共識EPS <b>{_n(a.eps_y0, 2)}</b>{g}")
    if a.pe_band:
        parts.append(f"本益比河道 {a.pe_band.pe_low:g}/{a.pe_band.pe_mid:g}/{a.pe_band.pe_high:g}x")
    summary = "　|　".join(parts) if parts else "資料整理中"

    river_div = _fig_river(a.river) if a.river else _placeholder("河流圖資料不足。")
    fcf_dual = _fig_fcf_dual(a.fcf) if a.fcf else _placeholder("FCF 雙線資料不足。")
    eps_div = _fig_eps_site(a.quarters, a.scenarios, a.quarter_label) if a.quarters else _placeholder("EPS 資料不足。")
    cons_div = _fig_consensus(a.consensus_history or [])

    river_zone = ""
    if a.river and a.river.current_pe is not None:
        r = a.river
        if r.current_pe <= (r.pe_low + r.pe_mid) / 2:
            z = f'偏<span style="color:{C_CHEAP}">低估(貼近低本益比河道)</span>'
        elif r.current_pe >= (r.pe_mid + r.pe_high) / 2:
            z = f'偏<span style="color:{C_EXP}">高估(貼近高本益比河道)</span>'
        else:
            z = f'約在<span style="color:{C_FAIR}">中樞附近</span>'
        river_zone = f' 目前 trailing PE ≈ <b>{r.current_pe:g}x</b>,位階{z}。'

    err = ""
    if a.errors:
        err = ('<div class="warn">部分資料抓取失敗(該區塊以「資料不足」顯示):'
               + _esc("；".join(a.errors)) + "</div>")

    body = f"""
<div class="wrap">
  <header>
    <div><a class="back" href="index.html">← 回總表</a></div>
    <h1>{_esc(a.name)}（{_esc(a.stock_id)}）個股詳情</h1>
    <div class="meta">更新時間 {generated}　|　{summary}</div>
    <div class="warn">⚠️ 公開數據估值研究,無持倉/交易紀錄,不構成投資建議。</div>
    {err}
  </header>

  <section>
    <h2>四指標(即時連動現價)</h2>
    {_cards_html(a.dashboard)}
    {_note('綠便宜/灰合理/橘偏貴/紅貴,單一指標不下結論,務必交叉看。')}
  </section>

  <section>
    <h2>本益比河流圖</h2>
    {river_div}
    {_note('河道 =「當時近四季實際EPS」×(近10年低/中/高本益比,必要時擴張以含括現價)。'
           '股價貼近<b style="color:'+C_CHEAP+'">綠</b>相對便宜、貼近<b style="color:'+C_EXP+'">紅</b>相對貴。' + river_zone)}
  </section>

  <section>
    <h2>FCF 品質檢查</h2>
    {fcf_dual}
    {_note('資本支出年增率(領先2年)vs 營收年增率:看前兩年擴產有沒有兌現成營收。')}
    {_lights_html(a.fcf)}
  </section>

  <section>
    <h2>EPS 走勢</h2>
    {eps_div}
    {_note('藍柱=財報實際;斜線紋柱=法說三情境試算(僅有指引檔的股票才有)。')}
  </section>

  <section>
    <h2>共識EPS 監控</h2>
    {cons_div}
    {_note('<span style="color:'+C_CHEAP+'">▲上修</span>/<span style="color:'+C_EXP+'">▼下修</span>;每日重跑會累積更長折線。')}
  </section>

  <footer><div><a class="back" href="index.html">← 回總表</a>　|　資料:FinMind + yfinance,不構成投資建議。</div></footer>
</div>
"""
    return _page(f"{a.name} {a.stock_id} 詳情", body, plotly=True)


# ======================================================================
# 寫出整個網站
# ======================================================================
def write_site(analyses: list, status: str, events: list, first_run: bool,
               log_rows: list[dict], out_dir: str | Path) -> dict:
    """把 index / 各詳情頁 / plotly.min.js / style.css 全部寫到 out_dir。回傳統計。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 排序:先成功、四指標齊全的在前(方便看);盈餘修正動能一併算好
    from .scan_state import revision_momentum
    rows = []
    for a in analyses:
        mdir, mpct = revision_momentum(a.consensus_history)
        rows.append((a, mdir, mpct))

    # 共用資源:plotly.min.js(本地一份)、style.css
    (out / "plotly.min.js").write_text(get_plotlyjs(), encoding="utf-8")
    (out / "style.css").write_text(_SITE_CSS, encoding="utf-8")

    # 首頁
    (out / "index.html").write_text(
        build_index_html(rows, status, events, first_run, log_rows, generated),
        encoding="utf-8",
    )
    # 各詳情頁(只為成功的股票產生)
    n_detail = 0
    for a in analyses:
        if a.ok:
            (out / f"stock_{a.stock_id}.html").write_text(
                build_detail_html(a, generated), encoding="utf-8")
            n_detail += 1

    return {"stocks": len(analyses), "details": n_detail, "out": str(out)}


# ======================================================================
# 頁面骨架 + CSS + 排序 JS
# ======================================================================
def _page(title: str, body: str, plotly: bool) -> str:
    js = '<script src="plotly.min.js"></script>' if plotly else ""
    return (
        '<!DOCTYPE html><html lang="zh-Hant"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        '<link rel="stylesheet" href="style.css">'
        f"{js}</head><body>{body}</body></html>"
    )


_SORT_JS = """
<script>
function sortTable(n){
  var t=document.getElementById('scan');
  var rows=Array.prototype.slice.call(t.tBodies[0].rows);
  var dir=t.getAttribute('data-col')==String(n)&&t.getAttribute('data-dir')=='asc'?'desc':'asc';
  t.setAttribute('data-col',n); t.setAttribute('data-dir',dir);
  rows.sort(function(a,b){
    var xs=a.cells[n].getAttribute('data-sort'), ys=b.cells[n].getAttribute('data-sort');
    var x=parseFloat(xs), y=parseFloat(ys);
    var xn=isNaN(x), yn=isNaN(y);
    if(xn&&yn){ return xs<ys?-1:xs>ys?1:0; }
    if(xn) return 1; if(yn) return -1;            /* N/A 永遠沉底 */
    return dir=='asc'? x-y : y-x;
  });
  rows.forEach(function(r){ t.tBodies[0].appendChild(r); });
}
</script>
"""

_SITE_CSS = BASE_CSS + """
.layer-tag { font-size: .8rem; font-weight: 700; color: #64748b; letter-spacing: .05em;
  margin: 18px 4px 6px; text-transform: none; }
.status { color: #fff; border-radius: 14px; padding: 18px 20px; margin: 4px 0 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,.14); }
.status-title { font-size: 1.5rem; font-weight: 800; }
.status-desc { font-size: .95rem; opacity: .95; margin-top: 4px; }
.stream { display: flex; flex-direction: column; gap: 2px; }
.stream-item { display: flex; align-items: center; gap: 10px; padding: 8px 6px;
  border-bottom: 1px solid #f1f5f9; font-size: .92rem; flex-wrap: wrap; }
.stream-date { color: #94a3b8; font-variant-numeric: tabular-nums; }
.stream-stock { font-weight: 700; color: #334155; }
.stream-msg { color: #475569; }
.stream-empty { color: #64748b; background: #f8fafc; border: 1px dashed #cbd5e1;
  border-radius: 8px; padding: 20px; text-align: center; }
.table-warn { background: #fffbeb; border: 1px solid #fde68a; color: #92400e;
  padding: 10px 12px; border-radius: 8px; font-size: .9rem; margin-bottom: 10px; }
.table-scroll { overflow-x: auto; }
table#scan { border-collapse: collapse; width: 100%; font-size: .92rem; min-width: 640px; }
table#scan th { background: #f1f5f9; padding: 10px 8px; text-align: right; cursor: pointer;
  white-space: nowrap; position: sticky; top: 0; user-select: none; }
table#scan th:nth-child(1), table#scan th:nth-child(2){ text-align: left; }
table#scan td { padding: 9px 8px; border-bottom: 1px solid #eef2f7; }
table#scan td.num { text-align: right; font-variant-numeric: tabular-nums; }
table#scan td.name a { color: #2563eb; text-decoration: none; font-weight: 600; }
table#scan tbody tr:hover { background: #f8fafc; }
.verdict { font-size: .72rem; color: #64748b; margin-left: 6px; }
a.back { color: #2563eb; text-decoration: none; font-size: .9rem; }
"""
