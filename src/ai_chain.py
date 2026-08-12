"""AI 產業鏈全景圖的資料組裝。

估值與月營收欄位一律重用 screener.evaluate()，本模組不重寫公式。
額外工作只包含：分類設定、非母體標的 best-effort 抓取、四大雲端 Capex、
循環股三取二標記，以及樣本足夠時的落後期相關性。
"""

from __future__ import annotations

import json
import math
import random
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .cache import cache_get, cache_set
from .ai_quotes import expected_quote_tickers, load_quote_snapshot
from .data_layer import (fetch_balance_pivot, fetch_cashflow_pivot,
                         fetch_daily_price_value, fetch_income_pivot,
                         fetch_month_revenue, fetch_price_daily_finmind,
                         month_revenue_momentum)
from .river import current_trailing_pe, daily_pe_series, supports_tw_filing_fallback
from .screener import evaluate, extract_metrics
from .us_data import build_us_record, compute_valuation
from .valuation_flag import (pe_history_is_compatible, pe_history_stats,
                             tw_pe_source_coverage)
from .tw_quotes import expected_tw_quote_tickers, load_tw_quote_snapshot

_TW_TZ = timezone(timedelta(hours=8))


def load_ai_chain_config(path: str | Path) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
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
    _validate_guidance(cfg)
    _validate_output_side(cfg)
    ids = {m["id"] for layer in layers for m in layer.get("members") or []}
    ids.update(cfg.get("cloud_capex", {}).get("tickers") or [])
    ids.update(m.get("company") for m in (cfg.get("output_side", {}).get("metrics") or []))
    logos = cfg.get("logos") or {}
    missing = sorted(x for x in ids if x and x not in logos)
    if missing:
        raise ValueError(f"ai_chain.yaml 缺少 Logo 設定:{missing}")
    for sid in ids:
        meta = logos[sid]
        if not meta.get("domain") or not str(meta.get("file", "")).startswith("assets/logos/"):
            raise ValueError(f"{sid} Logo 需要 domain 與 assets/logos/ 路徑")
        if not (cfg_path.parent.parent / meta["file"]).exists():
            raise ValueError(f"{sid} Logo 檔不存在:{meta['file']}")
    return cfg


def _validate_guidance(cfg: dict) -> None:
    cloud = cfg.get("cloud_capex") or {}
    tickers = [str(x) for x in cloud.get("tickers") or []]
    guidance = cloud.get("guidance") or {}
    kinds = {"approximate", "minimum", "range", "undisclosed"}
    bases = {"calendar_year", "fiscal_year", "quarter"}
    directions = {"up", "down", "unchanged", "yoy_increase", "not_stated"}
    for ticker in tickers:
        company = guidance.get(ticker)
        if not isinstance(company, dict) or not isinstance(company.get("entries", []), list):
            raise ValueError(f"{ticker} guidance 必須是含 entries 的 object")
        for i, entry in enumerate(company.get("entries") or [], 1):
            amount, period = entry.get("amount") or {}, entry.get("period") or {}
            kind = amount.get("kind")
            if kind not in kinds or not amount.get("unit"):
                raise ValueError(f"{ticker} 指引#{i}:amount.kind/unit 不完整")
            if kind in ("approximate", "minimum") and not isinstance(amount.get("value"), (int, float)):
                raise ValueError(f"{ticker} 指引#{i}:amount.value 必須是數字")
            if kind == "range":
                if not isinstance(amount.get("low"), (int, float)) or not isinstance(amount.get("high"), (int, float)):
                    raise ValueError(f"{ticker} 指引#{i}:range 需要 low/high")
                if amount["low"] > amount["high"]:
                    raise ValueError(f"{ticker} 指引#{i}:range low 不可大於 high")
            if kind == "undisclosed" and not amount.get("text"):
                raise ValueError(f"{ticker} 指引#{i}:未揭露數字時必須填 text")
            if period.get("basis") not in bases or not period.get("label"):
                raise ValueError(f"{ticker} 指引#{i}:period.basis/label 不完整")
            if entry.get("direction") not in directions:
                raise ValueError(f"{ticker} 指引#{i}:direction 不在允許清單")
            if not entry.get("source") or not entry.get("source_date"):
                raise ValueError(f"{ticker} 指引#{i}:source/source_date 必填")
            raw_date = str(entry["source_date"])
            try:
                if len(raw_date) == 7:
                    date.fromisoformat(raw_date + "-01")
                elif len(raw_date) == 10:
                    date.fromisoformat(raw_date)
                else:
                    raise ValueError
            except ValueError as e:
                raise ValueError(f"{ticker} 指引#{i}:source_date 必須為 YYYY-MM 或 YYYY-MM-DD") from e
        for i, actual in enumerate(company.get("reported_actuals") or [], 1):
            amount, period = actual.get("amount") or {}, actual.get("period") or {}
            if amount.get("kind") != "reported" or not isinstance(amount.get("value"), (int, float)) or not amount.get("unit"):
                raise ValueError(f"{ticker} 實際值#{i}:amount 必須含 reported/value/unit")
            if period.get("basis") != "trailing_12_months" or not period.get("label"):
                raise ValueError(f"{ticker} 實際值#{i}:period 必須明列 trailing_12_months/label")
            if not isinstance(actual.get("yoy_pct"), (int, float)):
                raise ValueError(f"{ticker} 實際值#{i}:yoy_pct 必須是數字")
            if not actual.get("source") or not actual.get("source_date"):
                raise ValueError(f"{ticker} 實際值#{i}:source/source_date 必填")
            try:
                date.fromisoformat(str(actual["source_date"]))
            except ValueError as e:
                raise ValueError(f"{ticker} 實際值#{i}:source_date 必須為 YYYY-MM-DD") from e


