"""
指引 / 假設載入 (guidance.py)
=============================
負責把 `config/assumptions.yaml` (你手動編輯的假設檔) 讀進來,
轉成程式好用的 `Guidance` 物件 (見 models.py)。

為什麼要獨立一個檔?
  因為「手動輸入指引」是這個工具的核心步驟之一。
  把它獨立出來,之後要換成別的輸入方式 (網頁表單、互動式問答) 也只改這裡。

容錯設計:
  - 找不到某個欄位時,給出「明確的中文錯誤訊息」,告訴你 YAML 哪裡少填,
    而不是丟一個看不懂的 KeyError。
"""

from __future__ import annotations

from pathlib import Path

import yaml  # PyYAML;requirements.txt 已列

from .models import Guidance, SourcedRange, SourcedValue


def _require(d: dict, key: str, where: str):
    """從 dict 取值,取不到就丟出「看得懂」的錯誤。"""
    if key not in d:
        raise ValueError(f"設定檔缺少欄位:{where} 底下的 '{key}'。請檢查 assumptions.yaml。")
    return d[key]


def _parse_range(node: dict, where: str) -> SourcedRange:
    """把 YAML 的 {low, high, source} 轉成 SourcedRange。"""
    low = _require(node, "low", where)
    high = _require(node, "high", where)
    source = node.get("source", "(未標註來源)")
    note = node.get("note", "")
    if low > high:
        # 低點比高點大 → 多半是打字打反了,提早提醒
        raise ValueError(f"{where}:low({low}) 不應大於 high({high}),請檢查。")
    return SourcedRange(low=float(low), high=float(high), source=source, note=note)


def _parse_value(node: dict, where: str) -> SourcedValue:
    """把 YAML 的 {value, source} 轉成 SourcedValue。"""
    value = _require(node, "value", where)
    source = node.get("source", "(未標註來源)")
    note = node.get("note", "")
    return SourcedValue(value=float(value), source=source, note=note)


def load_guidance(config_path: str | Path) -> Guidance:
    """讀取假設檔 → Guidance 物件。

    參數:
        config_path: assumptions.yaml 的路徑
    回傳:
        Guidance
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到假設檔:{path}\n請先建立 config/assumptions.yaml。")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    quarter_label = _require(raw, "quarter_label", "最外層")

    # A. 法說會指引 -------------------------------------------
    g = _require(raw, "guidance", "最外層")
    revenue_usd = _parse_range(_require(g, "revenue_usd", "guidance"), "guidance.revenue_usd")
    gross_margin = _parse_range(_require(g, "gross_margin", "guidance"), "guidance.gross_margin")
    fx_usdtwd = _parse_value(_require(g, "fx_usdtwd", "guidance"), "guidance.fx_usdtwd")

    # B. 其他模型假設 -----------------------------------------
    m = _require(raw, "model_assumptions", "最外層")
    opex_ratio = _parse_value(_require(m, "opex_ratio", "model_assumptions"), "model_assumptions.opex_ratio")
    tax_rate = _parse_value(_require(m, "tax_rate", "model_assumptions"), "model_assumptions.tax_rate")
    non_op_ratio = _parse_value(_require(m, "non_op_ratio", "model_assumptions"), "model_assumptions.non_op_ratio")
    shares_bn = _parse_value(_require(m, "shares_billion", "model_assumptions"), "model_assumptions.shares_billion")

    return Guidance(
        quarter_label=str(quarter_label),
        revenue_usd=revenue_usd,
        gross_margin=gross_margin,
        fx_usdtwd=fx_usdtwd,
        opex_ratio=opex_ratio,
        tax_rate=tax_rate,
        non_op_ratio=non_op_ratio,
        shares_bn=shares_bn,
    )


def load_raw_config(config_path: str | Path) -> dict:
    """回傳原始 YAML dict (給需要讀 consensus / valuation 區塊的模組用)。"""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
