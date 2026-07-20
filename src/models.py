"""
資料模型 (models.py)
====================
這個檔案定義整個工具會用到的「資料結構」。

設計核心:**每一個數字都要能回答「這個數字哪裡來?」**
所以我們不直接傳 float,而是把「數值 + 來源」綁在一起 (SourcedValue / SourcedRange)。
報告最後就能把每個假設連同來源逐條列出,方便你逐條檢查。

單位約定 (整個專案共用,務必一致):
  - 營收 (美元)      : 十億美元  Billion USD, 例如 25.0 代表 250 億美元
  - 營收 (台幣)      : 十億台幣  Billion TWD, 例如 800.0 代表 8000 億台幣
  - 匯率 fx_usdtwd   : 1 美元可換多少台幣, 例如 32.0
  - 百分比           : 直接填數字, 57.8 代表 57.8% (程式內部才 / 100)
  - 股數 shares      : 十億股, 例如 25.93 代表 259.3 億股
  - EPS              : 台幣元 / 股

小技巧:EPS = 淨利(十億台幣) / 股數(十億股)。
        因為分子分母都是「十億」,會自動約掉 → 直接得到「台幣元/股」。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ----------------------------------------------------------------------
# 1. 帶「來源」的數值
# ----------------------------------------------------------------------
@dataclass
class SourcedValue:
    """單一數值 + 來源說明。

    範例:SourcedValue(32.0, "TSMC 2024Q4 法說會匯率假設")
    """

    value: float
    source: str          # 這個數字哪裡來 (法說會 / 財報 / 歷史平均 / 自行假設...)
    note: str = ""        # 額外備註 (可留空)

    def __float__(self) -> float:
        return float(self.value)


@dataclass
class SourcedRange:
    """一段區間 (低~高) + 來源說明,用來表達「指引區間」。

    範例:SourcedRange(57.0, 59.0, "TSMC 法說會毛利率財測")
    """

    low: float
    high: float
    source: str
    note: str = ""

    @property
    def mid(self) -> float:
        """區間中點 (中性情境常用)。"""
        return (self.low + self.high) / 2.0


# ----------------------------------------------------------------------
# 2. 歷史單季財務數據 (近 8 季,資料層讀進來)
# ----------------------------------------------------------------------
@dataclass
class QuarterFinancials:
    """一季的實際財務數據 (來自財報 / API / 手動 CSV)。"""

    quarter: str                  # 季度標籤, 例如 "2024Q3"
    revenue_twd_bn: float         # 營收 (十億台幣) — 台積電財報以台幣計
    gross_margin_pct: float       # 毛利率 (%)
    opex_ratio_pct: float         # 營業費用率 (%) = 營業費用 / 營收
    tax_rate_pct: float           # 有效稅率 (%)
    shares_bn: float              # 流通在外股數 (十億股)
    non_op_ratio_pct: float       # 業外收支佔營收比 (%)
    reported_eps: float           # 財報實際公布 EPS (台幣) — 年化與回測用
    source: str = ""              # 這季資料的來源


# ----------------------------------------------------------------------
# 3. 法說會指引 + 模型假設 (手動輸入的核心)
# ----------------------------------------------------------------------
@dataclass
class Guidance:
    """把 config/assumptions.yaml 讀進來後的結構化結果。

    分成三塊:
      A. 公司官方「指引」: 營收區間、毛利率區間、匯率
      B. 指引沒給、要自己補的「模型假設」: 營業費用率、稅率、業外比、股數
      C. 要試算的季度標籤
    """

    quarter_label: str            # 要試算的季度, 例如 "2025Q1"

    # A. 法說會指引 (公司官方) --------------------------------
    revenue_usd: SourcedRange     # 季營收區間 (十億美元)
    gross_margin: SourcedRange    # 毛利率區間 (%)
    fx_usdtwd: SourcedValue       # 美元兌台幣匯率假設

    # B. 其他損益假設 (歷史平均 / 自行假設) --------------------
    opex_ratio: SourcedValue      # 營業費用率 (%)
    tax_rate: SourcedValue        # 有效稅率 (%)
    non_op_ratio: SourcedValue    # 業外收支佔營收比 (%)
    shares_bn: SourcedValue       # 流通在外股數 (十億股)


# ----------------------------------------------------------------------
# 4. EPS 試算結果 (單一情境)
# ----------------------------------------------------------------------
@dataclass
class EPSScenario:
    """單一情境 (樂觀/中性/悲觀) 的完整試算結果 + 中間過程。

    保留所有中間數字 (毛利、營業利益、稅前、淨利),
    這樣報告就能攤開「從營收到 EPS」的每一步,方便你檢查邏輯。
    """

    name: str                     # 情境名稱: 樂觀 / 中性 / 悲觀
    revenue_usd_bn: float         # 採用的季營收 (十億美元)
    fx_usdtwd: float              # 採用的匯率
    revenue_twd_bn: float         # = 營收美元 × 匯率 (十億台幣)
    gross_margin_pct: float       # 採用的毛利率 (%)
    gross_profit_twd_bn: float    # 毛利 = 營收 × 毛利率
    opex_ratio_pct: float         # 營業費用率 (%)
    opex_twd_bn: float            # 營業費用 = 營收 × 營業費用率
    operating_income_twd_bn: float  # 營業利益 = 毛利 − 營業費用
    non_op_ratio_pct: float       # 業外比 (%)
    non_op_twd_bn: float          # 業外收支 = 營收 × 業外比
    pretax_income_twd_bn: float   # 稅前淨利 = 營業利益 + 業外收支
    tax_rate_pct: float           # 有效稅率 (%)
    net_income_twd_bn: float      # 稅後淨利 = 稅前 × (1 − 稅率)
    shares_bn: float              # 股數 (十億股)
    eps_quarter: float            # 單季 EPS (台幣) = 淨利 / 股數
    eps_annualized: float = 0.0   # 年化 EPS (台幣),估值用;由 valuation 階段填入


# ----------------------------------------------------------------------
# 5. 估值結果 (價格矩陣)
# ----------------------------------------------------------------------
@dataclass
class PEBand:
    """近 N 年本益比區間統計。"""

    pe_low: float                 # 期間最低本益比
    pe_mid: float                 # 期間中位/平均本益比
    pe_high: float                # 期間最高本益比
    years_covered: str            # 涵蓋年度說明, 例如 "2015–2024 (10 年)"
    source: str = ""


@dataclass
class ValuationResult:
    """把三情境 EPS × 本益比區間 → 價格矩陣。"""

    pe_band: PEBand
    annualize_method: str         # 年化方式說明 ("ttm" / "x4")
    # price_matrix[情境名稱] = {"low": ?, "mid": ?, "high": ?}
    price_matrix: dict = field(default_factory=dict)


# ----------------------------------------------------------------------
# 6. 預期差結果 (對照分析師共識)
# ----------------------------------------------------------------------
@dataclass
class ExpectationGap:
    """我的試算 vs 市場共識的差距。"""

    my_eps: float                 # 我的試算 EPS (通常用中性情境)
    consensus_eps: SourcedValue   # 市場共識 EPS
    diff_abs: float               # 絕對差 = 我的 − 共識
    diff_pct: float               # 相對差 (%) = (我的 − 共識) / 共識 × 100
    scope: str                    # "單季" 或 "全年",說明比的是哪個口徑


# ----------------------------------------------------------------------
# 7. 估值儀表板:單一指標
# ----------------------------------------------------------------------
@dataclass
class ValuationMetric:
    """估值儀表板裡的一個指標 (前瞻PE / PEG / FCF Yield / EV·EBITDA)。

    每個指標都自帶:算式、白話說明、參考區間、判讀門檻、被誰影響、來源,
    這樣報告可以一次把「是什麼、多少、貴不貴、為何會動」全講清楚。
    """

    key: str                      # 程式用鍵:forward_pe / peg / fcf_yield / ev_ebitda
    name: str                     # 顯示名稱
    value: float | None           # 計算值 (None = 資料抓不到)
    unit: str                     # 單位: "x" 或 "%"
    formula: str                  # 算式(帶入實際數字),方便核對
    measures: str                 # 白話一行:這在衡量什麼
    reference: str                # 歷史參考區間 / 概略參考
    verdict: str                  # 判讀: 便宜 / 合理 / 貴 / 資料不足
    thresholds: str               # 速查表用:便宜 / 合理 / 貴 的門檻文字
    driven_by: str                # 被哪些輸入影響 (標明日變/季變)
    source: str                   # 資料來源

    @property
    def display(self) -> str:
        """格式化顯示值,如 '25.9x' / '1.8%' / '0.94' / 'N/A'。"""
        if self.value is None:
            return "N/A"
        # 無單位(PEG 這種比值)用 2 位小數,才看得出 0.94 vs 1.02 的差別
        dp = 2 if self.unit == "" else 1
        return f"{self.value:,.{dp}f}{self.unit}"


@dataclass
class DashboardResult:
    """整個估值儀表板的結果 (多個指標 + 共用的中間數字)。"""

    metrics: list[ValuationMetric]
    price: float                  # 現價 (台幣)
    ann_eps: float                # 中性年化 EPS (台幣)
    market_cap_bn: float          # 市值 (十億台幣) = 現價 × 股數
    ev_bn: float | None = None    # 企業價值 (十億台幣)
    fcf_ttm_bn: float | None = None    # 近4季自由現金流 (十億台幣)
    ebitda_ttm_bn: float | None = None  # 近4季 EBITDA (十億台幣)


# ----------------------------------------------------------------------
# 8. 共識 EPS 監控快照
# ----------------------------------------------------------------------
@dataclass
class ConsensusSnapshot:
    """一次「分析師共識 EPS」的抓取快照,並記錄相對上次的變化。"""

    as_of: str                    # 抓取時間字串
    eps_q0: float | None          # 當季共識 EPS
    eps_y0: float | None          # 今年 (FY) 共識 EPS
    eps_y1: float | None          # 明年 (FY+1) 共識 EPS
    growth_pct: float | None      # 盈餘成長率% = (y1 − y0)/y0 × 100
    n_analysts: int | None        # 分析師家數 (今年)
    source: str                   # 來源 (yfinance / config 手填)

    # 相對「上次記錄」的變化 (report 用)
    prev_eps_y0: float | None = None
    prev_eps_y1: float | None = None
    prev_as_of: str = ""

    def _dir(self, cur: float | None, prev: float | None) -> str:
        """回傳 上修 / 下修 / 持平 / (新)。"""
        if cur is None or prev is None:
            return "(無前值)"
        d = cur - prev
        if abs(d) < 1e-6:
            return "持平"
        return f"上修 (+{d:.2f})" if d > 0 else f"下修 ({d:.2f})"

    @property
    def y0_change(self) -> str:
        return self._dir(self.eps_y0, self.prev_eps_y0)

    @property
    def y1_change(self) -> str:
        return self._dir(self.eps_y1, self.prev_eps_y1)
