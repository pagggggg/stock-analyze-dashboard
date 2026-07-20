"""
視覺化儀表板 (dashboard_html.py)
================================
把所有試算結果組成「單一 HTML 檔」(python 生成,瀏覽器直接開,免架 server)。

設計:
  - plotly.js 內嵌(離線可開,不連網也能看圖)
  - 響應式(手機瀏覽器可讀):卡片 grid 自動換行、圖表寬度 100% 自適應
  - 每張圖下方都有一段白話註解

內含 6 塊:
  0) 三行摘要(置頂,大字)
  1) 估值儀表板:4 指標卡片(便宜綠 / 合理灰 / 貴紅)
  2) 本益比河流圖(股價 vs 低/中/高本益比河道 + 現價標記)
  3) EPS 走勢(近8季實際 + 3Q26 三情境試算)
  4) 共識EPS監控(2026/2027 共識歷史折線 + 上修/下修標記)
  5) FCF 品質檢查(資本支出 vs 營收年增率雙線 + 存貨/應收/OCF 三燈號)
"""

from __future__ import annotations

from datetime import datetime

import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs

from .fcf_quality import FcfQualityResult
from .models import DashboardResult, EPSScenario, Guidance, QuarterFinancials, ValuationResult
from .river import RiverSeries

# ── 配色 ──────────────────────────────────────────────────────────────
C_CHEAP = "#16a34a"   # 便宜 / 綠
C_FAIR = "#6b7280"    # 合理 / 灰
C_PRICEY = "#ea580c"  # 偏貴 / 橘
C_EXP = "#dc2626"     # 貴 / 紅
C_NA = "#9ca3af"      # 資料不足 / 淺灰
C_PRICE = "#111827"   # 股價線 / 近黑
C_BLUE = "#2563eb"    # 中性 / 藍

_VERDICT_COLOR = {
    "便宜": C_CHEAP, "合理": C_FAIR, "偏貴": C_PRICEY, "貴": C_EXP, "資料不足": C_NA,
}
_LIGHT_COLOR = {"green": C_CHEAP, "yellow": "#eab308", "red": C_EXP, "gray": C_NA}
_LIGHT_WORD = {"green": "綠 · 健康", "yellow": "黃 · 留意", "red": "紅 · 警訊", "gray": "— · 資料不足"}


# ── 數字格式 ──────────────────────────────────────────────────────────
def _n(x: float, d: int = 1) -> str:
    return f"{x:,.{d}f}"


