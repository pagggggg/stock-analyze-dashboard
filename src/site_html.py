"""
網站產生器 (site_html.py)
=========================
把整份觀察清單組成一個「可遠端存取的靜態網站」(多頁,離線也能開):

  index.html          三層儀表板首頁
    第一層:頂端狀態燈(綠/黃/紅)
    第二層:訊號流水(共識上下修 / FCF 燈變色;不放股價雜訊)
    掃描總表:所有股票四指標一覽,可點欄位排序;forward PE 僅顯示參考值
  stock_<id>.html     第三層:個股詳情(四指標卡 + 河流圖 + FCF三燈 + FCF雙線 + EPS走勢 + 共識折線)
  plotly.min.js       圖表函式庫(本地一份,所有頁共用 → 離線可開、不重複下載)
  style.css           共用樣式

★ 全站僅用公開市場數據做估值研究,無任何持倉 / 交易紀錄;掃描總表非買進清單。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil

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

_TW_TZ = timezone(timedelta(hours=8))   # 台北時間(資料是台股,一律用當地時區顯示)

_MOM = {
    "up": (C_CHEAP, "↑ 上修"),
    "down": (C_EXP, "↓ 下修"),
    "flat": (C_FAIR, "— 持平"),
    "na": (C_NA, "—"),
}
_STATUS = {
    "green": ("#15803d", "🟢 本次沒有新觸發", "目前監控的共識、FCF 與 Thesis 條件沒有新變化。"),
    # 黃燈底色刻意用較深的琥珀(#a16207)而非亮黃:亮黃配白字對比不足,手機戶外幾乎看不見。
    "yellow": ("#a16207", "🟡 有共識異動 / FCF 燈變色", "有『訊號級』變化,詳見下方訊號流水。"),
    "red": ("#dc2626", "🔴 有高優先級訊號", "有高優先級基本面訊號,詳見下方訊號流水。"),
}


# ======================================================================
# 個股詳情頁的 EPS 走勢圖(相容「有/無」法說三情境)
# ======================================================================
def _fig_eps_site(quarters, scenarios, quarter_label: str, currency: str = "TWD") -> str:
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
    fig.update_yaxes(title_text=f"單季 EPS ({'US$' if currency == 'USD' else 'NT$'})")
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
    if not m:
        return '<td class="num" data-sort="nan">N/A</td>'
    color = _VERDICT_COLOR.get(m.verdict, C_NA)
    txt = m.display
    sort = m.value if m.value is not None else "nan"
    return (f'<td class="num" data-sort="{sort}">'
            f'<span style="color:{color};font-weight:700">{_esc(txt)}</span>'
            f'<span class="verdict">{_esc(m.verdict)}</span></td>')


_MREV = {
    "accel": (C_CHEAP, "▲ 加速"),
    "decel": (C_EXP, "▼ 減速"),
    "flat": (C_FAIR, "— 持平"),
    "na": (C_NA, "—"),
}


def _mrev_cell(a) -> str:
    """月營收動能欄(不需分析師共識,近全市場都有)。無資料如實顯示 N/A。"""
    mv = getattr(a, "mrev", None) or {}
    yoy = mv.get("yoy_recent")
    if yoy is None:
        return '<td class="num" data-sort="nan">N/A</td>'
    col, lab = _MREV.get(mv.get("trend") or "na", _MREV["na"])
    return (f'<td class="num" data-sort="{yoy}" title="最新資料月 {_esc(mv.get("last_ym") or "")}">'
            f'<span style="color:{col};font-weight:700">{yoy:+.1f}%</span>'
            f'<span class="verdict">{_esc(lab)}</span></td>')


def _scan_table(rows: list[tuple]) -> str:
    """rows: list of (analysis, momentum_dir, momentum_pct)。"""
    body = []
    for a, mdir, mpct in rows:
        is_us = getattr(a, "market", "twse") == "us"
        price_txt = _n(a.price, 2 if is_us else 0) if a.price else "N/A"
        price_sort = a.price if a.price else "nan"
        mcolor, mlabel = _MOM.get(mdir, _MOM["na"])
        mtxt = mlabel + (f" {mpct:+.1f}%" if mpct not in (None, 0.0) and mdir in ("up", "down") else "")
        msort = mpct if (mpct is not None and mdir in ("up", "down")) else "nan"
        detail = (f'<a href="stock_{a.stock_id}.html">{_esc(a.name)}</a>'
                  if a.ok else _esc(a.name))
        market_label = "美股" if is_us else "台股"
        currency = getattr(a, "currency", "TWD")
        flag = getattr(a, "valuation_flag", "na")
        body.append(
            f'<tr data-market="{getattr(a, "market", "twse")}" data-flag="{flag}">'
            f'<td data-sort="{a.stock_id}">{_esc(a.stock_id)}</td>'
            f'<td class="name" data-sort="{_esc(a.name.lower())}">{detail}</td>'
            f'<td data-sort="{market_label}">{market_label}</td>'
            f'<td class="num" data-sort="{price_sort}" title="{_esc(a.price_date)}">{currency} {price_txt}</td>'
            f'{_metric_cell(a, "forward_pe", "x")}'
            f'{_metric_cell(a, "peg", "", 2)}'
            f'{_metric_cell(a, "fcf_yield", "%")}'
            f'{_metric_cell(a, "ev_ebitda", "x")}'
            f'<td class="num" data-sort="{msort}"><span style="color:{mcolor};font-weight:700">{_esc(mtxt)}</span></td>'
            f'{_mrev_cell(a)}'
            "</tr>"
        )
    heads = [
        ("代號", 0), ("名稱", 1), ("市場", 2), ("收盤價", 3), ("前瞻PE", 4), ("PEG", 5),
        ("FCF Yield", 6), ("EV/EBITDA", 7), ("盈餘修正動能", 8), ("月營收動能", 9),
    ]
    th = "".join(
        f'<th aria-sort="none"><button type="button" onclick="sortTable({i})">{_esc(h)} ⇅</button></th>'
        for h, i in heads)
    return (
        '<table id="scan" data-dir="asc"><caption class="sr-only">觀察清單估值與基本面指標</caption><thead><tr>'
        f"{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _mobile_cards(rows: list[tuple]) -> str:
    cards = []
    for a, mdir, mpct in rows:
        is_us = getattr(a, "market", "twse") == "us"
        market = "美股" if is_us else "台股"
        currency = "US$" if getattr(a, "currency", "TWD") == "USD" else "NT$"
        flag = getattr(a, "valuation_flag", "na")
        flag_text = {"green": "合理偏低", "yellow": "一般", "red": "高估值警戒", "na": "資料不足"}.get(flag, "資料不足")
        fpe = a.metric("forward_pe")
        peg = a.metric("peg")
        momentum = _MOM.get(mdir, _MOM["na"])[1]
        if mpct not in (None, 0.0) and mdir in ("up", "down"):
            momentum += f" {mpct:+.1f}%"
        tag = "a" if a.ok else "article"
        href = f' href="stock_{_esc(a.stock_id)}.html"' if a.ok else ""
        unavailable = "" if a.ok else '<div class="stock-card-unavailable">詳情資料不足，仍保留在觀察清單</div>'
        cards.append(f'''
        <{tag} class="stock-card"{href} data-market="{getattr(a, 'market', 'twse')}" data-flag="{flag}" data-search="{_esc((a.stock_id + ' ' + a.name).lower())}">
          <div class="stock-card-head"><div><b>{_esc(a.stock_id)}</b><span>{_esc(a.name)}</span></div><small>{market}</small></div>
          <div class="stock-card-price">{currency + ' ' + _n(a.price, 2 if is_us else 0) if a.price is not None else '收盤價 N/A'}<small>{_esc(a.price_date or '資料不足')}</small></div>
          <div class="stock-card-metrics"><span>前瞻PE <b>{_esc(fpe.display if fpe else 'N/A')}</b></span><span>PEG <b>{_esc(peg.display if peg else 'N/A')}</b></span></div>
          <div class="stock-card-foot"><span class="flag-{flag}">{flag_text}</span><span>{_esc(momentum)}</span></div>
          {unavailable}
        </{tag}>''')
    return f'<div id="stock-cards" class="stock-cards">{"".join(cards)}</div>'


# ======================================================================
# 第二層:訊號流水
# ======================================================================
def _event_items(rows: list) -> str:
    items = []
    lv_color = {"red": C_EXP, "yellow": "#d97706"}
    for r in rows:
        get = r.get if isinstance(r, dict) else lambda key, default="": getattr(r, key, default)
        c = lv_color.get(get("level", ""), C_FAIR)
        sid = _esc(get("stock_id", ""))
        name = _esc(get("name", ""))
        stock = f'<a href="stock_{sid}.html">{sid} {name}</a>' if sid else name
        items.append(
            '<div class="stream-item">'
            f'<span class="dot" style="background:{c}"></span>'
            f'<span class="stream-date">{_esc(get("date", ""))}</span>'
            f'<span class="stream-stock">{stock}</span>'
            f'<span class="stream-msg">{_esc(get("message", ""))}</span>'
            "</div>"
        )
    return "".join(items)


def _signal_stream(log_rows: list[dict], first_run: bool, current_events: list | None = None) -> str:
    if first_run and not current_events:
        return ('<div class="stream-empty">首次建立基準快照。'
                '從<b>下一次每日重跑</b>起,共識上下修 / FCF 燈變色 / thesis 證偽會出現在這裡。</div>')
    rows = list(log_rows)
    existing = {(r.get("date"), r.get("stock_id"), r.get("kind"), r.get("message")) for r in rows}
    for event in reversed(current_events or []):
        key = (event.date, event.stock_id, event.kind, event.message)
        if key not in existing:
            rows.insert(0, {"date": event.date, "stock_id": event.stock_id, "name": event.name,
                            "kind": event.kind, "level": event.level, "message": event.message})
            existing.add(key)
    if not rows:
        return ('<div class="stream-empty">目前沒有訊號事件。'
                '每日重跑後,只要有共識上下修、FCF 燈變色或 thesis 證偽,就會即時列在這裡'
                '(<b>股價漲跌不算訊號,不會出現</b>)。</div>')
    return (f'<div class="stream">{_event_items(rows[:8])}</div>'
            + (f'<details class="history-events"><summary>查看較早訊號（{len(rows)-8} 則）</summary>'
               f'<div class="stream">{_event_items(rows[8:])}</div></details>' if len(rows) > 8 else ""))


# ======================================================================
# 個股查詢框(輸入代號/名稱 → 跳到該股詳情頁,並即時過濾掃描總表)
# ======================================================================
_SEARCH_JS = """
function osApply(){
  var q=(document.getElementById('os-q').value||'').trim().toLowerCase();
  var market=document.getElementById('os-market').value, flag=document.getElementById('os-flag').value;
  var tableShown=0,cardShown=0,t=document.getElementById('scan');
  if(t){Array.prototype.forEach.call(t.tBodies[0].rows,function(r){
    var txt=(r.cells[0].innerText+' '+r.cells[1].innerText).toLowerCase();
    var ok=(!q||txt.indexOf(q)>=0)&&(!market||r.dataset.market===market)&&(!flag||r.dataset.flag===flag);
    r.style.display=ok?'':'none';if(ok)tableShown++;});}
  var cards=document.querySelectorAll('.stock-card');
  if(cards.length){cards.forEach(function(c){var ok=(!q||c.dataset.search.indexOf(q)>=0)&&(!market||c.dataset.market===market)&&(!flag||c.dataset.flag===flag);c.style.display=ok?'':'none';if(ok)cardShown++;});}
  var shown=window.matchMedia('(max-width: 640px)').matches?cardShown:tableShown;
  var n=document.getElementById('os-count');if(n)n.textContent='顯示 '+shown+' 檔';
}
function _osSug(show){var s=document.getElementById('os-sug');if(s)s.style.display=show?'block':'none';}
function osQuery(){
  var q=(document.getElementById('os-q').value||'').trim().toLowerCase();
  osApply();
  var sug=document.getElementById('os-sug');sug.innerHTML='';
  if(!q){_osSug(false);return;}
  var m=OS_STOCKS.filter(function(s){return (s.id+' '+s.name).toLowerCase().indexOf(q)>=0;}).slice(0,8);
  if(!m.length){sug.innerHTML='<div class="os-none">找不到「'+q+'」——此代號可能不在觀察範圍。'
    +'可到<a href=\\"screener.html\\">篩選器</a>查看目前可分析範圍。</div>';_osSug(true);return;}
  m.forEach(function(s){var d=document.createElement('div');d.className='os-item';d.setAttribute('role','option');d.setAttribute('tabindex','0');
    d.innerHTML='<b>'+s.id+'</b> '+s.name+'<span class=\\"os-go\\">→ 看詳情</span>';
    d.onmousedown=function(){location.href='stock_'+s.id+'.html';};d.onkeydown=function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();location.href='stock_'+s.id+'.html';}};sug.appendChild(d);});
  _osSug(true);
}
function osKey(e){if(e.key==='Enter'){var q=(document.getElementById('os-q').value||'').trim().toLowerCase();
  var s=OS_STOCKS.filter(function(x){return x.id.toLowerCase()===q||x.name.toLowerCase()===q;})[0]
      ||OS_STOCKS.filter(function(x){return (x.id+' '+x.name).toLowerCase().indexOf(q)>=0;})[0];
  if(s)location.href='stock_'+s.id+'.html';}}
