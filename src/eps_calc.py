"""
EPS 試算引擎 (eps_calc.py)
==========================
這是整個工具的心臟。把「指引 + 假設」換算成「單季 EPS」,並輸出三情境。

試算邏輯 (從上到下,一步都不跳,方便你對照財報損益表):

    營收(美元) × 匯率            = 營收(台幣)
    營收(台幣) × 毛利率          = 毛利
    營收(台幣) × 營業費用率      = 營業費用
    毛利 − 營業費用              = 營業利益
    營收(台幣) × 業外比          = 業外收支
    營業利益 + 業外收支          = 稅前淨利
    稅前淨利 × (1 − 稅率)        = 稅後淨利
    稅後淨利 ÷ 股數              = 單季 EPS

三情境怎麼分 (只讓「營收」與「毛利率」在指引區間內變動,其餘假設固定):
    樂觀  = 高營收 × 高毛利率
    中性  = 區間中點 × 中點毛利率
    悲觀  = 低營收 × 低毛利率

註:匯率、營業費用率、稅率、業外比、股數在三情境維持相同。
    若你想讓匯率或稅率也分情境,可自行擴充 (在 config 增加區間,再改這裡)。
"""

from __future__ import annotations

from .models import EPSScenario, Guidance, QuarterFinancials


def _compute_one(
    name: str,
    revenue_usd_bn: float,
    gross_margin_pct: float,
    g: Guidance,
) -> EPSScenario:
    """給定「營收(美元)」與「毛利率」,加上 Guidance 的其他假設,算出一個情境。

    刻意把每一個中間數字都存進 EPSScenario,報告才能攤開整條計算鏈。
    """
    fx = g.fx_usdtwd.value
    opex_ratio = g.opex_ratio.value
    tax_rate = g.tax_rate.value
    non_op_ratio = g.non_op_ratio.value
    shares = g.shares_bn.value

    # 1. 營收(美元) → 營收(台幣)
    revenue_twd_bn = revenue_usd_bn * fx

    # 2. 毛利 = 營收 × 毛利率
    gross_profit = revenue_twd_bn * (gross_margin_pct / 100.0)

    # 3. 營業費用 = 營收 × 營業費用率
    opex = revenue_twd_bn * (opex_ratio / 100.0)

    # 4. 營業利益 = 毛利 − 營業費用
    operating_income = gross_profit - opex

    # 5. 業外收支 = 營收 × 業外比 (台積電通常為小幅正值:利息+投資收益)
    non_op = revenue_twd_bn * (non_op_ratio / 100.0)

    # 6. 稅前淨利 = 營業利益 + 業外收支
    pretax_income = operating_income + non_op

    # 7. 稅後淨利 = 稅前 × (1 − 稅率)
    net_income = pretax_income * (1 - tax_rate / 100.0)

    # 8. 單季 EPS = 淨利(十億台幣) / 股數(十億股) → 台幣元/股
    eps_quarter = net_income / shares

    return EPSScenario(
        name=name,
        revenue_usd_bn=revenue_usd_bn,
        fx_usdtwd=fx,
        revenue_twd_bn=revenue_twd_bn,
        gross_margin_pct=gross_margin_pct,
        gross_profit_twd_bn=gross_profit,
        opex_ratio_pct=opex_ratio,
        opex_twd_bn=opex,
        operating_income_twd_bn=operating_income,
        non_op_ratio_pct=non_op_ratio,
        non_op_twd_bn=non_op,
        pretax_income_twd_bn=pretax_income,
        tax_rate_pct=tax_rate,
        net_income_twd_bn=net_income,
        shares_bn=shares,
        eps_quarter=eps_quarter,
    )


def calculate_scenarios(g: Guidance) -> dict[str, EPSScenario]:
    """依 Guidance 算出 樂觀 / 中性 / 悲觀 三情境。

    回傳 dict:{"樂觀": EPSScenario, "中性": ..., "悲觀": ...}
    """
    rev = g.revenue_usd     # SourcedRange (低~高)
    gm = g.gross_margin     # SourcedRange (低~高)

    scenarios = {
        # 樂觀:營收高標 + 毛利率高標
        "樂觀": _compute_one("樂觀", rev.high, gm.high, g),
        # 中性:營收中點 + 毛利率中點
        "中性": _compute_one("中性", rev.mid, gm.mid, g),
        # 悲觀:營收低標 + 毛利率低標
        "悲觀": _compute_one("悲觀", rev.low, gm.low, g),
    }
    return scenarios


# ----------------------------------------------------------------------
# 模型回測:用同一套公式重算歷史 EPS,和財報實際 EPS 比對
# ----------------------------------------------------------------------
def backtest_against_actuals(quarters: list[QuarterFinancials]) -> list[dict]:
    """用相同公式回推歷史每季 EPS,對照財報實際值,檢查模型偏誤。

    歷史資料的營收本來就是台幣,所以不經過匯率換算。
    回傳每季一個 dict:{quarter, model_eps, reported_eps, diff, diff_pct}
    """
    results = []
    for q in quarters:
        gross_profit = q.revenue_twd_bn * (q.gross_margin_pct / 100.0)
        opex = q.revenue_twd_bn * (q.opex_ratio_pct / 100.0)
        operating_income = gross_profit - opex
        non_op = q.revenue_twd_bn * (q.non_op_ratio_pct / 100.0)
        pretax = operating_income + non_op
        net_income = pretax * (1 - q.tax_rate_pct / 100.0)
        model_eps = net_income / q.shares_bn

        diff = model_eps - q.reported_eps
        diff_pct = (diff / q.reported_eps * 100.0) if q.reported_eps else 0.0
        results.append(
            {
                "quarter": q.quarter,
                "model_eps": model_eps,
                "reported_eps": q.reported_eps,
                "diff": diff,
                "diff_pct": diff_pct,
            }
        )
    return results
