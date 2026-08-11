"""AI 產業鏈美股行情快照：最近兩個交易日收盤與單日漲跌。"""

from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
SOURCE = "Yahoo Finance Close (split-adjusted, not dividend-adjusted)"
_MARKET_TZ = ZoneInfo("America/New_York")
_DAILY_BAR_READY = (16, 15)


def expected_quote_tickers(cfg: dict) -> set[str]:
    """從產業鏈、雲端 Capex、產出側設定推導唯一美股代號集合。"""
    tickers = {
        str(member["id"])
        for layer in cfg.get("layers") or []
        for member in layer.get("members") or []
        if member.get("market") == "us"
    }
    tickers.update(str(x) for x in (cfg.get("cloud_capex") or {}).get("tickers") or [])
    tickers.update(str(x["company"]) for x in (cfg.get("output_side") or {}).get("metrics") or [])
    return tickers


def _validate_quote(ticker: str, quote: dict, market_now: datetime,
                    max_age_days: int) -> None:
    try:
        close = float(quote["close"])
        previous = float(quote["previous_close"])
        change = float(quote["change"])
        change_pct = float(quote["change_pct"])
        close_date = date.fromisoformat(str(quote["close_date"]))
        previous_date = date.fromisoformat(str(quote["previous_date"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{ticker}:行情欄位格式錯誤") from e
    if quote.get("currency") != "USD":
        raise ValueError(f"{ticker}:預期 USD 報價")
    if not all(math.isfinite(x) and x > 0 for x in (close, previous)):
        raise ValueError(f"{ticker}:收盤價必須為正數")
    if not all(math.isfinite(x) for x in (change, change_pct)):
        raise ValueError(f"{ticker}:漲跌資料必須為有限數字")
    today = market_now.date()
    if previous_date >= close_date or close_date > today:
        raise ValueError(f"{ticker}:交易日期順序錯誤")
    if previous_date.weekday() >= 5 or close_date.weekday() >= 5:
        raise ValueError(f"{ticker}:收盤日不可為週末")
    if (close_date - previous_date).days > 7:
        raise ValueError(f"{ticker}:前後收盤日間隔過長")
    if (close_date == today
            and (market_now.hour, market_now.minute) < _DAILY_BAR_READY):
        raise ValueError(f"{ticker}:當日交易尚未完成")
    if (today - close_date).days > max_age_days:
        raise ValueError(f"{ticker}:行情已逾 {max_age_days} 天")
    expected_change = close - previous
    expected_pct = expected_change / previous * 100
    if abs(change - expected_change) > 0.0001 or abs(change_pct - expected_pct) > 0.0001:
        raise ValueError(f"{ticker}:單日漲跌公式不一致")


def _validate_snapshot_metadata(snapshot: dict) -> None:
    if not isinstance(snapshot, dict):
        raise ValueError("AI 行情快照格式錯誤")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("AI 行情快照 schema 不相容")
    if snapshot.get("source") != SOURCE:
        raise ValueError("AI 行情快照來源不相容")
    try:
        updated_at = datetime.fromisoformat(str(snapshot["updated_at"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("AI 行情快照更新時間格式錯誤") from e
    if updated_at.tzinfo is None:
        raise ValueError("AI 行情快照更新時間缺少時區")


def validate_quote_snapshot(snapshot: dict, expected: set[str], max_age_days: int = 7,
                            now: datetime | None = None) -> None:
    _validate_snapshot_metadata(snapshot)
    quotes = snapshot.get("quotes")
    if not isinstance(quotes, dict):
        raise ValueError("AI 行情 quotes 格式錯誤")
    if set(quotes) != expected:
        raise ValueError(f"AI 行情集合不一致；缺少 {sorted(expected-set(quotes))}；多餘 {sorted(set(quotes)-expected)}")
    market_now = (now or datetime.now(timezone.utc)).astimezone(_MARKET_TZ)
    for ticker in sorted(expected):
        _validate_quote(ticker, quotes[ticker], market_now, max_age_days)


def load_quote_snapshot(path: str | Path, expected: set[str], max_age_days: int = 7) -> dict:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_quote_snapshot(snapshot, expected, max_age_days)
    return snapshot


def fetch_quote(ticker: str, now: datetime | None = None) -> dict:
    """抓最近兩個有效交易日；短暫 Yahoo 錯誤會遞增等待後重試。"""
    import yfinance as yf

    last_error = None
    for attempt in range(3):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="10d", auto_adjust=False, actions=False)
            closes = hist["Close"].dropna()
            market_now = (now or datetime.now(timezone.utc)).astimezone(_MARKET_TZ)
            if (len(closes) and closes.index[-1].date() == market_now.date()
                    and (market_now.hour, market_now.minute) < _DAILY_BAR_READY):
                closes = closes.iloc[:-1]
            if len(closes) < 2:
                raise RuntimeError("最近有效收盤不足兩日")
            previous_ts, close_ts = closes.index[-2], closes.index[-1]
            previous, close = float(closes.iloc[-2]), float(closes.iloc[-1])
            currency = str((t.get_history_metadata() or {}).get("currency") or "USD")
            change = close - previous
            return {
                "currency": currency,
                "close": close,
                "close_date": close_ts.date().isoformat(),
                "previous_close": previous,
                "previous_date": previous_ts.date().isoformat(),
                "change": change,
                "change_pct": change / previous * 100,
            }
        except Exception as e:  # noqa: BLE001 - retry transient Yahoo/session failures
            last_error = e
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"{ticker}:Yahoo 行情抓取失敗:{last_error}")


def update_quote_snapshot(cfg: dict, path: str | Path) -> tuple[dict, list[str]]:
    """逐檔更新；失敗時保留前次有效值，沒有前值才視為缺漏。"""
    output = Path(path)
    expected = expected_quote_tickers(cfg)
    market_now = datetime.now(timezone.utc).astimezone(_MARKET_TZ)
    old_quotes, warnings = {}, []
    if output.exists():
        try:
            old_snapshot = json.loads(output.read_text(encoding="utf-8"))
            _validate_snapshot_metadata(old_snapshot)
            candidates = old_snapshot.get("quotes")
            if not isinstance(candidates, dict):
                raise ValueError("AI 行情 quotes 格式錯誤")
            for ticker in sorted(expected & set(candidates)):
                try:
                    _validate_quote(ticker, candidates[ticker], market_now, 7)
                except ValueError as e:
                    warnings.append(f"{ticker}:舊行情無法沿用:{e}")
                else:
                    old_quotes[ticker] = candidates[ticker]
        except (json.JSONDecodeError, OSError, ValueError) as e:
            warnings.append(f"舊行情快照無法沿用:{e}")
    quotes, fetched = {}, 0
    for ticker in sorted(expected):
        try:
            quote = fetch_quote(ticker)
            _validate_quote(ticker, quote, market_now, 7)
            quotes[ticker] = quote
            fetched += 1
        except Exception as e:  # noqa: BLE001 - preserve prior complete quote per ticker
            if ticker in old_quotes:
                quotes[ticker] = old_quotes[ticker]
                warnings.append(f"{ticker}:更新失敗:{e}；沿用前次行情")
            else:
                warnings.append(f"{ticker}:更新失敗:{e}")
    if expected and fetched == 0:
        raise RuntimeError("AI 美股行情全部更新失敗；保留原快照")
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "quotes": quotes,
    }
    validate_quote_snapshot(snapshot, expected)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot, warnings
