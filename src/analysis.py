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

資料一律走 FinMind(財報/資產負債/現金流/日股價)+ yfinance(共識/FCF/EV 元件),
不依賴 TWSE 逐月抓,所以能一致套用到任意台股代號。每個外部呼叫都各自 try/except,
局部失敗只記進 errors,不讓整檔掛掉(掃描總表該格顯示 N/A)。

★ 免責:本工具只用「公開市場數據」做估值研究,不含任何持倉或個人交易紀錄。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .data_layer import (
    fetch_balance_pivot,
    fetch_cashflow_pivot,
    fetch_income_pivot,
    fetch_price_daily_finmind,
    fetch_yfinance_metrics,
    load_consensus_history,
    quarters_from_income_pivot,
    record_consensus_history,
)
from .eps_calc import calculate_scenarios
from .fcf_quality import FcfQualityResult, build_fcf_quality
from .guidance import load_guidance
from .metrics import build_dashboard
from .models import DashboardResult, EPSScenario, PEBand, QuarterFinancials
from .river import RiverSeries, build_pe_river, compute_pe_band_finmind
from .valuation import build_valuation

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class StockAnalysis:
    """一檔股票的完整分析結果(給掃描總表 + 個股詳情頁用)。"""

    stock_id: str
    name: str
    price: float | None = None
    price_date: str = ""
    shares_bn: float | None = None

    dashboard: DashboardResult | None = None       # 四指標
    pe_band: PEBand | None = None
    river: RiverSeries | None = None
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

    def state_snapshot(self) -> dict:
        """給 scan_state 做「和上次比較」的當前狀態快照。"""
        fpe = self.metric("forward_pe")
        peg = self.metric("peg")
        lights = {s.kind: s.light for s in (self.fcf.signals if self.fcf else [])}
        return {
            "eps_y0": self.eps_y0,
            "eps_y1": self.eps_y1,
            "forward_pe_verdict": fpe.verdict if fpe else None,
            "peg_verdict": peg.verdict if peg else None,
            "fcf_lights": lights,
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
    record_consensus: bool = True,
) -> StockAnalysis:
    """把一檔股票的所有分析湊齊。任何一步失敗都會記進 errors,不中斷。"""
    a = StockAnalysis(stock_id=stock_id, name=name or stock_id)

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
            last = max(price_rows, key=lambda x: x["date"])
            a.price = last["close"]
            a.price_date = last["date"]
        a.pe_band = compute_pe_band_finmind(price_rows, income_piv, years=pe_years,
                                             fetched_date=pdate)
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
        a.eps_q0 = yf.get("eps_q0")
        a.eps_y0 = yf.get("eps_y0")
        a.eps_y1 = yf.get("eps_y1")
        a.n_analysts = yf.get("n_y0")
        a.consensus_source = f"yfinance 分析師共識 (抓取 {yf_date})"
        if a.eps_y0 and a.eps_y1 and a.eps_y0 != 0:
            a.growth_pct = (a.eps_y1 - a.eps_y0) / a.eps_y0 * 100.0

    # 前瞻PE 的年化EPS:共識今年FY 優先,抓不到退回 TTM(近4季實際)
    if a.eps_y0:
        a.ann_eps = float(a.eps_y0)
        a.ann_eps_source = "共識今年FY EPS"
    else:
        a.ann_eps = _ttm_from_quarters(a.quarters)
        a.ann_eps_source = "近4季實際EPS(TTM,共識抓不到的替代)"

    # ---- 4. 四指標卡(沿用 metrics.build_dashboard)---------------------
    if a.price and a.ann_eps and a.pe_band and a.shares_bn:
        growth_src = (f"共識 2027 {a.eps_y1:.1f} vs 2026 {a.eps_y0:.1f} → {a.growth_pct:.1f}%"
                      if a.growth_pct is not None else "(無成長率,PEG 無法計算)")
        try:
            a.dashboard = build_dashboard(
                price=a.price, ann_eps=a.ann_eps, shares_bn=a.shares_bn,
                pe_band=a.pe_band, yf=yf, growth_pct=a.growth_pct, growth_source=growth_src,
            )
        except Exception as e:  # noqa: BLE001
            a.errors.append(f"指標計算失敗:{e}")

    # ---- 5. 河流圖序列 ------------------------------------------------
    if price_rows and a.pe_band:
        try:
            a.river = build_pe_river(price_rows, income_piv, a.pe_band, current_price=a.price)
        except Exception as e:  # noqa: BLE001
            a.errors.append(f"河流圖失敗:{e}")

    # ---- 6. FCF 品質(資產負債 + 現金流)-------------------------------
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
    if a.eps_y0 is not None or a.eps_y1 is not None:
        hist_path = ROOT / f"data/consensus/{stock_id}.csv"
        if record_consensus:
            record_consensus_history(
                hist_path, a.eps_y0, a.eps_y1,
                round(a.growth_pct, 2) if a.growth_pct is not None else None,
                a.consensus_source,
            )
        a.consensus_history = load_consensus_history(hist_path)

    return a
