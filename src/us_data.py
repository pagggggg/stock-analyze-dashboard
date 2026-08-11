"""
美股資料 (us_data.py)
=====================
用 yfinance 把「一檔美股」抓成和台股 screener 相同的紀錄格式(data/universe/<TICKER>.json),
讓同一套兩層篩選邏輯可以跨市場共用。先做測試用(如 TSLA)。

對應(yfinance → 我們的欄位):
  income_stmt      Total Revenue / Gross Profit / Diluted(Basic) EPS / Net Income → annual
  balance_sheet    Total Assets / Total Liabilities.. / Total Debt / Cash / Stockholders Equity → annual_bs, latest_bs
  cashflow         Operating Cash Flow → annual_ocf;quarterly_cashflow → ocf_q
  history(max)     最早日=上市日代理(c1);近60日 Close×Volume=流動性;最後一筆=現價
  info + estimate  產業、前瞻PE/PEG/FCF Yield(估值檢查,僅參考)

★ 只用公開市場數據,無持倉/交易紀錄。美股年度財報 yfinance 約 5 個會計年度,足夠跑本篩選。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone

US_RIVER_TICKERS = frozenset({"ASML", "AMAT", "LRCX", "KLAC"})
US_DETAIL_SCHEMA_VERSION = 2
EXPECTED_CURRENCIES = {
    "ASML": ("USD", "EUR"),
    "AMAT": ("USD", "USD"),
    "LRCX": ("USD", "USD"),
    "KLAC": ("USD", "USD"),
}


def _get(df, row, col):
    """安全取 yfinance DataFrame 值,NaN/缺列回 None。"""
    try:
        v = df.loc[row, col]
    except (KeyError, TypeError, ValueError):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v == v else None      # 濾 NaN


def compute_valuation(ticker: str, price: float | None) -> dict | None:
    """估值檢查(僅供參考,不用於淘汰):前瞻PE / PEG / FCF Yield。跨市場共用。"""
    from .data_layer import fetch_yfinance_metrics
    try:
        yf, _ = fetch_yfinance_metrics(ticker)
    except Exception:  # noqa: BLE001
        return None
    e0, e1 = yf.get("eps_y0"), yf.get("eps_y1")
    mcap, fcf = yf.get("marketCap"), yf.get("fcf_ttm")
    cov = yf.get("n_y0") or yf.get("n_q0") or yf.get("n_y1")   # 分析師共識覆蓋家數
    fpe = (price / e0) if (price and e0 is not None and e0 > 0) else None
    g = ((e1 - e0) / e0 * 100) if (e0 and e1 and e0 != 0) else None
    peg = (fpe / g) if (fpe and g and g > 0) else None
    fy = (fcf / mcap * 100) if (fcf and mcap) else None
    return {"forward_pe": fpe, "peg": peg, "fcf_yield": fy, "growth_pct": g, "coverage": cov}


def _fx_history(pair: str, start: date, end: date) -> list[tuple[date, float]]:
    """從 Yahoo chart API 取日匯率；yfinance 對 FX 偶爾會錯判 delisted。"""
    from .cache import cache_get, cache_set

    key = f"yahoo_fx_{pair}"
    cached = cache_get(key, ttl_seconds=12 * 3600)
    if (cached is not None and cached.get("start_date", "9999-12-31") <= start.isoformat()
            and cached.get("end_date", "") >= end.isoformat()):
        return [(date.fromisoformat(d), float(value)) for d, value in cached["data"]]

    period1 = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=1), datetime.min.time(),
                                   tzinfo=timezone.utc).timestamp())
    import json
    import subprocess
    from urllib.parse import urlencode

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}?" + urlencode({
        "period1": period1, "period2": period2, "interval": "1d", "events": "history",
    })
    raw = subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "--retry-all-errors", "-A", "Mozilla/5.0", url],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    result = json.loads(raw)["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    rows = []
    exchange_tz = result.get("meta", {}).get("exchangeTimezoneName") or "UTC"
    from zoneinfo import ZoneInfo
    for ts, value in zip(result["timestamp"], closes):
        if value is not None:
            rows.append((datetime.fromtimestamp(ts, ZoneInfo(exchange_tz)).date(), float(value)))
    if not rows:
        raise RuntimeError(f"{pair} 無匯率歷史")
    cache_set(key, [[d.isoformat(), value] for d, value in rows],
              start_date=start.isoformat(), end_date=end.isoformat())
    return rows


def _normalized_eps_events(earnings_dates, quote_currency: str,
                           financial_currency: str, as_of: date,
                           release_time_aware: bool = False) -> tuple[
                               list[tuple[date, float]], list[tuple[date, float]] | None, str, float]:
    from .valuation_flag import _us_reported_eps_events

    events = _us_reported_eps_events(earnings_dates, release_time_aware)
    if not events or financial_currency == quote_currency:
        return events, None, "", 1.0
    if financial_currency != "EUR" or quote_currency != "USD":
        raise ValueError(f"不支援 EPS 幣別轉換:{financial_currency}->{quote_currency}")
    start = as_of - timedelta(days=365 * 11)
    events = [(d, eps) for d, eps in events if d >= start - timedelta(days=370)]
    fx = _fx_history("EURUSD=X", start, as_of)
    fx_dates = [d for d, _ in fx]
    latest_i = bisect_right(fx_dates, as_of) - 1
    if latest_i < 0 or (as_of - fx[latest_i][0]).days > 7:
        raise ValueError("找不到最新 EUR/USD 匯率")
    latest_rate = fx[latest_i][1]
    return (events, fx,
            f"ASML ADR 的 EUR Reported EPS 依各交易日當時 Yahoo EUR/USD 換算為 USD；"
            f"目前值採股價日可得的 {fx[latest_i][0]} 匯率 {latest_rate:.4f}", latest_rate)


def _quarterly_eps_rows(qi, earnings_dates, latest_fx: float = 1.0) -> list[dict]:
    """近八季 EPS，顯示用；非 USD 原生值以目前 FX 換算。"""
    if qi is None or not len(qi.columns) or earnings_dates is None:
        return []
    event_rows = []
    for ts, row in earnings_dates.iterrows():
        try:
            raw = float(row["Reported EPS"])
        except (KeyError, TypeError, ValueError):
            continue
        available = ts.date() + timedelta(days=1)
        event_rows.append((available, raw * latest_fx))
    event_rows.sort()
    out = []
    for col in sorted(qi.columns):
        raw = _get(qi, "Diluted EPS", col) or _get(qi, "Basic EPS", col)
        if raw is None:
            continue
        candidates = [(d, value) for d, value in event_rows
                      if col.date() <= d <= col.date() + timedelta(days=150)]
        value = candidates[0][1] if candidates else raw * latest_fx
        out.append({"period": col.strftime("%Y-%m-%d"), "eps": round(value, 4)})
    return out[-8:]


def build_us_record(ticker: str, name: str, cfg: dict) -> dict:
    """用 yfinance 組出美股的 screener 紀錄(和台股同 schema)。失敗記進 errors,不中斷。"""
    import yfinance as yf

    rec: dict = {"stock_id": ticker, "name": name or ticker, "market": "us",
                 "currency": "USD", "fetched": date.today().isoformat(),
                 "industry": "", "errors": []}
    err = rec["errors"]
    t = yf.Ticker(ticker)

    info = {}
    try:
        info = t.info or {}
        rec["industry"] = info.get("industry") or info.get("sector") or ""
    except Exception:  # noqa: BLE001 - metadata/estimate provide the required fallbacks
        pass
    try:
        metadata = t.get_history_metadata() or {}
    except Exception:  # noqa: BLE001
        metadata = {}
    rec["currency"] = str(info.get("currency") or metadata.get("currency") or "USD")
    if not rec["industry"]:
        rec["industry"] = "Semiconductor Equipment & Materials"

    def _safe(attr):
        try:
            return getattr(t, attr)
        except Exception as e:  # noqa: BLE001
            err.append(f"{attr}:{e}")
            return None

    inc = _safe("income_stmt")
    bs = _safe("balance_sheet")
    cf = _safe("cashflow")
    qi = _safe("quarterly_income_stmt")
    qcf = _safe("quarterly_cashflow")
    try:
        # Yahoo Close is split-adjusted but, unlike Adj Close, not dividend-adjusted.
        hist = t.history(period="max", auto_adjust=False, actions=True)
    except Exception as e:  # noqa: BLE001
        err.append(f"history:{e}")
        hist = None
    earnings_dates = None
    earnings_error = None
    for _ in range(3):
        try:
            # Recreate Ticker so yfinance does not retain a failed scraper/session state.
            earnings_dates = yf.Ticker(ticker).get_earnings_dates(limit=100)
            break
        except Exception as e:  # noqa: BLE001
            earnings_error = e
    if earnings_dates is None and earnings_error is not None:
        err.append(f"earnings_dates:{earnings_error}")
    estimate_currency = None
    try:
        estimates = t.earnings_estimate
        if estimates is not None and "currency" in estimates.columns:
            values = estimates["currency"].dropna()
            estimate_currency = str(values.iloc[0]) if len(values) else None
    except Exception as e:  # noqa: BLE001
        err.append(f"earnings_estimate_currency:{e}")

    # --- 年度損益 ---
    annual: dict[str, dict] = {}
    if inc is not None and len(inc.columns):
        for col in inc.columns:
            annual[str(col.year)] = {
                "revenue": _get(inc, "Total Revenue", col),
                "gross_profit": _get(inc, "Gross Profit", col),
                "eps": _get(inc, "Diluted EPS", col) or _get(inc, "Basic EPS", col),
                "parent_ni": _get(inc, "Net Income", col),
            }
    rec["annual"] = annual

    # --- 年度資產負債(年底)+ 最新一季 ---
    annual_bs: dict[str, dict] = {}
    latest_bs = None
    if bs is not None and len(bs.columns):
        for col in bs.columns:
            la = _get(bs, "Total Liabilities Net Minority Interest", col)
            ta = _get(bs, "Total Assets", col)
            if la is None or ta is None:
                continue
            annual_bs[str(col.year)] = {
                "liabilities": la, "total_assets": ta,
                "nci": _get(bs, "Minority Interest", col) or 0.0,
            }
        latest = max(bs.columns)
        ta = _get(bs, "Total Assets", latest)
        if ta:
            latest_bs = {
                "date": latest.strftime("%Y-%m-%d"),
                "liabilities": _get(bs, "Total Liabilities Net Minority Interest", latest),
                "total_assets": ta,
                "short_borrow": _get(bs, "Total Debt", latest),  # 美股用 Total Debt 當有息負債
                "long_borrow": None, "bonds": None,
                "cash": _get(bs, "Cash And Cash Equivalents", latest),
                "equity": _get(bs, "Stockholders Equity", latest),
            }
    rec["annual_bs"] = annual_bs
    rec["latest_bs"] = latest_bs

    # --- 年度 / 單季 OCF ---
    annual_ocf: dict[str, float] = {}
    if cf is not None and len(cf.columns):
        for col in cf.columns:
            v = _get(cf, "Operating Cash Flow", col)
            if v is not None:
                annual_ocf[str(col.year)] = v
    rec["annual_ocf"] = annual_ocf
    ocf_q: list = []
    if qcf is not None and len(qcf.columns):
        for col in sorted(qcf.columns):
            v = _get(qcf, "Operating Cash Flow", col)
            if v is not None:
                ocf_q.append([col.strftime("%Y-%m-%d"), v])
    rec["ocf_q"] = ocf_q[-12:]

    # --- 上市日代理(c1)+ 最新財報(c6)---
    rec["first_report"] = str(hist.index.min().date()) if (hist is not None and len(hist)) else None
    dates = []
    if inc is not None:
        dates += [c.strftime("%Y-%m-%d") for c in inc.columns]
    if qi is not None:
        dates += [c.strftime("%Y-%m-%d") for c in qi.columns]
    rec["latest_report"] = max(dates) if dates else None

    # --- 流動性 + 現價 ---
    if hist is not None and len(hist):
        days = cfg["layer1"]["liquidity"]["days"]
        recent = hist.tail(days)
        vals = (recent["Close"] * recent["Volume"]).dropna()
        rec["liq_avg_value"] = float(vals.mean()) if len(vals) else None
        rec["liq_days"] = int(len(vals))
        rec["price_last"] = float(hist["Close"].iloc[-1])
        rec["price_date"] = str(hist.index[-1].date())

    # --- 估值檢查(僅參考)+ 估值旗標用的個股近N年PE分布 ---
    if cfg["fetch"].get("valuation", True):
        valuation = compute_valuation(ticker, rec.get("price_last"))
        rec["valuation"] = valuation or {}
        try:
            from .data_layer import fetch_yfinance_metrics
            yf_raw, _ = fetch_yfinance_metrics(ticker)
        except Exception as e:  # noqa: BLE001
            err.append(f"dashboard_metrics:{e}")
            yf_raw = {}
        from .valuation_flag import (pe_history_stats, pe_series_us,
                                     us_pe_source_coverage, us_pe_source_error)
        from .river import build_pe_river_us

        try:
            current_date = str(hist.index[-1].date()) if hist is not None and len(hist) else None
            expected_quote, expected_financial = EXPECTED_CURRENCIES.get(
                ticker, (str(info.get("currency") or rec["currency"]),
                         str(info.get("financialCurrency") or estimate_currency or rec["currency"])))
            quote_currency = str(info.get("currency") or expected_quote)
            financial_currency = str(info.get("financialCurrency") or estimate_currency or expected_financial)
            if (quote_currency, financial_currency) != (expected_quote, expected_financial):
                raise ValueError(
                    f"{ticker} 幣別不符預期:{financial_currency}->{quote_currency}，"
                    f"預期 {expected_financial}->{expected_quote}")
            if not current_date:
                raise ValueError("缺 price_date，無法決定 FX as-of")
            eps_events, fx_series, fx_note, latest_fx = _normalized_eps_events(
                earnings_dates, quote_currency, financial_currency,
                date.fromisoformat(current_date), ticker in US_RIVER_TICKERS)
            years = cfg["valuation_flag"]["pe_history_years"]
            pe_ser = pe_series_us(hist, earnings_dates, years=years, eps_events=eps_events,
                                  fx_series=fx_series,
                                  release_time_aware=ticker in US_RIVER_TICKERS)
            current_tpe = pe_ser[-1][1] if pe_ser and pe_ser[-1][0] == current_date else None
            source_error = us_pe_source_error(hist, earnings_dates, years=years,
                                              eps_events=eps_events)
            if source_error:
                raise ValueError(source_error)
            new_pe_hist = pe_history_stats(
                pe_ser, current_tpe, years=years,
                current_date=current_date, market="us",
                source_coverage=us_pe_source_coverage(
                    hist, earnings_dates, years, eps_events),
                currency_conversion=(
                    {"from": financial_currency, "to": quote_currency,
                     "pair": "EURUSD=X", "rate": latest_fx,
                     "as_of": current_date,
                     "basis": "point-in-time daily FX; current estimates use latest FX"}
                    if financial_currency != quote_currency else None),
                release_time_aware=ticker in US_RIVER_TICKERS,
            )
            detail = None
            if ticker in US_RIVER_TICKERS:
                river = build_pe_river_us(hist, earnings_dates, years=years,
                                          eps_events=eps_events, fx_series=fx_series,
                                          source_note=fx_note)
                eps_y0, eps_y1 = yf_raw.get("eps_y0"), yf_raw.get("eps_y1")
                if financial_currency != quote_currency:
                    eps_y0 = eps_y0 * latest_fx if eps_y0 is not None else None
                    eps_y1 = eps_y1 * latest_fx if eps_y1 is not None else None
                growth = ((eps_y1 - eps_y0) / eps_y0 * 100) if eps_y0 and eps_y1 else None
                rec["valuation"].update({
                    "forward_pe": (rec.get("price_last") / eps_y0
                                   if rec.get("price_last") and eps_y0 else None),
                    "peg": ((rec.get("price_last") / eps_y0) / growth
                            if rec.get("price_last") and eps_y0 and growth and growth > 0 else None),
                    "growth_pct": growth,
                    "fcf_yield": ((float(yf_raw["fcf_ttm"]) * latest_fx)
                                  / float(yf_raw["marketCap"]) * 100
                                  if yf_raw.get("fcf_ttm") is not None and yf_raw.get("marketCap")
                                  else rec["valuation"].get("fcf_yield")),
                })
                detail = {
                    "schema_version": US_DETAIL_SCHEMA_VERSION,
                    "quote_currency": quote_currency,
                    "financial_currency": financial_currency, "fx_note": fx_note,
                    "latest_fx": latest_fx,
                    "shares_bn": ((yf_raw.get("sharesOutstanding")
                                   or info.get("sharesOutstanding") or 0) / 1e9),
                    "eps_y0": eps_y0, "eps_y1": eps_y1,
                    "growth_pct": growth, "yf": yf_raw,
                    "quarters": _quarterly_eps_rows(qi, earnings_dates, latest_fx),
                    "splits": ([[ts.date().isoformat(), float(value)]
                                for ts, value in hist["Stock Splits"].items()
                                if value == value and float(value) > 0]
                               if "Stock Splits" in hist.columns else []),
                    "river": river.__dict__,
                }
            rec["pe_hist"] = new_pe_hist
            if detail is not None:
                rec["detail"] = detail
        except Exception as e:  # noqa: BLE001 - _save preserves the prior complete snapshot
            rec["pe_refresh_error"] = f"calculation_error:{type(e).__name__}"
            err.append(f"pe_hist/detail:{e}")

    return rec
