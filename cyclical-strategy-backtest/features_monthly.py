"""
月營收訊號(features_monthly.py)—— 階段一補充測試
==================================================
把「三檔月營收加總 YoY」接到既有的週線決策格點上。

核心假設(本次要驗證的):季報落後 2-3 個月是階段一右側訊號失敗的主因;
月營收次月 10 日就公布,比季報快約 1.2~2.6 個月,可能改善進場時點。

前視偏誤處理:
  - 月營收(y,m)的可用日 = 次月 10 日(法定申報期限),見 sources_finmind.fetch_month_revenue。
  - 在週線格點 t,只能看到 avail <= t 的月份。

訊號欄位:
  agg_yoy_latest   最近一個「可得」月份的加總營收 YoY(%)
  agg_yoy_pos2     最近 2 個可得月份 YoY 皆 > 0(「由負轉正」由回測的上升緣觸發保證)
  agg_yoy_neg2     最近 2 個可得月份 YoY 皆 < 0(出場用)
"""
from __future__ import annotations

import sources_finmind as F
from util import to_date


def aggregate_monthly_yoy(stock_ids: list[str]) -> list[dict]:
    """三檔(或任意檔數)月營收「加總」後算 YoY。

    回傳 [{ym, avail, agg_revenue, yoy}] 由舊到新。
    只在「該月所有標的都有營收」時才納入,避免加總口徑跳動(例如某檔缺月)。
    """
    per = {sid: F.fetch_month_revenue(sid) for sid in stock_ids}
    # (ry, rm) -> {sid: revenue}
    table: dict[tuple[int, int], dict[str, float]] = {}
    avail_map: dict[tuple[int, int], str] = {}
    for sid, rows in per.items():
        for r in rows:
            k = (r["ry"], r["rm"])
            table.setdefault(k, {})[sid] = r["revenue"]
            avail_map[k] = r["avail"]
    keys = sorted(k for k, v in table.items() if len(v) == len(stock_ids))
    agg = {k: sum(table[k].values()) for k in keys}
    out = []
    for k in keys:
        y, m = k
        prev = (y - 1, m)
        if prev in agg and agg[prev] > 0:
            yoy = (agg[k] / agg[prev] - 1.0) * 100.0
            out.append({"ym": f"{y:04d}-{m:02d}", "avail": avail_map[k],
                        "agg_revenue": agg[k], "yoy": round(yoy, 3)})
    return out


def attach_monthly_signals(features: list[dict], agg_series: list[dict],
                           pos_months: int = 2, neg_months: int = 2) -> list[dict]:
    """把加總月營收 YoY 狀態貼到每個週線格點(嚴格用 avail <= t 的月份)。"""
    ser = sorted(agg_series, key=lambda x: x["avail"])
    out = []
    j = 0
    seen: list[dict] = []
    for f in features:
        t = f["date"]
        while j < len(ser) and ser[j]["avail"] <= t:
            seen.append(ser[j])
            j += 1
        g = dict(f)
        last = seen[-pos_months:] if len(seen) >= pos_months else []
        lastn = seen[-neg_months:] if len(seen) >= neg_months else []
        g["agg_yoy_latest"] = seen[-1]["yoy"] if seen else None
        g["agg_yoy_month"] = seen[-1]["ym"] if seen else None
        g["agg_yoy_pos2"] = bool(last) and all(x["yoy"] > 0 for x in last)
        g["agg_yoy_neg2"] = bool(lastn) and all(x["yoy"] < 0 for x in lastn)
        out.append(g)
    return out