def _esc(s: str) -> str:
    """最小 HTML 逸脫,避免來源字串含 < > & 破版。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ── plotly 共用:套用手機友善版面 + 轉成可內嵌的 div 片段 ──────────────
def _layout(fig: go.Figure, height: int = 380, legend_top: bool = True) -> go.Figure:
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=48, r=18, t=48, b=40),
        font=dict(family="-apple-system, 'Noto Sans TC', 'Microsoft JhengHei', sans-serif", size=13),
        hovermode="x unified",
        autosize=True,
    )
    if legend_top:
        fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                      xanchor="left", x=0))
    return fig


def _fig_div(fig: go.Figure) -> str:
    return pio.to_html(
        fig, include_plotlyjs=False, full_html=False,
        config={"responsive": True, "displayModeBar": False},
        default_width="100%",
    )


def _placeholder(msg: str) -> str:
    return f'<div class="placeholder">{_esc(msg)}</div>'


# ======================================================================
# 圖表 1:本益比河流圖
# ======================================================================
def _fig_river(r: RiverSeries) -> str:
    fig = go.Figure()
    # 河道(由下到上,用 tonexty 填色):低=綠、中、高=紅
    fig.add_trace(go.Scatter(
        x=r.dates, y=r.band_low, name=f"低本益比 {r.pe_low:g}x",
        line=dict(color=C_CHEAP, width=1), hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=r.dates, y=r.band_mid, name=f"中本益比 {r.pe_mid:g}x",
        line=dict(color=C_FAIR, width=1, dash="dash"),
        fill="tonexty", fillcolor="rgba(22,163,74,0.10)", hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=r.dates, y=r.band_high, name=f"高本益比 {r.pe_high:g}x",
        line=dict(color=C_EXP, width=1), fill="tonexty",
        fillcolor="rgba(220,38,38,0.08)", hoverinfo="skip",
    ))
    # 股價線
    fig.add_trace(go.Scatter(
        x=r.dates, y=r.price, name="月收盤價",
        line=dict(color=C_PRICE, width=2.4),
        hovertemplate="%{x}<br>股價 %{y:,.0f}<extra></extra>",
    ))
    # 現價標記
    pe_txt = f"(trailing PE {r.current_pe:g}x)" if r.current_pe else ""
    fig.add_trace(go.Scatter(
        x=[r.current_date], y=[r.current_price], name="現價",
        mode="markers+text", marker=dict(color=C_EXP, size=12, line=dict(color="white", width=1.5)),
        text=[f" 現價 {r.current_price:,.0f} {pe_txt}"], textposition="top left",
        textfont=dict(color=C_EXP, size=12),
        hovertemplate="現價 %{y:,.0f}<extra></extra>",
    ))
    fig.update_yaxes(title_text="股價 (NT$)")
    return _fig_div(_layout(fig, height=420))


# ======================================================================
# 圖表 2:EPS 走勢(近8季實際 + 3Q26 三情境)
# ======================================================================
def _fig_eps(quarters: list[QuarterFinancials], scenarios: dict[str, EPSScenario],
             quarter_label: str) -> str:
    actual = quarters[-8:] if len(quarters) > 8 else quarters
    ax = [q.quarter for q in actual]
    ay = [round(q.reported_eps, 2) for q in actual]

    pes = scenarios["悲觀"].eps_quarter
    neu = scenarios["中性"].eps_quarter
    opt = scenarios["樂觀"].eps_quarter
    ex = [f"{quarter_label}悲觀E", f"{quarter_label}中性E", f"{quarter_label}樂觀E"]
    ey = [round(pes, 2), round(neu, 2), round(opt, 2)]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=ax, y=ay, name="實際 EPS", marker_color=C_BLUE,
        text=[f"{v:.1f}" for v in ay], textposition="outside",
        hovertemplate="%{x}<br>實際 EPS %{y:.2f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=ex, y=ey, name=f"{quarter_label} 試算",
        marker_color=[C_EXP, C_BLUE, C_CHEAP],
        marker_pattern_shape="/",  # 斜線紋:一眼區分「試算」與「實際」
        text=[f"{v:.1f}" for v in ey], textposition="outside",
        hovertemplate="%{x}<br>試算 EPS %{y:.2f}<extra></extra>",
    ))
    fig.update_layout(barmode="group", bargap=0.25)
    fig.update_yaxes(title_text="單季 EPS (NT$)")
    fig.update_xaxes(type="category")
    return _fig_div(_layout(fig, height=380))


# ======================================================================
# 圖表 3:共識EPS監控(折線 + 上修/下修標記)
# ======================================================================
def _parse_consensus(rows: list[dict]) -> list[dict]:
    """字串列 → [{dt, y0, y1}],並去掉「和前一列完全相同」的重複列。"""
    out: list[dict] = []
    prev = None
    for r in rows:
        try:
            y0 = float(r["eps_y0"]) if r.get("eps_y0") not in (None, "") else None
            y1 = float(r["eps_y1"]) if r.get("eps_y1") not in (None, "") else None
        except (TypeError, ValueError):
            continue
        dt = (r.get("datetime") or "").strip()
        cur = (dt, y0, y1)
        if cur == prev:            # 連續完全重複 → 跳過(避免同一時點重跑塞爆)
            continue
        prev = cur
        out.append({"dt": dt, "y0": y0, "y1": y1})
    return out


def _revision_markers(xs: list[str], ys: list[float]):
    """回傳上修 / 下修的 (x, y, 符號) 讓圖上標記。"""
    up_x, up_y, dn_x, dn_y = [], [], [], []
    last = None
    for x, y in zip(xs, ys):
        if y is None:
            continue
        if last is not None and abs(y - last) > 1e-6:
            (up_x if y > last else dn_x).append(x)
            (up_y if y > last else dn_y).append(y)
        last = y
    return up_x, up_y, dn_x, dn_y


def _fig_consensus(rows: list[dict]) -> str:
    pts = _parse_consensus(rows)
    if len(pts) < 1:
        return _placeholder("尚無共識EPS歷史紀錄(auto 模式跑過幾次後,這裡會累積折線)。")
    xs = [p["dt"] for p in pts]
    y0 = [p["y0"] for p in pts]
    y1 = [p["y1"] for p in pts]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=y0, name="2026(今年FY)共識EPS",
                             mode="lines+markers", line=dict(color=C_BLUE, width=2.4)))
    fig.add_trace(go.Scatter(x=xs, y=y1, name="2027(明年FY)共識EPS",
                             mode="lines+markers", line=dict(color=C_CHEAP, width=2.4)))
    # 上修(綠▲)/ 下修(紅▼)標記(對 2026 那條)
    ux, uy, dx, dy = _revision_markers(xs, y0)
    if ux:
        fig.add_trace(go.Scatter(x=ux, y=uy, name="上修", mode="markers",
                                 marker=dict(color=C_CHEAP, size=13, symbol="triangle-up")))
    if dx:
        fig.add_trace(go.Scatter(x=dx, y=dy, name="下修", mode="markers",
                                 marker=dict(color=C_EXP, size=13, symbol="triangle-down")))
    fig.update_yaxes(title_text="共識 EPS (NT$)")
    fig.update_xaxes(type="category")
    return _fig_div(_layout(fig, height=360))


# ======================================================================
# 圖表 4:FCF 品質 — 資本支出年增率(領先2年) vs 營收年增率
# ======================================================================
def _fig_fcf_dual(f: FcfQualityResult) -> str:
    # 營收年增率:畫在當年 Y
    rx, ry = [], []
    for y, g in zip(f.years, f.rev_growth):
        if g is not None:
            rx.append(y)
            ry.append(round(g, 1))
    # 資本支出年增率:往後推 capex_lead_years 年,對齊「幾年後的營收」
    cx, cy = [], []
    for y, g in zip(f.years, f.capex_growth):
        if g is not None:
            cx.append(y + f.capex_lead_years)
            cy.append(round(g, 1))

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=rx, y=ry, name="營收年增率",
                             mode="lines+markers", line=dict(color=C_BLUE, width=2.6)))
    fig.add_trace(go.Scatter(
        x=cx, y=cy, name=f"資本支出年增率(領先{f.capex_lead_years}年)",
        mode="lines+markers", line=dict(color=C_PRICEY, width=2.4, dash="dash")))
    fig.add_hline(y=0, line=dict(color=C_NA, width=1, dash="dot"))
    fig.update_yaxes(title_text="年增率 (%)")
    fig.update_xaxes(title_text="年", type="category")
    return _fig_div(_layout(fig, height=380))


# ======================================================================
# HTML 區塊:估值卡片 / FCF 三燈號 / 註解
# ======================================================================
def _cards_html(dash: DashboardResult | None) -> str:
    if dash is None:
        return _placeholder("未取得現價,無法計算即時估值指標(用 --data-mode auto,或在 config 填 current_price)。")
    cells = []
    for m in dash.metrics:
        color = _VERDICT_COLOR.get(m.verdict, C_NA)
        cells.append(
            f'<div class="card" style="border-top:5px solid {color}">'
            f'<div class="card-name">{_esc(m.name)}</div>'
            f'<div class="card-val" style="color:{color}">{_esc(m.display)}</div>'
            f'<div class="badge" style="background:{color}">{_esc(m.verdict)}</div>'
            f'<div class="card-note">{_esc(m.measures)}</div>'
            f'</div>'
        )
    return f'<div class="cards">{"".join(cells)}</div>'


def _lights_html(f: FcfQualityResult | None) -> str:
    if f is None:
        return ""
    cells = []
    for s in f.signals:
        color = _LIGHT_COLOR.get(s.light, C_NA)
        word = _LIGHT_WORD.get(s.light, "—")
        cells.append(
            f'<div class="light-card">'
            f'<div class="light-row"><span class="dot" style="background:{color}"></span>'
            f'<span class="light-name">{_esc(s.name)}</span></div>'
            f'<div class="light-val">{_esc(s.value_text)}</div>'
            f'<div class="light-word" style="color:{color}">{_esc(word)}</div>'
            f'<div class="light-note">{_esc(s.note)}</div>'
            f'</div>'
        )
    return f'<div class="lights">{"".join(cells)}</div>'


def _note(html: str) -> str:
    return f'<p class="note">{html}</p>'


# ======================================================================
# 主組裝
# ======================================================================
def build_html_dashboard(
    guidance: Guidance,
    scenarios: dict[str, EPSScenario],
    valuation: ValuationResult,
    data_mode: str,
    dashboard: DashboardResult | None = None,
    river: RiverSeries | None = None,
    quarters: list[QuarterFinancials] | None = None,
    consensus_rows: list[dict] | None = None,
    fcf: FcfQualityResult | None = None,
    current_price: tuple[float, str] | None = None,
) -> str:
    """回傳完整、可離線開啟的 HTML 字串。"""
    neutral = scenarios["中性"]
    opt = scenarios["樂觀"]
    pes = scenarios["悲觀"]
    ann = neutral.eps_annualized
    pb = valuation.pe_band
    mid_price = valuation.price_matrix["中性"]["mid"]
    lo_price = valuation.price_matrix["悲觀"]["low"]
    hi_price = valuation.price_matrix["樂觀"]["high"]

    price = market_pe = premium = None
    price_src = ""
    if current_price:
        price, price_src = current_price
        market_pe = price / ann if ann else 0.0
        premium = (market_pe - pb.pe_mid) / pb.pe_mid * 100.0 if pb.pe_mid else 0.0

    # ---- 三行摘要(大字) ----
    line1 = (f'我估 <b>{_esc(guidance.quarter_label)}</b> 單季 EPS ≈ '
             f'<b>NT$ {_n(neutral.eps_quarter, 2)}</b>'
             f'<span class="sub">(悲觀 {_n(pes.eps_quarter, 2)} ~ 樂觀 {_n(opt.eps_quarter, 2)})</span>')
    line2 = (f'合理股價中樞約 <b>NT$ {_n(mid_price, 0)}</b>'
             f'<span class="sub">(全區間 {_n(lo_price, 0)} ~ {_n(hi_price, 0)})</span>')
    if price is not None:
        pd_word = "溢價" if premium >= 0 else "折價"
        pd_color = C_EXP if premium >= 0 else C_CHEAP
        line3 = (f'現價 <b>NT$ {_n(price, 0)}</b>,隱含本益比 <b>{_n(market_pe, 1)}x</b> '
                 f'<span class="sub">vs 歷史中樞 {_n(pb.pe_mid, 1)}x → '
                 f'<span style="color:{pd_color};font-weight:700">{pd_word} {premium:+.1f}%</span></span>')
    else:
        line3 = '現價:未取得<span class="sub">(用 --data-mode auto 自動抓,或 config 填 current_price)</span>'

    summary = (
        '<div class="summary">'
        f'<div class="s-line">① {line1}</div>'
        f'<div class="s-line">② {line2}</div>'
        f'<div class="s-line">③ {line3}</div>'
        '</div>'
    )

    # ---- 各圖 ----
    river_div = _fig_river(river) if river else _placeholder(
        "未取得股價 / EPS 長序列,略過河流圖(需 --data-mode auto)。")
    eps_div = _fig_eps(quarters, scenarios, guidance.quarter_label) if quarters else _placeholder(
        "未取得近8季財務,略過 EPS 走勢。")
    cons_div = _fig_consensus(consensus_rows or [])
    fcf_div = _fig_fcf_dual(fcf) if fcf else _placeholder(
        "未取得資產負債/現金流長序列,略過 FCF 品質圖(需 --data-mode auto)。")

    # ---- 河流圖現價位階白話 ----
    river_note_extra = ""
    if river and river.current_pe is not None:
        if river.current_pe <= (river.pe_low + river.pe_mid) / 2:
            zone = f'偏<span style="color:{C_CHEAP}">低估區(貼近低本益比河道)</span>'
        elif river.current_pe >= (river.pe_mid + river.pe_high) / 2:
            zone = f'偏<span style="color:{C_EXP}">高估區(貼近高本益比河道)</span>'
        else:
            zone = f'約在<span style="color:{C_FAIR}">中樞河道附近(合理帶)</span>'
        river_note_extra = (f' 目前 trailing PE ≈ <b>{river.current_pe:g}x</b>,'
                            f'位階{zone}。')

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    plotly_js = get_plotlyjs()

    # ---- 組 HTML ----
    body = f"""
