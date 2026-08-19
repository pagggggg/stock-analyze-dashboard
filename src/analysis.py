"""
單檔分析協調器 (analysis.py)
============================
把「一檔股票」的所有分析湊齊,給多股掃描 / 個股詳情頁共用。

一檔股票會產出:
  - 四指標(前瞻PE / PEG / FCF Yield / EV·EBITDA)→ 沿用 metrics.build_dashboard
  - 本益比河流圖序列                              → river.build_pe_river
  - FCF 品質(存貨/應收/OCF 三燈號 + 雙線)         → fcf_quality.build_fcf_quality
  - 近8季實際 EPS(+ 若有法說指引則加三情境試算)   → data_layer / eps_calc
  - 分析師共識 EPS(當季/今年/明年 + 成長率)        → yfinance

資料走 TWSE/TPEx(多股站近期官方收盤)、FinMind(財報/歷史股價)+ yfinance
(共識/FCF/EV 元件)。每個外部呼叫都各自 try/except,
局部失敗只記進 errors,不讓整檔掛掉(掃描總表該格顯示 N/A)。

★ 免責:本工具只用「公開市場數據」做估值研究,不含任何持倉或個人交易紀錄。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .data_layer import (
    fetch_balance_pivot,
    fetch_cashflow_pivot,
    fetch_income_pivot,
    fetch_price_daily_finmind,
    fetch_yfinance_metrics,
    load_consensus_history,
    quarters_from_income_pivot,
)
from .eps_calc import calculate_scenarios
from .fcf_quality import FcfQualityResult, build_fcf_quality
from .guidance import load_guidance
from .metrics import build_dashboard, is_financial_company
from .models import DashboardResult, EPSScenario, PEBand, QuarterFinancials
from .river import (RiverSeries, build_pe_river, compute_pe_band_finmind,
                    supports_tw_filing_fallback)
from .us_data import US_DETAIL_SCHEMA_VERSION
from .valuation import build_valuation

if TYPE_CHECKING:
    from .thesis import ThesisResult

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class StockAnalysis:
    """一檔股票的完整分析結果(給掃描總表 + 個股詳情頁用)。"""

    stock_id: str
    name: str
    price: float | None = None
    price_date: str = ""
    shares_bn: float | None = None
    market: str = "twse"
    currency: str = "TWD"
    track_signals: bool = True
    industry: str = ""
    is_financial: bool = False

    dashboard: DashboardResult | None = None       # 四指標
    pe_band: PEBand | None = None
    river: RiverSeries | None = None
    # 由 build_site 的同一次 screen_all() 回填，確保個股頁與 screener 表格同源。
    trailing_pe: float | None = None
    pe_median: float | None = None
    pe_p90: float | None = None
    pe_percentile: float | None = None
    valuation_flag: str = "na"
    fcf: FcfQualityResult | None = None
    quarters: list[QuarterFinancials] = field(default_factory=list)  # 近8季實際
    scenarios: dict[str, EPSScenario] | None = None  # 有法說指引才有
    quarter_label: str = ""

    # 共識
    eps_y0: float | None = None                    # 今年 FY 共識 EPS
    eps_y1: float | None = None                    # 明年 FY 共識 EPS
    eps_q0: float | None = None                    # 當季共識 EPS
    growth_pct: float | None = None                # (y1-y0)/y0
    n_analysts: int | None = None
    consensus_source: str = ""
    consensus_history: list[dict] = field(default_factory=list)

    ann_eps: float | None = None                   # 前瞻PE 用的年化EPS(共識優先)
    ann_eps_source: str = ""
    yf_raw: dict | None = None                     # yfinance 原始指標(供前端「換個價格試算」重算用)
    mrev: dict | None = None                       # 月營收動能(不需分析師共識;台股每月公告)
    thesis: ThesisResult | None = None              # 個人持有 thesis（若有設定）
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """至少能算出四指標卡才算成功。"""
        return self.dashboard is not None

    def metric(self, key: str):
        """取某個指標(forward_pe/peg/fcf_yield/ev_ebitda)。"""
        if not self.dashboard:
            return None
        return next((m for m in self.dashboard.metrics if m.key == key), None)

    def state_snapshot(self, previous: dict | None = None) -> dict:
        """給 scan_state 做「和上次比較」的當前狀態快照。"""
        previous = previous or {}
        peg = self.metric("peg")
        lights = {s.kind: s.light for s in (self.fcf.signals if self.fcf else [])}
        previous_lights = previous.get("fcf_lights") or {}
        # 暫時抓不到欄位時保留上次基準，避免資料恢復後跨過 outage 的變化永遠漏報。
        lights = {key: lights.get(key) or previous_lights.get(key)
                  for key in set(lights) | set(previous_lights)}
        thesis_conditions = {
            x.id: {"status": x.status, "label": x.label,
                   "current_value": x.current_value, "basis": x.basis}
            for x in (self.thesis.conditions if self.thesis else [])
        }
        return {
            "eps_y0": self.eps_y0 if self.eps_y0 is not None else previous.get("eps_y0"),
            "eps_y1": self.eps_y1 if self.eps_y1 is not None else previous.get("eps_y1"),
            # forward PE 不再拿 trailing 歷史河道判級，因此不產生估值跨級事件。
            "forward_pe_verdict": None,
            "peg_verdict": peg.verdict if peg else None,
            "fcf_lights": lights,
            "thesis_conditions": thesis_conditions,
        }


def _ttm_from_quarters(quarters: list[QuarterFinancials], n: int = 4) -> float | None:
    """近 n 季實際 EPS 加總(共識抓不到時,前瞻PE 的 fallback 年化EPS)。"""
    if len(quarters) < n:
        return None
    return round(sum(q.reported_eps for q in quarters[-n:]), 2)


def analyze_stock(
    stock_id: str,
    name: str = "",
    guidance_path: str | Path | None = None,
    pe_years: int = 10,
) -> StockAnalysis:
    """把一檔股票的所有分析湊齊。任何一步失敗都會記進 errors,不中斷。"""
    a = StockAnalysis(stock_id=stock_id, name=name or stock_id)
    record = {}
    try:
        import json
        record = json.loads(
            (ROOT / f"data/universe/{stock_id}.json").read_text(encoding="utf-8"))
        a.industry = str(record.get("industry") or "")
    except (OSError, ValueError):
        pass
    a.is_financial = is_financial_company(stock_id, a.industry, a.market)
    filing_fallback_supported = supports_tw_filing_fallback(a.name)

    # ---- 1. 長區間損益(河流圖 TTM / FCF 的營收COGS / 近8季報表共用一份)----
    income_piv = None
    fetched_date = ""
    try:
        income_piv, fetched_date = fetch_income_pivot(stock_id)
        a.quarters = quarters_from_income_pivot(income_piv, last_n=8, fetched_date=fetched_date)
    except Exception as e:  # noqa: BLE001
        a.errors.append(f"財報(FinMind)抓取失敗:{e}")
        return a  # 沒有財報就無法繼續

    if a.quarters:
        a.shares_bn = a.quarters[-1].shares_bn or None

    # ---- 2. 日股價 + 本益比區間(FinMind 自算)+ 現價 --------------------
    price_rows = None
    try:
        price_rows, pdate = fetch_price_daily_finmind(stock_id)
        if price_rows:
            official_date = record.get("price_date")
            official_price = record.get("price_last")
            if official_date and official_price is not None:
                # Committed records are the latest-price contract. Cache seed may lag
                # during a code-only push, so use it only for history through that date.
                by_date = {str(row["date"]): dict(row) for row in price_rows
                           if str(row.get("date") or "") <= str(official_date)}
                by_date[str(official_date)] = {
                    "date": str(official_date), "close": float(official_price)}
                price_rows = [by_date[d] for d in sorted(by_date)]
                a.price, a.price_date = float(official_price), str(official_date)
            else:
                last = max(price_rows, key=lambda x: x["date"])
                a.price = last["close"]
                a.price_date = last["date"]
        a.pe_band = compute_pe_band_finmind(price_rows, income_piv, years=pe_years,
                                              fetched_date=pdate,
                                              filing_fallback_supported=filing_fallback_supported,
                                              financial_company=a.is_financial)
    except Exception as e:  # noqa: BLE001
        a.errors.append(f"股價/本益比計算失敗:{e}")

    # ---- 3. yfinance:共識EPS + FCF + EV 元件 --------------------------
    yf = None
    yf_date = ""
    try:
        yf, yf_date = fetch_yfinance_metrics(f"{stock_id}.TW")
    except Exception as e:  # noqa: BLE001
        a.errors.append(f"yfinance 抓取失敗:{e}")

    if yf:
        a.yf_raw = yf
        a.eps_q0 = yf.get("eps_q0")
        a.eps_y0 = yf.get("eps_y0")
        a.eps_y1 = yf.get("eps_y1")
        a.n_analysts = yf.get("n_y0")
        a.consensus_source = f"yfinance 分析師共識 (抓取 {yf_date})"
        if a.eps_y0 is not None and a.eps_y1 is not None and a.eps_y0 > 0:
            a.growth_pct = (a.eps_y1 - a.eps_y0) / a.eps_y0 * 100.0

    # ---- 月營收動能(台股每月10日前公告;不依賴分析師覆蓋)----
    # 多數台股沒有分析師共識 → 共識類訊號(PEG/盈餘修正動能)是空的;
    # 月營收覆蓋近全市場,提供一個以「實際已發生營收」為準的動能訊號(口徑不同,獨立顯示)。
    try:
        from .data_layer import fetch_month_revenue, month_revenue_momentum
        a.mrev = month_revenue_momentum(fetch_month_revenue(stock_id)[0])
    except Exception as e:  # noqa: BLE001
        a.errors.append(f"月營收動能抓取失敗:{e}")

    # 前瞻PE 的年化EPS:共識今年FY 優先,抓不到退回 TTM(近4季實際)
    if a.eps_y0 is not None:
        a.ann_eps = float(a.eps_y0)
        a.ann_eps_source = "共識今年FY EPS"
    else:
        a.ann_eps = _ttm_from_quarters(a.quarters)
        a.ann_eps_source = "近4季實際EPS(TTM,共識抓不到的替代)"

    # ---- 4. 四指標卡(沿用 metrics.build_dashboard)---------------------
    if a.price and a.ann_eps is not None and a.shares_bn:
        growth_src = (f"共識 2027 {a.eps_y1:.1f} vs 2026 {a.eps_y0:.1f} → {a.growth_pct:.1f}%"
                      if a.growth_pct is not None else "(無成長率,PEG 無法計算)")
        try:
            a.dashboard = build_dashboard(
                price=a.price, ann_eps=a.ann_eps, shares_bn=a.shares_bn,
                pe_band=a.pe_band, yf=yf, growth_pct=a.growth_pct, growth_source=growth_src,
                is_financial=a.is_financial,
            )
        except Exception as e:  # noqa: BLE001
            a.errors.append(f"指標計算失敗:{e}")

    # ---- 5. 河流圖序列 ------------------------------------------------
    if price_rows:
        try:
            a.river = build_pe_river(price_rows, income_piv,
                                     current_price=a.price, current_date=a.price_date,
                                     years=pe_years,
                                     filing_fallback_supported=filing_fallback_supported,
                                     financial_company=a.is_financial)
        except Exception as e:  # noqa: BLE001
            a.errors.append(f"河流圖失敗:{e}")

    # ---- 6. FCF 品質(資產負債 + 現金流)-------------------------------
    if not a.is_financial:
        try:
            bal_piv, _ = fetch_balance_pivot(stock_id)
            cf_piv, _ = fetch_cashflow_pivot(stock_id)
            a.fcf = build_fcf_quality(income_piv, bal_piv, cf_piv)
        except Exception as e:  # noqa: BLE001
            a.errors.append(f"FCF 品質失敗:{e}")

    # ---- 7. 法說指引三情境試算(選配,只有提供 guidance 檔的股票才做)----
    if guidance_path:
        gp = Path(guidance_path)
        if not gp.is_absolute():
            gp = ROOT / gp
        if gp.exists():
            try:
                g = load_guidance(gp)
                a.quarter_label = g.quarter_label
                a.scenarios = calculate_scenarios(g)
                # 年化(TTM):前3季實際 + 本季試算,回填 eps_annualized 供詳情頁顯示
                trailing = sum(q.reported_eps for q in a.quarters[-3:])
                if a.pe_band:
                    build_valuation(a.scenarios, a.pe_band, method="ttm",
                                    trailing_eps_sum=trailing)
            except Exception as e:  # noqa: BLE001
                a.errors.append(f"法說指引試算失敗:{e}")

    # ---- 8. 共識歷史(每檔各自一個 CSV,供詳情頁折線 + 修正動能)--------
    hist_path = ROOT / f"data/consensus/{stock_id}.csv"
    a.consensus_history = load_consensus_history(hist_path)

    return a


def analyze_us_record(record: dict, pe_years: int = 5) -> StockAnalysis:
    """由已持久化的美股母體紀錄建立詳情頁，不在 push 建站時臨時連網。"""
    sid = str(record["stock_id"])
    a = StockAnalysis(stock_id=sid, name=record.get("name") or sid,
                      market="us", currency=str(record.get("currency") or "USD"),
                      track_signals=False, industry=str(record.get("industry") or ""))
    a.is_financial = is_financial_company(sid, a.industry, a.market)
    a.price = record.get("price_last")
    a.price_date = str(record.get("price_date") or "")
    detail = record.get("detail") or {}
    if detail.get("schema_version") != US_DETAIL_SCHEMA_VERSION:
        a.errors.append("美股詳情資料 schema 缺失或過期")
        return a
    try:
        a.river = RiverSeries(**detail["river"])
    except (KeyError, TypeError, ValueError) as e:
        a.errors.append(f"美股河流圖資料錯誤:{e}")
        return a
    ph = record.get("pe_hist") or {}
    if ph.get("status") == "ok":
        a.pe_band = PEBand(
            pe_low=float(ph["p10"]), pe_mid=float(ph["median"]), pe_high=float(ph["p90"]),
            years_covered=f"{ph.get('window_start')}–{ph.get('as_of')},rolling {pe_years} 年",
            source=a.river.source,
        )
    a.trailing_pe = ph.get("current_trailing_pe")
    a.pe_median = ph.get("median")
    a.pe_p90 = ph.get("p90")
    a.pe_percentile = ph.get("percentile")
    a.shares_bn = detail.get("shares_bn") or None
    a.eps_y0 = detail.get("eps_y0")
    a.eps_y1 = detail.get("eps_y1")
    a.growth_pct = detail.get("growth_pct")
    a.ann_eps = a.eps_y0
    a.ann_eps_source = "Yahoo 分析師共識"
    a.consensus_source = "Yahoo 分析師共識"
    a.yf_raw = detail.get("yf") or {}
    # ASML 的財報/共識原生為 EUR，但詳情頁市值與股價為 USD；貨幣欄位必須同口徑。
    if detail.get("financial_currency") != detail.get("quote_currency"):
        fx = detail.get("latest_fx")
        if fx:
            for key in ("ebitda", "totalDebt", "totalCash", "fcf_ttm", "ocf_ttm", "capex_ttm"):
                if a.yf_raw.get(key) is not None:
                    a.yf_raw[key] = float(a.yf_raw[key]) * float(fx)
    for row in detail.get("quarters") or []:
        a.quarters.append(QuarterFinancials(
            quarter=str(row["period"]), revenue_twd_bn=0.0, gross_margin_pct=0.0,
            opex_ratio_pct=0.0, tax_rate_pct=0.0, shares_bn=a.shares_bn or 0.0,
            non_op_ratio_pct=0.0, reported_eps=float(row["eps"]),
            source="Yahoo Reported EPS（拆股／幣別調整後）",
        ))
    if a.yf_raw.get("n_y0") is not None:
        a.n_analysts = int(a.yf_raw["n_y0"])
    if a.price and a.ann_eps is not None and a.shares_bn:
        try:
            a.dashboard = build_dashboard(
                price=float(a.price), ann_eps=float(a.ann_eps), shares_bn=float(a.shares_bn),
                pe_band=a.pe_band, yf=a.yf_raw, growth_pct=a.growth_pct,
                growth_source="Yahoo 分析師共識", currency=a.currency,
                is_financial=a.is_financial,
            )
        except Exception as e:  # noqa: BLE001
            a.errors.append(f"美股估值指標計算失敗:{e}")
    a.consensus_history = load_consensus_history(ROOT / f"data/consensus/{sid}.csv")
    return a
