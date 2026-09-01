"""Transactional US close/valuation refresh without refetching financial statements."""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

from .ai_quotes import fetch_quote, update_quote_snapshot
from .cache import cache_entry, cache_get, cache_path
from .river import build_pe_river_us
from .us_data import (EXPECTED_CURRENCIES, US_RIVER_TICKERS,
                      _completed_history, _normalize_reported_eps_events,
                      _reported_eps_events_regressed)
from .valuation_flag import (pe_history_is_compatible, pe_history_stats, pe_series_us,
                             us_pe_source_coverage, us_pe_source_error,
                             _us_reported_eps_events)


def fetch_us_price_inputs(
    ticker: str,
) -> tuple[object, list[tuple[date, float]], bool]:
    """Fetch only completed daily bars and Reported EPS release events."""
    import yfinance as yf

    try:
        history = yf.Ticker(ticker).history(
            period="max", auto_adjust=False, actions=True)
        history = _completed_history(history)
        if history is None or not len(history):
            raise RuntimeError("no completed daily bars")
    except Exception as e:  # noqa: BLE001 - add ticker/source context for CI logs
        raise RuntimeError(f"history: {e}") from e

    release_time_aware = ticker in US_RIVER_TICKERS
    cache_key = f"us_reported_eps_events_v1_{ticker}"
    cached = cache_get(cache_key, ttl_seconds=None)
    cached_events = []
    for row in (cached or {}).get("data") or []:
        try:
            cached_events.append((date.fromisoformat(str(row[0])), float(row[1])))
        except (IndexError, TypeError, ValueError):
            cached_events = []
            break

    earnings_dates = None
    last_error = None
    for _ in range(3):
        try:
            earnings_dates = yf.Ticker(ticker).get_earnings_dates(limit=100)
            if earnings_dates is None or not len(earnings_dates):
                raise RuntimeError("no Reported EPS events")
            break
        except Exception as e:  # noqa: BLE001 - retry transient Yahoo scraper failures
            last_error = e
    if earnings_dates is None or not len(earnings_dates):
        if not cached_events:
            raise RuntimeError(f"earnings_dates: {last_error}")
        print(f"! {ticker}: Reported EPS 更新失敗，沿用已驗證事件 cache:{last_error}")
        return history, cached_events, False

    events = _us_reported_eps_events(earnings_dates, release_time_aware)
    if len(events) < 4:
        if not cached_events:
            raise RuntimeError("earnings_dates: fewer than four Reported EPS events")
        print(f"! {ticker}: Reported EPS 回傳不足，沿用已驗證事件 cache")
        return history, cached_events, False
    if _reported_eps_events_regressed(cached_events, events):
        print(f"! {ticker}: Reported EPS 回傳範圍退步，沿用已驗證事件 cache")
        return history, cached_events, False
    return history, events, True


def _history_at_quote(history, quote: dict):
    """Return history ending at the canonical quote, with an exact matching close."""
    try:
        quote_date = date.fromisoformat(str(quote["close_date"]))
        quote_close = float(quote["close"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("invalid canonical quote") from e
    if not math.isfinite(quote_close) or quote_close <= 0:
        raise ValueError("canonical close must be positive")

    completed = _completed_history(history)
    if completed is None or not len(completed) or "Close" not in completed.columns:
        raise ValueError("completed price history is empty")
    completed = completed.loc[[ts.date() <= quote_date for ts in completed.index]].copy()
    matches = [ts for ts in completed.index if ts.date() == quote_date]
    if len(matches) != 1:
        raise ValueError(f"history does not contain canonical close date {quote_date}")
    raw_close = float(completed.loc[matches[0], "Close"])
    if not math.isfinite(raw_close) or abs(raw_close - quote_close) > 0.01:
        raise ValueError(
            f"history close {raw_close} differs from canonical close {quote_close}")
    completed.loc[matches[0], "Close"] = quote_close
    if completed.index[-1].date() != quote_date:
        raise ValueError("canonical close is not the last completed price bar")
    return completed


def _splits_after(history, previous_date: str, current_date: str) -> list[tuple[str, float]]:
    """Return positive split events not represented by the persisted per-share inputs."""
    if "Stock Splits" not in history.columns:
        return []
    start = date.fromisoformat(previous_date)
    end = date.fromisoformat(current_date)
    events = []
    for ts, raw in history["Stock Splits"].items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if start < ts.date() <= end and math.isfinite(value) and value > 0:
            events.append((ts.date().isoformat(), value))
    return events


def _publish_transaction(contents: dict[Path, str]) -> None:
    """Publish staged text files with rollback if any replacement fails."""
    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for target, text in contents.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            originals[target] = target.read_bytes() if target.exists() else None
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=target.parent,
                    prefix=f".{target.name}.", delete=False) as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
                staged[target] = Path(handle.name)
        for target, candidate in staged.items():
            os.replace(candidate, target)
            replaced.append(target)
    except Exception:
        for target in reversed(replaced):
            original = originals[target]
            if original is None:
                target.unlink(missing_ok=True)
                continue
            with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target.parent,
                    prefix=f".{target.name}.rollback.", delete=False) as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
                rollback = Path(handle.name)
            os.replace(rollback, target)
        raise
    finally:
        for candidate in staged.values():
            candidate.unlink(missing_ok=True)


