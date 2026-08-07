"""AI 產業鏈全景圖的資料組裝。

估值與月營收欄位一律重用 screener.evaluate()，本模組不重寫公式。
額外工作只包含：分類設定、非母體標的 best-effort 抓取、四大雲端 Capex、
循環股三取二標記，以及樣本足夠時的落後期相關性。
"""

from __future__ import annotations

import json
import math
import random
import statistics
from pathlib import Path

import yaml

from .cache import cache_get, cache_set
from .data_layer import (fetch_balance_pivot, fetch_cashflow_pivot,
                         fetch_daily_price_value, fetch_income_pivot,
                         fetch_month_revenue, fetch_price_daily_finmind,
                         month_revenue_momentum)
from .river import daily_pe_series
from .screener import evaluate, extract_metrics
from .us_data import build_us_record, compute_valuation
from .valuation_flag import pe_history_stats


def load_ai_chain_config(path: str | Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    layers = cfg.get("layers") or []
    if not layers:
        raise ValueError("ai_chain.yaml 缺少 layers")
    valid_markets = {"twse", "tpex", "us"}
    for layer in layers:
        if not layer.get("id") or not layer.get("name"):
            raise ValueError("每個 AI 產業鏈層級都需要 id/name")
        for member in layer.get("members") or []:
            if str(member.get("market")) not in valid_markets:
                raise ValueError(f"{member}: market 必須為 twse/tpex/us")
            member["id"] = str(member["id"])
    return cfg


def _df_series(df, names: tuple[str, ...]) -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    row = next((name for name in names if name in df.index), None)
    if row is None:
        return []
    out = []
    for col, raw in df.loc[row].items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value == value:
            out.append({"date": col.strftime("%Y-%m-%d"), "value": value})
    return sorted(out, key=lambda x: x["date"])


def fetch_us_quarterly(ticker: str, ttl_seconds: int = 12 * 3600) -> dict:
    """抓美股季度 Capex/營收。yfinance 免費端通常只有約 5 季。"""
    key = f"ai_chain_us_quarterly_{ticker}"
    cached = cache_get(key, ttl_seconds=ttl_seconds)
    if cached is not None:
        return cached["data"]

    import yfinance as yf

    t = yf.Ticker(ticker)
    qcf = t.quarterly_cashflow
    qi = t.quarterly_income_stmt
    capex = _df_series(qcf, ("Capital Expenditure", "Capital Expenditures"))
    revenue = _df_series(qi, ("Total Revenue", "Operating Revenue"))
    gross = _df_series(qi, ("Gross Profit",))
    eps = _df_series(qi, ("Diluted EPS", "Basic EPS"))
    # Capex 在現金流量表通常是負數；研究圖統一顯示投資額正值。
    for row in capex:
        row["value"] = abs(row["value"])
    data = {"ticker": ticker, "capex": capex, "revenue": revenue,
            "gross_profit": gross, "eps": eps,
            "source": "yfinance quarterly_cashflow / quarterly_income_stmt"}
    cache_set(key, data)
    return data


def _build_tpex_record(member: dict, cfg: dict) -> dict:
    sid = member["id"]
    key = f"ai_chain_tpex_record_{sid}"
    cached = cache_get(key, ttl_seconds=12 * 3600)
    if cached is not None:
        return cached["data"]

    rec = {"stock_id": sid, "name": member.get("name", sid), "market": "tpex",
           "currency": "TWD", "industry": member.get("industry", ""), "errors": []}
    start = cfg["fetch"]["financial_start"]
    inc, _ = fetch_income_pivot(sid, start_date=start)
    bal, _ = fetch_balance_pivot(sid, start_date=start)
    cf, _ = fetch_cashflow_pivot(sid, start_date=start)
    rec.update(extract_metrics(inc, bal, cf))
    pxv, _ = fetch_daily_price_value(sid, start_date=start)
    if pxv:
        recent = sorted(pxv, key=lambda x: x["date"])[-cfg["layer1"]["liquidity"]["days"]:]
        rec["price_last"] = recent[-1]["close"]
        rec["price_date"] = recent[-1]["date"]
        rec["liq_avg_value"] = sum(x["value"] for x in recent) / len(recent)
        rec["liq_days"] = len(recent)
    rec["valuation"] = compute_valuation(f"{sid}.TWO", rec.get("price_last"))
    prices, _ = fetch_price_daily_finmind(sid, start_date=start)
    pe_ser = daily_pe_series(prices, inc)
    rec["pe_hist"] = pe_history_stats(
        pe_ser, pe_ser[-1][1] if pe_ser else None,
        years=cfg["valuation_flag"]["pe_history_years"],
    ) or {"basis": "trailing_pe", "status": "insufficient"}
    mrows, _ = fetch_month_revenue(sid, start_date=cfg["fetch"].get("month_revenue_start", "2021-01-01"))
    rec["mrev"] = month_revenue_momentum(mrows, recent=cfg["fetch"].get("month_revenue_recent", 3))
    cache_set(key, rec)
    return rec


def load_member_records(chain_cfg: dict, screener_cfg: dict,
                        local_records: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """回傳可用 records 與不可用原因。非母體標的只存在 memory/cache，不寫 universe。"""
    records = {str(r["stock_id"]): r for r in local_records}
    unavailable: dict[str, str] = {}
    wanted = {m["id"]: m for layer in chain_cfg["layers"] for m in layer.get("members", [])}
    for sid, member in wanted.items():
        if sid in records:
            continue
        try:
            if member["market"] == "us":
                key = f"ai_chain_us_record_{sid}"
                cached = cache_get(key, ttl_seconds=12 * 3600)
                if cached is not None:
                    rec = cached["data"]
                else:
                    rec = build_us_record(sid, member.get("name", sid), screener_cfg)
                    cache_set(key, rec)
            elif member["market"] == "tpex":
                rec = _build_tpex_record(member, screener_cfg)
            else:
                unavailable[sid] = "不在目前母體，未另行抓取上市股"
                continue
            ph = rec.get("pe_hist") or {}
            if not rec.get("price_last") or ph.get("current_trailing_pe") is None:
                raise ValueError("缺股價或可同口徑比較的 trailing PE")
            records[sid] = rec
        except Exception as e:  # noqa: BLE001 - 單一額外標的失敗不影響整頁
            unavailable[sid] = f"資料抓取失敗:{type(e).__name__}: {e}"
    return records, unavailable


def _quarter_rows_from_pivot(piv: dict) -> list[dict]:
    rows = []
    for d, x in sorted(piv.items()):
        rev = x.get("Revenue")
        gp = x.get("GrossProfit")
        eps = next((x.get(k) for k in ("EPS", "DilutedEPS", "BasicEPS")
                    if x.get(k) is not None), None)
        rows.append({"date": d, "revenue": rev,
                     "gross_margin": (gp / rev * 100) if rev and gp is not None else None,
                     "eps": eps})
    return rows


def quarterly_fundamentals(member: dict) -> list[dict]:
    if member["market"] in ("twse", "tpex"):
        piv, _ = fetch_income_pivot(member["id"], start_date="2015-01-01")
        return _quarter_rows_from_pivot(piv)
    q = fetch_us_quarterly(member["id"])
    maps = {k: {x["date"]: x["value"] for x in q[k]}
            for k in ("revenue", "gross_profit", "eps")}
    dates = sorted(set().union(*[set(x) for x in maps.values()]))
    return [{"date": d, "revenue": maps["revenue"].get(d),
             "gross_margin": (maps["gross_profit"].get(d) / maps["revenue"][d] * 100)
             if maps["revenue"].get(d) and maps["gross_profit"].get(d) is not None else None,
             "eps": maps["eps"].get(d)} for d in dates]


def classify_cyclical(member: dict, cfg: dict) -> dict:
    """沿用 cyclical-strategy-backtest 的近10年三取二定義。"""
    c = cfg["cyclical_definition"]
    try:
        rows = quarterly_fundamentals(member)[-(c["lookback_years"] * 4):]
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "reason": f"季度資料失敗:{e}"}
    if len(rows) < c["min_quarters"]:
        return {"status": "unknown", "reason": f"季度樣本 {len(rows)}<{c['min_quarters']}"}

    gm = [r["gross_margin"] for r in rows if r["gross_margin"] is not None]
    gm_std = statistics.pstdev(gm) if len(gm) >= 2 else None
    by_date = {r["date"]: r for r in rows}
    eps_yoy, rev_yoy = [], []
    for r in rows:
        prev = f"{int(r['date'][:4]) - 1:04d}{r['date'][4:]}"
        old = by_date.get(prev)
        if not old:
            continue
        if old.get("eps") is not None and old["eps"] > 0 and r.get("eps") is not None:
            eps_yoy.append((r["eps"] - old["eps"]) / abs(old["eps"]) * 100)
        if old.get("revenue") and r.get("revenue") is not None:
            rev_yoy.append((r["revenue"] / old["revenue"] - 1) * 100)
    worst = min(eps_yoy) if eps_yoy else None
    rev_range = max(rev_yoy) - min(rev_yoy) if len(rev_yoy) >= 2 else None
    tests = [None if gm_std is None else gm_std > c["gross_margin_std_pp"],
             None if worst is None else -worst > c["eps_yoy_drop_pct"],
             None if rev_range is None else rev_range > c["revenue_yoy_range_pp"]]
    hits = sum(x is True for x in tests)
    misses = sum(x is False for x in tests)
    if hits >= c["need_hits"]:
        status = "cyclical"
    elif misses >= (len(tests) - c["need_hits"] + 1):
        status = "non_cyclical"
    else:
        status = "unknown"
    return {"status": status, "hits": hits,
            "available_tests": sum(x is not None for x in tests),
            "n_quarters": len(rows), "gm_std": gm_std,
            "worst_eps_yoy": worst, "revenue_yoy_range": rev_range}


def _calendar_quarter(d: str) -> str:
    y, m = int(d[:4]), int(d[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"


def _shift_quarter(q: str, lag: int) -> str:
    y, n = int(q[:4]), int(q[-1])
    z = y * 4 + (n - 1) + lag
    return f"{z // 4}-Q{z % 4 + 1}"


def yoy_series(rows: list[dict], value_key: str = "value") -> list[dict]:
    by_q = {_calendar_quarter(x["date"]): x[value_key] for x in rows if x.get(value_key) is not None}
    out = []
    for q, value in sorted(by_q.items()):
        prev = f"{int(q[:4]) - 1}{q[4:]}"
        if prev in by_q and by_q[prev] != 0:
            out.append({"quarter": q, "yoy": (value / by_q[prev] - 1) * 100})
    return out


def aggregate_cloud_capex(tickers: list[str]) -> dict:
    per_company, by_quarter = {}, {}
    errors = {}
    for ticker in tickers:
        try:
            q = fetch_us_quarterly(ticker)
            rows = q["capex"]
            per_company[ticker] = rows
            for row in rows:
                quarter = _calendar_quarter(row["date"])
                by_quarter.setdefault(quarter, {})[ticker] = row["value"]
        except Exception as e:  # noqa: BLE001
            errors[ticker] = str(e)
    combined = [{"quarter": q, "value": sum(v.values()), "n": len(v),
                 "members": sorted(v)} for q, v in sorted(by_quarter.items())]
    by_q = {x["quarter"]: x for x in combined}
    yoy = []
    for row in combined:
        q = row["quarter"]
        prev = f"{int(q[:4]) - 1}{q[4:]}"
        old = by_q.get(prev)
        # 只有兩期都含四家公司才是可比較合計；部分基期不可拿來算 YoY。
        if old and row["n"] == len(tickers) and old["n"] == len(tickers) and old["value"] != 0:
            yoy.append({"quarter": q, "yoy": (row["value"] / old["value"] - 1) * 100,
                        "n": len(tickers)})
    return {"companies": per_company, "combined": combined, "yoy": yoy, "errors": errors,
            "source": "yfinance quarterly_cashflow: Capital Expenditure"}


def _revenue_yoy(member: dict) -> list[dict]:
    rows = quarterly_fundamentals(member)
    return yoy_series([{"date": r["date"], "value": r["revenue"]} for r in rows if r.get("revenue")])


def aggregate_layer_revenue(members: list[dict], available_ids: set[str]) -> list[dict]:
    series = []
    for m in members:
        if m["id"] not in available_ids:
            continue
        try:
            series.append({x["quarter"]: x["yoy"] for x in _revenue_yoy(m)})
        except Exception:
            continue
    quarters = sorted(set().union(*[set(s) for s in series])) if series else []
    out = []
    for q in quarters:
        vals = [s[q] for s in series if q in s]
        if vals:
            out.append({"quarter": q, "yoy": sum(vals) / len(vals), "n": len(vals)})
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    dx, dy = [x - mx for x in xs], [y - my for y in ys]
    den = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    return sum(x * y for x, y in zip(dx, dy)) / den if den else None


def _permutation_p(xs: list[float], ys: list[float], observed: float,
                   draws: int = 1000) -> float:
    rng = random.Random(20260807)
    extreme = 0
    work = list(ys)
    for _ in range(draws):
        rng.shuffle(work)
        r = _pearson(xs, work)
        if r is not None and abs(r) >= abs(observed):
            extreme += 1
    return (extreme + 1) / (draws + 1)


def lag_correlations(capex_yoy: list[dict], layer_yoy: list[dict], cfg: dict) -> dict:
    """Capex(t) 對營收(t+lag)；樣本/顯著性不足則不宣稱傳導期。"""
    cap = {x["quarter"]: x["yoy"] for x in capex_yoy}
    rev = {x["quarter"]: x["yoy"] for x in layer_yoy}
    results = []
    for lag in range(cfg["transmission"]["max_lag_quarters"] + 1):
        xs, ys = [], []
        for q in sorted(cap):
            q2 = _shift_quarter(q, lag)
            if q2 in rev:
                xs.append(cap[q]); ys.append(rev[q2])
        r = _pearson(xs, ys)
        p = _permutation_p(xs, ys, r) if r is not None and len(xs) >= cfg["transmission"]["min_pairs"] else None
        results.append({"lag": lag, "n": len(xs), "r": r, "p": p})
    p_cut = cfg["transmission"]["significance_p"] / len(results)
    valid = [x for x in results if x["n"] >= cfg["transmission"]["min_pairs"]
             and x["p"] is not None and x["p"] < p_cut]
    best = max(valid, key=lambda x: abs(x["r"])) if valid else None
    return {"results": results, "best": best, "p_cut": p_cut,
            "status": "ok" if best else "insufficient_or_not_significant"}


def build_ai_chain_data(chain_cfg: dict, screener_cfg: dict,
                        records: list[dict], screen_results: list) -> dict:
    record_map, unavailable = load_member_records(chain_cfg, screener_cfg, records)
    screen_map = {r.stock_id: r for r in screen_results}
    # 額外標的也走同一個 evaluate()，不重寫任何估值或動能公式。
    for sid, rec in record_map.items():
        if sid not in screen_map:
            screen_map[sid] = evaluate(rec, screener_cfg)

    cloud = aggregate_cloud_capex(chain_cfg["cloud_capex"]["tickers"])
    layers = []
    for layer in chain_cfg["layers"]:
        nodes = []
        for m in layer.get("members") or []:
            r = screen_map.get(m["id"])
            cycle = classify_cyclical(m, chain_cfg) if m["id"] in record_map else {
                "status": "unknown", "reason": unavailable.get(m["id"], "無資料")}
            nodes.append({"member": m, "result": r, "cycle": cycle,
                          "unavailable": unavailable.get(m["id"])})
        available = {m["id"] for m in layer.get("members") or [] if m["id"] in record_map}
        rev_yoy = aggregate_layer_revenue(layer.get("members") or [], available)
        pcts = [screen_map[sid].metrics.get("pe_pct") for sid in available if sid in screen_map]
        pcts = [x for x in pcts if x is not None]
        mrev = [screen_map[sid].metrics.get("mrev_yoy_recent") for sid in available if sid in screen_map]
        mrev = [x for x in mrev if x is not None]
        layers.append({**layer, "nodes": nodes, "revenue_yoy": rev_yoy,
                       "avg_pe_pct": sum(pcts) / len(pcts) if pcts else None,
                       "pe_pct_n": len(pcts),
                       "avg_mrev_yoy": sum(mrev) / len(mrev) if mrev else None,
                       "mrev_n": len(mrev),
                       "transmission": lag_correlations(cloud["yoy"], rev_yoy, chain_cfg)})
    cloud["available_companies"] = sum(bool(x) for x in cloud.get("companies", {}).values())
    return {"cloud": cloud, "layers": layers, "unavailable": unavailable,
            "guidance": chain_cfg["cloud_capex"].get("guidance") or {}}
