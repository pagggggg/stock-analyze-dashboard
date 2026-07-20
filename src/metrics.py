"""
估值儀表板 (metrics.py)
=======================
把「現價 + 我的年化EPS + yfinance 財務數據 + 共識成長率」組裝成 4 個估值指標:

    前瞻PE     = 現價 ÷ 年化EPS
    PEG        = 前瞻PE ÷ 盈餘成長率
    FCF Yield  = 近4季自由現金流 ÷ 市值
    EV/EBITDA  = 企業價值 ÷ 近4季EBITDA

每個指標都算出:當前值、算式、白話說明、參考區間、判讀(便宜/合理/貴)、被誰影響。
判讀門檻是「經驗法則」,不是鐵律 → 報告會加警語:單一指標不下結論,要交叉看。
"""

from __future__ import annotations

from .models import DashboardResult, PEBand, ValuationMetric


def _t(bn: float | None) -> str:
    """把『十億台幣』顯示成『兆』方便讀,例如 64052 → '64.1兆'。"""
    if bn is None:
        return "—"
    return f"{bn / 1000:,.2f}兆"


def build_dashboard(
    price: float,
    ann_eps: float,
    shares_bn: float,
    pe_band: PEBand,
    yf: dict | None,
    growth_pct: float | None,
    growth_source: str,
) -> DashboardResult:
    """組裝儀表板。yf 為 None(抓取失敗)時,只會有前瞻PE、PEG(若有手填成長)。"""
    yf = yf or {}
    metrics: list[ValuationMetric] = []

    # 市值(十億台幣)= 現價 × 股數
    market_cap_bn = price * shares_bn

    # ---- 1. 前瞻 PE = 現價 ÷ 年化EPS -------------------------------
    fpe = price / ann_eps if ann_eps else None
    # 門檻以近10年本益比區間切三段:低-中中點以下=便宜,中-高中點以上=貴
    cut_lo = (pe_band.pe_low + pe_band.pe_mid) / 2
    cut_hi = (pe_band.pe_mid + pe_band.pe_high) / 2
    if fpe is None:
        pe_verdict = "資料不足"
    elif fpe <= cut_lo:
        pe_verdict = "便宜"
    elif fpe >= cut_hi:
        pe_verdict = "貴"
    else:
        pe_verdict = "合理"
    metrics.append(ValuationMetric(
        key="forward_pe",
        name="前瞻本益比 (Forward PE)",
        value=fpe, unit="x",
        formula=f"現價 {price:,.0f} ÷ 年化EPS {ann_eps:,.2f} = {fpe:,.1f}x" if fpe else "—",
        measures="市場願意為每 1 元(未來一年)盈餘付幾元;越高=越貴 / 市場越樂觀。",
        reference=f"近10年 {pe_band.pe_low:.1f}~{pe_band.pe_high:.1f}x(中樞 {pe_band.pe_mid:.1f}x)",
        verdict=pe_verdict,
        thresholds=f"便宜 <{cut_lo:.0f}x｜合理 {cut_lo:.0f}~{cut_hi:.0f}x｜貴 >{cut_hi:.0f}x(對照近10年區間)",
        driven_by="現價(日變) + 我的年化EPS(法說指引→試算,季變)",
        source="現價 TWSE + 年化EPS 本工具試算 + 區間 TWSE 近10年",
    ))

    # ---- 2. PEG = 前瞻PE ÷ 盈餘成長率 -------------------------------
    peg = (fpe / growth_pct) if (fpe and growth_pct and growth_pct > 0) else None
    if peg is None:
        peg_verdict = "資料不足"
    elif peg < 1:
        peg_verdict = "便宜"
    elif peg <= 1.5:
        peg_verdict = "合理"
    elif peg <= 2:
        peg_verdict = "偏貴"
    else:
        peg_verdict = "貴"
    metrics.append(ValuationMetric(
        key="peg",
        name="PEG(本益成長比)",
        value=peg, unit="",
        formula=(f"前瞻PE {fpe:,.1f} ÷ 盈餘成長率 {growth_pct:,.1f}% = {peg:,.2f}"
                 if peg else "—(缺成長率或前瞻PE)"),
        measures="把『貴』和『成長』一起看:每 1% 盈餘成長,市場付多少本益比。1 附近算合理。",
        reference="約 1 為合理;<1 難得便宜、>2 偏貴(成長率見算式與共識節)",
        verdict=peg_verdict,
        thresholds="便宜 <1｜合理 1~1.5｜偏貴 1.5~2｜貴 >2",
        driven_by="現價(日變) + 年化EPS(季變) + 共識EPS 2026/2027 成長(季/事件變)",
        source="前瞻PE(本工具) + 盈餘成長率(共識)",
    ))

    # ---- 3. FCF Yield = 近4季FCF ÷ 市值 -----------------------------
    fcf = yf.get("fcf_ttm")
    fcf_bn = fcf / 1e9 if fcf is not None else None
    fcf_yield = (fcf_bn / market_cap_bn * 100) if (fcf_bn is not None and market_cap_bn) else None
    if fcf_yield is None:
        fcf_verdict = "資料不足"
    elif fcf_yield > 4:
        fcf_verdict = "便宜"       # 殖利率越高越划算
    elif fcf_yield >= 2:
        fcf_verdict = "合理"
    else:
        fcf_verdict = "偏貴"
    ocf = yf.get("ocf_ttm"); capex = yf.get("capex_ttm")
    fcf_formula = "—"
    if fcf_yield is not None:
        if ocf is not None and capex is not None:
            fcf_formula = (f"近4季FCF {_t(fcf_bn)}(營運現金 {_t(ocf/1e9)} − 資本支出 {_t(abs(capex)/1e9)})"
                           f" ÷ 市值 {_t(market_cap_bn)} = {fcf_yield:.2f}%")
        else:
            fcf_formula = f"近4季FCF {_t(fcf_bn)} ÷ 市值 {_t(market_cap_bn)} = {fcf_yield:.2f}%"
    metrics.append(ValuationMetric(
        key="fcf_yield",
        name="自由現金流殖利率 (FCF Yield)",
        value=fcf_yield, unit="%",
        formula=fcf_formula,
        measures="用現價買,公司每年產生多少『可自由運用現金』回饋你;越高越划算。台積電擴產期通常偏低。",
        reference="概略參考:台積電近年約 1~4%(重資本支出→偏低)",
        verdict=fcf_verdict,
        thresholds="便宜(高) >4%｜合理 2~4%｜偏貴(低) <2%",
        driven_by="現價(日變,影響市值) + 近4季自由現金流(季變)",
        source="FCF 近4季 yfinance 現金流 + 市值(現價×股數)",
    ))

    # ---- 4. EV/EBITDA = 企業價值 ÷ 近4季EBITDA ----------------------
    ebitda = yf.get("ebitda")
    debt = yf.get("totalDebt")
    cash = yf.get("totalCash")
    ebitda_bn = ebitda / 1e9 if ebitda is not None else None
    ev_bn = None
    if debt is not None and cash is not None:
        ev_bn = market_cap_bn + debt / 1e9 - cash / 1e9   # EV = 市值 + 負債 − 現金
    ev_ebitda = (ev_bn / ebitda_bn) if (ev_bn is not None and ebitda_bn) else None
    if ev_ebitda is None:
        ev_verdict = "資料不足"
    elif ev_ebitda < 12:
        ev_verdict = "便宜"
    elif ev_ebitda <= 18:
        ev_verdict = "合理"
    else:
        ev_verdict = "貴"
    ev_formula = "—"
    if ev_ebitda is not None:
        ev_formula = (f"EV {_t(ev_bn)}(= 市值 {_t(market_cap_bn)} + 總負債 {_t(debt/1e9)} − 現金 {_t(cash/1e9)})"
                      f" ÷ EBITDA {_t(ebitda_bn)} = {ev_ebitda:.1f}x")
    metrics.append(ValuationMetric(
        key="ev_ebitda",
        name="EV/EBITDA(企業價值倍數)",
        value=ev_ebitda, unit="x",
        formula=ev_formula,
        measures="把負債與現金也算進去的『整體企業』估值,排除資本結構與稅率差異,較能跨公司比。",
        reference="概略參考:一般 10~20x;台積電近年約 12~22x",
        verdict=ev_verdict,
        thresholds="便宜 <12x｜合理 12~18x｜貴 >18x(經驗法則)",
        driven_by="現價(日變,影響市值→EV) + 負債/現金(季變) + EBITDA(季變)",
        source="EV=市值+負債−現金;EBITDA 近4季 yfinance",
    ))

    return DashboardResult(
        metrics=metrics,
        price=price,
        ann_eps=ann_eps,
        market_cap_bn=market_cap_bn,
        ev_bn=ev_bn,
        fcf_ttm_bn=fcf_bn,
        ebitda_ttm_bn=ebitda_bn,
    )