def _validate_output_side(cfg: dict) -> None:
    out = cfg.get("output_side") or {}
    as_of = str(out.get("as_of_period") or "")
    if not re.fullmatch(r"\d{4}Q[1-4]", as_of):
        raise ValueError("output_side.as_of_period 需為 YYYYQn")
    valid_types = {"level", "growth_rate"}
    valid_status = {"disclosed", "not_disclosed"}
    seen = set()
    for metric in out.get("metrics") or []:
        mid = metric.get("id")
        if not mid or mid in seen:
            raise ValueError(f"output_side metric id 缺失或重複:{mid}")
        seen.add(mid)
        if not metric.get("company") or not metric.get("name") or not metric.get("unit"):
            raise ValueError(f"{mid}:company/name/unit 必填")
        if metric.get("value_type") not in valid_types:
            raise ValueError(f"{mid}:value_type 必須為 level/growth_rate")
        if metric.get("period_basis") not in {"calendar_quarter", "fiscal_quarter"}:
            raise ValueError(f"{mid}:period_basis 必須為 calendar_quarter/fiscal_quarter")
        periods = set()
        calendar_periods = set()
        for i, obs in enumerate(metric.get("observations") or [], 1):
            period = str(obs.get("period") or "")
            fiscal = metric.get("period_basis") == "fiscal_quarter"
            if fiscal:
                valid_period = re.fullmatch(r"FY\d{2,4}Q[1-4]", period)
            else:
                valid_period = re.fullmatch(r"\d{4}Q[1-4]", period)
            if not valid_period or period in periods:
                raise ValueError(f"{mid} observation#{i}:period 格式錯誤或重複")
            periods.add(period)
            if fiscal and not obs.get("calendar_period"):
                raise ValueError(f"{mid} {period}:fiscal_quarter 必須填 calendar_period")
            if not fiscal and obs.get("calendar_period") not in (None, period):
                raise ValueError(f"{mid} {period}:日曆季不可映射成其他 calendar_period")
            comparable_period = str(obs.get("calendar_period") or period)
            if (not re.fullmatch(r"\d{4}Q[1-4]", comparable_period)
                    or comparable_period in calendar_periods):
                raise ValueError(f"{mid} {period}:calendar_period 需為 YYYYQn")
            calendar_periods.add(comparable_period)
            if _quarter_index(comparable_period) > _quarter_index(as_of):
                raise ValueError(f"{mid} {period}:不可晚於 as_of_period {as_of}")
            period_end = obs.get("period_end")
            if fiscal and not period_end:
                raise ValueError(f"{mid} {period}:fiscal_quarter 必須填 period_end")
            end_date = None
            if period_end:
                try:
                    end_date = date.fromisoformat(str(period_end))
                except ValueError as e:
                    raise ValueError(f"{mid} {period}:period_end 需為 YYYY-MM-DD") from e
                end_period = f"{end_date.year}Q{(end_date.month - 1) // 3 + 1}"
                if end_period != comparable_period:
                    raise ValueError(f"{mid} {period}:calendar_period 與 period_end 不一致")
            if obs.get("status") not in valid_status:
                raise ValueError(f"{mid} {period}:status 必須為 disclosed/not_disclosed")
            if obs["status"] == "disclosed" and not isinstance(obs.get("value"), (int, float)):
                raise ValueError(f"{mid} {period}:disclosed 必須填 value")
            if obs.get("kind", "exact") not in {"exact", "minimum", "derived"}:
                raise ValueError(f"{mid} {period}:kind 必須為 exact/minimum/derived")
            if obs.get("scope_break") not in (None, True, False):
                raise ValueError(f"{mid} {period}:scope_break 必須為 boolean")
            if obs["status"] == "not_disclosed" and obs.get("value") is not None:
                raise ValueError(f"{mid} {period}:not_disclosed 不可沿用 value")
            if not obs.get("source") or not obs.get("disclosure_date"):
                raise ValueError(f"{mid} {period}:source/disclosure_date 必填")
            raw_date = str(obs["disclosure_date"])
            try:
                disclosed = date.fromisoformat(raw_date + "-01" if len(raw_date) == 7 else raw_date)
            except ValueError as e:
                raise ValueError(f"{mid} {period}:disclosure_date 需為 YYYY-MM 或 YYYY-MM-DD") from e
            year, quarter = int(comparable_period[:4]), int(comparable_period[-1])
            period_start = date(year, (quarter - 1) * 3 + 1, 1)
            if disclosed < period_start:
                raise ValueError(f"{mid} {period}:disclosure_date 不可早於所屬季度")
            if disclosed > datetime.now(_TW_TZ).date():
                raise ValueError(f"{mid} {period}:disclosure_date 不可晚於今天")
            if end_date and disclosed < end_date:
                raise ValueError(f"{mid} {period}:disclosure_date 不可早於 period_end")