"""


def _search_box(rows: list[tuple]) -> str:
    import json
    stocks = [{"id": a.stock_id, "name": a.name} for a, _, _ in rows if a.ok]
    data = json.dumps(stocks, ensure_ascii=False)
    n = len(stocks)
    return (
        '<div class="search-box">'
        '<label class="sr-only" for="os-q">搜尋股票代號或名稱</label>'
        '<input id="os-q" type="search" autocomplete="off" '
        f'placeholder="🔍 輸入代號或名稱查詢個股(目前可查 {n} 檔有詳情頁的股票)…" '
        'oninput="osQuery()" onkeydown="osKey(event)" onblur="setTimeout(function(){_osSug(false)},200)" '
        'onfocus="osQuery()">'
        '<div id="os-sug" class="os-suggest" role="listbox"></div>'
        '</div>'
        '<div class="stock-filters"><select id="os-market" onchange="osApply()" aria-label="市場篩選">'
        '<option value="">全部市場</option><option value="twse">台股</option><option value="us">美股</option></select>'
        '<select id="os-flag" onchange="osApply()" aria-label="估值旗標篩選">'
        '<option value="">全部估值狀態</option><option value="red">高估值警戒</option><option value="green">合理偏低</option><option value="yellow">一般</option><option value="na">資料不足</option></select>'
        f'<span id="os-count" aria-live="polite">顯示 {len(rows)} 檔</span></div>'
        f'<script>var OS_STOCKS={data};{_SEARCH_JS}document.addEventListener("DOMContentLoaded",osApply);</script>'
    )


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
    screener_info: dict | None = None,
) -> str:
    scolor, stitle, sdesc = _STATUS.get(status, _STATUS["green"])
    n_red = sum(1 for e in events if e.level == "red"
                and not e.message.startswith("Thesis 目前仍為紅燈"))
    n_yellow = sum(1 for e in events if e.level == "yellow")
    n_up = sum(1 for e in events if e.kind == "consensus" and "上修" in e.message)
    n_down = sum(1 for e in events if e.kind == "consensus" and "下修" in e.message)
    n_fcf = sum(1 for e in events if e.kind == "fcf")
    n_thesis = sum(1 for e in events if e.kind == "thesis"
                   and not e.message.startswith("Thesis 目前仍為紅燈"))
    event_summary = (
        '<div class="event-summary">'
        f'<span>本次新訊號 <b>{n_red+n_yellow}</b></span>'
        f'<span class="event-up">共識上修 {n_up}</span>'
        f'<span class="event-down">共識下修 {n_down}</span>'
        f'<span>FCF 變化 {n_fcf}</span><span>Thesis 觸發 {n_thesis}</span>'
        '</div>'
    )
    # 價格是「哪一個交易日的收盤價」——不標出來,使用者會誤以為是即時報價
    tw_dates = sorted({a.price_date for a, _, _ in rows
                       if getattr(a, "market", "twse") != "us" and getattr(a, "price_date", None)})
    us_dates = sorted({a.price_date for a, _, _ in rows
                       if getattr(a, "market", "twse") == "us" and getattr(a, "price_date", None)})
    price_days = "、".join(filter(None, [
        f"台股 {tw_dates[-1]}" if tw_dates else "",
        f"美股 {us_dates[-1]}" if us_dates else "",
    ])) or "最近交易日"
    count_txt = ""
    if not first_run or n_red or n_yellow:
        # 狀態燈是彩色底(綠/黃/紅),計數若再用紅/黃字會「紅底紅字」看不見 →
        # 一律白字 + 半透明白底藥丸,在任何底色上都保持高對比。
        count_txt = (f'　本次:<b class="cnt">紅 {n_red}</b>'
                     f'<b class="cnt">黃 {n_yellow}</b>')

    banner = (
        f'<div class="status" style="background:{scolor}">'
        f'<div class="status-title">{_esc(stitle)}</div>'
        f'<div class="status-desc">{_esc(sdesc)}{count_txt}</div>'
        "</div>"
    )

    screener_cta = ""
    ai_cta = ""
    if screener_info:
        screener_cta = (
            '<a class="screener-cta" href="screener.html">'
            '<span class="arrow">→</span>'
            '🔎 <b>兩層選股篩選器</b>(可分析母體)　'
            f'通過第一層 <b>{screener_info.get("layer1_pass", 0)}</b> 檔・'
            f'兩層全過 <b>{screener_info.get("both_pass", 0)}</b> 檔'
            '　<span style="opacity:.85">點此看完整篩選結果 →</span></a>'
        )
        ai_cta = (
            '<a class="ai-cta" href="ai-chain.html">'
            '<span class="arrow">→</span>🧭 <b>AI 產業鏈全景圖</b>　'
            f'{screener_info.get("ai_layers", 0)} 個層級・'
            f'資料不足 {screener_info.get("ai_unavailable", 0)} 檔　'
            '<span style="opacity:.86">從雲端 Capex 往上游追蹤 →</span></a>'
        )

    table = _scan_table(rows)
    cards = _mobile_cards(rows)
    stream = _signal_stream(log_rows, first_run, events)
    search = _search_box(rows)

    body = f"""
