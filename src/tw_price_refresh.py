"""台股盤後價格同步：一次日價來源，同步母體 record 與 AI 行情快照。"""

from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import subprocess
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .cache import cache_get, cache_set
from .metrics import is_financial_company
from .river import (_filing_available_date, current_trailing_pe,
                    daily_pe_series, supports_tw_filing_fallback)
from .screener import extract_metrics
from .tw_quotes import (SCHEMA_VERSION as QUOTE_SCHEMA_VERSION,
                        SOURCE as QUOTE_SOURCE, expected_tw_quote_tickers,
                        quote_from_price_rows, validate_tw_quote_snapshot)
from .valuation_flag import (historical_peg, pe_history_is_compatible,
                             pe_history_stats, pe_source_regressed,
                             tw_pe_source_coverage)


def _number(value) -> float:
    return float(str(value).replace(",", "").strip())


def _market_day_ttl(day: date) -> int | None:
    return 6 * 3600 if (date.today() - day).days <= 1 else None


def _json_get(url: str) -> object:
    import requests

    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException:
        raw = subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "--retry-all-errors",
             "-A", "Mozilla/5.0", url],
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout
        return json.loads(raw)


def _fetch_twse_day(day: date, target_ids: set[str]) -> dict[str, dict]:
    key = f"twse_market_close_{day:%Y%m%d}"
    cached = cache_get(key, ttl_seconds=_market_day_ttl(day))
    if (cached is not None and cached.get("status") == "ok"
            and target_ids <= set(cached.get("target_ids") or [])):
        return {sid: row for sid, row in (cached.get("data") or {}).items()
                if sid in target_ids}
    url = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
           f"?date={day:%Y%m%d}&type=ALLBUT0999&response=json")
    payload = _json_get(url)
    if not isinstance(payload, dict):
        raise RuntimeError("TWSE 全市場日價格式錯誤")
    stat = str(payload.get("stat") or "")
    if stat != "OK":
        if "沒有符合條件" in stat or "查無資料" in stat:
            return {}
        raise RuntimeError(f"TWSE 全市場日價回傳異常:{stat}")
    compact = day.strftime("%Y%m%d")
    if str(payload.get("date") or "") != compact:
        raise RuntimeError(f"TWSE 全市場日價日期不符:{payload.get('date')}!={compact}")
    table = next((item for item in payload.get("tables") or []
                  if "證券代號" in (item.get("fields") or [])
                  and "收盤價" in (item.get("fields") or [])), None)
    if not table:
        raise RuntimeError("TWSE 全市場日價缺少股票表")
    fields = table["fields"]
    raw_data = table.get("data") or []
    if len(raw_data) < 500 or any(len(item) != len(fields) for item in raw_data):
        raise RuntimeError(f"TWSE 全市場日價原始表不完整:{len(raw_data)}")
    indexes = {name: fields.index(name) for name in ("證券代號", "收盤價", "成交金額")}
    rows = {}
    for item in raw_data:
        try:
            sid = str(item[indexes["證券代號"]]).strip()
            if sid not in target_ids:
                continue
            close = _number(item[indexes["收盤價"]])
            value = _number(item[indexes["成交金額"]])
        except (IndexError, TypeError, ValueError):
            continue
        if close > 0 and value >= 0:
            rows[sid] = {"date": day.isoformat(), "close": close, "value": value}
    minimum = max(1, math.ceil(len(target_ids) * 0.95))
    if target_ids and len(rows) < minimum:
        raise RuntimeError(f"TWSE 全市場日價覆蓋不足:{len(rows)}/{len(target_ids)}")
    cache_set(key, rows, status="ok", target_ids=sorted(target_ids))
    return rows


