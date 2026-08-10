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


def is_financial_company(stock_id: str = "", industry: str = "", market: str = "twse") -> bool:
    """金融業的現金、負債與現金流是營運原料，不套一般企業 FCF/EV 倍數。"""
    label = (industry or "").strip().lower()
    if label in {"金融保險", "financial services", "banks", "insurance"}:
        return True
    if market != "us" and str(stock_id).isdigit():
        sid = int(stock_id)
        return 2800 <= sid <= 2899 or str(stock_id) in {
            "5871", "5876", "5880", "6005", "9941",
        }
    return False


def _t(bn: float | None) -> str:
    """把『十億台幣』顯示成『兆』方便讀,例如 64052 → '64.1兆'。"""
    if bn is None:
        return "—"
    return f"{bn / 1000:,.2f}兆"


def build_dashboard(
    price: float,
    ann_eps: float,
    shares_bn: float,
    pe_band: PEBand | None,
    yf: dict | None,
    growth_pct: float | None,
    growth_source: str,
    currency: str = "TWD",
    is_financial: bool = False,
) -> DashboardResult:
    """組裝儀表板。yf 為 None(抓取失敗)時,只會有前瞻PE、PEG(若有手填成長)。"""
    yf = yf or {}
    metrics: list[ValuationMetric] = []
    is_usd = currency == "USD"
    price_source = "Yahoo" if is_usd else "TWSE"

    # 市值(十億報價幣別)= 現價 × 股數
    market_cap_bn = price * shares_bn

    # ---- 1. 前瞻 PE = 現價 ÷ 年化EPS -------------------------------
    fpe = price / ann_eps if ann_eps is not None and ann_eps > 0 else None
    pe_nm = ann_eps is not None and ann_eps <= 0
    if pe_nm:
        pe_verdict = "不適用"
    elif fpe is None:
        pe_verdict = "資料不足"
    else:
        pe_verdict = "前瞻參考"
    metrics.append(ValuationMetric(
        key="forward_pe",
        name="前瞻本益比 (Forward PE)",
        value=fpe, unit="x",
        formula=(f"現價 {price:,.0f} ÷ 年化EPS {ann_eps:,.2f} = {fpe:,.1f}x" if fpe is not None
                 else f"年化 EPS {ann_eps:,.2f} 非正，本益比無意義" if pe_nm else "—"),
        measures=("EPS 非正，前瞻本益比不具經濟意義。" if pe_nm else
                  "市場願意為每 1 元(未來一年)盈餘付幾元;越高=越貴 / 市場越樂觀。"),
        reference="forward PE 僅與同口徑的未來預估或同業 forward PE 比較",
        verdict=pe_verdict,
        thresholds="不以 trailing 歷史河道判讀 forward PE",
        driven_by="現價(日變) + 我的年化EPS(法說指引→試算,季變)",
        source=f"現價 {price_source} + 年化EPS(共識/本工具試算)",
        display_override="N/M" if pe_nm else "",
    ))

    # ---- 2. PEG = 前瞻PE ÷ 盈餘成長率 -------------------------------
    peg = (fpe / growth_pct) if (fpe and growth_pct and growth_pct > 0) else None
    peg_nm = pe_nm or (growth_pct is not None and growth_pct <= 0 and fpe is not None)
    if peg_nm:
        peg_verdict = "不適用"
    elif peg is None:
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
                 if peg is not None else "EPS 或成長率非正，PEG 無意義" if peg_nm
                 else "—(缺成長率或前瞻PE)"),
        measures=("EPS 或盈餘成長率非正，PEG 不具經濟意義。" if peg_nm else
                  "把『貴』和『成長』一起看:每 1% 盈餘成長,市場付多少本益比。1 附近算合理。"),
        reference="約 1 為合理;<1 難得便宜、>2 偏貴(成長率見算式與共識節)",
        verdict=peg_verdict,
        thresholds="便宜 <1｜合理 1~1.5｜偏貴 1.5~2｜貴 >2",
        driven_by="現價(日變) + 年化EPS(季變) + 共識EPS 2026/2027 成長(季/事件變)",
        source="前瞻PE(本工具) + 盈餘成長率(共識)",
        display_override="N/M" if peg_nm else "",
    ))

    # ---- 3. FCF Yield = 近4季FCF ÷ 市值 -----------------------------
    fcf = yf.get("fcf_ttm")
    fcf_bn = fcf / 1e9 if fcf is not None else None
    fcf_yield = (fcf_bn / market_cap_bn * 100) if (fcf_bn is not None and market_cap_bn and not is_financial) else None
    if is_financial:
        fcf_verdict = "不適用"
    elif fcf_yield is None:
        fcf_verdict = "資料不足"
    elif fcf_yield < 0:
        fcf_verdict = "負現金流"
    elif fcf_yield == 0:
        fcf_verdict = "無現金流"
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
        measures=("金融業的現金流與負債屬營運原料，不適用一般企業 FCF Yield。" if is_financial else
                  "近四季自由現金流為負，代表資本支出或營運現金流尚未形成正向餘額。"
                  if fcf_yield is not None and fcf_yield < 0 else
                  "近四季自由現金流為零，尚未形成可供分配的現金餘額。"
                  if fcf_yield == 0 else
                  "用現價買,公司每年產生多少『可自由運用現金』回饋你;越高越划算。"),
        reference="概略參考:重資本支出公司通常較低；須與自身歷史及同業比較",
        verdict=fcf_verdict,
        thresholds="便宜(高) >4%｜合理 2~4%｜偏貴(低) <2%",
        driven_by="現價(日變,影響市值) + 近4季自由現金流(季變)",
        source="FCF 近4季 yfinance 現金流 + 市值(現價×股數)",
        display_override="N/M" if is_financial else "",
    ))

    # ---- 4. EV/EBITDA = 企業價值 ÷ 近4季EBITDA ----------------------
    ebitda = yf.get("ebitda")
    debt = yf.get("totalDebt")
    cash = yf.get("totalCash")
    ebitda_bn = ebitda / 1e9 if ebitda is not None else None
    ev_bn = None
    if not is_financial and debt is not None and cash is not None:
        ev_bn = market_cap_bn + debt / 1e9 - cash / 1e9   # EV = 市值 + 負債 − 現金
    ev_ebitda = (ev_bn / ebitda_bn) if (not is_financial and ev_bn is not None
                                        and ebitda_bn is not None and ebitda_bn > 0) else None
    ev_nm = is_financial or (ebitda_bn is not None and ebitda_bn <= 0)
    if ev_nm:
        ev_verdict = "不適用"
    elif ev_ebitda is None:
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
        measures=("金融業的負債與現金是營運原料，不適用一般企業 EV/EBITDA。" if is_financial else
                  "EBITDA 非正，企業價值倍數不具經濟意義。" if ev_nm else
                  "把負債與現金也算進去的『整體企業』估值,排除資本結構與稅率差異,較能跨公司比。"),
        reference="概略參考:一般 10~20x；仍須與自身歷史及同業比較",
        verdict=ev_verdict,
        thresholds="便宜 <12x｜合理 12~18x｜貴 >18x(經驗法則)",
        driven_by="現價(日變,影響市值→EV) + 負債/現金(季變) + EBITDA(季變)",
        source="EV=市值+負債−現金;EBITDA 近4季 yfinance",
        display_override="N/M" if ev_nm else "",
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