<div class="wrap">
  <header>
    <h1>個人選股分析儀表板</h1>
    <div class="meta">更新時間 {generated}　|　觀察清單 {len(rows)} 檔　|　資料:TWSE/TPEx + FinMind + yfinance(公開市場數據)</div>
    <details class="site-disclosure"><summary>資料時間與使用說明</summary>
      <div class="notice"><b>本站不是即時報價。</b>收盤價截至 <b>{_esc(price_days)}</b>；所有估值都以該收盤價計算。</div>
      <div class="warn">全站僅為公開數據研究，無持倉或交易紀錄，不構成投資建議。</div>
    </details>
  </header>

  <div class="layer-tag">第一層 · 狀態燈</div>
  {banner}

  <div class="layer-tag">第二層 · 訊號流水(只看訊號,不看股價雜訊)</div>
  <section>
    {event_summary}
    {stream}
    {_note('這裡只收<b>基本面訊號</b>:共識EPS 上/下修、FCF 品質燈變色、thesis 證偽條件。'
           '估值位階改由篩選器的 trailing-to-trailing 同口徑旗標顯示,不再把 forward PE 對 trailing 歷史河道的變化當事件。'
           '<b>股價每日漲跌屬雜訊,刻意不列</b>——真正該花時間研究的是這些訊號背後的原因。')}
  </section>

  {search}

  <div class="research-links">{screener_cta}{ai_cta}</div>

  <div class="layer-tag">掃描總表 · 縮小研究範圍用</div>
  <section>
    <div class="table-warn">📌 本表僅供<b>縮小研究範圍</b>,<b>非買進清單</b>。點欄位標題可排序。
      <b>前瞻PE為藍色參考值,不拿它對照 trailing 歷史河道判便宜/貴</b>;其他顏色仍為經驗法則。</div>
    <div class="swipe-hint scan-swipe">← 手機可左右滑動看更多欄位 →</div>
    <div class="table-scroll">{table}</div>
    {cards}
    {_note('<b>前瞻PE</b>=現價÷今年共識EPS;<b>PEG</b>=前瞻PE÷盈餘成長率;'
           '<b>FCF Yield</b>=近4季自由現金流÷市值;<b>EV/EBITDA</b>=(市值+負債−現金)÷近4季EBITDA。'
           '<b>盈餘修正動能</b>僅<b>標記</b>近期共識被上/下修的方向,<b>目前不納入評分</b>'
           '(依原則,等回測驗證後才考慮加權重)。'
           '<br><b>月營收動能</b>=近3個月平均營收年增率(YoY),並與其前3個月比較判加速/減速。'
           '資料為台股每月10日前依法公告的月營收——<b>不需分析師覆蓋,近全市場都有</b>,'
           '正好補上多數台股(尤其金融/傳產)沒有共識、導致前瞻PE/PEG/盈餘修正動能空白的缺口。'
           '<b style="color:#b91c1c">但它與共識類指標口徑不同</b>:一個是已發生的實際營收、一個是對未來的預估,'
           '兩者不可互相取代;且月營收只反映營收,<b>不含毛利與費用變化</b>。'
           '點名稱進個股詳情看河流圖與 FCF 品質。')}
  </section>

  <footer>
    <div>資料來源:台股最新收盤/成交額 TWSE/TPEx、台股財報與歷史資料 FinMind、分析師共識EPS/FCF/EV 元件與美股 yfinance。</div>
    <div>本工具僅為個人估值研究,數字可能過時或有誤,請務必回原始出處核對,不構成投資建議。</div>
  </footer>