def _fetch_tpex_day(day: date, target_ids: set[str]) -> dict[str, dict]:
    key = f"tpex_market_close_{day:%Y%m%d}"
    cached = cache_get(key, ttl_seconds=_market_day_ttl(day))
    if (cached is not None and cached.get("status") == "ok"
            and target_ids <= set(cached.get("target_ids") or [])):
        return {sid: row for sid, row in (cached.get("data") or {}).items()
                if sid in target_ids}
    url = ("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
           f"?date={day:%Y/%m/%d}&id=&response=json")
    payload = _json_get(url)
    if not isinstance(payload, dict):
        raise RuntimeError("TPEx 全市場日價格式錯誤")
    stat = str(payload.get("stat") or "")
    if stat.lower() != "ok":
        if "沒有符合條件" in stat or "查無資料" in stat:
            return {}
        raise RuntimeError(f"TPEx 全市場日價回傳異常:{stat}")
    compact = day.strftime("%Y%m%d")
    if str(payload.get("date") or "") != compact:
        raise RuntimeError(f"TPEx 全市場日價日期不符:{payload.get('date')}!={compact}")
    table = next((item for item in payload.get("tables") or []
                  if "代號" in (item.get("fields") or [])
                  and "收盤" in (item.get("fields") or [])), None)
    if not table:
        raise RuntimeError("TPEx 全市場日價缺少股票表")
    fields = table["fields"]
    raw_data = table.get("data") or []
    try:
        total_count = int(table["totalCount"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError("TPEx 全市場日價缺少總筆數") from e
    if total_count == 0 and not raw_data:
        return {}
    if (total_count != len(raw_data) or len(raw_data) < 500
            or any(len(item) != len(fields) for item in raw_data)):
        raise RuntimeError(
            f"TPEx 全市場日價原始表不完整:{len(raw_data)}/{total_count}")
    indexes = {name: fields.index(name) for name in ("代號", "收盤", "成交金額(元)")}
    rows = {}
    for item in raw_data:
        try:
            sid = str(item[indexes["代號"]]).strip()
        except (IndexError, TypeError):
            continue
        if sid not in target_ids:
            continue
        try:
            close = _number(item[indexes["收盤"]])
            value = _number(item[indexes["成交金額(元)"]])
        except (IndexError, TypeError, ValueError):
            continue
        if close > 0 and value >= 0:
            rows[sid] = {"date": day.isoformat(), "close": close, "value": value}
    cache_set(key, rows, status="ok", target_ids=sorted(target_ids))
    return rows


def _latest_completed_day() -> date:
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Taipei"))
    if (now.hour, now.minute) < (15, 0):
        return now.date() - timedelta(days=1)
    return now.date()


def _fetch_exchange_rows(twse_ids: set[str], tpex_ids: set[str],
                         start_twse: date | None = None,
                         start_tpex: date | None = None,
                         end: date | None = None,
                         verified: dict[str, str] | None = None) -> dict[str, list[dict]]:
    end = end or _latest_completed_day()
    start_twse = start_twse or (end - timedelta(days=45))
    start_tpex = start_tpex or (end - timedelta(days=45))
    start = min(start_twse, start_tpex)
    rows = {sid: [] for sid in twse_ids | tpex_ids}
    current = start
    while current <= end:
        if current.weekday() < 5:
            if current >= start_twse:
                day_rows = _fetch_twse_day(current, twse_ids)
                if (verified is not None and _market_cache_is_complete(
                        f"twse_market_close_{current:%Y%m%d}", twse_ids)):
                    verified["twse"] = current.isoformat()
                for sid, row in day_rows.items():
                    rows[sid].append(row)
            if current >= start_tpex:
                day_rows = _fetch_tpex_day(current, tpex_ids)
                if (verified is not None and _market_cache_is_complete(
                        f"tpex_market_close_{current:%Y%m%d}", tpex_ids)):
                    verified["tpex"] = current.isoformat()
                for sid, row in day_rows.items():
                    rows[sid].append(row)
        current += timedelta(days=1)
    return rows


def _market_cache_is_complete(key: str, target_ids: set[str]) -> bool:
    cached = cache_get(key, ttl_seconds=None)
    return bool(cached and cached.get("status") == "ok"
                and target_ids <= set(cached.get("target_ids") or []))


def _recover_publish(root: Path) -> None:
    journal = root / "data/.tw-price-publish.json"
    if not journal.exists():
        return
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
        backup = Path(state["backup"])
        hashes = state.get("hashes") or {}
        missing = [relative for relative in state.get("files") or []
                   if not (backup / relative).exists()]
        if missing:
            raise RuntimeError(f"備份檔不完整:{missing}")
        for relative, expected_hash in hashes.items():
            raw = (backup / relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                raise RuntimeError(f"備份雜湊不符:{relative}")
        for relative in state.get("files") or []:
            source = backup / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            restore = target.with_name(target.name + ".restore-tmp")
            shutil.copy2(source, restore)
            os.replace(restore, target)
        shutil.rmtree(backup, ignore_errors=True)
        journal.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 - never publish over an unrecovered transaction
        raise RuntimeError(f"前次台股價格發布交易無法復原:{e}") from e


def _publish_records(root: Path, records: dict[str, dict], snapshot: dict) -> None:
    data_dir = root / "data"
    files = [f"data/universe/{sid}.json" for sid in records]
    files.append("data/ai_chain_tw_quotes.json")
    _recover_publish(root)
    backup = Path(tempfile.mkdtemp(dir=data_dir, prefix=".tw-price-backup-"))
    stage = Path(tempfile.mkdtemp(dir=data_dir, prefix=".tw-price-stage-"))
    journal = data_dir / ".tw-price-publish.json"
    try:
        hashes = {}
        for relative in files:
            target = root / relative
            backup_path = backup / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_path)
            hashes[relative] = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        for sid, record in records.items():
            path = stage / f"data/universe/{sid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        quote_path = stage / "data/ai_chain_tw_quotes.json"
        quote_path.parent.mkdir(parents=True, exist_ok=True)
        quote_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        journal_tmp = data_dir / ".tw-price-publish.tmp"
        with journal_tmp.open("w", encoding="utf-8") as f:
            json.dump({"backup": str(backup), "files": files, "hashes": hashes}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(journal_tmp, journal)
        for relative in files:
            os.replace(stage / relative, root / relative)
        journal.unlink()
        shutil.rmtree(backup)
    except Exception:
        if journal.exists():
            _recover_publish(root)
        else:
            shutil.rmtree(backup, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _merge_prices(old_rows: list[dict], recent_rows: list[dict],
                  min_rows: int = 60) -> list[dict]:
    by_date = {}
    for row in [*(old_rows or []), *recent_rows]:
        try:
            dstr = date.fromisoformat(str(row["date"])).isoformat()
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            merged = {"date": dstr, "close": round(close, 2)}
            if row.get("value") is not None:
                try:
                    value = float(row["value"])
                    if math.isfinite(value) and value >= 0:
                        merged["value"] = value
                except (TypeError, ValueError):
                    pass
            by_date[dstr] = merged
    rows = [by_date[d] for d in sorted(by_date)]
    if len(rows) < min_rows:
        raise RuntimeError(f"合併後日價不足 {min_rows} 日")
    return rows


def _valuation_from_metrics(raw: dict, price: float) -> dict:
    e0, e1 = raw.get("eps_y0"), raw.get("eps_y1")
    coverage = raw.get("n_y0") or raw.get("n_q0") or raw.get("n_y1")
    forward_pe = price / float(e0) if e0 is not None and float(e0) > 0 else None
    growth = ((float(e1) - float(e0)) / float(e0) * 100
              if e0 is not None and e1 is not None and float(e0) != 0 else None)
    peg = forward_pe / growth if forward_pe and growth and growth > 0 else None
    shares = raw.get("sharesOutstanding")
    market_cap = price * float(shares) if shares is not None and float(shares) > 0 else raw.get("marketCap")
    fcf = raw.get("fcf_ttm")
    fcf_yield = (float(fcf) / float(market_cap) * 100
                 if fcf is not None and market_cap is not None and float(market_cap) > 0 else None)
    return {"forward_pe": forward_pe, "peg": peg, "fcf_yield": fcf_yield,
            "growth_pct": growth, "coverage": coverage}


def _load_cache_data(key: str, label: str) -> dict | list:
    cached = cache_get(key, ttl_seconds=None)
    if cached is None or not cached.get("data"):
        raise RuntimeError(f"缺少 {label} 快取:{key}")
    return cached["data"]


def _parse_pivot(frame, ticker: str) -> dict:
    pivot = {}
    for _, row in frame.iterrows():
        try:
            pivot.setdefault(str(row["date"]), {})[str(row["type"])] = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
    if not pivot:
        raise RuntimeError(f"{ticker}:FinMind 損益表解析不到資料")
    return pivot


def _expected_income_period(as_of: date, financial_company: bool = False) -> str | None:
    periods = []
    for year in range(as_of.year - 2, as_of.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            qend = date(year, month, day)
            if _filing_available_date(qend, financial_company) <= as_of:
                periods.append(qend)
    return max(periods).isoformat() if periods else None


def _has_finite_eps(income: dict, period: str) -> bool:
    try:
        eps = float((income.get(period) or {})["EPS"])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(eps)


def _carry_forward_pe_history(old_ph: dict, rows: list[dict], latest: dict,
                              years: int, expected_period: str | None) -> dict | None:
    """Reprice a persisted, valid TTM basis when a restored cache regresses."""
    if old_ph.get("status") != "ok":
        return None
    old_eps_end = str((old_ph.get("source_coverage") or {}).get("eps_end") or "")
    if expected_period and old_eps_end < expected_period:
        return None
    try:
        ttm_eps = float(old_ph["current_ttm_eps"])
        close = float(latest["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (ttm_eps, close)):
        return None
    as_of = date.fromisoformat(str(latest["date"]))
    try:
        cutoff = as_of.replace(year=as_of.year - years)
    except ValueError:
        cutoff = as_of.replace(year=as_of.year - years, day=28)
    price_dates = sorted({str(row["date"]) for row in rows
                          if str(row.get("date") or "") <= as_of.isoformat()})
    coverage = dict(old_ph.get("source_coverage") or {})
    if price_dates:
        coverage.update(price_start=price_dates[0], price_end=price_dates[-1],
                        price_n=len(price_dates))
    carried = dict(old_ph)
    carried.update({
        "current_date": as_of.isoformat(),
        "as_of": as_of.isoformat(),
        "window_start": cutoff.isoformat(),
        "current_trailing_pe": round(close / ttm_eps, 1),
        "current_ttm_eps": ttm_eps,
        "source_coverage": coverage,
        "percentile": None,
        "source_cache_regressed": True,
    })
    carried.pop("reason", None)
    return carried


def _income_for_price_date(loader, ticker: str, record: dict, as_of: str,
                           start_date: str,
                           allow_refresh: bool = True) -> tuple[dict, bool, bool]:
    income = _load_cache_data(f"finmind_fs_long_{ticker}", "財報")
    if not supports_tw_filing_fallback(record.get("name") or ticker):
        return income, False, False
    financial = is_financial_company(
        ticker, record.get("industry", ""), record.get("market", "twse"))
    expected = _expected_income_period(date.fromisoformat(as_of), financial)
    if expected is None or _has_finite_eps(income, expected):
        return income, False, False
    if not allow_refresh:
        return income, False, False
    try:
        frame = loader.taiwan_stock_financial_statement(
            stock_id=ticker, start_date=start_date)
        if frame is None or not len(frame):
            raise RuntimeError("未回傳資料")
        refreshed = _parse_pivot(frame, ticker)
        merged = {period: dict(values) for period, values in income.items()}
        for period, values in refreshed.items():
            merged.setdefault(period, {}).update(values)
        if not _has_finite_eps(merged, expected):
            raise RuntimeError(f"最新資料仍缺應有季度 EPS:{expected}")
        return merged, True, True
    except Exception as e:  # noqa: BLE001 - price sync continues with PE marked insufficient
        print(f"! {ticker}:最新損益表補抓失敗，價格仍同步，PE 標資料不足:{e}")
        return income, False, True


def _updated_record(record: dict, rows: list[dict], recent_rows: list[dict],
                    screener_cfg: dict, income: dict,
                    supporting: dict | None = None,
                    checked_through: str | None = None) -> dict:
    sid = str(record["stock_id"])
    latest = rows[-1]
    liquidity_days = int(screener_cfg["layer1"]["liquidity"]["days"])
    liquidity_rows = [row for row in recent_rows if row.get("value") is not None][-liquidity_days:]
    if len(liquidity_rows) >= liquidity_days:
        liq_avg = sum(row["value"] for row in liquidity_rows) / len(liquidity_rows)
        liq_days = len(liquidity_rows)
    elif (record.get("liq_avg_value") is not None
          and int(record.get("liq_days") or 0) >= liquidity_days):
        liq_avg, liq_days = record["liq_avg_value"], record["liq_days"]
    else:
        raise RuntimeError(f"{sid}:成交額資料不足 {len(liquidity_rows)}/{liquidity_days}")
    supporting = supporting or {}
    yf_metrics = supporting.get("yf_metrics")
    if yf_metrics is None:
        yf_metrics = _load_cache_data(f"yf_metrics_{sid}.TW", "yfinance 指標")
    years = int(screener_cfg["valuation_flag"]["pe_history_years"])
    fallback_ok = supports_tw_filing_fallback(record.get("name") or sid)
    financial = is_financial_company(sid, record.get("industry", ""), "twse")
    pe_series = daily_pe_series(rows, income, fallback_ok, financial)
    current_pe, current_date = current_trailing_pe(
        rows, income, fallback_ok, latest["close"], latest["date"], financial)
    expected = _expected_income_period(date.fromisoformat(latest["date"]), financial)
    reason = ("unsupported_foreign_issuer_filing_deadline" if not fallback_ok else
              "financial_eps_source_unavailable"
              if financial and expected and not _has_finite_eps(income, expected) else
              "financial_report_not_yet_available"
              if expected and not _has_finite_eps(income, expected) else
              "current_trailing_pe_unavailable" if current_pe is None else None)
    pe_hist = pe_history_stats(
        pe_series, current_pe, years=years, current_date=current_date,
        market="twse", insufficient_reason=reason,
        source_coverage=tw_pe_source_coverage(rows, income, years))
    if pe_source_regressed(record.get("pe_hist") or {}, pe_hist):
        pe_hist = (_carry_forward_pe_history(
            record.get("pe_hist") or {}, rows, latest, years, expected)
            or pe_history_stats(
                [], None, years=years, current_date=latest["date"], market="twse",
                insufficient_reason="financial_report_not_yet_available",
                source_coverage=tw_pe_source_coverage(rows, income, years)))
    elif current_pe is not None and latest["close"]:
        pe_hist["current_ttm_eps"] = round(float(latest["close"]) / current_pe, 6)
        pe_hist.pop("source_cache_regressed", None)
    if not pe_history_is_compatible(pe_hist, "twse", latest["date"], years):
        raise RuntimeError(f"{sid}:更新後 PE schema/date 不相容")

    balance = supporting.get("balance") or {}
    cashflow = supporting.get("cashflow") or {}
    income_metrics = extract_metrics(income, balance, cashflow)
    merged_annual = dict(record.get("annual") or {})
    merged_annual.update(income_metrics["annual"])
    first_candidates = [x for x in (record.get("first_report"),
                                    income_metrics["first_report"]) if x]
    latest_candidates = [x for x in (record.get("latest_report"),
                                     income_metrics["latest_report"]) if x]
    updated = dict(record)
    updated.update({
        "price_last": latest["close"],
        "price_date": latest["date"],
        "price_checked_through": checked_through or latest["date"],
        "liq_avg_value": liq_avg,
        "liq_days": liq_days,
        "pe_hist": pe_hist,
        "valuation": _valuation_from_metrics(yf_metrics, latest["close"]),
        "hist_peg": historical_peg(
            merged_annual, latest["close"], years=years),
    })
    updated["annual"] = merged_annual
    updated["first_report"] = min(first_candidates) if first_candidates else None
    updated["latest_report"] = max(latest_candidates) if latest_candidates else None
    for key in ("annual_bs", "annual_ocf"):
        if income_metrics.get(key):
            merged = dict(record.get(key) or {})
            merged.update(income_metrics[key])
            updated[key] = merged
    for key in ("latest_bs", "ocf_q"):
        if income_metrics.get(key):
            updated[key] = income_metrics[key]
    if supporting.get("mrev") is not None:
        updated["mrev"] = supporting["mrev"]
    updated.pop("pe_refresh_error", None)
    return updated


def _supporting_data(ticker: str, screener_cfg: dict, record: dict,
                     refresh_finmind: bool) -> dict:
    """Refresh TTL-governed non-price inputs; preserve the stored block on failure."""
    from .data_layer import (fetch_balance_pivot, fetch_cashflow_pivot,
                             fetch_month_revenue, fetch_yfinance_metrics,
                             month_revenue_momentum)

    start = str(screener_cfg.get("fetch", {}).get("financial_start", "2018-01-01"))
    month_start = str(screener_cfg.get("fetch", {}).get(
        "month_revenue_start", "2021-01-01"))
    output = {}
    jobs = [("yf_metrics", lambda: fetch_yfinance_metrics(f"{ticker}.TW")[0])]
    if refresh_finmind:
        jobs.extend((
            ("balance", lambda: fetch_balance_pivot(ticker, start)[0]),
            ("cashflow", lambda: fetch_cashflow_pivot(ticker, start)[0]),
            ("mrev_rows", lambda: fetch_month_revenue(ticker, month_start)[0]),
        ))
    for label, fn in jobs:
        try:
            output[label] = fn()
        except Exception as e:  # noqa: BLE001 - preserve the last published record block
            print(f"! {ticker}:{label} 更新失敗，沿用既有 record:{e}")
    if output.get("mrev_rows") is not None:
        output["mrev"] = month_revenue_momentum(
            output.pop("mrev_rows"),
            recent=int(screener_cfg.get("fetch", {}).get("month_revenue_recent", 3)))
    return output


def _record_payload(record: dict) -> dict:
    comparable = dict(record)
    comparable.pop("price_updated_at", None)
    return comparable


def _rotated_missing_income_ids(root: Path, ids: list[str], records: dict[str, dict],
                                merged_by_id: dict[str, list[dict]]) -> tuple[
                                    list[str], int, dict, set[str]]:
    """Prioritize stocks missing the legally due EPS quarter, with a persisted cursor."""
    missing = []
    for sid in ids:
        record = records[sid]
        if not supports_tw_filing_fallback(record.get("name") or sid):
            continue
        financial = is_financial_company(sid, record.get("industry", ""), "twse")
        expected = _expected_income_period(
            date.fromisoformat(merged_by_id[sid][-1]["date"]), financial)
        try:
            income = _load_cache_data(f"finmind_fs_long_{sid}", "財報")
        except RuntimeError:
            missing.append(sid)
            continue
        if not isinstance(income, dict) or (expected and not _has_finite_eps(income, expected)):
            missing.append(sid)
    state_path = root / "cache/tw-income-refresh-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        cursor = int(state.get("cursor", 0))
    except (OSError, ValueError, TypeError):
        state = {}
        cursor = 0
    all_missing = set(missing)
    retry_after = state.get("retry_after") or {}
    today = date.today().isoformat()
    missing = [sid for sid in missing if str(retry_after.get(sid) or "") <= today]
    if not missing:
        return [], cursor, state, all_missing
    start = cursor % len(ids)
    stable_order = ids[start:] + ids[:start]
    missing_set = set(missing)
    return [sid for sid in stable_order if sid in missing_set], start, state, all_missing


def refresh_tw_prices(root: str | Path, screener_cfg: dict, chain_cfg: dict,
                      loader=None, sleep_seconds: float | None = None,
                      financial_refresh_limit: int | None = None,
                      supporting_refresh_limit: int | None = None,
                      refresh_supporting: bool = True,
                      verbose: bool = True) -> dict:
    """抓一次台股日價並在全部驗證後，同步發布母體與 AI 行情。"""
    root = Path(root)
    _recover_publish(root)
    universe_doc = yaml.safe_load((root / "config/universe.yaml").read_text(encoding="utf-8")) or {}
    universe_ids = [str(row["stock_id"]) for row in universe_doc.get("twse") or []]
    chain_ids = expected_tw_quote_tickers(chain_cfg)
    chain_markets = {
        str(member["id"]): str(member["market"])
        for layer in chain_cfg.get("layers") or []
        for member in layer.get("members") or []
    }
    chain_names = {
        str(member["id"]): str(member.get("name") or member["id"])
        for layer in chain_cfg.get("layers") or []
        for member in layer.get("members") or []
    }
    all_ids = sorted(set(universe_ids) | chain_ids)
    if not universe_ids or len(chain_ids) != 17:
        raise RuntimeError("台股母體或 AI 行情集合不完整")
    if loader is None:
        from .data_layer import _finmind_loader
        loader = _finmind_loader()
    existing_records = {
        sid: json.loads((root / f"data/universe/{sid}.json").read_text(encoding="utf-8"))
        for sid in universe_ids
    }
    quote_path = root / "data/ai_chain_tw_quotes.json"
    try:
        existing_snapshot = json.loads(quote_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing_snapshot = {}
    existing_quotes = existing_snapshot.get("quotes") or {}
    price_cache = {sid: cache_get(f"finmind_price_{sid}", ttl_seconds=None) or {}
                   for sid in all_ids}
    value_cache = {sid: cache_get(f"finmind_pxv_{sid}", ttl_seconds=None) or {}
                   for sid in all_ids}
    twse_ids = set(universe_ids) | {
        sid for sid in chain_ids if chain_markets.get(sid) == "twse"}
    tpex_ids = {sid for sid in chain_ids if chain_markets.get(sid) == "tpex"}
    def checked_date(sid: str) -> date | None:
        artifact = existing_records.get(sid) or existing_quotes.get(sid) or {}
        dstr = artifact.get("price_checked_through") or artifact.get("checked_through")
        dstr = dstr or artifact.get("price_date") or artifact.get("close_date")
        rows = value_cache[sid].get("data") or price_cache[sid].get("data") or []
        dstr = dstr or (rows[-1].get("date") if rows else None)
        try:
            return date.fromisoformat(str(dstr))
        except (TypeError, ValueError):
            return None

    latest_twse_dates = [d for sid in twse_ids if (d := checked_date(sid))]
    latest_tpex_dates = [d for sid in tpex_ids if (d := checked_date(sid))]
    end = _latest_completed_day()
    start_twse = (min(latest_twse_dates)
                  if latest_twse_dates else end - timedelta(days=120))
    start_tpex = (min(latest_tpex_dates)
                  if latest_tpex_dates else end - timedelta(days=120))
    verified_market_date = {}
    exchange_rows = _fetch_exchange_rows(
        twse_ids, tpex_ids, start_twse, start_tpex, end,
        verified=verified_market_date)
    baseline_checked = {
        "twse": max(latest_twse_dates).isoformat() if latest_twse_dates else "",
        "tpex": max(latest_tpex_dates).isoformat() if latest_tpex_dates else "",
    }
    for market in ("twse", "tpex"):
        verified_market_date.setdefault(market, baseline_checked[market])
    merged_by_id, recent_by_id = {}, {}
    for index, sid in enumerate(all_ids, 1):
        recent = exchange_rows.get(sid) or []
        market = "tpex" if sid in tpex_ids else "twse"
        verified_date = verified_market_date[market]
        old_prices = [row for row in (price_cache[sid].get("data") or [])
                      if not verified_date or str(row.get("date") or "") <= verified_date]
        old_values = [row for row in (value_cache[sid].get("data") or [])
                      if not verified_date or str(row.get("date") or "") <= verified_date]
        merged = _merge_prices(old_prices, recent)
        value_rows = _merge_prices(
            old_values, recent, min_rows=1)
        merged_by_id[sid], recent_by_id[sid] = merged, value_rows
        if verbose:
            print(f"[{index}/{len(all_ids)}] {sid}:收盤 {merged[-1]['close']} ({merged[-1]['date']})")
    observed_latest = {
        "twse": max((merged_by_id[sid][-1]["date"] for sid in twse_ids), default=""),
        "tpex": max((merged_by_id[sid][-1]["date"] for sid in tpex_ids), default=""),
    }
    for market in ("twse", "tpex"):
        verified_market_date[market] = verified_market_date[market] or observed_latest[market]

    records, income_updates = {}, {}
    refresh_limit = (int(financial_refresh_limit) if financial_refresh_limit is not None
                     else int(os.getenv("TW_FINANCIAL_REFRESH_LIMIT", "20")))
    support_limit = (int(supporting_refresh_limit)
                     if supporting_refresh_limit is not None else
                     int(os.getenv("TW_SUPPORTING_REFRESH_LIMIT", "20")))
    refresh_attempts = 0
    sleep_s = (float(sleep_seconds) if sleep_seconds is not None else
               float(screener_cfg.get("fetch", {}).get("sleep_seconds", 0)))
    financial_start = str(screener_cfg.get("fetch", {}).get("financial_start", "2018-01-01"))
    core_ids = list(dict.fromkeys(
        sid for sid in ["2330", *sorted(chain_ids & set(universe_ids))]
        if sid in universe_ids))
    other_ids = [sid for sid in universe_ids if sid not in core_ids]
    day_number = date.today().toordinal()
    core_slots = min(5, refresh_limit)
    core_rotation = (day_number * max(1, core_slots)) % max(1, len(core_ids))
    other_slots = max(1, refresh_limit - core_slots)
    other_rotation = (day_number * other_slots) % max(1, len(other_ids))
    rotated_core = core_ids[core_rotation:] + core_ids[:core_rotation]
    rotated_others = other_ids[other_rotation:] + other_ids[:other_rotation]
    missing_income_ids, missing_cursor, income_refresh_state, all_missing_income_ids = (
        _rotated_missing_income_ids(
        root, universe_ids, existing_records, merged_by_id)
    )
    missing_set = set(missing_income_ids)
    fallback_order = rotated_core[:core_slots] + rotated_others + rotated_core[core_slots:]
    # 所有 record 每次都要同步價格；只有不在退避期的缺季標的可以使用補抓名額。
    ordered_universe_ids = missing_income_ids + [sid for sid in fallback_order
                                                 if sid not in missing_set]
    support_core_slots = min(5, support_limit)
    supporting_ids = set(
        rotated_core[:support_core_slots]
        + rotated_others[:max(0, support_limit - support_core_slots)])
    # 上櫃 AI 成員沒有 universe record，但其估值 cache 也必須隨官方行情補 EPS。
    missing_refresh_attempts = 0
    last_missing_attempt_sid = None
    missing_refresh_successes = set()
    missing_refresh_failures = set()
    for sid in sorted(tpex_ids):
        income, refreshed, attempted = _income_for_price_date(
            loader, sid,
            {"stock_id": sid, "name": chain_names.get(sid, sid),
             "industry": "", "market": "tpex"},
            merged_by_id[sid][-1]["date"], financial_start,
            allow_refresh=refresh_attempts < refresh_limit)
        if attempted:
            refresh_attempts += 1
            if sleep_s > 0 and refresh_attempts < refresh_limit:
                time.sleep(sleep_s)
        if refreshed:
            income_updates[sid] = income
    for sid in ordered_universe_ids:
        record_path = root / f"data/universe/{sid}.json"
        record = existing_records[sid]
        income, refreshed, attempted = _income_for_price_date(
            loader, sid, record, merged_by_id[sid][-1]["date"], financial_start,
            allow_refresh=(sid in missing_set and refresh_attempts < refresh_limit))
        if attempted:
            refresh_attempts += 1
            if sid in missing_set:
                missing_refresh_attempts += 1
                last_missing_attempt_sid = sid
                (missing_refresh_successes if refreshed else missing_refresh_failures).add(sid)
            if sleep_s > 0 and refresh_attempts < refresh_limit:
                time.sleep(sleep_s)
        if refreshed:
            income_updates[sid] = income
        supporting = (_supporting_data(
            sid, screener_cfg, record, sid in supporting_ids)
                      if refresh_supporting else {})
        checked_through = verified_market_date["twse"] or end.isoformat()
        records[sid] = _updated_record(
            record, merged_by_id[sid], recent_by_id[sid], screener_cfg, income,
            supporting=supporting, checked_through=checked_through)
        if records[sid]["price_date"] < checked_through:
            records[sid]["price_stale_reason"] = "no_official_trade"
        else:
            records[sid].pop("price_stale_reason", None)

    quotes = {}
    for sid in chain_ids:
        quote = quote_from_price_rows(sid, merged_by_id[sid])
        checked_through = verified_market_date[chain_markets[sid]] or end.isoformat()
        if quote["close_date"] < checked_through:
            quote["checked_through"] = checked_through
            quote["stale_reason"] = "no_official_trade"
        quotes[sid] = quote
    quotes_changed = (existing_snapshot.get("schema_version") != QUOTE_SCHEMA_VERSION
                      or existing_snapshot.get("source") != QUOTE_SOURCE
                      or not existing_snapshot.get("updated_at")
                      or existing_snapshot.get("quotes") != quotes)
    snapshot = {"schema_version": QUOTE_SCHEMA_VERSION, "source": QUOTE_SOURCE,
                "updated_at": (datetime.now(timezone.utc).isoformat(timespec="seconds")
                               if quotes_changed else existing_snapshot.get("updated_at")),
                "quotes": quotes}
    validate_tw_quote_snapshot(snapshot, chain_ids)
    for sid in chain_ids & set(records):
        quote = quotes[sid]
        record = records[sid]
        if (record["price_date"] != quote["close_date"]
                or abs(float(record["price_last"]) - float(quote["close"])) > 0.0001):
            raise RuntimeError(f"{sid}:母體與 AI 行情不同步")

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed_records = set()
    for sid, record in records.items():
        old = existing_records[sid]
        if _record_payload(record) != _record_payload(old):
            changed_records.add(sid)
            record["price_updated_at"] = now_iso
        elif old.get("price_updated_at") is not None:
            record["price_updated_at"] = old["price_updated_at"]
    artifact_changed = bool(changed_records or quotes_changed)
    # Cache first. If artifact publication then fails, the next run reconciles from the
    # newer cache; publishing records that depend on an unwritten cache is less recoverable.
    for sid in all_ids:
        cache_set(f"finmind_price_{sid}", merged_by_id[sid], start_date="2015-01-01")
        cache_set(f"finmind_pxv_{sid}", recent_by_id[sid])
    for sid, income in income_updates.items():
        old_cache = cache_get(f"finmind_fs_long_{sid}", ttl_seconds=None) or {}
        old_start = old_cache.get("start_date")
        metadata = ({"start_date": min(str(old_start), financial_start)}
                    if old_start else {})
        cache_set(f"finmind_fs_long_{sid}", income, **metadata)
        if sid in tpex_ids:
            (root / f"cache/ai_chain_tpex_record_v2_{sid}.json").unlink(missing_ok=True)
    if missing_income_ids and missing_refresh_attempts and last_missing_attempt_sid:
        state_path = root / "cache/tw-income-refresh-state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        retry_after = dict(income_refresh_state.get("retry_after") or {})
        for sid in missing_refresh_successes:
            retry_after.pop(sid, None)
        for sid in missing_refresh_failures:
            days = 7 if is_financial_company(
                sid, existing_records[sid].get("industry", ""), "twse") else 1
            retry_after[sid] = (date.today() + timedelta(days=days)).isoformat()
        state_path.write_text(
            json.dumps({"cursor": (universe_ids.index(last_missing_attempt_sid) + 1)
                                  % len(universe_ids),
                        "retry_after": retry_after,
                        "updated_at": end.isoformat()}),
            encoding="utf-8")
    if artifact_changed:
        _publish_records(root, records, snapshot)
    return {"records": len(records), "quotes": len(quotes),
            "income_updates": len(income_updates),
            "income_refresh_attempts": refresh_attempts,
            "close_dates": sorted({row["price_date"] for row in records.values()}),
            "updated": artifact_changed}
