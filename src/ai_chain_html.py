"""AI 產業鏈全景圖 HTML。"""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import plotly.graph_objects as go

from .dashboard_html import _fig_div, _layout
from .site_html import _page


FLAG = {
    "green": ("🟢", "合理偏低", "#16a34a"),
    "yellow": ("🟡", "一般", "#a16207"),
    "red": ("🔴", "高估值警戒", "#dc2626"),
    "na": ("⚪", "資料不足", "#9ca3af"),
}
CYCLE = {"cyclical": ("循環", "#b45309"), "non_cyclical": ("非循環", "#475569"),
         "unknown": ("未知", "#94a3b8")}
DIRECTION = {"up": "上調", "down": "下調", "unchanged": "維持不變",
             "yoy_increase": "預期年增（非指引上調）", "not_stated": "未說明／無指引"}
BASIS = {"calendar_year": "日曆年", "fiscal_year": "會計年度", "quarter": "單季"}
OUTPUT_DIRECTION = {
    "accel": ("▲ 加速", "up"), "decel": ("▼ 減速", "down"), "flat": ("— 持平", "flat"),
    "not_disclosed": ("本季未揭露", "missing"), "pending": ("待輸入", "na"),
    "insufficient": ("前期／口徑不足", "na"),
}


def _n(v, dp=1, suffix=""):
    return "—" if v is None else f"{v:,.{dp}f}{suffix}"


def _guidance_amount(entry: dict) -> str:
    a = entry["amount"]
    unit = escape(a.get("unit", ""))
    if a["kind"] == "approximate":
        return f"約 {_n(a['value'], 1)} {unit}"
    if a["kind"] == "minimum":
        return f"超過 {_n(a['value'], 1)} {unit}"
    if a["kind"] == "range":
        return f"{_n(a['low'], 1)}–{_n(a['high'], 1)} {unit}"
    return escape(a.get("text") or "未揭露具體數字")


def _auto_ttm_capex(cloud: dict, ticker: str) -> dict | None:
    return (cloud.get("companies", {}).get(ticker) or {}).get("ttm_capex")


def _logo(sid: str, name: str, logos: dict) -> str:
    meta = logos.get(sid) or {}
    src = escape(meta.get("file") or f"assets/logos/{sid}.png")
    first = (name.strip()[:1] if name and ord(name.strip()[0]) > 127 else sid[:2]).upper()
    return (f'<span class="company-logo"><img src="{src}" alt="" loading="lazy" '
            f'onerror="this.hidden=true;this.nextElementSibling.hidden=false">'
            f'<span class="logo-fallback" hidden>{escape(first)}</span></span>')


def _quote_url(ticker: str, market: str = "us") -> str:
    if market in ("twse", "tpex"):
        suffix = "TW" if market == "twse" else "TWO"
        return f"https://tw.stock.yahoo.com/quote/{escape(ticker)}.{suffix}"
    return f"https://finance.yahoo.com/quote/{escape(ticker)}/"


def _quote_html(ticker: str, quote: dict | None, compact: bool = False,
                detail_url: str | None = None, market: str = "us") -> str:
    if not quote:
        return '<span class="quote-na">行情 N/M</span>'
    pct = float(quote["change_pct"])
    display_pct = round(pct, 1)
    cls = "up" if display_pct > 0 else "down" if display_pct < 0 else "flat"
    arrow = "▲" if display_pct > 0 else "▼" if display_pct < 0 else "—"
    pct_text = f"{display_pct:+.1f}%" if display_pct else "0.0%"
    compact_class = " compact" if compact else ""
    close = float(quote["close"])
    close_date = escape(str(quote["close_date"]))
    currency = "NT$" if quote.get("currency") == "TWD" else "US$"
    if detail_url:
        link = f'href="{escape(detail_url)}" title="{escape(ticker)} 個股詳情"'
        destination = "站內個股詳情"
    else:
        link = (f'href="{_quote_url(ticker, market)}" target="_blank" rel="noopener" '
                f'title="最近交易日收盤；不是即時報價"')
        destination = "Yahoo（另開視窗）"
    body = (f'<a class="quote-box {cls}{compact_class}" data-quote-ticker="{escape(ticker)}" '
            f'data-quote-market="{escape(market)}" data-quote-date="{close_date}" {link} '
            f'aria-label="{escape(ticker)} 最近收盤 {currency} {close:,.2f}，單日漲跌 {pct_text}，'
            f'{close_date}，前往 {destination}">'
            f'<b>{currency} {close:,.2f}</b>'
            f'<span>{arrow} {pct_text}</span>'
            f'<small>{close_date}</small></a>')
    return body


