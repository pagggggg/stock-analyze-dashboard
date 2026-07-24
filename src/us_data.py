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

from datetime import date


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
    fpe = (price / e0) if (price and e0) else None
    g = ((e1 - e0) / e0 * 100) if (e0 and e1 and e0 != 0) else None
    peg = (fpe / g) if (fpe and g and g > 0) else None
    fy = (fcf / mcap * 100) if (fcf and mcap) else None
    return {"forward_pe": fpe, "peg": peg, "fcf_yield": fy, "growth_pct": g}


def build_us_record(ticker: str, name: str, cfg: dict) -> dict:
    """用 yfinance 組出美股的 screener 紀錄(和台股同 schema)。失敗記進 errors,不中斷。"""
    import yfinance as yf

    rec: dict = {"stock_id": ticker, "name": name or ticker, "market": "us",
                 "currency": "USD", "fetched": date.today().isoformat(),
                 "industry": "", "errors": []}
    err = rec["errors"]
    t = yf.Ticker(ticker)

    try:
        info = t.info or {}
        rec["industry"] = info.get("industry") or info.get("sector") or ""
    except Exception as e:  # noqa: BLE001
        err.append(f"info:{e}")

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
        hist = t.history(period="max")
    except Exception as e:  # noqa: BLE001
        err.append(f"history:{e}")
        hist = None

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

    # --- 估值檢查(僅參考)---
    if cfg["fetch"].get("valuation", True):
        rec["valuation"] = compute_valuation(ticker, rec.get("price_last"))

    return rec
