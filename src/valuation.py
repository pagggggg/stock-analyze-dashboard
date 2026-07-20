"""
估值 (valuation.py)
===================
把三情境的「單季 EPS」轉成「價格區間」。兩步:

  步驟 1:年化 EPS (單季 → 全年)。因為本益比 (P/E) 是對「全年 EPS」講的。
          提供兩種年化方式,在 config 的 valuation.annualize_method 選:
            - "ttm" (預設,較嚴謹):最近 3 季『實際 EPS』 + 本季『試算 EPS』
                                    = 滾動 12 個月 (Trailing Twelve Months)
            - "x4"  (較粗略)      :本季試算 EPS × 4

  步驟 2:價格 = 年化 EPS × 本益比。
          用近 10 年本益比的 低 / 中 / 高,對三情境各算一組價格 → 價格矩陣。

輸出的價格矩陣長這樣 (列=情境,欄=本益比水準):
                低本益比    中本益比    高本益比
    樂觀          ...         ...         ...
    中性          ...         ...         ...
    悲觀          ...         ...         ...
"""

from __future__ import annotations

from .models import EPSScenario, PEBand, ValuationResult


def annualize_eps(
    scenario: EPSScenario,
    method: str,
    trailing_eps_sum: float = 0.0,
) -> float:
    """把單季 EPS 年化。

    參數:
        scenario         : 單一情境 (含 eps_quarter)
        method           : "ttm" 或 "x4"
        trailing_eps_sum : 最近 3 季實際 EPS 加總 (ttm 才會用到)
    """
    if method == "x4":
        return scenario.eps_quarter * 4.0
    elif method == "ttm":
        # 滾動 12 個月 = 前 3 季實際 + 本季試算
        return trailing_eps_sum + scenario.eps_quarter
    else:
        raise ValueError(f"未知的年化方式:{method} (只支援 'ttm' 或 'x4')")


def build_valuation(
    scenarios: dict[str, EPSScenario],
    pe_band: PEBand,
    method: str = "ttm",
    trailing_eps_sum: float = 0.0,
) -> ValuationResult:
    """產生價格矩陣,並把年化 EPS 回填到每個情境。"""
    price_matrix: dict[str, dict[str, float]] = {}

    for name, sc in scenarios.items():
        # 步驟 1:年化 EPS,並回填到 scenario 供報告顯示
        eps_ann = annualize_eps(sc, method, trailing_eps_sum)
        sc.eps_annualized = eps_ann

        # 步驟 2:年化 EPS × 本益比 (低/中/高)
        price_matrix[name] = {
            "low": eps_ann * pe_band.pe_low,
            "mid": eps_ann * pe_band.pe_mid,
            "high": eps_ann * pe_band.pe_high,
        }

    method_desc = {
        "ttm": "TTM 滾動12月 (最近3季實際 + 本季試算)",
        "x4": "單季試算 × 4",
    }.get(method, method)

    return ValuationResult(
        pe_band=pe_band,
        annualize_method=method_desc,
        price_matrix=price_matrix,
    )