def _quote_update_meta(label: str, updated_at: str, quotes: dict) -> str:
    try:
        updated = datetime.fromisoformat(updated_at).astimezone(ZoneInfo("Asia/Taipei"))
        update_text = updated.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        update_text = "N/M"
    dates = sorted({str(q.get("close_date")) for q in quotes.values() if q.get("close_date")})
    if not dates:
        close_text = "N/M"
    elif len(dates) == 1:
        close_text = dates[0]
    else:
        close_text = f"{dates[0]}–{dates[-1]}"
    return f"{label}行情更新 {update_text}（收盤日 {close_text}）"


def _guidance_card(ticker: str, company: dict, cloud: dict, logos: dict,
                   quote: dict | None = None) -> str:
    entries = company.get("entries") or []
    numeric_calendar = [x for x in entries if x["period"]["basis"] == "calendar_year"
                        and x["amount"]["kind"] != "undisclosed"]
    calendar_any = [x for x in entries if x["period"]["basis"] == "calendar_year"]
    primary = (sorted(numeric_calendar, key=lambda x: (x["period"]["label"], x["source_date"]))[-1]
               if numeric_calendar else
               sorted(calendar_any, key=lambda x: (x["period"]["label"], x["source_date"]))[-1]
               if calendar_any else None)

    if primary:
        amount = _guidance_amount(primary)
        period = escape(primary["period"]["label"])
        direction = DIRECTION[primary["direction"]]
        comparable = primary["amount"]["kind"] != "undisclosed"
        key_point = escape(primary.get("key_point") or "")
        source_date = escape(str(primary["source_date"]))
    else:
        amount, period, direction = "待補", "日曆年資料", "未填"
        comparable, key_point, source_date = False, escape(company.get("note") or ""), "—"

    extra = []
    for entry in entries:
        if entry is primary:
            continue
        extra.append(
            f'<li><b>{escape(entry["period"]["label"])}</b>　{_guidance_amount(entry)}'
            f'<span>{DIRECTION[entry["direction"]]}</span></li>')

    actual_lines = []
    for actual in company.get("reported_actuals") or []:
        auto = _auto_ttm_capex(cloud, ticker)
        line = (f'<b>TTM 實際</b> {_n(actual["amount"]["value"], 1)} {escape(actual["amount"]["unit"])}'
                f'　<span>YoY {_n(actual["yoy_pct"], 1, "%")}</span>')
        if auto:
            line += f'<br><small>yfinance 自動值 {_n(auto["value"] / 1e9, 1)} USD bn（{escape(auto["date"]) }）</small>'
        actual_lines.append(f'<div class="guidance-actual">{line}</div>')

    detail_items = []
    for entry in entries:
        detail_items.append(
            f'<li><b>{escape(entry["period"]["label"])}</b>｜{escape(entry["source"])}｜'
            f'{escape(str(entry["source_date"]))}<br><span>{escape(entry.get("note") or "—")}</span></li>')
    for actual in company.get("reported_actuals") or []:
        detail_items.append(
            f'<li><b>{escape(actual["period"]["label"])}</b>｜{escape(actual["source"])}｜'
            f'{escape(str(actual["source_date"]))}<br><span>{escape(actual.get("note") or "—")}</span></li>')
    details = (f'<details class="guidance-source"><summary>查看來源與口徑</summary><ul>{"".join(detail_items)}</ul></details>'
               if detail_items else '')

    direction_class = ("up" if primary and primary["direction"] in ("up", "yoy_increase")
                       else "flat" if primary and primary["direction"] == "unchanged" else "na")
    return f'''
    <article class="guidance-card">
      <div class="guidance-head"><div class="logo-name">{_logo(ticker, ticker, logos)}<b>{ticker}</b></div><span class="direction {direction_class}">{escape(direction)}</span></div>
      {_quote_html(ticker, quote)}
      <div class="guidance-amount">{amount}</div>
      <div class="guidance-period">{period}</div>
      <div class="guidance-compare {'yes' if comparable else 'no'}">{'可並列（日曆年；口徑仍不同）' if comparable else '不可直接比較'}</div>
      {f'<p class="guidance-point">{key_point}</p>' if key_point else ''}
      {f'<ul class="guidance-extra">{"".join(extra)}</ul>' if extra else ''}
      {''.join(actual_lines)}
      <div class="guidance-date">資料日期 {source_date}</div>
      {details}
    </article>'''