</div>
{_SORT_JS}
"""
    return _page(f"個人選股分析儀表板", body, plotly=False)


# ======================================================================
# 個股詳情 stock_<id>.html
# ======================================================================
_CALC_JS = """
function whatIf(sid){
  var P = WHATIF[sid]; if(!P) return;
  var el = document.getElementById('wi-price');
  var p = parseFloat(el.value);
  var box = document.getElementById('wi-out');
  if(!(p > 0)){ box.innerHTML = '<span class="wi-none">請輸入大於 0 的價格。</span>'; return; }

  function cell(label, val, unit, verdict, note){
    var col = {'便宜':'#16a34a','合理':'#6b7280','偏貴':'#ea580c','貴':'#dc2626','前瞻參考':'#2563eb','資料不足':'#9ca3af','不適用':'#9ca3af','負現金流':'#dc2626','無現金流':'#dc2626'}[verdict]||'#6b7280';
    var v = verdict==='不適用' ? 'N/M' : ((val===null||val===undefined||!isFinite(val)) ? 'N/A' : (val.toFixed(unit==='' ? 2 : 1) + unit));
    return '<div class="wi-card"><div class="wi-name">'+label+'</div>'
         + '<div class="wi-val" style="color:'+col+'">'+v+'</div>'
         + '<div class="wi-badge" style="background:'+col+'">'+verdict+'</div>'
         + (note ? '<div class="wi-note">'+note+'</div>' : '') + '</div>';
  }

  var out = '';
  // 1) 前瞻PE只顯示數值；歷史河道是 trailing 口徑,不可據此判便宜/貴。
  var peNM = P.ann_eps !== null && P.ann_eps <= 0;
  var fpe = P.ann_eps > 0 ? p / P.ann_eps : null, peV = peNM ? '不適用' : (fpe === null ? '資料不足' : '前瞻參考');
  out += cell('前瞻PE', fpe, 'x', peV, peNM ? 'EPS 非正，PE 無意義' : (P.ann_eps ? ('÷ 年化EPS ' + P.ann_eps.toFixed(2)) : ''));

  // 2) PEG
  var pgNM = peNM || (fpe !== null && P.growth_pct !== null && P.growth_pct <= 0);
  var peg = (fpe && P.growth_pct > 0) ? fpe / P.growth_pct : null, pgV=pgNM?'不適用':'資料不足';
  if(peg !== null){ pgV = peg < 1 ? '便宜' : (peg <= 1.5 ? '合理' : (peg <= 2 ? '偏貴' : '貴')); }
  out += cell('PEG', peg, '', pgV, P.growth_pct ? ('成長率 ' + P.growth_pct.toFixed(1) + '%') : '無成長率');

  // 3) FCF Yield(市值隨價格變 → 這欄會跟著動)
  var mcap = P.shares_bn ? p * P.shares_bn : null;
  var fy = (!P.is_financial && mcap && P.fcf_bn !== null) ? (P.fcf_bn / mcap * 100) : null, fyV=P.is_financial?'不適用':'資料不足';
  if(fy !== null){ fyV = fy < 0 ? '負現金流' : (fy === 0 ? '無現金流' : (fy > 4 ? '便宜' : (fy >= 2 ? '合理' : '偏貴'))); }
  out += cell('FCF Yield', fy, '%', fyV, P.is_financial ? '金融業不適用' : (mcap ? ('市值 ' + mcap.toFixed(0) + ' 十億') : ''));

  // 4) EV/EBITDA
  var ev = (!P.is_financial && mcap !== null && P.debt_bn !== null && P.cash_bn !== null) ? (mcap + P.debt_bn - P.cash_bn) : null;
  var evNM = P.is_financial || (P.ebitda_bn !== null && P.ebitda_bn <= 0);
  var eve = (ev !== null && P.ebitda_bn > 0) ? ev / P.ebitda_bn : null, evV=evNM?'不適用':'資料不足';
  if(eve !== null){ evV = eve < 12 ? '便宜' : (eve <= 18 ? '合理' : '貴'); }
  out += cell('EV/EBITDA', eve, 'x', evV, P.is_financial ? '金融業不適用' : (evNM ? 'EBITDA 非正' : ''));

  var diff = P.base_price ? ((p / P.base_price - 1) * 100) : null;
  var d = (diff === null) ? '' :
     ('<div class="wi-diff">輸入價 ' + p.toLocaleString() + ' vs 收盤價 ' + P.base_price.toLocaleString()
      + '(' + (diff>=0?'+':'') + diff.toFixed(1) + '%)</div>');
  box.innerHTML = d + '<div class="wi-grid">' + out + '</div>';
}
function whatIfReset(sid){
  var P = WHATIF[sid]; if(!P) return;
  document.getElementById('wi-price').value = P.base_price;
  whatIf(sid);
}
"""


def _whatif_block(a) -> str:
    """『換一個價格,估值變多少』試算器。

    為什麼是「手動輸入」而不是自動抓即時價:
      證交所 MIS 即時報價**沒有 CORS 標頭、也不支援 JSONP**(已實測),
      瀏覽器無法直接取用;要自動抓就得自己架 proxy,等於讓網站依賴一台常駐機器。
      改成輸入價格即時重算,不但零依賴,還能回答「如果跌到 X,PE 剩多少」——
      這比單純顯示當下報價更有用。
    ★ 計算與判讀門檻和後端完全一致(見 metrics.build_dashboard),只是把價格換掉。
    """
    import json as _json

    yf = getattr(a, "yf_raw", None) or {}
    pb = a.pe_band
    if not a.price or a.ann_eps is None:
        return ""

    def _bn(x):
        return (x / 1e9) if isinstance(x, (int, float)) else None

    params = {
        "base_price": a.price,
        "ann_eps": a.ann_eps,
        "shares_bn": a.shares_bn,
        "growth_pct": a.growth_pct,
        "fcf_bn": _bn(yf.get("fcf_ttm")),
        "debt_bn": _bn(yf.get("totalDebt")),
        "cash_bn": _bn(yf.get("totalCash")),
        "ebitda_bn": _bn(yf.get("ebitda")),
        "is_financial": bool(getattr(a, "is_financial", False)),
        "pe_low": pb.pe_low if pb else None,
        "pe_mid": pb.pe_mid if pb else None,
        "pe_high": pb.pe_high if pb else None,
    }
    data = _json.dumps({a.stock_id: params}, ensure_ascii=False)
    sid = _esc(a.stock_id)
    is_us = getattr(a, "market", "twse") == "us"
    mis = f"https://mis.twse.com.tw/stock/fibest.jsp?stock={sid}"
    yhoo = (f"https://finance.yahoo.com/quote/{sid}/" if is_us
            else f"https://tw.stock.yahoo.com/quote/{sid}.TW")
    quote_links = (f'<a href="{yhoo}" target="_blank" rel="noopener">Yahoo Finance</a>' if is_us else
                   f'<a href="{mis}" target="_blank" rel="noopener">證交所即時報價</a> 或 '
                   f'<a href="{yhoo}" target="_blank" rel="noopener">Yahoo 股市</a>')
    currency_label = "US$" if a.currency == "USD" else "NT$"
    return f"""
  <section>
    <h2>換個價格試算(即時報價可用這裡換算)</h2>
    <div class="notice">本站的收盤價每日更新一次、<b>盤中不會跳動</b>。
      想知道「現在這個價位」的估值,先到
      {quote_links} 看現價,
      再填進下面欄位,四個指標會<b>立刻用新價格重算</b>(判讀門檻與本頁完全相同)。
      也可以直接試算「如果跌到 X / 漲到 Y」。</div>
    <div class="wi-bar">
      <label for="wi-price">價格 {currency_label}</label>
      <input id="wi-price" type="number" step="0.01" min="0" value="{a.price}"
             oninput="whatIf('{sid}')" onkeydown="if(event.key==='Enter')whatIf('{sid}')">
      <button onclick="whatIf('{sid}')">試算</button>
      <button class="wi-sec" onclick="whatIfReset('{sid}')">回到收盤價</button>
    </div>
    <div id="wi-out"></div>
    {_note('只有<b>跟價格連動</b>的指標會變(前瞻PE、PEG、FCF Yield、EV/EBITDA);'
           'EPS、自由現金流、負債等來自財報,不會因為股價變動而改變。'
           '<b>這是試算工具,不是預測</b> —— 它只回答「這個價格對應的估值是多少」。')}
  </section>
  <script>var WHATIF={data};{_CALC_JS}
  document.addEventListener('DOMContentLoaded',function(){{whatIf('{sid}');}});</script>