<div class="wrap">
  <header>
    <h1>台積電 (2330.TW) 估值視覺化儀表板 — {_esc(guidance.quarter_label)}</h1>
    <div class="meta">產生時間 {now}　|　數據模式 <b>{_esc(data_mode)}</b></div>
    <div class="warn">⚠️ 個人試算工具產出,所有數字請回原始來源核實,<b>不構成投資建議</b>。</div>
  </header>

  {summary}

  <section>
    <h2>① 估值儀表板(即時連動現價)</h2>
    {_cards_html(dashboard)}
    {_note('四指標交叉看,別用單一指標下結論。<b style="color:'+C_CHEAP+'">綠=便宜</b>、'
           '<b style="color:'+C_FAIR+'">灰=合理</b>、<b style="color:'+C_PRICEY+'">橘=偏貴</b>、'
           '<b style="color:'+C_EXP+'">紅=貴</b>。PEG 把「貴」與「成長」一起看、FCF Yield 看用現價買每年拿回多少現金、'
           'EV/EBITDA 把負債現金也算進來較能跨公司比。')}
  </section>

  <section>
    <h2>② 本益比河流圖(現在位於歷史估值哪一段?)</h2>
    {river_div}
    {_note('河道 = 「當時的近四季實際EPS」×(低/中/高 本益比);EPS 成長會讓整條河道往上抬。'
           '股價線貼近<b style="color:'+C_CHEAP+'">綠(低本益比)</b>相對便宜、貼近<b style="color:'+C_EXP+'">紅(高本益比)</b>'
           '相對貴。' + river_note_extra + ' 口徑為 trailing(過去4季),與摘要的前瞻PE 略有差異屬正常。')}
  </section>

  <section>
    <h2>③ EPS 走勢(近8季實際 + {_esc(guidance.quarter_label)} 三情境試算)</h2>
    {eps_div}
    {_note('實心藍柱為<b>財報實際 EPS</b>;斜線紋柱為我對 '+_esc(guidance.quarter_label)+
           ' 的<b>試算</b>('
           '<span style="color:'+C_EXP+'">悲觀</span> / '
           '<span style="color:'+C_BLUE+'">中性</span> / '
           '<span style="color:'+C_CHEAP+'">樂觀</span>)。看的是趨勢與試算落點是否延續成長軌跡。')}
  </section>

  <section>
    <h2>④ 共識EPS監控(最該盯的訊號:上修還是下修?)</h2>
    {cons_div}
    {_note('分析師<b>共識EPS 的上修/下修</b>,比每天股價漲跌更代表基本面預期真的在變。'
           '<span style="color:'+C_CHEAP+'">▲ 綠=上修</span>、'
           '<span style="color:'+C_EXP+'">▼ 紅=下修</span>;每次 auto 執行都會累積一筆,折線會越來越長。')}
  </section>

  <section>
    <h2>⑤ FCF 品質檢查(獲利含金量 + 擴產有沒有兌現)</h2>
    {fcf_div}
    {_note('把<b>資本支出年增率</b>往後推 '+str(fcf.capex_lead_years if fcf else 2)+
           ' 年,和<b>營收年增率</b>疊圖:台積電今天蓋廠買機台,約 2 年後才變產能與營收。'
           '若「前兩年的猛擴產(橘線)」後面跟著「營收成長(藍線)」,代表擴產有兌現;'
           '若橘線衝高、兩年後藍線沒跟上,要留意產能利用率與折舊壓力。')}
    {_lights_html(fcf)}
    {(_note('三燈號抓「獲利含金量」警訊:<b>存貨天數</b>/<b>應收天數</b>快速拉長=庫存堆積或收款變慢;'
            '<b>營運現金流年增率</b>衰退=帳面獲利沒轉成真現金。門檻為經驗法則(寫在程式註解),'
            '<b style="color:'+C_CHEAP+'">綠健康</b>/<b style="color:#eab308">黃留意</b>/'
            '<b style="color:'+C_EXP+'">紅警訊</b>。') if fcf else '')}
  </section>

  <footer>
    <div>資料來源:財報/資產負債/現金流 FinMind、本益比河道 TWSE、股價 FinMind、共識EPS yfinance。</div>
    <div>本工具僅為個人試算,數字可能過時或有誤,請務必回到原始出處核對,不構成投資建議。</div>
  </footer>
