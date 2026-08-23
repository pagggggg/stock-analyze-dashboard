"""
估值旗標層 (valuation_flag.py)
=============================
只加旗標、不淘汰任何標的。用「個股自己的近N年每日本益比分布」給三段旗標:

  🟢 合理偏低:前瞻PEG < green_peg_below 且目前 trailing PE < 歷史 trailing PE 中位
  🔴 高估值警戒:目前 trailing PE > 歷史 trailing PE P90，或前瞻 PEG/PE 過高
  🟡 一般:其餘
  ⚪ 估值資料不足:沒有可同口徑比較的目前 trailing PE

★ 百分位一律用「個股自己的歷史」,不用全市場平均——不同產業 PE 水準天生不同。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from .river import _percentile


PE_SCHEMA_VERSION = 6
TW_PE_SCHEMA_VERSION = 7
US_PE_SCHEMA_VERSION = 8
TW_PE_BASIS = "trailing_pe_rolling"
US_PE_BASIS = "adjusted_trailing_pe_rolling"
TW_PE_METHOD = "finmind_basic_eps_statutory_fallback_financial_deadline_latest_restated"
US_PE_METHOD = "yahoo_reported_adjusted_eps_first_market_close_fx_normalized"


def _pe_schema(market: str, currency_conversion: bool = False,
               release_time_aware: bool = False) -> dict:
    if market == "us":
        return {
            "schema_version": US_PE_SCHEMA_VERSION if release_time_aware else PE_SCHEMA_VERSION,
            "basis": US_PE_BASIS,
            "method": (US_PE_METHOD if currency_conversion else
                       "yahoo_reported_adjusted_eps_first_market_close" if release_time_aware else
                       "yahoo_reported_adjusted_eps_earnings_date"),
            "eps_basis": ("Yahoo Reported EPS (adjusted; converted to quote currency)"
                          if currency_conversion else "Yahoo Reported EPS (adjusted)"),
            "availability": ("first US market close after release timestamp" if release_time_aware
                             else "earnings date + 1 calendar day"),
            "price_basis": "Yahoo Close (split-adjusted, not dividend-adjusted)",
        }
    return {
        "schema_version": TW_PE_SCHEMA_VERSION,
        "basis": TW_PE_BASIS,
        "method": TW_PE_METHOD,
        "eps_basis": "FinMind basic EPS (latest-restated values)",
        "availability": "statutory filing deadline fallback",
        "price_basis": "FinMind close",
    }


def pe_history_is_compatible(ph: dict, market: str, price_date: str | None,
                             years: int) -> bool:
    """Whether a stored PE snapshot was computed by the current, date-bound schema."""
    if not isinstance(ph, dict) or not price_date:
        return False
    schema = _pe_schema(market, bool(ph.get("currency_conversion")),
                        bool(ph.get("release_time_aware")))
    conversion = ph.get("currency_conversion") or {}
    if market == "us" and conversion and (
            conversion.get("from") != "EUR" or conversion.get("to") != "USD"
            or conversion.get("as_of") != price_date):
        return False
    if (ph.get("schema_version") != schema["schema_version"]
            or ph.get("basis") != schema["basis"]
            or ph.get("method") != schema["method"]
            or ph.get("market") != market
            or ph.get("current_date") != price_date
            or ph.get("as_of") != price_date
            or ph.get("years") != years
            or ph.get("status") not in {"ok", "insufficient"}):
        return False
    try:
        as_of = date.fromisoformat(price_date)
    except ValueError:
        return False
    try:
        cut = as_of.replace(year=as_of.year - years)
    except ValueError:
        cut = as_of.replace(year=as_of.year - years, day=28)
    coverage = ph.get("source_coverage") or {}
    if (ph.get("window_start") != cut.isoformat()
            or coverage.get("price_end") != price_date
            or not isinstance(coverage.get("price_n"), int)
            or coverage["price_n"] < 60):
        return False
    reason = ph.get("reason") or ""
    if reason != "financials_not_fetched" and (
            not isinstance(coverage.get("eps_n"), int) or coverage["eps_n"] < 4):
        return False
    if reason != "financials_not_fetched":
        try:
            eps_end = date.fromisoformat(coverage["eps_end"])
        except (KeyError, TypeError, ValueError):
            return False
        if eps_end < as_of - timedelta(days=370):
            return False
    if ph["status"] == "ok":
        try:
            current = float(ph["current_trailing_pe"])
        except (KeyError, TypeError, ValueError):
            return False
        if not math.isfinite(current) or current <= 0:
            return False
        if market == "us":
            if (not isinstance(coverage.get("eps_max_gap_days"), int)
                    or coverage["eps_max_gap_days"] > 150
                    or coverage.get("eps_pre_window_n", 0) < 4):
                return False
        elif (not isinstance(coverage.get("eps_max_gap_quarters"), int)
              or coverage["eps_max_gap_quarters"] > 1
              or coverage.get("eps_pre_window_n", 0) < 4):
            return False
        required = max(60, int(years * 252 * 0.60))
        return (isinstance(ph.get("n"), int) and ph["n"] >= required
                and all(ph.get(k) is not None for k in ("p10", "median", "p90")))
    return (reason in {"no_positive_pe_history", "financials_not_fetched",
                       "unsupported_foreign_issuer_filing_deadline",
                       "financial_eps_source_unavailable",
                       "financial_report_not_yet_available",
                       "current_trailing_pe_unavailable"}
            or reason == f"history_span_under_{years}_years"
            or reason.startswith("valid_days_under_"))


def tw_pe_source_coverage(price_rows: list[dict], income: dict, years: int = 5) -> dict:
    prices = []
    for row in price_rows or []:
        try:
            d = date.fromisoformat(row["date"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            prices.append(d)
    eps_dates = []
    for d, row in (income or {}).items():
        try:
            eps = float(row["EPS"])
            qend = date.fromisoformat(d)
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(eps):
            eps_dates.append(qend)
    prices = sorted(set(prices))
    eps_dates = sorted(set(eps_dates))
    relevant_eps = eps_dates
    cutoff = None
    if prices:
        as_of = prices[-1]
        try:
            cutoff = as_of.replace(year=as_of.year - years)
        except ValueError:
            cutoff = as_of.replace(year=as_of.year - years, day=28)
        relevant_eps = [d for d in eps_dates if d >= cutoff - timedelta(days=370)]
    pre_window_n = 0
    if cutoff is not None:
        pre_window_n = sum(cutoff - timedelta(days=370) <= d <= cutoff
                           for d in relevant_eps)
    indexes = [d.year * 4 + d.month // 3 - 1 for d in relevant_eps
               if d.month in (3, 6, 9, 12)]
    max_gap = max((b - a for a, b in zip(indexes, indexes[1:])), default=0)
    return {
        "price_start": prices[0].isoformat() if prices else None,
        "price_end": prices[-1].isoformat() if prices else None,
        "price_n": len(prices),
        "eps_start": relevant_eps[0].isoformat() if relevant_eps else None,
        "eps_end": relevant_eps[-1].isoformat() if relevant_eps else None,
        "eps_n": len(relevant_eps),
        "eps_max_gap_quarters": max_gap,
        "eps_pre_window_n": pre_window_n,
    }


def us_pe_source_coverage(hist, earnings_dates, years: int = 5,
                          eps_events: list[tuple[date, float]] | None = None) -> dict:
    prices = []
    if hist is not None and len(hist):
        try:
            for ts, close in hist["Close"].items():
                value = float(close)
                if math.isfinite(value) and value > 0:
                    prices.append(ts.date())
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    price_end = max(prices) if prices else None
    events = [d for d, _ in (eps_events or [])]
    if eps_events is None and earnings_dates is not None and len(earnings_dates) and "Reported EPS" in earnings_dates.columns:
        for ts, row in earnings_dates.iterrows():
            try:
                eps = float(row["Reported EPS"])
            except (TypeError, ValueError):
                continue
            if math.isfinite(eps) and (price_end is None or ts.date() <= price_end):
                events.append(ts.date())
    prices = sorted(set(prices))
    events = sorted(set(events))
    relevant_events = events
    if prices:
        as_of = prices[-1]
        try:
            cutoff = as_of.replace(year=as_of.year - years)
        except ValueError:
            cutoff = as_of.replace(year=as_of.year - years, day=28)
        relevant_events = [d for d in events if d >= cutoff - timedelta(days=370)]
    pre_window_n = 0
    if prices:
        pre_window_n = sum(cutoff - timedelta(days=370) <= d <= cutoff
                           for d in relevant_events)
    max_gap = max(((b - a).days for a, b in zip(relevant_events, relevant_events[1:])), default=0)
    return {
        "price_start": prices[0].isoformat() if prices else None,
        "price_end": prices[-1].isoformat() if prices else None,
        "price_n": len(prices),
        "eps_start": relevant_events[0].isoformat() if relevant_events else None,
        "eps_end": relevant_events[-1].isoformat() if relevant_events else None,
        "eps_n": len(relevant_events),
        "eps_max_gap_days": max_gap,
        "eps_pre_window_n": pre_window_n,
    }


def pe_source_regressed(old_ph: dict, new_ph: dict) -> bool:
    """Detect a materially smaller raw response before preserving the old snapshot."""
    old = (old_ph or {}).get("source_coverage") or {}
    new = (new_ph or {}).get("source_coverage") or {}
    if not old or not new:
        return False
    if old.get("price_end") and (not new.get("price_end") or new["price_end"] < old["price_end"]):
        return True
    if old.get("eps_end") and (not new.get("eps_end") or new["eps_end"] < old["eps_end"]):
        return True
    if old_ph.get("market") != "us" and old.get("eps_start"):
        try:
            old_start = date.fromisoformat(old["eps_start"])
            new_start = date.fromisoformat(new["eps_start"])
            new_as_of = date.fromisoformat(new_ph["current_date"])
            years = int(new_ph["years"])
            try:
                cutoff = new_as_of.replace(year=new_as_of.year - years)
            except ValueError:
                cutoff = new_as_of.replace(year=new_as_of.year - years, day=28)
            required_floor = cutoff - timedelta(days=370)
        except (KeyError, TypeError, ValueError):
            return True
        if new_start > old_start and old_start >= required_floor:
            return True
    old_price_n, new_price_n = old.get("price_n", 0), new.get("price_n", 0)
    old_eps_n, new_eps_n = old.get("eps_n", 0), new.get("eps_n", 0)
    if old_ph.get("status") == "ok":
        if old_ph.get("market") == "us":
            if (old.get("eps_max_gap_days", 0) <= 150 < new.get("eps_max_gap_days", 0)
                    or (old.get("eps_pre_window_n", 0) >= 4
                        and new.get("eps_pre_window_n", 0) < 4)):
                return True
        elif old.get("eps_max_gap_quarters", 0) <= 1 < new.get("eps_max_gap_quarters", 0):
            return True
        elif (old.get("eps_pre_window_n", 0) >= 4
              and new.get("eps_pre_window_n", 0) < 4):
            return True
    return ((old_price_n >= 60 and new_price_n < old_price_n * 0.90)
            or (old_eps_n >= 4 and new_eps_n < old_eps_n * 0.75))

FLAG = {
    "green": ("🟢", "合理偏低"),
    "yellow": ("🟡", "一般"),
    "red": ("🔴", "高估值警戒"),
    "na": ("⚪", "估值資料不足"),
}

# 紅旗必附警語(逐字,依需求)
RED_WARNING = (
    "此標的基本面通過篩選,但現價已隱含極高成長預期。買進前請書面回答:"
    "①你相信的成長劇本是什麼 ②什麼證據會證明它失敗 ③若劇本完全兌現,現價對應PE是多少。"
)


def pe_history_stats(pe_series: list, current_trailing_pe: float | None,
                      years: int = 5, min_days: int = 60,
                      current_date: str | None = None, market: str = "twse",
                      insufficient_reason: str | None = None,
                      source_coverage: dict | None = None,
                      currency_conversion: dict | None = None,
                      release_time_aware: bool = False) -> dict:
    """由 trailing PE 序列算近 N 年分布與「目前 trailing PE」所在百分位。

    歷史序列是收盤價÷依資料可用日落後的 TTM 實際 EPS，因此只能和目前 trailing PE 比；
    把 forward PE 放進來會使成長股系統性顯得較便宜，是已知的口徑混用。
    """
    schema = _pe_schema(market, bool(currency_conversion), release_time_aware)
    current_raw = (float(current_trailing_pe)
                   if current_trailing_pe is not None
                   and math.isfinite(current_trailing_pe) and current_trailing_pe > 0 else None)
    current = round(current_raw, 1) if current_raw is not None else None
    if current_date is None and pe_series:
        current_date = max(d for d, _ in pe_series)
    base = {**schema, "market": market,
            "years": years, "current_date": current_date, "as_of": current_date,
            "current_trailing_pe": current, "source_coverage": source_coverage or {}}
    if currency_conversion:
        base["currency_conversion"] = currency_conversion
    if release_time_aware:
        base["release_time_aware"] = True
    try:
        as_of = date.fromisoformat(current_date or "")
    except ValueError:
        return {**base, "status": "insufficient", "reason": "missing_current_date",
                "window_start": None, "n": 0}
    try:
        cut = as_of.replace(year=as_of.year - years)
    except ValueError:
        cut = as_of.replace(year=as_of.year - years, day=28)
    base["window_start"] = cut.isoformat()
    points = []
    for d, pe in pe_series:
        try:
            pd = date.fromisoformat(d)
            value = float(pe)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            points.append((pd, value))
    vals = sorted(pe for d, pe in points if cut < d <= as_of)
    if insufficient_reason:
        return {**base, "status": "insufficient", "reason": insufficient_reason,
                "n": len(vals)}
    if not points:
        return {**base, "status": "insufficient", "reason": "no_positive_pe_history",
                "n": 0}
    if current_raw is None:
        return {**base, "status": "insufficient",
                "reason": "current_trailing_pe_unavailable", "n": len(vals)}
    required = max(min_days, int(years * 252 * 0.60))
    if min(d for d, _ in points) > cut + timedelta(days=7):
        return {**base, "status": "insufficient",
                "reason": f"history_span_under_{years}_years", "n": len(vals)}
    if len(vals) < required:
        return {**base, "status": "insufficient",
                "reason": f"valid_days_under_{required}", "n": len(vals)}
    p10 = round(_percentile(vals, 0.1), 1)
    median = round(_percentile(vals, 0.5), 1)
    p90 = round(_percentile(vals, 0.9), 1)
    pct = None
    if current_raw is not None:
        less = sum(1 for v in vals if v < current_raw)
        equal = sum(1 for v in vals if v == current_raw)
        pct = round((less + 0.5 * equal) / len(vals) * 100, 0)
    return {**base, "status": "ok", "p10": p10, "median": median, "p90": p90,
            "percentile": pct, "n": len(vals)}


def historical_peg(annual: dict, price: float | None, years: int = 5) -> dict | None:
    """『歷史PEG』——完全不依賴分析師共識,只用實際財報 EPS 與現價。

    ★ 與前瞻PEG 口徑不同,兩者**不可直接比較、不可互相取代**:
        前瞻PEG = 現價÷今年共識EPS  ÷  共識預估成長率   (看未來,需分析師覆蓋)
        歷史PEG = 現價÷最新年度EPS  ÷  實際EPS年化成長率 (看過去,人人都有)
      台股多數個股(金融/傳產尤甚)沒有分析師覆蓋,前瞻PEG 永遠是空的;
      歷史PEG 至少提供一個『用已發生事實』算出的估值/成長對照。

    限制(誠實揭露):
      - 過去成長不代表未來,循環股在景氣高點會算出很漂亮的低 PEG,**最容易騙人**。
      - 起始年 EPS ≤ 0 無法算年化成長 → 回 None(不硬湊、不補值)。
      - 衰退(成長率 ≤ 0)不給 PEG(負 PEG 無意義),但仍回報成長率供判讀。
    """
    if not annual or not price or price <= 0:
        return None
    ys = sorted(int(y) for y, a in annual.items() if (a or {}).get("eps") is not None)
    if len(ys) < 2:
        return None
    window = ys[-years:] if len(ys) >= years else ys
    y0, y1 = window[0], window[-1]
    e0 = annual[str(y0)]["eps"]
    e1 = annual[str(y1)]["eps"]
    if e1 is None or e1 <= 0:                 # 最新年度虧損 → 本益比無意義
        return None
    trailing_pe = price / e1
    n = y1 - y0
    cagr = None
    if e0 is not None and e0 > 0 and n >= 1:
        cagr = ((e1 / e0) ** (1 / n) - 1) * 100
    peg = (trailing_pe / cagr) if (cagr and cagr > 0) else None
    return {
        "trailing_pe": round(trailing_pe, 1),
        "eps_cagr": round(cagr, 1) if cagr is not None else None,
        "peg": round(peg, 2) if peg is not None else None,
        "eps_last": e1, "year_from": y0, "year_to": y1, "span": n,
    }


def _us_reported_eps_events(earnings_dates,
                            release_time_aware: bool = False) -> list[tuple[date, float]]:
    """Yahoo earnings table -> deduplicated availability-date EPS events."""
    if earnings_dates is None or not len(earnings_dates):
        return []
    events_by_date: dict[date, float] = {}
    for ts, row in earnings_dates.iterrows():
        try:
            eps = float(row["Reported EPS"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(eps):
            if release_time_aware:
                try:
                    local = ts.tz_convert("America/New_York") if ts.tzinfo else ts
                    effective = local.date() + (timedelta(days=1) if local.hour >= 16 else timedelta())
                except (AttributeError, TypeError):
                    effective = ts.date() + timedelta(days=1)
            else:
                effective = ts.date() + timedelta(days=1)
            events_by_date.setdefault(effective, eps)
    return sorted(events_by_date.items())


def pe_series_us(hist, earnings_dates=None, years: int = 5,
                 eps_events: list[tuple[date, float]] | None = None,
                 fx_series: list[tuple[date, float]] | None = None,
                 release_time_aware: bool = False) -> list:
    """美股 point-in-time trailing PE:收盤 ÷ 已公告最近四季 Reported EPS。

    依發布時間取市場可交易的第一個收盤日；不再用年度 EPS 回填整個曆年。
    """
    if hist is None or not len(hist):
        return []
    events = (list(eps_events) if eps_events is not None else
              _us_reported_eps_events(earnings_dates, release_time_aware))
    if not events:
        return []
    fx_dates = [d for d, _ in (fx_series or [])]
    fx_values = [value for _, value in (fx_series or [])]
    out, run, i = [], [], 0
    for ts, row in hist.sort_index().iterrows():
        d = ts.date()
        while i < len(events) and events[i][0] <= d:
            event_date, eps = events[i]
            if run:
                gap = (event_date - run[-1][0]).days
                if gap < 45:
                    i += 1                          # 同季重複/修正列只留最早公告
                    continue
                if gap > 150:
                    run = []                        # 缺季時不可把跨五季四筆冒充 TTM
            run.append((event_date, eps))
            i += 1
        if len(run) < 4:
            continue
        if (d - run[-1][0]).days > 150:
            continue                                # 缺下一季時，不無限沿用舊 TTM
        ttm = sum(eps for _, eps in run[-4:])
        if fx_series:
            from bisect import bisect_right
            fx_i = bisect_right(fx_dates, d) - 1
            if fx_i < 0 or (d - fx_dates[fx_i]).days > 7:
                continue
            ttm *= fx_values[fx_i]
        try:
            close = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue
        if math.isfinite(close) and ttm > 0 and close > 0:
            out.append((d.isoformat(), close / ttm))
    return out


def us_pe_source_error(hist, earnings_dates, years: int = 5,
                       eps_events: list[tuple[date, float]] | None = None) -> str | None:
    """Detect malformed/truncated Yahoo inputs before classifying history as insufficient."""
    coverage = us_pe_source_coverage(hist, earnings_dates, years, eps_events)
    if not coverage["price_n"]:
        return "price_history_fetch_error"
    if coverage["price_n"] < 60:
        return "price_history_truncated"
    if eps_events is None and (earnings_dates is None or not len(earnings_dates)):
        return "earnings_dates_fetch_error"
    if eps_events is None and "Reported EPS" not in earnings_dates.columns:
        return "earnings_dates_invalid"

    if coverage["eps_n"] < 4:
        return "earnings_dates_invalid"

    as_of = date.fromisoformat(coverage["price_end"])
    try:
        cutoff = as_of.replace(year=as_of.year - years)
    except ValueError:
        cutoff = as_of.replace(year=as_of.year - years, day=28)
    listed = date.fromisoformat(coverage["price_start"])
    last_event = date.fromisoformat(coverage["eps_end"])
    # An established company should have the four-quarter warm-up needed at cutoff.
    if listed <= cutoff - timedelta(days=365):
        if coverage["eps_pre_window_n"] < 4:
            return "earnings_dates_truncated"
        if coverage["eps_max_gap_days"] > 150:
            return "earnings_dates_gap"
    if last_event < as_of - timedelta(days=200):
        return "earnings_dates_stale"
    return None


def compute_flag(current_trailing_pe: float | None, forward_pe: float | None,
                 forward_peg: float | None, pe_median: float | None,
                 pe_p90: float | None, cfg: dict) -> str:
    """回傳 green / yellow / red / na。"""
    vf = cfg.get("valuation_flag", {})
    if current_trailing_pe is None:
        return "na"
    # 歷史位階只用 trailing 對 trailing；forward PE / PEG 是獨立前瞻警戒。
    if ((vf.get("red_pe_above_p90", True) and pe_p90 is not None
         and current_trailing_pe > pe_p90)
            or (forward_peg is not None and forward_peg > vf.get("red_peg_above", 2.0))
            or (forward_pe is not None and forward_pe > vf.get("red_pe_above", 60))):
        return "red"
    if (forward_peg is not None and forward_peg < vf.get("green_peg_below", 1.0)
            and pe_median is not None and current_trailing_pe < pe_median):
        return "green"
    return "yellow"