def _price_linked_valuation(record: dict, price: float, latest_fx: float) -> dict:
    """Revalue persisted consensus/FCF inputs without calling statement endpoints."""
    old_price = float(record.get("price_last") or 0)
    valuation = deepcopy(record.get("valuation") or {})
    detail = record.get("detail") or {}
    raw = detail.get("yf") or {}
    shares_bn = detail.get("shares_bn")
    financial_currency = detail.get("financial_currency")
    quote_currency = detail.get("quote_currency")

    if detail and shares_bn:
        factor = latest_fx if financial_currency != quote_currency else 1.0
        eps_y0 = raw.get("eps_y0")
        eps_y1 = raw.get("eps_y1")
        eps_y0 = float(eps_y0) * factor if eps_y0 is not None else None
        eps_y1 = float(eps_y1) * factor if eps_y1 is not None else None
        growth = ((eps_y1 - eps_y0) / eps_y0 * 100
                  if eps_y0 and eps_y1 else None)
        forward_pe = price / eps_y0 if eps_y0 and eps_y0 > 0 else None
        fcf = raw.get("fcf_ttm")
        market_cap = price * float(shares_bn) * 1e9
        valuation.update({
            "forward_pe": forward_pe,
            "peg": (forward_pe / growth
                    if forward_pe and growth and growth > 0 else None),
            "fcf_yield": (float(fcf) * factor / market_cap * 100
                          if fcf is not None and market_cap > 0 else None),
            "growth_pct": growth,
            "coverage": raw.get("n_y0") or raw.get("n_q0")
                        or raw.get("n_y1") or valuation.get("coverage"),
        })
        return valuation

    if old_price > 0:
        price_ratio = price / old_price
        for key in ("forward_pe", "peg"):
            if valuation.get(key) is not None:
                valuation[key] = float(valuation[key]) * price_ratio
        if valuation.get("fcf_yield") is not None:
            valuation["fcf_yield"] = float(valuation["fcf_yield"]) / price_ratio
    return valuation