</div>
"""

    return _PAGE_TEMPLATE.format(plotly_js=plotly_js, css=_CSS, body=body,
                                 title=f"台積電估值儀表板 {guidance.quarter_label}")


# ── 頁面骨架 + CSS(非 f-string,避免大括號衝突)──────────────────────
_CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #f3f4f6;
  font-family: -apple-system, BlinkMacSystemFont, 'Noto Sans TC', 'Microsoft JhengHei', 'Segoe UI', sans-serif;
  color: #1f2937; line-height: 1.6; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 16px; }
header h1 { font-size: 1.5rem; margin: 8px 0 4px; }
.meta { color: #6b7280; font-size: .9rem; }
.warn { background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412;
  padding: 8px 12px; border-radius: 8px; margin: 10px 0; font-size: .9rem; }
.summary { background: linear-gradient(135deg,#0f172a,#1e293b); color: #f8fafc;
  border-radius: 14px; padding: 18px 20px; margin: 14px 0 22px;
  box-shadow: 0 8px 24px rgba(15,23,42,.18); }
.summary .s-line { font-size: 1.15rem; margin: 8px 0; }
.summary b { color: #fff; font-size: 1.25rem; }
.summary .sub { color: #94a3b8; font-size: .9rem; margin-left: 6px; font-weight: 400; }
section { background: #fff; border-radius: 14px; padding: 16px 18px; margin: 16px 0;
  box-shadow: 0 2px 10px rgba(0,0,0,.05); }
section h2 { font-size: 1.15rem; margin: 4px 0 12px; padding-bottom: 8px;
  border-bottom: 2px solid #f1f5f9; }
.note { background: #f8fafc; border-left: 4px solid #cbd5e1; color: #475569;
  padding: 10px 12px; border-radius: 6px; font-size: .9rem; margin: 12px 0 2px; }
.placeholder { background: #f8fafc; color: #94a3b8; text-align: center;
  padding: 40px 16px; border-radius: 8px; border: 1px dashed #cbd5e1; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
.card { background: #fff; border: 1px solid #eef2f7; border-radius: 12px; padding: 14px;
  box-shadow: 0 1px 4px rgba(0,0,0,.04); }
.card-name { font-size: .85rem; color: #6b7280; }
.card-val { font-size: 2rem; font-weight: 800; margin: 4px 0; }
.badge { display: inline-block; color: #fff; font-size: .8rem; font-weight: 700;
  padding: 2px 10px; border-radius: 999px; }
.card-note { font-size: .82rem; color: #64748b; margin-top: 8px; }
.lights { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px; margin-top: 12px; }
.light-card { border: 1px solid #eef2f7; border-radius: 12px; padding: 14px; background:#fff; }
.light-row { display: flex; align-items: center; gap: 8px; }
.dot { width: 16px; height: 16px; border-radius: 50%; display: inline-block;
  box-shadow: 0 0 0 3px rgba(0,0,0,.05); }
.light-name { font-weight: 700; font-size: .95rem; }
.light-val { font-size: 1.35rem; font-weight: 800; margin: 6px 0 2px; }
.light-word { font-size: .85rem; font-weight: 700; }
.light-note { font-size: .8rem; color: #64748b; margin-top: 6px; }
footer { color: #94a3b8; font-size: .8rem; text-align: center; margin: 24px 0 8px; }
footer div { margin: 4px 0; }
@media (max-width: 640px) {
  header h1 { font-size: 1.2rem; }
  .summary .s-line { font-size: 1rem; }
  .summary b { font-size: 1.1rem; }
  .card-val { font-size: 1.7rem; }
  .wrap { padding: 10px; }
  section { padding: 12px; }
}
"""

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
<script type="text/javascript">{plotly_js}</script>
</head>
<body>
{body}
</body>
</html>
"""
