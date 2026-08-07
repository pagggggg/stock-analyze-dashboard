"""AI 產業鏈全景圖 HTML。"""

from __future__ import annotations

from html import escape

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


def _n(v, dp=1, suffix=""):
    return "—" if v is None else f"{v:,.{dp}f}{suffix}"


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


def _node_row(node: dict, detail_ids: set[str]) -> str:
    m = node["member"]
    r = node.get("result")
    if not r:
        reason = escape(node.get("unavailable") or "資料不足")
        return (f'<tr class="unavailable"><td>{escape(m["id"])}</td><td>{escape(m["name"])}</td>'
                f'<td colspan="6">⚠ 不納入:{reason}</td><td>{_cycle_html(node["cycle"])}</td></tr>')
    x = r.metrics
    name = escape(m["name"])
    if m["id"] in detail_ids:
        name = f'<a href="stock_{escape(m["id"])}.html">{name}</a>'
    trend = {"accel": "▲加速", "decel": "▼減速", "flat": "—持平"}.get(x.get("mrev_trend"), "—")
    return (
        f'<tr><td>{escape(m["id"])}</td><td class="name">{name}</td><td>{escape(m["market"])}</td>'
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
    guidance = data.get("guidance") or {}
    errors = cloud.get("errors") or {}
    guide_rows = []
    for ticker in ("MSFT", "GOOGL", "AMZN", "META"):
        g = guidance.get(ticker) or {}
        value = g.get("value")
        guide_rows.append(
            f"<tr><td>{ticker}</td><td>{_n(value, 1, ' ' + g.get('unit', '')) if value is not None else '未填'}</td>"
            f"<td>{escape(g.get('source') or '法說口頭指引無法自動抓取')}</td></tr>"
        )

    layer_html = []
    for i, layer in enumerate(data["layers"], 1):
        rows = "".join(_node_row(x, detail_ids) for x in layer["nodes"])
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
              <th>代號</th><th>名稱</th><th>市場</th><th>trailing PE</th><th>PE百分位</th>
              <th>估值旗標</th><th>月營收動能</th><th>共識覆蓋</th><th>循環標記</th>
            </tr></thead><tbody>{rows}</tbody></table></div>
            <div class="transmission"><b>Capex 傳導檢查:</b>{escape(_transmission_text(layer['transmission']))}</div>
          </div>
        </details>""")

    source_note = "、".join(f"{k}:{v}" for k, v in errors.items()) or "四家公司皆取得資料"
    body = f"""
<div class="wrap ai-wrap" data-ai-capex-companies="{cloud.get('available_companies', 0)}" data-ai-layers="{len(data['layers'])}">
  <header>
    <div><a class="back" href="index.html">← 回總表</a></div>
    <h1>AI 產業鏈全景圖</h1>
    <div class="meta">更新時間 {escape(generated)}　|　由資金源頭往上游排列</div>
    <div class="warn">⚠️ 本圖為研究工具。層級關係描述供應鏈位置,<b>不代表營收、獲利或股價必然連動</b>；
      公司也可能跨越多個層級。估值歷史位階一律使用 trailing PE 對 trailing PE。<br>
      美股免費資料的歷史 PE 以年度 EPS step approximation 近似,精度低於台股逐季 TTM；跨市場百分位不可視為完全等精度。</div>
  </header>

  <section class="capex-hero">
    <h2>01 需求源頭:四大雲端業者季度 Capex</h2>
    {_capex_fig(cloud)}
    {('<div class="warn"><b>目前無可比較的四家公司合計 YoY:</b>前後同季尚未同時涵蓋 MSFT、GOOGL、AMZN、META 四家。只顯示各季合計與覆蓋家數,不硬算 YoY。</div>' if not cloud.get('yoy') else '')}
    <div class="note">資料源:{escape(cloud.get('source', ''))}。yfinance 免費端目前通常只提供約 5 季;
      各季柱狀圖 hover 會顯示涵蓋幾家公司,只有前後兩期都四家齊全才計算合計 YoY。
      Capex YoY 與落後期相關性若樣本不足會直接標示,不外推。抓取狀態:{escape(source_note)}</div>
    <h3>法說口頭指引(人工輸入)</h3>
    <div class="table-scroll"><table class="tbl"><thead><tr><th>公司</th><th>指引值</th><th>來源/狀態</th></tr></thead>
      <tbody>{''.join(guide_rows)}</tbody></table></div>
  </section>

  <div class="chain-flow">{''.join(layer_html)}</div>
  <footer>資料:FinMind、yfinance；估值與動能沿用主篩選器。缺資料標示不納入,不以替代值硬湊。</footer>
</div>"""
    return _page("AI 產業鏈全景圖", body, plotly=True)