def _output_value(metric: dict, obs: dict | None) -> str:
    if not obs:
        return "—"
    if obs["status"] == "not_disclosed":
        return "本季未揭露"
    if obs.get("display"):
        return escape(obs["display"])
    prefix = "超過 " if obs.get("kind") == "minimum" else "推算 " if obs.get("kind") == "derived" else ""
    return f"{prefix}{_n(obs.get('value'), 1)} {escape(metric['unit'])}"


def _output_period(obs: dict | None) -> str:
    if not obs:
        return "—"
    period = escape(str(obs["period"]))
    if obs.get("period_end"):
        return f"{period}（截至 {escape(str(obs['period_end']))}）"
    return period


def _output_side(data: dict, logos: dict, quotes: dict) -> str:
    out = data.get("output_side") or {"metrics": [], "counts": {}}
    c = out.get("counts") or {}
    cards = []
    for metric in out.get("metrics") or []:
        latest, previous = metric.get("latest"), metric.get("previous")
        direction, cls = OUTPUT_DIRECTION.get(metric.get("direction"), ("資料不足", "na"))
        company = metric["company"]
        if metric.get("direction") == "not_disclosed":
            streak = metric.get("non_disclosure_streak", 0)
            direction = f"本季未揭露（連續 {streak} 季）"
        elif metric.get("direction") == "insufficient" and metric.get("direction_reason"):
            direction = metric["direction_reason"]
        source = ""
        observations = sorted(metric.get("observations") or [],
                              key=lambda x: x.get("calendar_period") or x["period"], reverse=True)
        if observations:
            source_rows = "".join(
                f'<li><b>{_output_period(x)}</b>｜{escape(x["source"])}｜'
                f'{escape(str(x["disclosure_date"]))}｜{_output_value(metric, x)}</li>'
                for x in observations)
            method = ("<p>level 型的加速/減速使用連續三季，比較本季成長率與前季成長率。</p>"
                      if metric["value_type"] == "level" else "")
            if metric.get("period_basis") == "fiscal_quarter":
                method += "<p>保留公司原生財季與截止日；calendar_period 只供排序，不冒充日曆季。</p>"
            else:
                method += "<p>季度軸使用日曆季；會計季揭露須先映射到對應日曆季。</p>"
            method += "<p>下限值（超過 X）只顯示方向，不當成精確值計算加速或減速。</p>"
            source = (f'<details class="guidance-source"><summary>查看來源與判定</summary>{method}'
                      f'<ul>{source_rows}</ul>'
                      f'{("<p>" + escape(metric.get("note")) + "</p>") if metric.get("note") else ""}</details>')
        elif metric.get("note"):
            source = (f'<details class="guidance-source"><summary>指標口徑</summary><p>'
                      f'{escape(metric["note"])}</p></details>')
        cards.append(f'''
        <article class="output-card" data-output-direction="{escape(str(metric.get('direction') or ''))}">
          <div class="guidance-head"><div class="logo-name">{_logo(company, metric.get('company_name', company), logos)}
            <div><b>{escape(metric.get('company_name', company))}</b><small>{company}</small></div></div>
            <span class="direction {cls}">{escape(direction)}</span></div>
          {_quote_html(company, quotes.get(company))}
          <h4>{escape(metric['name'])}</h4>
          <div class="output-values">
            <div><span>最新</span><b>{_output_value(metric, latest)}</b><small>{_output_period(latest) if latest else '尚未輸入'}</small></div>
            <div><span>前期</span><b>{_output_value(metric, previous)}</b><small>{_output_period(previous)}</small></div>
          </div>
          {f'<p class="output-change">{escape(latest.get("change_text"))}</p>' if latest and latest.get('change_text') else ''}
          {source}
        </article>''')

    return f'''
    <section class="output-side" data-output-metrics="{len(out.get('metrics') or [])}" data-output-accel="{c.get('accel', 0)}" data-output-decel="{c.get('decel', 0)}" data-output-flat="{c.get('flat', 0)}" data-output-not-disclosed="{c.get('not_disclosed', 0)}" data-output-insufficient="{c.get('insufficient', 0)}" data-output-pending="{c.get('pending', 0)}">
      <div class="output-title"><div><span>OUTPUT · 截至 {escape(out.get('as_of_period', '—'))}</span><h2>產出側：AI 是否產生經濟價值</h2></div>
        <div class="output-summary"><b class="up">▲ {c.get('accel', 0)} 加速</b><b class="down">▼ {c.get('decel', 0)} 減速</b>
          <b>— {c.get('flat', 0)} 持平</b><b class="missing">{c.get('not_disclosed', 0)} 未揭露</b><b>{c.get('insufficient', 0)} 前期／口徑不足</b>
          <b>{c.get('pending', 0)} 待輸入</b></div></div>
      <p class="output-thesis"><b>上游 Capex 是投入，產出側是回收。</b>兩側都健康代表循環可持續；
        投入持續增加而產出側整體減速，是需求論述出現裂縫的早期訊號。
        單一公司減速多為競爭問題，需整組同時轉向才具意義。<br>
        <small>這是待驗證的研究假說；目前彙總是指標數，不是公司層級或產業整體判定。</small></p>
      <div class="output-grid">{''.join(cards)}</div>
      <div class="warn output-warning"><b>限制:</b>這些指標為公司自選揭露，口徑不一致、可比性有限；
        {escape(out.get('scale_warning') or '')}不可單獨代表產業；多數為同步指標而非領先指標。
        規模警語來源狀態:{escape(out.get('scale_warning_source') or '未提供')}。
        上方彙總計算的是<b>指標數</b>,不是公司數或可加總的產業指數；同一公司多項指標會分別計數。
        「待輸入」不等於公司未揭露；只有已填入 <code>not_disclosed</code> 的季度才計入連續未揭露。</div>
    </section>'''