def build_output_side(cfg: dict) -> dict:
    """以 as_of 季為準計算最新/前期/方向；缺季不冒充未揭露或連續。"""
    output_cfg = cfg.get("output_side") or {}
    flat = float(output_cfg.get("flat_threshold_pct", 3))
    as_of = str(output_cfg["as_of_period"])
    rows, counts = [], {"accel": 0, "decel": 0, "flat": 0,
                       "not_disclosed": 0, "pending": 0, "insufficient": 0}
    for metric in output_cfg.get("metrics") or []:
        obs = sorted(metric.get("observations") or [],
                     key=lambda x: x.get("calendar_period") or x["period"])
        by_period = {x.get("calendar_period") or x["period"]: x for x in obs}
        previous_period = _shift_compact_quarter(as_of, -1)
        previous2_period = _shift_compact_quarter(as_of, -2)
        latest = by_period.get(as_of)
        previous = by_period.get(previous_period)
        previous2 = by_period.get(previous2_period)
        missing_streak = 0
        check_period = as_of
        while True:
            x = by_period.get(check_period)
            if x and x["status"] == "not_disclosed":
                missing_streak += 1
                check_period = _shift_compact_quarter(check_period, -1)
            else:
                break
        if latest is None:
            direction, direction_reason = "pending", "最新期尚未輸入"
        elif latest["status"] == "not_disclosed":
            direction, direction_reason = "not_disclosed", "公司本季未揭露可比數值"
        elif latest.get("kind", "exact") != "exact":
            direction, direction_reason = "insufficient", "下限／衍生值・不精算"
        elif latest.get("scope_break"):
            direction, direction_reason = "insufficient", "口徑斷點"
        elif not previous or previous["status"] != "disclosed":
            direction, direction_reason = "insufficient", "前期未揭露"
        elif previous.get("kind", "exact") != "exact":
            direction, direction_reason = "insufficient", "前期下限／衍生值・不精算"
        else:
            cur, old = float(latest["value"]), float(previous["value"])
            if metric["value_type"] == "growth_rate":
                delta = cur - old              # 百分點
            else:
                # level 要判「加速度」需連續三季:比較本季成長率與前季成長率。
                if previous.get("scope_break"):
                    direction, direction_reason = "insufficient", "口徑斷點"
                    counts[direction] += 1
                    rows.append({**metric, "latest": latest, "previous": previous,
                                 "previous2": previous2, "direction": direction,
                                 "direction_reason": direction_reason,
                                 "non_disclosure_streak": missing_streak})
                    continue
                if not previous2 or previous2["status"] != "disclosed":
                    direction, direction_reason = "insufficient", "需連續三季"
                    counts[direction] += 1
                    rows.append({**metric, "latest": latest, "previous": previous,
                                 "previous2": previous2, "direction": direction,
                                 "direction_reason": direction_reason,
                                 "non_disclosure_streak": missing_streak})
                    continue
                if (previous2.get("kind", "exact") != "exact"
                        or not old or not previous2["value"]):
                    direction, direction_reason = "insufficient", "前三期非精確值"
                    counts[direction] += 1
                    rows.append({**metric, "latest": latest, "previous": previous,
                                 "previous2": previous2, "direction": direction,
                                 "direction_reason": direction_reason,
                                 "non_disclosure_streak": missing_streak})
                    continue
                delta = ((cur / old - 1) - (old / float(previous2["value"]) - 1)) * 100
            direction = "accel" if delta > flat else "decel" if delta < -flat else "flat"
            direction_reason = f"可比數值變化 {delta:+.1f} 個百分點"
        counts[direction] += 1
        rows.append({**metric, "latest": latest, "previous": previous,
                      "previous2": previous2,
                      "direction": direction, "direction_reason": direction_reason,
                      "non_disclosure_streak": missing_streak})
    return {"metrics": rows, "counts": counts, "as_of_period": as_of,
            "scale_warning": output_cfg.get("scale_warning") or "",
            "scale_warning_source": output_cfg.get("scale_warning_source") or ""}


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
    key = f"ai_chain_us_quarterly_v2_{ticker}"
    cached = cache_get(key, ttl_seconds=ttl_seconds)
    if cached is not None:
        return cached["data"]

    import yfinance as yf

    t = yf.Ticker(ticker)
    qcf = t.quarterly_cashflow
    qi = t.quarterly_income_stmt
    try:
        ttm_cf = t.ttm_cashflow
    except Exception:
        ttm_cf = None
    capex = _df_series(qcf, ("Capital Expenditure", "Capital Expenditures"))
    revenue = _df_series(qi, ("Total Revenue", "Operating Revenue"))
    gross = _df_series(qi, ("Gross Profit",))
    eps = _df_series(qi, ("Diluted EPS", "Basic EPS"))
    # Capex 在現金流量表通常是負數；研究圖統一顯示投資額正值。
    for row in capex:
        row["value"] = abs(row["value"])
    ttm_capex_rows = _df_series(ttm_cf, ("Capital Expenditure", "Purchase Of PPE", "Capital Expenditures"))
    ttm_capex = ttm_capex_rows[-1] if ttm_capex_rows else None
    if ttm_capex:
        ttm_capex["value"] = abs(ttm_capex["value"])
    data = {"ticker": ticker, "capex": capex, "ttm_capex": ttm_capex, "revenue": revenue,
            "gross_profit": gross, "eps": eps,
            "source": "yfinance quarterly_cashflow / quarterly_income_stmt"}
    cache_set(key, data)
    return data