def update_us_record_from_price_inputs(
    record: dict,
    quote: dict,
    cfg: dict,
    history,
    eps_events: list[tuple[date, float]],
    fx_series: list[tuple[date, float]] | None = None,
    fx_note: str = "",
    latest_fx: float = 1.0,
) -> dict:
    """Apply one canonical close while preserving every persisted financial block."""
    rec = deepcopy(record)
    sid = str(rec["stock_id"])
    years = int(cfg["valuation_flag"]["pe_history_years"])
    completed = _history_at_quote(history, quote)
    current_date = str(quote["close_date"])
    current_price = float(quote["close"])
    previous_date = str(rec.get("price_date") or "")
    split_events = _splits_after(completed, previous_date, current_date)
    if split_events:
        raise ValueError(
            f"new stock split requires full financial refresh: {split_events}")
    release_time_aware = sid in US_RIVER_TICKERS

    pe_series = pe_series_us(
        completed, years=years, eps_events=eps_events, fx_series=fx_series,
        release_time_aware=release_time_aware)
    source_error = us_pe_source_error(
        completed, None, years=years, eps_events=eps_events)
    if source_error:
        raise ValueError(source_error)
    current_pe = (pe_series[-1][1]
                  if pe_series and pe_series[-1][0] == current_date else None)

    detail = deepcopy(rec.get("detail") or {})
    quote_currency = str(detail.get("quote_currency") or rec.get("currency") or "USD")
    financial_currency = str(
        detail.get("financial_currency")
        or EXPECTED_CURRENCIES.get(sid, (quote_currency, quote_currency))[1])
    conversion = None
    if financial_currency != quote_currency:
        conversion = {
            "from": financial_currency,
            "to": quote_currency,
            "pair": "EURUSD=X",
            "rate": latest_fx,
            "as_of": current_date,
            "basis": "point-in-time daily FX; current estimates use latest FX",
        }
    pe_hist = pe_history_stats(
        pe_series, current_pe, years=years, current_date=current_date,
        market="us",
        source_coverage=us_pe_source_coverage(
            completed, None, years, eps_events),
        currency_conversion=conversion,
        release_time_aware=release_time_aware,
    )
    if not pe_history_is_compatible(pe_hist, "us", current_date, years):
        raise ValueError("new PE snapshot is incompatible")

    rec["price_last"] = current_price
    rec["price_date"] = current_date
    rec["price_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recent = completed.tail(int(cfg["layer1"]["liquidity"]["days"]))
    values = (recent["Close"] * recent["Volume"]).dropna()
    rec["liq_avg_value"] = float(values.mean()) if len(values) else None
    rec["liq_days"] = int(len(values))
    rec["pe_hist"] = pe_hist
    rec["valuation"] = _price_linked_valuation(record, current_price, latest_fx)

    if release_time_aware:
        if not detail:
            raise ValueError("detail snapshot is missing")
        river = build_pe_river_us(
            completed, years=years, eps_events=eps_events,
            fx_series=fx_series, source_note=fx_note)
        old_latest_fx = float(detail.get("latest_fx") or 0)
        detail.update({
            "quote_currency": quote_currency,
            "financial_currency": financial_currency,
            "fx_note": fx_note,
            "latest_fx": latest_fx,
            "river": river.__dict__,
            "splits": [[ts.date().isoformat(), float(value)]
                       for ts, value in completed["Stock Splits"].items()
                       if value == value and float(value) > 0]
                      if "Stock Splits" in completed.columns else [],
        })
        raw = detail.get("yf") or {}
        factor = latest_fx if financial_currency != quote_currency else 1.0
        eps_y0 = raw.get("eps_y0")
        eps_y1 = raw.get("eps_y1")
        detail["eps_y0"] = float(eps_y0) * factor if eps_y0 is not None else None
        detail["eps_y1"] = float(eps_y1) * factor if eps_y1 is not None else None
        detail["growth_pct"] = (
            (detail["eps_y1"] - detail["eps_y0"]) / detail["eps_y0"] * 100
            if detail["eps_y0"] and detail["eps_y1"] else None)
        if (financial_currency != quote_currency and old_latest_fx > 0
                and latest_fx > 0):
            ratio = latest_fx / old_latest_fx
            for row in detail.get("quarters") or []:
                if row.get("eps") is not None:
                    row["eps"] = round(float(row["eps"]) * ratio, 4)
        rec["detail"] = detail
    return rec


def refresh_us_record_price(record: dict, quote: dict, cfg: dict,
                            pending_event_updates: dict[str, list] | None = None) -> dict:
    """Fetch minimal sources and produce one price-only record update."""
    sid = str(record["stock_id"])
    history, reported_events, events_refreshed = fetch_us_price_inputs(sid)
    detail = record.get("detail") or {}
    expected_quote, expected_financial = EXPECTED_CURRENCIES.get(
        sid, (str(record.get("currency") or "USD"),
              str(record.get("currency") or "USD")))
    quote_currency = str(detail.get("quote_currency") or record.get("currency")
                         or expected_quote)
    financial_currency = str(detail.get("financial_currency") or expected_financial)
    if (quote_currency, financial_currency) != (expected_quote, expected_financial):
        raise ValueError(
            f"currency mismatch {financial_currency}->{quote_currency}; "
            f"expected {expected_financial}->{expected_quote}")
    eps_events, fx_series, fx_note, latest_fx = _normalize_reported_eps_events(
        reported_events, quote_currency, financial_currency,
        date.fromisoformat(str(quote["close_date"])))
    updated = update_us_record_from_price_inputs(
        record, quote, cfg, history, eps_events, fx_series, fx_note, latest_fx)
    if events_refreshed and pending_event_updates is not None:
        pending_event_updates[f"us_reported_eps_events_v1_{sid}"] = [
            [event_date.isoformat(), eps] for event_date, eps in reported_events]
    return updated


def _chain_us_members(chain_cfg: dict) -> dict[str, dict]:
    return {
        str(member["id"]): member
        for layer in chain_cfg.get("layers") or []
        for member in layer.get("members") or []
        if member.get("market") == "us"
    }


def _price_only_cache_wrapper(wrapper: dict, record: dict) -> dict:
    """Replace record data without making its financial cache age look newer."""
    result = deepcopy(wrapper)
    result["data"] = record
    return result


def refresh_us_prices(root: str | Path, screener_cfg: dict,
                      chain_cfg: dict) -> dict:
    """Update quotes, universe records, and AI-only records as one transaction."""
    root = Path(root)
    quote_path = root / "data/ai_chain_quotes.json"
    import yaml

    universe_doc = yaml.safe_load(
        (root / "config/universe.yaml").read_text(encoding="utf-8")) or {}
    universe_ids = [str(row["stock_id"]) for row in universe_doc.get("us") or []]
    if not universe_ids:
        raise RuntimeError("config/universe.yaml has no US records")

    with tempfile.TemporaryDirectory() as directory:
        candidate_path = Path(directory) / "ai_chain_quotes.json"
        if quote_path.exists():
            shutil.copyfile(quote_path, candidate_path)
        snapshot, warnings = update_quote_snapshot(chain_cfg, candidate_path)
        chain_quotes = snapshot["quotes"]
        close_dates = {str(row["close_date"]) for row in chain_quotes.values()}
        if len(close_dates) != 1:
            raise RuntimeError(f"AI quote dates differ: {sorted(close_dates)}")
        canonical_date = next(iter(close_dates))
        old_snapshot = (json.loads(quote_path.read_text(encoding="utf-8"))
                        if quote_path.exists() else {})
        old_dates = {str(row.get("close_date"))
                     for row in (old_snapshot.get("quotes") or {}).values()
                     if row.get("close_date")}
        if old_dates and canonical_date < max(old_dates):
            raise RuntimeError(
                f"candidate close {canonical_date} is older than stored quote {max(old_dates)}")

        quotes = dict(chain_quotes)
        for sid in universe_ids:
            if sid in quotes:
                continue
            extra = fetch_quote(sid)
            if str(extra.get("close_date")) != canonical_date:
                raise RuntimeError(
                    f"{sid}: close date {extra.get('close_date')} differs from {canonical_date}")
            quotes[sid] = extra

        records: dict[str, dict] = {}
        event_updates: dict[str, list] = {}
        failures: list[str] = []
        for sid in universe_ids:
            path = root / f"data/universe/{sid}.json"
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                if str(old.get("price_date") or "") > canonical_date:
                    raise ValueError(
                        f"stored price date {old.get('price_date')} is newer than candidate")
                records[sid] = refresh_us_record_price(
                    old, quotes[sid], screener_cfg, event_updates)
            except Exception as e:  # noqa: BLE001 - collect all ticker-specific failures
                failures.append(f"{sid}: {type(e).__name__}: {e}")

        chain_members = _chain_us_members(chain_cfg)
        extra_records: dict[str, dict] = {}
        extra_wrappers: dict[str, dict] = {}
        for sid in sorted(set(chain_members) - set(universe_ids)):
            key = f"ai_chain_us_record_v2_{sid}"
            cached = cache_get(key, ttl_seconds=None)
            if cached is None or not isinstance(cached.get("data"), dict):
                failures.append(f"{sid}: verified AI record cache is missing")
                continue
            try:
                extra_wrappers[sid] = cached
                if str(cached["data"].get("price_date") or "") > canonical_date:
                    raise ValueError(
                        f"stored price date {cached['data'].get('price_date')} is newer than candidate")
                extra_records[sid] = refresh_us_record_price(
                    cached["data"], quotes[sid], screener_cfg, event_updates)
            except Exception as e:  # noqa: BLE001 - collect all ticker-specific failures
                failures.append(f"{sid}: {type(e).__name__}: {e}")

        for warning in warnings:
            print(f"! quote: {warning}")
        if failures:
            for failure in failures:
                print(f"! price-only refresh failed: {failure}")
            raise RuntimeError(
                f"US price refresh aborted; preserved all prior files ({len(failures)} failures)")

        publications: dict[Path, str] = {
            quote_path: json.dumps(snapshot, ensure_ascii=False, indent=2),
        }
        for sid, record in records.items():
            path = root / f"data/universe/{sid}.json"
            publications[path] = json.dumps(record, ensure_ascii=False)
            state = ("partial preserved: " + "; ".join(record.get("errors") or [])
                     if record.get("partial_update") else "complete")
            print(f"[{sid}] {record['price_last']} ({record['price_date']}); {state}")
        for sid, record in extra_records.items():
            wrapper = _price_only_cache_wrapper(extra_wrappers[sid], record)
            publications[cache_path(f"ai_chain_us_record_v2_{sid}")] = json.dumps(
                wrapper, ensure_ascii=False)
            print(f"[{sid}] AI cache {record['price_last']} ({record['price_date']})")
        for key, data in event_updates.items():
            publications[cache_path(key)] = json.dumps(
                cache_entry(data), ensure_ascii=False)
        _publish_transaction(publications)

    return {
        "universe_records": len(records),
        "ai_cache_records": len(extra_records),
        "quotes": len(chain_quotes),
        "close_date": canonical_date,
    }
