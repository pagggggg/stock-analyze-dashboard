"""
共用工具(util.py):設定載入、日期、財報可用日(前視偏誤處理)、百分位。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent


def load_config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


def to_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


# ---------------------------------------------------------------------
# 財報可用日:用法定申報期限,保守且不早於實際公布日(避免前視偏誤)
#   Q1(季底03-31)→ 5/15 ; Q2(06-30)→ 8/14 ; Q3(09-30)→ 11/14 ; Q4(12-31)→ 次年 3/31
# ---------------------------------------------------------------------
def fin_available_date(quarter_end: str) -> dt.date:
    d = to_date(quarter_end)
    y, m = d.year, d.month
    if m == 3:
        return dt.date(y, 5, 15)
    if m == 6:
        return dt.date(y, 8, 14)
    if m == 9:
        return dt.date(y, 11, 14)
    if m == 12:
        return dt.date(y + 1, 3, 31)
    # 非標準季底(少數公司)→ 保守加 45 天
    return d + dt.timedelta(days=45)


def quarter_label(quarter_end: str) -> str:
    d = to_date(quarter_end)
    q = (d.month - 1) // 3 + 1
    return f"{d.year}Q{q}"


def percentile_rank(value: float, history: list[float]) -> float | None:
    """value 在 history 中的百分位(0~100);history 為「可用當下」的歷史值。"""
    hist = [h for h in history if h is not None]
    if not hist:
        return None
    below = sum(1 for h in hist if h <= value)
    return below / len(hist) * 100.0