def _build_tpex_record(member: dict, cfg: dict) -> dict:
    sid = member["id"]
    key = f"ai_chain_tpex_record_v2_{sid}"
    cached = cache_get(key, ttl_seconds=12 * 3600)
    if cached is not None:
        rec = cached["data"]
        ph = rec.get("pe_hist") or {}
        years = cfg["valuation_flag"]["pe_history_years"]
        if (ph.get("status") == "ok" and ph.get("current_trailing_pe") is not None
                and pe_history_is_compatible(ph, "tpex", rec.get("price_date"), years)):
            return rec

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
    fallback_ok = supports_tw_filing_fallback(member.get("name", sid))
    pe_ser = daily_pe_series(prices, inc, fallback_ok)
    current_pe, current_date = current_trailing_pe(
        prices, inc, fallback_ok, rec.get("price_last"), rec.get("price_date"))
    rec["pe_hist"] = pe_history_stats(
        pe_ser, current_pe,
        years=cfg["valuation_flag"]["pe_history_years"],
        current_date=current_date, market="tpex",
        insufficient_reason=(None if fallback_ok else
                             "unsupported_foreign_issuer_filing_deadline"),
        source_coverage=tw_pe_source_coverage(
            prices, inc, cfg["valuation_flag"]["pe_history_years"]),
    )
    if (rec["pe_hist"].get("status") != "ok"
            or rec["pe_hist"].get("current_trailing_pe") is None
            or not pe_history_is_compatible(
                rec["pe_hist"], "tpex", rec.get("price_date"),
                cfg["valuation_flag"]["pe_history_years"])):
        raise ValueError("缺股價或可同口徑比較的 trailing PE")
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
                key = f"ai_chain_us_record_v2_{sid}"
                cached = cache_get(key, ttl_seconds=12 * 3600)
                rec = cached["data"] if cached is not None else None
                years = screener_cfg["valuation_flag"]["pe_history_years"]
                cached_ph = (rec or {}).get("pe_hist") or {}
                cached_ok = (rec is not None and cached_ph.get("status") == "ok"
                             and cached_ph.get("current_trailing_pe") is not None
                             and pe_history_is_compatible(
                                 cached_ph, "us", rec.get("price_date"), years))
                if not cached_ok:
                    rec = build_us_record(sid, member.get("name", sid), screener_cfg)
            elif member["market"] == "tpex":
                rec = _build_tpex_record(member, screener_cfg)
            else:
                unavailable[sid] = "不在目前母體，未另行抓取上市股"
                continue
            ph = rec.get("pe_hist") or {}
            years = screener_cfg["valuation_flag"]["pe_history_years"]
            if (not rec.get("price_last") or ph.get("status") != "ok"
                    or not pe_history_is_compatible(
                        ph, rec.get("market", "twse"), rec.get("price_date"), years)
                    or ph.get("current_trailing_pe") is None):
                raise ValueError("缺股價或可同口徑比較的 trailing PE")
            if member["market"] == "us" and not cached_ok:
                cache_set(key, rec)                # 只快取通過 current schema 的完整快照
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