"""


def _fig_thesis_gross_margin(thesis) -> str:
    rows = thesis.gross_margins
    if not rows:
        return _placeholder("毛利率資料不足。")
    fig = go.Figure(go.Scatter(
        x=[x["quarter"] for x in rows], y=[x["value"] for x in rows],
        name="毛利率", mode="lines+markers+text", line=dict(color=C_BLUE, width=2.5),
        marker=dict(size=8), text=[f'{x["value"]:.1f}%' for x in rows], textposition="top center",
        hovertemplate="%{x}<br>毛利率 %{y:.2f}%<extra></extra>",
    ))
    floor = thesis.gross_margin_floor_pct
    fig.add_hline(y=floor, line_dash="dash", line_color=C_EXP,
                  annotation_text=f"證偽門檻 {floor:g}%", annotation_position="bottom right")
    fig.update_yaxes(title_text="毛利率 (%)")
    fig.update_xaxes(type="category")
    return _fig_div(_layout(fig, height=330))


def _thesis_html(a) -> str:
    thesis = getattr(a, "thesis", None)
    if not thesis:
        return ""
    meta = {
        "green": ("#15803d", "綠・未觸發"),
        "yellow": ("#a16207", "黃・接近／觀察"),
        "red": (C_EXP, "紅・已觸發"),
        "gray": (C_NA, "灰・資料不足"),
    }
    cards = []
    for item in thesis.conditions:
        color, label = meta[item.status]
        manual = (f'<div class="thesis-updated">人工判定最後更新:{_esc(item.last_updated)}</div>'
                  if item.manual else "")
        validation = ({"backtested": "回測驗證出場訊號",
                       "backtest_proxy": "回測代理・未直接驗證"}.get(
                           item.validation, "人工補充門檻・未經回測"))
        cards.append(f'''
        <article class="thesis-condition" data-thesis-id="{_esc(item.id)}" data-thesis-status="{_esc(item.status)}" style="border-left-color:{color}">
          <div class="thesis-condition-head"><b>{_esc(item.label)}</b>
            <span style="color:{color}">{_esc(label)}</span></div>
          <div class="thesis-current">{_esc(item.current_value)}</div>
          <p>{_esc(item.basis)}</p>{manual}
          <div class="thesis-validation">{_esc(validation)}｜{_esc(item.validation_note)}</div>
        </article>''')
    pr = thesis.position_rules
    look_through = "、".join(pr.get("look_through_symbols") or []) or "—"
    return f'''
  <section class="thesis-section" data-thesis-status="{_esc(thesis.status)}" data-thesis-conditions="{len(thesis.conditions)}" data-thesis-as-of="{_esc(thesis.as_of_period)}" data-thesis-gm-latest="{_esc(thesis.gross_margins[-1]['quarter'] if thesis.gross_margins else '')}">
    <div class="thesis-title"><h2>Thesis 狀態</h2><b>{_esc(meta[thesis.status][1])}</b></div>
    <div class="warn">⚠️ <b>站主個人持有假設</b>，不是通用評分。{_esc(thesis.disclaimer)} 條件 1 為回測驗證出場訊號；條件 2 僅有回測代理、逐季下修軌跡未直接驗證；條件 3、4 為人工補充門檻、未經回測。</div>
    <div class="thesis-assumption"><span>持有假設</span><b>{_esc(thesis.holding_assumption)}</b></div>
    {_fig_thesis_gross_margin(thesis)}
    <div class="thesis-grid">{''.join(cards)}</div>
    <div class="thesis-position"><b>部位規則</b>
      <span>單一標的總曝險上限:總資產 {_n(pr.get('max_total_exposure_pct'), 0)}%</span>
      <span>需穿透計算:{_esc(look_through)} 權重</span>
      <span>進場方式:{_esc(pr.get('entry_method') or '—')}</span>
      <small>{_esc(pr.get('note') or '—')}｜檢查頻率:{_esc(thesis.check_frequency)}</small>
    </div>
  </section>'''


def build_detail_html(a, generated: str, momentum_min_pct: float = 0.5) -> str:
    # 頂部小摘要
    parts = []
    is_us = getattr(a, "market", "twse") == "us"
    currency_label = "US$" if a.currency == "USD" else "NT$"
    if a.price:
        parts.append(f"現價 <b>{currency_label} {_n(a.price, 2 if is_us else 0)}</b>（{_esc(a.price_date)}）")
    if a.eps_y0 is not None:
        g = f"，成長 {a.growth_pct:+.1f}%" if a.growth_pct is not None else ""
        parts.append(f"今年共識EPS <b>{_n(a.eps_y0, 2)}</b>{g}")
    if a.pe_band:
        parts.append(f"本益比河道 {a.pe_band.pe_low:g}/{a.pe_band.pe_mid:g}/{a.pe_band.pe_high:g}x")
    summary = "　|　".join(parts) if parts else "資料整理中"

    river_div = _fig_river(a.river) if a.river else _placeholder("河流圖資料不足。")
    fcf_dual = (_placeholder("金融業的現金流、負債與現金屬營運項目，不適用一般企業 FCF 品質檢查。")
                if getattr(a, "is_financial", False) else
                _fig_fcf_dual(a.fcf) if a.fcf else _placeholder("FCF 雙線資料不足。"))
    fcf_note = ("金融業不套用 DIO／DSO／OCF 與資本支出傳導的通用門檻。"
                if getattr(a, "is_financial", False) else
                "資本支出年增率(領先2年)vs 營收年增率:看前兩年擴產有沒有兌現成營收。")
    eps_div = (_fig_eps_site(a.quarters, a.scenarios, a.quarter_label, a.currency)
               if a.quarters else _placeholder("EPS 資料不足。"))
    cons_div = _fig_consensus(a.consensus_history or [], a.currency)
    consensus_section = ""
    if a.consensus_history or getattr(a, "track_signals", True):
        consensus_section = f'''
  <section>
    <h2>共識EPS 監控</h2>
    {cons_div}
    {_note('<span style="color:'+C_CHEAP+'">▲上修</span>/<span style="color:'+C_EXP+'">▼下修</span>;每次資料更新會累積更長折線。')}
  </section>'''

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

    flag_map = {
        "green": ("🟢", "合理偏低", C_CHEAP),
        "yellow": ("🟡", "一般", "#a16207"),
        "red": ("🔴", "高估值警戒", C_EXP),
        "na": ("⚪", "資料不足", C_NA),
    }
    fem, flab, fcol = flag_map.get(getattr(a, "valuation_flag", "na"), flag_map["na"])
    pct_txt = (f"{a.pe_percentile:.0f}%" if a.pe_percentile is not None else "—")
    trailing_txt = f"{_n(a.trailing_pe, 1)}x" if a.trailing_pe is not None else "—"
    median_txt = f"{_n(a.pe_median, 1)}x" if a.pe_median is not None else "—"
    p90_txt = f"{_n(a.pe_p90, 1)}x" if a.pe_p90 is not None else "—"
    position = (
        '<div class="pe-position">'
        f'<b style="color:{fcol}">{fem}{_esc(flab)}</b>　'
        f'目前 trailing PE <b>{trailing_txt}</b>　|　'
        f'近5年 P50 <b>{median_txt}</b>　|　P90 <b>{p90_txt}</b>　|　'
        f'百分位 <b>{pct_txt}</b>'
        '</div>'
    )
    river_snapshot_attrs = (
        f'data-pe-current="{float(a.trailing_pe):.1f}" '
        f'data-pe-p50="{float(a.pe_median):.1f}" '
        f'data-pe-p90="{float(a.pe_p90):.1f}" '
        f'data-pe-source-cache-regressed="{str(bool(getattr(a, "pe_source_cache_regressed", False))).lower()}"'
    ) if all(value is not None for value in (a.trailing_pe, a.pe_median, a.pe_p90)) else (
        'data-pe-current="" data-pe-p50="" data-pe-p90="" '
        f'data-pe-source-cache-regressed="{str(bool(getattr(a, "pe_source_cache_regressed", False))).lower()}"'
    )
    momentum_dir, momentum_pct = __import__("src.scan_state", fromlist=["revision_momentum"]).revision_momentum(
        a.consensus_history or [], min_pct=momentum_min_pct)
    momentum_txt = {"up": "共識上修", "down": "共識下修", "flat": "共識持平", "na": "共識資料不足"}.get(
        momentum_dir, "共識資料不足")
    if momentum_pct is not None and momentum_dir in ("up", "down"):
        momentum_txt += f" {momentum_pct:+.1f}%"
    quality_txt = "金融業不適用一般 FCF 品質" if getattr(a, "is_financial", False) else "FCF 品質資料不足"
    if a.fcf and not getattr(a, "is_financial", False):
        lights = [s.light for s in a.fcf.signals]
        quality_txt = ("FCF 有紅燈" if "red" in lights else "FCF 有黃燈" if "yellow" in lights
                       else "FCF 品質正常" if lights else quality_txt)
    if getattr(a, "pe_source_cache_regressed", False):
        confidence = "估值沿用已驗證快照，等待原始快取恢復"
    elif not a.errors and a.dashboard and a.river and a.trailing_pe is not None:
        confidence = "主要估值資料完整"
    elif a.price is not None and a.price_date:
        confidence = "收盤價完整，部分估值資料不足"
    else:
        confidence = "部分資料不足"
    quick = f'''
  <section class="quick-summary">
    <div class="quick-title"><h2>快速摘要</h2><small>先看結論，再往下看方法</small></div>
    <div class="quick-grid">
      <div><span>估值位置</span><b style="color:{fcol}">{fem}{_esc(flab)}</b><small>近5年百分位 {pct_txt}</small></div>
      <div><span>盈餘修正</span><b>{_esc(momentum_txt)}</b><small>{_esc(a.consensus_source or '尚無共識來源')}</small></div>
      <div><span>現金流品質</span><b>{_esc(quality_txt)}</b><small>依 DIO／DSO／OCF</small></div>
      <div><span>資料信心</span><b>{_esc(confidence)}</b><small>收盤日 {_esc(a.price_date)}</small></div>
    </div>
  </section>'''
    if is_us:
        river_note = (
            '河道 =「當時公告後可得的近四季 Reported EPS」×<b>截至當月為止</b> rolling 5年 '
            'trailing PE P10/P50/P90。Yahoo Close 採拆股調整、不含股息；財報通常盤後公布，'
            '從市場可交易的第一個收盤日起生效；黑線為每日收盤。'
            + _esc(a.river.source if a.river else '') + '。')
    else:
        river_note = (
            '河道 =「當時可得的近四季實際EPS」×<b>截至當月為止</b> rolling 5年 trailing PE P10/P50/P90。'
            'FinMind 無實際公告日欄位,本國發行人財報生效日採法定申報期限 fallback'
            '（一般業 Q2 8/14、金融保險業 Q2 8/31）；'
            'KY/外國發行人不套用此假設；黑線為每日收盤。')

    err = ""
    if a.errors:
        err = ('<div class="warn">部分資料抓取失敗(該區塊以「資料不足」顯示):'
               + _esc("；".join(a.errors)) + "</div>")
    if getattr(a, "pe_source_cache_regressed", False):
        err += ('<div class="warn"><b>估值資料保護中:</b>本次原始 cache 覆蓋較前次縮減；'
                '目前 trailing PE、P10/P50/P90、圖例與門檻價沿用已驗證 snapshot，'
                '歷史月頻河道仍由現有 cache 繪製，百分位暫不顯示；待 cache 恢復後自動解除。</div>')

    body = f"""
<div class="wrap">
  <header>
    <div><a class="back" href="index.html">← 回總表</a></div>
    <h1>{_esc(a.name)}（{_esc(a.stock_id)}）個股詳情</h1>
    <div class="meta">更新時間 {generated}　|　{summary}</div>
    <details class="site-disclosure"><summary>資料與使用說明</summary>
      <div class="warn">公開數據估值研究，無持倉或交易紀錄，不構成投資建議。</div>
    </details>
    {err}
  </header>

