"""
點時特徵(features.py)
======================
以「運價指數週線日期」為決策格點,建立每檔股票在每個決策日 t 的特徵,
全部嚴格使用「t 當下可得」的資料(避免前視偏誤):

  價格類:  close(用當日或最近一個交易日收盤)
  運價類:  freight, freight_pctile(擴張歷史百分位), rising_4w(連4週回升), turn_down(由升轉降)
  估值類:  pbr, pbr_pctile_10y(近10年百分位)
  基本面:  margin_up2(毛利率連2季升), margin_down2(連2季降),
           rev_yoy_pos(最近可得季營收YoY>0), profit_expansion(=margin_up2)

財報可用日採法定申報期限(util.fin_available_date),保守不早於實際公布。
"""
from __future__ import annotations

import bisect
import datetime as dt

import sources_finmind as F
import sources_freight as G
from util import fin_available_date, to_date


def _as_of_close(px_dates: list[str], px_close: list[float], t: str) -> float | None:
    """t 當日或之前最近交易日收盤。"""
    i = bisect.bisect_right(px_dates, t) - 1
    return px_close[i] if i >= 0 else None


def _expanding_pctile(sorted_vals: list[float], upto_idx: int, value: float) -> float | None:
    """在 vals[0..upto_idx] 中,value 的百分位(<=)。"""
    if upto_idx < 0:
        return None
    hist = sorted_vals[: upto_idx + 1]
    below = sum(1 for h in hist if h <= value)
    return below / len(hist) * 100.0


def build_features(stock_id: str, freight_rows: list[dict],
                   need_financials: bool = True) -> list[dict]:
    """回傳每個運價週線日期 t 的特徵 dict 串列(由舊到新)。"""
    px = F.fetch_prices(stock_id)
    px_dates = [r["date"] for r in px]
    px_close = [r["close"] for r in px]

    pbr = F.fetch_pbr(stock_id)
    pbr_dates = [r["date"] for r in pbr]
    pbr_vals = [r["pbr"] for r in pbr]

    fin = F.fetch_financials(stock_id) if need_financials else []
    # 財報(季)→ (available_date, quarter_end, gross_margin, revenue, eps)
    fin_avail = []
    for r in fin:
        fin_avail.append({
            "avail": fin_available_date(r["date"]),
            "qend": r["date"],
            "gm": r["gross_margin"],
            "rev": r["revenue"],
            "eps": r["eps"],
        })
    fin_avail.sort(key=lambda x: x["avail"])
    rev_by_q = {r["date"]: r["revenue"] for r in fin}

    fr_dates = [r["date"] for r in freight_rows]
    fr_vals = [r["close"] for r in freight_rows]

    out = []
    for i, t in enumerate(fr_dates):
        td = to_date(t)
        f = fr_vals[i]
        # 運價擴張歷史百分位(用到 i 為止)
        # 為求效率:直接對前 i+1 個值計數(週線資料量小,可接受)
        hist = fr_vals[: i + 1]
        f_pct = sum(1 for h in hist if h <= f) / len(hist) * 100.0

        rising_4w = False
        if i >= 4:
            rising_4w = all(fr_vals[i - k] > fr_vals[i - k - 1] for k in range(0, 4))
        # 運價方向確認(月營收版用):指數 > 4 週前(動能為正),比「連4週皆漲」寬鬆
        freight_up_4w = (i >= 4) and (fr_vals[i] > fr_vals[i - 4])
        # 由升轉降:4週動能由正翻負(前一週 >=4週前,本週 <4週前)
        turn_down = False
        if i >= 5:
            mom_now = fr_vals[i] - fr_vals[i - 4]
            mom_prev = fr_vals[i - 1] - fr_vals[i - 5]
            turn_down = (mom_prev >= 0) and (mom_now < 0)

        close = _as_of_close(px_dates, px_close, t)

        # PBR 近10年百分位
        j = bisect.bisect_right(pbr_dates, t) - 1
        pbr_now = pbr_vals[j] if j >= 0 else None
        pbr_pct = None
        if pbr_now is not None:
            lo = to_date(t).replace(year=td.year - 10).isoformat()
            k0 = bisect.bisect_left(pbr_dates, lo)
            window = pbr_vals[k0: j + 1]
            if window:
                pbr_pct = sum(1 for h in window if h <= pbr_now) / len(window) * 100.0

        # 可得財報(available <= t)
        avail_fin = [r for r in fin_avail if r["avail"] <= td]
        gms = [r["gm"] for r in avail_fin if r["gm"] is not None]
        margin_up2 = margin_down2 = False
        if len(gms) >= 3:
            margin_up2 = gms[-1] > gms[-2] and gms[-2] > gms[-3]
            margin_down2 = gms[-1] < gms[-2] and gms[-2] < gms[-3]
        # 最近可得季營收 YoY > 0
        rev_yoy_pos = False
        rev_yoy_val = None
        if avail_fin:
            last_q = avail_fin[-1]["qend"]
            ld = to_date(last_q)
            prev_q = f"{ld.year - 1:04d}-{ld.month:02d}-{ld.day:02d}"
            cur = rev_by_q.get(last_q)
            prv = rev_by_q.get(prev_q)
            if cur is not None and prv:
                rev_yoy_val = (cur / prv - 1.0) * 100.0
                rev_yoy_pos = rev_yoy_val > 0

        out.append({
            "date": t,
            "close": close,
            "freight": f,
            "freight_pctile": f_pct,
            "rising_4w": rising_4w,
            "freight_up_4w": freight_up_4w,
            "turn_down": turn_down,
            "pbr": pbr_now,
            "pbr_pctile_10y": pbr_pct,
            "margin_up2": margin_up2,
            "margin_down2": margin_down2,
            "profit_expansion": margin_up2,
            "rev_yoy": round(rev_yoy_val, 2) if rev_yoy_val is not None else None,
            "rev_yoy_pos": rev_yoy_pos,
            "n_fin_avail": len(avail_fin),
        })
    return out


def load_freight(cfg: dict, which: str | None = None) -> list[dict]:
    which = which or cfg["freight"]["primary"]
    pid = cfg["freight"][which]["pair_id"]
    return G.fetch_freight(pid)
