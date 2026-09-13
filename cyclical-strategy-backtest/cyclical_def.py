"""
循環股量化定義(cyclical_def.py)
=================================
近10年(或可得最長)季資料,三取二標記為循環股;輸出各項數值供人工複核:
  A) 季毛利率標準差 > 5 個百分點(pp)
  B) 曾單季 EPS 年減 > 50%(需去年同季 EPS 為正,避免負基期失真)
  C) 營收 YoY 振幅(max-min)> 60 個百分點(pp)

不做任何調參;只依 config 門檻計算並回報 True/False 與原始數值。
"""
from __future__ import annotations

import statistics

import sources_finmind as F
from util import load_config, quarter_label, to_date


def _revenue_yoy_series(fin: list[dict]) -> list[tuple[str, float]]:
    """季營收 YoY(%),需去年同季存在。回傳 [(quarter_end, yoy_pct)]。"""
    by_q = {r["date"]: r["revenue"] for r in fin if r["revenue"] is not None}
    out = []
    for r in fin:
        d = r["date"]
        rev = r["revenue"]
        if rev is None:
            continue
        dd = to_date(d)
        prev = f"{dd.year - 1:04d}-{dd.month:02d}-{dd.day:02d}"
        pr = by_q.get(prev)
        if pr and pr != 0:
            out.append((d, (rev / pr - 1.0) * 100.0))
    return out


def _eps_yoy_drops(fin: list[dict]) -> list[tuple[str, float]]:
    """單季 EPS YoY 變動(%),僅列去年同季 EPS>0 者。回傳 [(quarter_end, yoy_pct)]。"""
    by_q = {r["date"]: r["eps"] for r in fin if r["eps"] is not None}
    out = []
    for r in fin:
        d = r["date"]
        eps = r["eps"]
        if eps is None:
            continue
        dd = to_date(d)
        prev = f"{dd.year - 1:04d}-{dd.month:02d}-{dd.day:02d}"
        pe = by_q.get(prev)
        if pe is not None and pe > 0:
            out.append((d, (eps - pe) / abs(pe) * 100.0))
    return out


def classify(stock_id: str, cfg: dict | None = None, as_of: str | None = None) -> dict:
    """回傳判定結果與各項原始數值。as_of=None 用全期;否則只用季底 <= as_of 的資料。"""
    cfg = cfg or load_config()
    crit = cfg["cyclical_definition"]["criteria"]
    need = cfg["cyclical_definition"]["need_hits"]
    lookback_years = cfg["cyclical_definition"]["lookback_years"]

    fin = F.fetch_financials(stock_id)
    if as_of:
        fin = [r for r in fin if r["date"] <= as_of]
    # 近 N 年:取最後 lookback_years*4 季(有多少用多少)
    fin = fin[-(lookback_years * 4):] if len(fin) > lookback_years * 4 else fin

    gm = [r["gross_margin"] for r in fin if r["gross_margin"] is not None]
    gm_std = statistics.pstdev(gm) if len(gm) >= 2 else None

    eps_yoy = _eps_yoy_drops(fin)
    worst_eps_drop = min((v for _, v in eps_yoy), default=None)  # 最大跌幅(最負)

    rev_yoy = _revenue_yoy_series(fin)
    rev_vals = [v for _, v in rev_yoy]
    rev_range = (max(rev_vals) - min(rev_vals)) if len(rev_vals) >= 2 else None

    A = gm_std is not None and gm_std > crit["gross_margin_std_pp"]["value"]
    B = worst_eps_drop is not None and (-worst_eps_drop) > crit["eps_yoy_drop_pct"]["value"]
    C = rev_range is not None and rev_range > crit["revenue_yoy_range_pp"]["value"]
    hits = sum([A, B, C])

    return {
        "stock_id": stock_id,
        "n_quarters_used": len(gm),
        "period": f"{fin[0]['date']}→{fin[-1]['date']}" if fin else "n/a",
        "gross_margin_std_pp": round(gm_std, 3) if gm_std is not None else None,
        "worst_eps_yoy_drop_pct": round(worst_eps_drop, 2) if worst_eps_drop is not None else None,
        "revenue_yoy_range_pp": round(rev_range, 2) if rev_range is not None else None,
        "A_margin_std_gt5": A,
        "B_eps_drop_gt50": B,
        "C_rev_range_gt60": C,
        "hits": hits,
        "is_cyclical": hits >= need,
    }


def classify_table(stock_ids: list[str], cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config()
    return [classify(s, cfg) for s in stock_ids]


if __name__ == "__main__":
    cfg = load_config()
    ids = [s["id"] for s in cfg["universe"]["phase1_targets"]]
    ids.append(cfg["universe"]["controls"]["overfit"]["id"])  # 台積電對照
    names = {s["id"]: s["name"] for s in cfg["universe"]["phase1_targets"]}
    names[cfg["universe"]["controls"]["overfit"]["id"]] = cfg["universe"]["controls"]["overfit"]["name"]
    print("循環股量化定義(三取二);各項數值供人工複核\n")
    hdr = f"{'股票':<12}{'季數':>4}  {'毛利率std(pp)':>13} {'A':>2}  {'EPS最大年減%':>12} {'B':>2}  {'營收YoY振幅pp':>13} {'C':>2}  {'命中':>4} {'循環股':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in classify_table(ids, cfg):
        nm = f"{r['stock_id']} {names.get(r['stock_id'],'')}"
        print(f"{nm:<12}{r['n_quarters_used']:>4}  {str(r['gross_margin_std_pp']):>13} "
              f"{'✓' if r['A_margin_std_gt5'] else '·':>2}  {str(r['worst_eps_yoy_drop_pct']):>12} "
              f"{'✓' if r['B_eps_drop_gt50'] else '·':>2}  {str(r['revenue_yoy_range_pp']):>13} "
              f"{'✓' if r['C_rev_range_gt60'] else '·':>2}  {r['hits']:>4} {'是' if r['is_cyclical'] else '否':>6}")