{quick}

  <section>
    <h2>四指標(連動收盤價/試算價)</h2>
    {_cards_html(a.dashboard)}
    {_note('前瞻PE只顯示藍色參考值;PEG/FCF Yield/EV·EBITDA 依各自同口徑門檻著色。單一指標不下結論。')}
  </section>

  <section {river_snapshot_attrs}>
    <h2>本益比河流圖</h2>
    {position}
    {river_div}
    {_note(river_note +
           '圖例數字是目前分位:'+_esc(a.pe_band.years_covered if a.pe_band else '資料不足')+'。'
           '河道按月更新歷史分位，黑線按交易日更新收盤價；紅點標示最新收盤。'
           '<b>河道不再為了包住股價而向外擴張</b>;股價超出上緣/下緣是極端估值訊號,不是繪圖錯誤。'
           '股價貼近<b style="color:'+C_CHEAP+'">綠</b>相對便宜、貼近或超過<b style="color:'+C_EXP+'">紅</b>相對貴。' + river_zone)}
  </section>

{_thesis_html(a)}

  <section>
    <h2>FCF 品質檢查</h2>
    {fcf_dual}
    {_note(_esc(fcf_note))}
    {_lights_html(a.fcf)}
  </section>

  <section>
    <h2>EPS 走勢</h2>
    {eps_div}
    {_note('藍柱=財報實際;斜線紋柱=法說三情境試算(僅有指引檔的股票才有)。')}
  </section>

{consensus_section}
{_whatif_block(a)}
  <footer><div><a class="back" href="index.html">← 回總表</a>　|　資料:{'Yahoo Finance' if is_us else 'TWSE/TPEx + FinMind + yfinance'},不構成投資建議。</div></footer>