def _quarter_index(q: str) -> int:
    compact = q.replace("-Q", "Q")
    return int(compact[:4]) * 4 + int(compact[-1]) - 1


def _shift_compact_quarter(q: str, lag: int) -> str:
    z = _quarter_index(q) + lag
    return f"{z // 4}Q{z % 4 + 1}"


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
            per_company[ticker] = q
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
                        records: list[dict], screen_results: list,
                        quotes_path: str | Path | None = None,
                        tw_quotes_path: str | Path | None = None) -> dict:
    quote_path = Path(quotes_path) if quotes_path else Path(__file__).resolve().parent.parent / "data/ai_chain_quotes.json"
    tw_quote_path = (Path(tw_quotes_path) if tw_quotes_path else
                     Path(__file__).resolve().parent.parent / "data/ai_chain_tw_quotes.json")
    us_snapshot = load_quote_snapshot(quote_path, expected_quote_tickers(chain_cfg))
    tw_snapshot = load_tw_quote_snapshot(
        tw_quote_path, expected_tw_quote_tickers(chain_cfg))
    us_quotes, tw_quotes = us_snapshot["quotes"], tw_snapshot["quotes"]
    quotes = {**us_quotes, **tw_quotes}
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
                          "quote": quotes.get(m["id"]),
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
            "logos": chain_cfg.get("logos") or {},
            "guidance": chain_cfg["cloud_capex"].get("guidance") or {},
            "output_side": build_output_side(chain_cfg),
            "quotes": quotes, "us_quotes": us_quotes, "tw_quotes": tw_quotes,
            "quote_updates": {"us": us_snapshot.get("updated_at") or "",
                              "tw": tw_snapshot.get("updated_at") or ""},
            "quote_sources": {"us": us_snapshot.get("source") or "",
                              "tw": tw_snapshot.get("source") or ""}}
