"""
預期差 (expectation.py)
=======================
把「我的試算」對照「市場分析師共識」,算出差距 %。

差距 % = (我的 − 共識) / 共識 × 100
    > 0 代表我比市場樂觀 (可能有超預期空間)
    < 0 代表我比市場保守 (可能有下修風險)

提供兩種口徑,分開比才公平:
  - 單季:我的『中性單季 EPS』 vs 共識『單季 EPS』
  - 全年:我的『中性年化 EPS』 vs 共識『全年 EPS』
"""

from __future__ import annotations

from .models import EPSScenario, ExpectationGap, SourcedValue


def compute_gap(
    my_eps: float,
    consensus: SourcedValue,
    scope: str,
) -> ExpectationGap:
    """計算單一口徑的預期差。"""
    diff_abs = my_eps - consensus.value
    diff_pct = (diff_abs / consensus.value * 100.0) if consensus.value else 0.0
    return ExpectationGap(
        my_eps=my_eps,
        consensus_eps=consensus,
        diff_abs=diff_abs,
        diff_pct=diff_pct,
        scope=scope,
    )


def compute_all_gaps(
    scenarios: dict[str, EPSScenario],
    consensus_quarter: SourcedValue | None,
    consensus_annual: SourcedValue | None,
) -> list[ExpectationGap]:
    """用『中性情境』當作我的代表值,分別和單季 / 全年共識比。

    (中性情境代表最可能的基準;若想改用其他情境當代表,改這裡即可。)
    """
    neutral = scenarios["中性"]
    gaps: list[ExpectationGap] = []

    if consensus_quarter is not None:
        gaps.append(compute_gap(neutral.eps_quarter, consensus_quarter, "單季"))

    if consensus_annual is not None:
        # 全年用年化後的 EPS 比 (需先跑過 valuation 才有 eps_annualized)
        gaps.append(compute_gap(neutral.eps_annualized, consensus_annual, "全年(年化)"))

    return gaps