</div>
"""
    return _page(f"{a.name} {a.stock_id} 詳情", body, plotly=True)


# ======================================================================
# 寫出整個網站
# ======================================================================
def write_site(analyses: list, status: str, events: list, first_run: bool,
               log_rows: list[dict], out_dir: str | Path,
               screener_html: str | None = None, screener_info: dict | None = None,
               ai_chain_html: str | None = None,
               momentum_min_pct: float = 0.5) -> dict:
    """把 index / 各詳情頁 / plotly.min.js / style.css 全部寫到 out_dir。回傳統計。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # 避免前次產物殘留，讓品質閘門誤把已不在本次建站內的舊頁視為成功。
    for path in out.glob("stock_*.html"):
        path.unlink()
    for name in ("index.html", "screener.html", "ai-chain.html"):
        path = out / name
        if path.exists():
            path.unlink()
    # 一律用台北時間顯示:CI 跑在 UTC,直接印 datetime.now() 會變成
    # 「更新時間 09:28」而實際是台灣 17:28 —— 看的人會以為早上更新過。
    generated = datetime.now(_TW_TZ).strftime("%Y-%m-%d %H:%M") + " (台北時間)"

    # 排序:先成功、四指標齊全的在前(方便看);盈餘修正動能一併算好
    from .scan_state import revision_momentum
    rows = []
    for a in analyses:
        mdir, mpct = revision_momentum(
            a.consensus_history, min_pct=momentum_min_pct)
        rows.append((a, mdir, mpct))

    # 共用資源:plotly.min.js(本地一份)、style.css
    (out / "plotly.min.js").write_text(get_plotlyjs(), encoding="utf-8")
    (out / "style.css").write_text(_SITE_CSS, encoding="utf-8")
    logo_src = Path(__file__).resolve().parent.parent / "assets/logos"
    if logo_src.exists():
        logo_out = out / "assets/logos"
        logo_out.mkdir(parents=True, exist_ok=True)
        for p in logo_src.glob("*.png"):
            shutil.copy2(p, logo_out / p.name)

    # 首頁
    (out / "index.html").write_text(
        build_index_html(rows, status, events, first_run, log_rows, generated, screener_info),
        encoding="utf-8",
    )
    # 選股篩選頁(有資料才寫)
    if screener_html:
        (out / "screener.html").write_text(screener_html, encoding="utf-8")
    if ai_chain_html:
        (out / "ai-chain.html").write_text(ai_chain_html, encoding="utf-8")
    # 各詳情頁(只為成功的股票產生)
    n_detail = 0
    for a in analyses:
        if a.ok:
            (out / f"stock_{a.stock_id}.html").write_text(
                build_detail_html(a, generated, momentum_min_pct), encoding="utf-8")
            n_detail += 1

    return {"stocks": len(analyses), "details": n_detail, "out": str(out),
            "screener": bool(screener_html), "ai_chain": bool(ai_chain_html)}


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
  Array.prototype.forEach.call(t.tHead.rows[0].cells,function(th,i){th.setAttribute('aria-sort',i===n?(dir==='asc'?'ascending':'descending'):'none');});
  rows.sort(function(a,b){
    var xs=a.cells[n].getAttribute('data-sort')||a.cells[n].innerText.toLowerCase();
    var ys=b.cells[n].getAttribute('data-sort')||b.cells[n].innerText.toLowerCase();
    var x=parseFloat(xs), y=parseFloat(ys);
    var xn=isNaN(x), yn=isNaN(y);
    if(xn&&yn){ var c=xs<ys?-1:xs>ys?1:0; return dir=='asc'?c:-c; }
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
/* 狀態燈內的計數:白字+半透明白底,避免「紅底紅字」在彩色橫幅上看不見。
   選擇器同時涵蓋 .cnt 與任何 <b>,舊版已產生的 HTML 也能被修正(含行內 color 也蓋掉)。 */
.status-desc b, .status-desc .cnt { display: inline-block; color: #fff !important;
  background: rgba(255,255,255,.25); border: 1px solid rgba(255,255,255,.45);
  border-radius: 999px; padding: 1px 10px; margin-left: 6px;
  font-variant-numeric: tabular-nums; }
.stream { display: flex; flex-direction: column; gap: 2px; }
.stream-item { display: flex; align-items: center; gap: 10px; padding: 8px 6px;
  border-bottom: 1px solid #f1f5f9; font-size: .92rem; flex-wrap: wrap; }
.stream-date { color: #64748b; font-variant-numeric: tabular-nums; }
.stream-stock { font-weight: 700; color: #334155; }
.stream-stock a { color:#1d4ed8; text-decoration:none; }
.stream-msg { color: #475569; }
.event-summary { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
.event-summary span { padding:5px 10px; border-radius:999px; background:#f1f5f9;
  color:#475569; font-size:.8rem; }
.event-summary .event-up { background:#dcfce7; color:#166534; }
.event-summary .event-down { background:#fee2e2; color:#991b1b; }
.history-events { margin-top:8px; }
.history-events summary { cursor:pointer; color:#475569; font-weight:700; padding:8px 4px; }
.stream-empty { color: #64748b; background: #f8fafc; border: 1px dashed #cbd5e1;
  border-radius: 8px; padding: 20px; text-align: center; }
.table-warn { background: #fffbeb; border: 1px solid #fde68a; color: #92400e;
  padding: 10px 12px; border-radius: 8px; font-size: .9rem; margin-bottom: 10px; }
.table-scroll { overflow-x: auto; }
table#scan { border-collapse: collapse; width: 100%; font-size: .92rem; min-width: 640px; }
table#scan th { background: #f1f5f9; padding: 10px 8px; text-align: right; cursor: pointer;
  white-space: nowrap; position: sticky; top: 0; user-select: none; }
table#scan th button { border:0; background:transparent; color:#334155; font:inherit; font-weight:700;
  padding:0; cursor:pointer; }
table#scan th button:focus-visible { outline:2px solid #2563eb; outline-offset:3px; border-radius:3px; }
table#scan th:nth-child(1), table#scan th:nth-child(2){ text-align: left; }
table#scan td { padding: 9px 8px; border-bottom: 1px solid #eef2f7; }
table#scan td.num { text-align: right; font-variant-numeric: tabular-nums; }
table#scan td.name a { color: #2563eb; text-decoration: none; font-weight: 600; }
table#scan tbody tr:hover { background: #f8fafc; }
.verdict { font-size: .72rem; color: #64748b; margin-left: 6px; }
.stock-cards { display:none; }
.stock-card { color:#1f2937; text-decoration:none; border:1px solid #e2e8f0; border-radius:14px;
  padding:13px; background:#fff; box-shadow:0 2px 8px rgba(15,23,42,.05); }
.stock-card-head,.stock-card-foot { display:flex; justify-content:space-between; gap:10px; align-items:center; }
.stock-card-head b { color:#1d4ed8; margin-right:7px; }
.stock-card-head small,.stock-card-price small { color:#64748b; }
.stock-card-price { font-size:1.35rem; font-weight:800; margin:9px 0; }
.stock-card-price small { display:block; font-size:.72rem; font-weight:500; }
.stock-card-metrics { display:grid; grid-template-columns:1fr 1fr; gap:7px; }
.stock-card-metrics span { background:#f8fafc; padding:7px 8px; border-radius:8px; font-size:.78rem; }
.stock-card-metrics b { display:block; color:#334155; font-size:.95rem; }
.stock-card-foot { margin-top:9px; font-size:.76rem; color:#64748b; }
.stock-card-unavailable { margin-top:9px; padding-top:8px; border-top:1px dashed #cbd5e1;
  color:#64748b; font-size:.76rem; }
.stock-card-foot [class^="flag-"] { font-weight:700; }
.flag-red { color:#b91c1c; }.flag-green { color:#15803d; }.flag-yellow { color:#a16207; }.flag-na { color:#64748b; }
a.back { color: #2563eb; text-decoration: none; font-size: .9rem; }
table.tbl { border-collapse: collapse; width: 100%; font-size: .9rem; min-width: 560px; }
table.tbl th { background: #f1f5f9; padding: 9px 8px; text-align: left; white-space: nowrap; }
table.tbl td { padding: 9px 8px; border-bottom: 1px solid #eef2f7; }
table.tbl td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.tbl tbody tr:hover { background: #f8fafc; }
.price-level-table { min-width: 1500px !important; }
.price-level-table small { color:#64748b; font-weight:500; }
.price-level-warning { line-height:1.65; border-left:5px solid #f59e0b; }
code { background: #f1f5f9; padding: 1px 5px; border-radius: 4px; font-size: .85em; }
.screener-cta { display: block; background: linear-gradient(135deg,#065f46,#047857); color: #fff;
  border-radius: 14px; padding: 16px 18px; margin: 14px 0; text-decoration: none;
  box-shadow: 0 6px 18px rgba(4,120,87,.20); }
.screener-cta b { color: #fff; }
.screener-cta .arrow { float: right; opacity: .8; font-size: 1.3rem; }
.ai-cta { display:block; background:linear-gradient(135deg,#1e3a8a,#4338ca); color:#fff;
  border-radius:14px; padding:16px 18px; margin:14px 0; text-decoration:none;
  box-shadow:0 6px 18px rgba(49,46,129,.22); }
.ai-cta b { color:#fff; }
.ai-cta .arrow { float:right; opacity:.8; font-size:1.3rem; }
.research-links { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.research-links .screener-cta,.research-links .ai-cta { margin:12px 0 0; }
.ai-wrap { max-width:1180px; }
.capex-hero { border-top:4px solid #2563eb; }
.chain-flow { margin:18px 0; }
.chain-arrow { text-align:center; color:#64748b; font-size:1.35rem; line-height:1; margin:7px 0; }
.chain-layer { background:#fff; border:1px solid #e2e8f0; border-radius:14px; overflow:hidden;
  box-shadow:0 4px 14px rgba(15,23,42,.06); }
.chain-layer summary { display:flex; align-items:center; gap:12px; cursor:pointer; padding:15px 18px;
  font-weight:800; color:#0f172a; list-style:none; }
.chain-layer summary::-webkit-details-marker { display:none; }
.chain-layer summary::after { content:'＋'; margin-left:auto; color:#64748b; }
.chain-layer[open] summary::after { content:'−'; }
.layer-no { display:inline-grid; place-items:center; width:36px; height:36px; border-radius:10px;
  background:#e0e7ff; color:#3730a3; font-variant-numeric:tabular-nums; }
.layer-summary { margin-left:auto; margin-right:10px; font-size:.82rem; font-weight:500; color:#64748b; }
.layer-body { border-top:1px solid #eef2f7; padding:12px 18px 18px; }
.ai-table { min-width:850px; }
.ai-guidance { min-width:1050px; }
.guidance-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:12px; }
.guidance-card { border:1px solid #dbe3ee; border-radius:14px; padding:15px; background:#fff;
  box-shadow:0 3px 10px rgba(15,23,42,.05); min-width:0; }
.guidance-head { display:flex; align-items:center; justify-content:space-between; gap:10px; }
.logo-name,.ticker-logo { display:inline-flex; align-items:center; gap:8px; }
.company-logo { display:inline-grid; place-items:center; width:30px; height:30px; flex:0 0 30px;
  border-radius:8px; background:#fff; border:1px solid #e2e8f0; overflow:hidden; vertical-align:middle; }
.company-logo img { width:24px; height:24px; object-fit:contain; }
.logo-fallback { display:grid; place-items:center; width:100%; height:100%; background:#e0e7ff;
  color:#3730a3; font-size:.68rem; font-weight:850; }
.logo-fallback[hidden] { display:none !important; }
.ticker-logo .company-logo { width:26px; height:26px; flex-basis:26px; }
.ticker-logo .company-logo img { width:21px; height:21px; }
.guidance-head b { font-size:1.05rem; color:#0f172a; }
.quote-box { display:grid; grid-template-columns:1fr auto; gap:1px 10px; margin-top:10px;
  padding:8px 10px; border-radius:9px; background:#f8fafc; border:1px solid #e2e8f0;
  color:#334155; text-decoration:none; font-variant-numeric:tabular-nums; }
.quote-box b { font-size:.95rem; color:#0f172a; }
.quote-box span { font-size:.8rem; font-weight:800; text-align:right; }
.quote-box small { grid-column:1 / -1; color:#64748b; font-size:.7rem; }
.quote-box.up span { color:#15803d; }.quote-box.down span { color:#b91c1c; }.quote-box.flat span { color:#64748b; }
.quote-box.compact { display:inline-grid; min-width:122px; margin:0; padding:5px 7px; }
.quote-box.compact b { font-size:.82rem; }.quote-box.compact span { font-size:.72rem; }
.quote-cell { min-width:130px; }.quote-na { color:#64748b; font-size:.78rem; }
.direction { border-radius:999px; padding:2px 9px; font-size:.76rem; font-weight:700; white-space:nowrap; }
.direction.up { color:#166534; background:#dcfce7; }
.direction.flat { color:#475569; background:#f1f5f9; }
.direction.na { color:#92400e; background:#fef3c7; }
.guidance-amount { font-size:1.45rem; font-weight:850; color:#1e3a8a; margin-top:10px; line-height:1.25; }
.guidance-period { color:#64748b; font-size:.85rem; margin-top:2px; }
.guidance-compare { display:inline-block; margin-top:9px; padding:3px 9px; border-radius:7px; font-size:.76rem; font-weight:700; }
.guidance-compare.yes { color:#166534; background:#ecfdf5; }
.guidance-compare.no { color:#9a3412; background:#fff7ed; }
.guidance-point { margin:10px 0 0; color:#334155; line-height:1.45; }
.guidance-extra { list-style:none; margin:10px 0 0; padding:9px 10px; border-radius:9px; background:#f8fafc; }
.guidance-extra li { margin:4px 0; color:#334155; }
.guidance-extra span { float:right; color:#64748b; font-size:.8rem; }
.guidance-actual { margin-top:10px; padding:9px 10px; border-left:3px solid #0ea5e9; background:#f0f9ff;
  border-radius:0 8px 8px 0; color:#0c4a6e; }
.guidance-actual span,.guidance-actual small { color:#475569; }
.guidance-date { color:#94a3b8; font-size:.76rem; margin-top:10px; }
.guidance-source { margin-top:10px; font-size:.8rem; color:#64748b; }
.guidance-source summary { cursor:pointer; color:#64748b; font-weight:700; }
.guidance-source ul { padding-left:18px; margin:7px 0 0; }
.guidance-source li { margin:7px 0; }
.guidance-source li span { color:#64748b; }
.output-side { margin-top:32px; border-top:5px solid #0f766e; }
.output-title { display:flex; justify-content:space-between; gap:18px; align-items:flex-start; flex-wrap:wrap; }
.output-title > div > span { color:#0f766e; font-size:.72rem; font-weight:900; letter-spacing:.15em; }
.output-title h2 { margin:2px 0 0; }
.output-summary { display:flex; gap:6px; flex-wrap:wrap; }
.output-summary b { background:#f1f5f9; color:#475569; padding:5px 9px; border-radius:999px; font-size:.78rem; }
.output-summary b.up { background:#dcfce7; color:#166534; }
.output-summary b.down { background:#fee2e2; color:#991b1b; }
.output-summary b.missing { background:#fef3c7; color:#92400e; }
.output-thesis { line-height:1.7; color:#334155; background:#f0fdfa; border-left:4px solid #14b8a6;
  padding:10px 12px; border-radius:0 9px 9px 0; }
.output-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
.output-card { border:1px solid #dbe3ee; border-radius:12px; padding:13px; background:#fff; }
.output-card h4 { margin:10px 0 8px; font-size:.95rem; color:#0f172a; }
.logo-name small { display:block; color:#94a3b8; font-size:.7rem; font-weight:500; }
.direction.down { color:#991b1b; background:#fee2e2; }
.direction.missing { color:#92400e; background:#fef3c7; }
.output-values { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.output-values > div { background:#f8fafc; border-radius:8px; padding:8px; min-width:0; }
.output-values span,.output-values small { display:block; color:#94a3b8; font-size:.7rem; }
.output-values b { display:block; margin:3px 0; color:#1e3a8a; font-size:1.05rem; overflow-wrap:anywhere; }
.output-change { margin:9px 0 0; color:#475569; font-size:.8rem; line-height:1.45; }
.output-warning { margin-top:12px; }
.thesis-section { border:2px solid #cbd5e1; }
.thesis-title { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.thesis-title h2 { margin:0; }
.thesis-assumption { margin:14px 0; padding:14px; border-radius:10px; background:#eff6ff; }
.thesis-assumption span,.thesis-assumption b { display:block; }
.thesis-assumption span { color:#64748b; font-size:.78rem; margin-bottom:4px; }
.thesis-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:12px; }
.thesis-condition { border:1px solid #e2e8f0; border-left:5px solid; border-radius:10px; padding:12px; }
.thesis-condition-head { display:flex; justify-content:space-between; gap:10px; }
.thesis-condition-head span { white-space:nowrap; font-weight:700; font-size:.8rem; }
.thesis-current { margin:9px 0 4px; font-weight:700; color:#1e3a8a; }
.thesis-condition p { margin:4px 0; color:#475569; font-size:.82rem; }
.thesis-validation,.thesis-updated { margin-top:7px; color:#64748b; font-size:.74rem; }
.thesis-position { display:flex; flex-wrap:wrap; gap:7px 16px; margin-top:12px; padding:12px; border-radius:10px; background:#f8fafc; }
.thesis-position b,.thesis-position small { flex-basis:100%; }
.ai-table td.name a { color:#2563eb; text-decoration:none; font-weight:700; }
.unavailable { color:#94a3b8; }
.cycle-tag { display:inline-block; border:1px solid; border-radius:999px; padding:1px 8px;
  font-size:.76rem; font-weight:700; white-space:nowrap; }
.transmission { margin-top:10px; padding:9px 11px; background:#f8fafc; border-radius:8px;
  color:#475569; font-size:.86rem; }
.search-box { position: relative; margin: 14px 0; }
.search-box input { width: 100%; box-sizing: border-box; padding: 13px 16px; font-size: 1rem;
  border: 2px solid #cbd5e1; border-radius: 12px; outline: none; background: #fff; }
.search-box input:focus { border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
.os-suggest { display: none; position: absolute; z-index: 20; left: 0; right: 0; top: 100%;
  margin-top: 4px; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
  box-shadow: 0 12px 28px rgba(0,0,0,.14); overflow: hidden; }
.os-item { padding: 11px 16px; cursor: pointer; border-bottom: 1px solid #f1f5f9; }
.os-item:hover { background: #eff6ff; }
.os-item:focus-visible { outline:2px solid #2563eb; outline-offset:-2px; background:#eff6ff; }
.os-item b { color: #1d4ed8; font-variant-numeric: tabular-nums; }
.os-go { float: right; color: #2563eb; font-size: .85rem; opacity: .85; }
.os-none { padding: 14px 16px; color: #64748b; font-size: .9rem; }
.os-none a { color: #2563eb; }
.stock-filters { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:-5px 0 10px; }
.stock-filters select { border:1px solid #cbd5e1; background:#fff; border-radius:9px; padding:8px 10px;
  color:#334155; font-size:.86rem; }
.stock-filters span { margin-left:auto; color:#64748b; font-size:.82rem; }
.site-disclosure { margin-top:8px; }
.site-disclosure > summary { cursor:pointer; color:#64748b; font-size:.84rem; }
.notice { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e3a8a;
  padding: 10px 12px; border-radius: 8px; font-size: .9rem; margin: 10px 0 4px; line-height: 1.6; }
.pe-position { background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px;
  padding:10px 12px; margin:2px 0 10px; color:#334155; font-size:.9rem;
  font-variant-numeric:tabular-nums; }
.wi-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 12px 0 6px; }
.wi-bar label { font-weight: 700; color: #334155; }
.wi-bar input { padding: 10px 12px; font-size: 1.05rem; border: 2px solid #cbd5e1; border-radius: 10px;
  width: 150px; font-variant-numeric: tabular-nums; }
.wi-bar input:focus { outline: none; border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
.wi-bar button { padding: 10px 16px; font-size: .95rem; font-weight: 700; color: #fff; background: #2563eb;
  border: 0; border-radius: 10px; cursor: pointer; }
.wi-bar button.wi-sec { background: #64748b; }
.wi-diff { color: #475569; font-size: .9rem; margin: 6px 2px; }
.wi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 6px; }
.wi-card { border: 1px solid #eef2f7; border-radius: 12px; padding: 12px; background: #fff; }
.wi-name { font-size: .82rem; color: #6b7280; }
.wi-val { font-size: 1.6rem; font-weight: 800; margin: 2px 0 6px; font-variant-numeric: tabular-nums; }
.wi-badge { display: inline-block; color: #fff; font-size: .75rem; font-weight: 700; padding: 2px 10px; border-radius: 999px; }
.wi-note { font-size: .78rem; color: #64748b; margin-top: 6px; }
.wi-none { color: #64748b; }
.quick-summary { border:1px solid #dbeafe; background:linear-gradient(180deg,#fff,#f8fbff); }
.quick-title { display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
.quick-title h2 { border:0; margin:0; padding:0; }
.quick-title small { color:#64748b; }
.quick-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:9px; margin-top:12px; }
.quick-grid > div { border:1px solid #e2e8f0; background:#fff; border-radius:10px; padding:11px; min-width:0; }
.quick-grid span,.quick-grid small,.quick-grid b { display:block; }
.quick-grid span { color:#64748b; font-size:.74rem; }
.quick-grid b { margin:4px 0; font-size:.96rem; overflow-wrap:anywhere; }
.quick-grid small { color:#64748b; font-size:.7rem; }
.sr-only { position:absolute!important; width:1px!important; height:1px!important; padding:0!important;
  margin:-1px!important; overflow:hidden!important; clip:rect(0,0,0,0)!important; white-space:nowrap!important; border:0!important; }
.swipe-hint { display: none; }
@media (max-width: 640px) {
  .swipe-hint { display: block; font-size: .72rem; color: #94a3b8; margin: 2px 2px 5px; text-align: right; }
  table#scan { display:none; }
  .table-scroll:has(table#scan), .swipe-hint + .table-scroll:has(table#scan) { display:none; }
  .stock-cards { display:grid; grid-template-columns:1fr; gap:9px; }
  .scan-swipe { display:none; }
  .layer-tag { margin: 14px 4px 6px; }
  .research-links { grid-template-columns:1fr; }
  .screener-cta { padding: 14px; }
  .ai-cta { padding:14px; }
  .quick-grid { grid-template-columns:1fr 1fr; }
  .event-summary span { flex:1 1 calc(50% - 8px); text-align:center; }
  .chain-layer summary { align-items:flex-start; flex-wrap:wrap; padding:13px; }
  .layer-summary { flex-basis:100%; margin:0 0 0 48px; }
  .layer-body { padding:10px 12px 14px; }
  .guidance-grid { grid-template-columns:1fr; gap:10px; }
  .guidance-card { padding:13px; }
  .guidance-amount { font-size:1.3rem; }
  .guidance-extra span { float:none; display:block; margin-top:2px; }
  .output-grid { grid-template-columns:1fr; }
  .output-values b { font-size:1rem; }
  .output-card .guidance-head { flex-wrap:wrap; }
  .output-card .direction { white-space:normal; max-width:100%; text-align:center; }
  .thesis-grid { grid-template-columns:1fr; }
  .thesis-condition-head { align-items:flex-start; }
}
"""
