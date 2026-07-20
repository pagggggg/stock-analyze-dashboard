"""
樣本內 / 樣本外驗證 (validation.py)
===================================
過擬合防護的一環:把觸發事件依「公開日」切成兩段——
  樣本內 (IS):IS_OOS_SPLIT 之前
  樣本外 (OOS):IS_OOS_SPLIT 之後(含當日)
兩段各自算指標。若訊號只在 IS 有效、OOS 就失靈,代表很可能是過去那段行情的巧合。

注意:本專案的兩個門檻參數採用「題目給定的預設值」,並未在 IS 上搜尋最佳化,
故 IS/OOS 主要當作「跨期穩定度」檢查(而非最佳化後的樣本外驗證)。
"""

from __future__ import annotations


def split_is_oos(records: list[dict], split_date: str, key: str = "available_date"):
    """回傳 (樣本內, 樣本外) 兩個清單,依 record[key] 與 split_date 比較。"""
    is_recs = [r for r in records if r[key] < split_date]
    oos_recs = [r for r in records if r[key] >= split_date]
    return is_recs, oos_recs


def group_by_horizon(records: list[dict], months_list: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {m: [] for m in months_list}
    for r in records:
        m = r.get("months")
        if m in out:
            out[m].append(r)
    return out