def _capex_fig(cloud: dict) -> str:
    rows = cloud.get("combined") or []
    yoy = {x["quarter"]: x["yoy"] for x in cloud.get("yoy") or []}
    if not rows:
        return '<div class="stream-empty">四大雲端季度 Capex 資料不足。</div>'
    x = [r["quarter"] for r in rows]
    fig = go.Figure()
    custom = [f"{r.get('n', 0)}/4 公司" for r in rows]
    colors = ["#2563eb" if r.get("n") == 4 else "#f59e0b" for r in rows]
    fig.add_trace(go.Bar(x=x, y=[r["value"] / 1e9 for r in rows], name="可得資料合計 Capex(十億美元)",
                         customdata=custom, marker_color=colors,
                         text=["" if r.get("n") == 4 else f"{r.get('n')}/4" for r in rows],
                         textposition="outside",
                         hovertemplate="%{x}<br>Capex %{y:.1f} B<br>%{customdata}<extra></extra>"))
    fig.add_trace(go.Scatter(x=x, y=[yoy.get(q) for q in x], name="YoY(%)", mode="lines+markers",
                             yaxis="y2", line=dict(color="#dc2626", width=3),
                             hovertemplate="%{x}<br>YoY %{y:.1f}%<extra></extra>"))
    if not yoy:
        fig.add_annotation(x=0.99, y=0.98, xref="paper", yref="paper", xanchor="right", yanchor="top",
                           text="YoY樣本不足:需前後兩期皆含四家公司", showarrow=False,
                           bgcolor="#fff7ed", bordercolor="#fdba74", font=dict(color="#9a3412", size=12))
    fig.update_layout(yaxis=dict(title="Capex (USD bn)"),
                      yaxis2=dict(title="YoY (%)", overlaying="y", side="right", showgrid=False),
                      legend=dict(orientation="h", y=1.15))
    return _fig_div(_layout(fig, height=370))


def _flag_html(flag: str) -> str:
    em, lab, col = FLAG.get(flag, FLAG["na"])
    return f'<span style="color:{col};font-weight:700;white-space:nowrap">{em}{lab}</span>'


def _cycle_html(cycle: dict) -> str:
    state = cycle.get("status", "unknown")
    lab, col = CYCLE.get(state, CYCLE["unknown"])
    title = cycle.get("reason") or (
        f"三取二命中 {cycle.get('hits', 0)}；毛利率std={_n(cycle.get('gm_std'))}pp、"
        f"EPS最大年減={_n(cycle.get('worst_eps_yoy'))}%、營收YoY振幅={_n(cycle.get('revenue_yoy_range'))}pp")
    return f'<span class="cycle-tag" style="color:{col};border-color:{col}" title="{escape(title)}">{lab}</span>'


def _node_row(node: dict, detail_ids: set[str], logos: dict) -> str:
    m = node["member"]
    r = node.get("result")
    quote = node.get("quote")
    detail_url = f'stock_{escape(m["id"])}.html' if m["id"] in detail_ids else None
    name = escape(m["name"])
    if detail_url:
        name = f'<a href="{detail_url}">{name}</a>'
    elif m["market"] in ("us", "twse", "tpex"):
        name = (f'<a href="{_quote_url(m["id"], m["market"])}" target="_blank" '
                f'rel="noopener">{name}</a>')
    if not r:
        reason = escape(node.get("unavailable") or "資料不足")
        return (f'<tr class="unavailable"><td>{_logo(m["id"], m["name"], logos)} {escape(m["id"])}</td>'
                f'<td>{name}</td><td>{escape(m["market"])}</td>'
                f'<td class="quote-cell">{_quote_html(m["id"], quote, True, detail_url, m["market"])}</td>'
                f'<td colspan="5">⚠ 不納入:{reason}</td><td>{_cycle_html(node["cycle"])}</td></tr>')
    x = r.metrics
    trend = {"accel": "▲加速", "decel": "▼減速", "flat": "—持平"}.get(x.get("mrev_trend"), "—")
    return (
        f'<tr><td><span class="ticker-logo">{_logo(m["id"], m["name"], logos)}<b>{escape(m["id"])}</b></span></td><td class="name">{name}</td><td title="{escape(x.get("pe_basis_label", ""))}">{escape(m["market"])}</td>'
        f'<td class="quote-cell">{_quote_html(m["id"], quote, True, detail_url, m["market"])}</td>'
        f'<td class="num">{_n(x.get("trailing_pe"), 1, "x")}</td>'
        f'<td class="num">{_n(x.get("pe_pct"), 0, "%")}</td><td>{_flag_html(x.get("flag", "na"))}</td>'
        f'<td class="num">{_n(x.get("mrev_yoy_recent"), 1, "%")} {trend}</td>'
        f'<td class="num">{x.get("coverage") if x.get("coverage") is not None else "—"}</td>'
        f'<td>{_cycle_html(node["cycle"])}</td></tr>'
    )


def _transmission_text(t: dict) -> str:
    if t.get("status") != "ok":
        max_n = max((x.get("n", 0) for x in t.get("results") or []), default=0)
        return (f"樣本不足或相關性未達顯著。最大可配對季數 {max_n};"
                "不據此宣稱 Capex 會在幾季後傳導。")
    b = t["best"]
    return (f"最佳落後期 {b['lag']} 季:r={b['r']:.2f}, p={b['p']:.3f}, n={b['n']}"
            f"(多重檢定門檻 p<{t.get('p_cut', 0):.3f})。僅為相關,不代表因果。")


def build_ai_chain_page(data: dict, generated: str, detail_ids: set[str]) -> str:
    cloud = data["cloud"]
    logos = data.get("logos") or {}
    guidance = data.get("guidance") or {}
    errors = cloud.get("errors") or {}
    quotes = data.get("quotes") or {}
    us_quotes = data.get("us_quotes") or {}
    tw_quotes = data.get("tw_quotes") or {}
    quote_updates = data.get("quote_updates") or {}
    guidance_cards = []
    for ticker in ("MSFT", "GOOGL", "AMZN", "META"):
        company = guidance.get(ticker) or {}
        guidance_cards.append(_guidance_card(ticker, company, cloud, logos, quotes.get(ticker)))

    layer_html = []
    for i, layer in enumerate(data["layers"], 1):
        rows = "".join(_node_row(x, detail_ids, logos) for x in layer["nodes"])
        summary = (f"平均月營收YoY <b>{_n(layer.get('avg_mrev_yoy'), 1, '%')}</b>"
                   f"(n={layer.get('mrev_n', 0)})　|　平均 trailing PE 百分位 "
                   f"<b>{_n(layer.get('avg_pe_pct'), 0, '%')}</b>(n={layer.get('pe_pct_n', 0)})")
        open_attr = " open" if i <= 2 else ""
        layer_html.append(f"""
        <div class="chain-arrow">↓</div>
        <details class="chain-layer"{open_attr}>
          <summary><span class="layer-no">{i:02d}</span><span>{escape(layer['name'])}</span>
            <span class="layer-summary">{summary}</span></summary>
          <div class="layer-body">
            {f'<p>{escape(layer.get("description"))}</p>' if layer.get('description') else ''}
            <div class="swipe-hint">← 手機可左右滑動看更多欄位 →</div>
            <div class="table-scroll"><table class="tbl ai-table"><thead><tr>
              <th>代號</th><th>名稱</th><th>市場</th><th>最近收盤</th><th>trailing PE</th><th>PE百分位</th>
              <th>估值旗標</th><th>月營收動能</th><th>共識覆蓋</th><th>循環標記</th>
            </tr></thead><tbody>{rows}</tbody></table></div>
            <div class="transmission"><b>Capex 傳導檢查:</b>{escape(_transmission_text(layer['transmission']))}</div>
          </div>
        </details>""")

    source_note = "、".join(f"{k}:{v}" for k, v in errors.items()) or "四家公司皆取得資料"
    body = f"""
<div class="wrap ai-wrap" data-ai-capex-companies="{cloud.get('available_companies', 0)}" data-ai-layers="{len(data['layers'])}" data-ai-unavailable="{len(data['unavailable'])}" data-ai-quote-tickers="{len(quotes)}" data-ai-us-quote-tickers="{len(us_quotes)}" data-ai-tw-quote-tickers="{len(tw_quotes)}">
  <header>
    <div><a class="back" href="index.html">← 回總表</a></div>
    <h1>AI 產業鏈全景圖</h1>
    <div class="meta">頁面建置 {escape(generated)}<br>
      {escape(_quote_update_meta('台股', quote_updates.get('tw', ''), tw_quotes))}<br>
      {escape(_quote_update_meta('美股', quote_updates.get('us', ''), us_quotes))}　|　由資金源頭往上游排列</div>
    <div class="warn">⚠️ 本圖為研究工具。層級關係描述供應鏈位置,<b>不代表營收、獲利或股價必然連動</b>；
      公司也可能跨越多個層級。估值歷史位階一律使用 trailing PE 對 trailing PE。<br>
      台股採 FinMind basic EPS 與本國發行人法定期限 fallback；美股採 Yahoo Reported EPS(調整後)與實際 earnings date。
      兩者只做各股自身歷史比較,不跨口徑混算。<br>
      <b>台股與美股行情均為最近交易日收盤價,不是即時報價；單日漲跌只供資訊,不納入基本面訊號或評分。</b></div>
  </header>

  <section class="capex-hero">
    <h2>01 需求源頭:四大雲端業者季度 Capex</h2>
    {_capex_fig(cloud)}
    {('<div class="warn"><b>目前無可比較的四家公司合計 YoY:</b>前後同季尚未同時涵蓋 MSFT、GOOGL、AMZN、META 四家。只顯示各季合計與覆蓋家數,不硬算 YoY。</div>' if not cloud.get('yoy') else '')}
    <div class="note">資料源:{escape(cloud.get('source', ''))}。yfinance 免費端目前通常只提供約 5 季;
      各季柱狀圖 hover 會顯示涵蓋幾家公司,只有前後兩期都四家齊全才計算合計 YoY。
      Capex YoY 與落後期相關性若樣本不足會直接標示,不外推。抓取狀態:{escape(source_note)}</div>
    <h3>2026 Capex 指引重點</h3>
    <div class="note"><b>跨公司只並列日曆年數字，但會計/租賃口徑仍可能不同。</b>會計年度、單季與未揭露金額的定性指引只作補充。</div>
    <div class="guidance-grid">{''.join(guidance_cards)}</div>
  </section>

  <div class="chain-flow">{''.join(layer_html)}</div>
  {_output_side(data, logos, quotes)}
  <footer>資料:FinMind、yfinance；估值與動能沿用主篩選器。缺資料標示不納入,不以替代值硬湊。公司名稱與商標權利屬各公司所有,Logo 僅作識別用途。</footer>
</div>"""
    return _page("AI 產業鏈全景圖", body, plotly=True)
