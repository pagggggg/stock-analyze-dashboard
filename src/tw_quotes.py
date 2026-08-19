"""AI 產業鏈台股行情快照：最近兩個交易日收盤與單日漲跌。"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
SOURCE = "TWSE/TPEx official daily close"
_MARKET_TZ = ZoneInfo("Asia/Taipei")
_DAILY_BAR_READY = (14, 0)
_MAX_AGE_DAYS = 14


def expected_tw_quote_tickers(cfg: dict) -> set[str]:
    """從產業鏈設定推導唯一上市／上櫃代號集合。"""
    return {
        str(member["id"])
        for layer in cfg.get("layers") or []
        for member in layer.get("members") or []
        if member.get("market") in ("twse", "tpex")
    }


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
    if quote.get("currency") != "TWD":
        raise ValueError(f"{ticker}:預期 TWD 報價")
    if not all(math.isfinite(x) and x > 0 for x in (close, previous)):
        raise ValueError(f"{ticker}:收盤價必須為正數")
    if not all(math.isfinite(x) for x in (change, change_pct)):
        raise ValueError(f"{ticker}:漲跌資料必須為有限數字")
    today = market_now.date()
    if previous_date >= close_date or close_date > today:
        raise ValueError(f"{ticker}:交易日期順序錯誤")
    if previous_date.weekday() >= 5 or close_date.weekday() >= 5:
        raise ValueError(f"{ticker}:收盤日不可為週末")
    if (close_date == today
            and (market_now.hour, market_now.minute) < _DAILY_BAR_READY):
        raise ValueError(f"{ticker}:當日交易尚未完成")
    checked_through = close_date
    stale_reason = quote.get("stale_reason")
    if stale_reason is not None:
        if stale_reason != "no_official_trade":
            raise ValueError(f"{ticker}:未知的行情停滯原因")
        try:
            checked_through = date.fromisoformat(str(quote["checked_through"]))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{ticker}:停牌行情缺少查核日期") from e
        if checked_through < close_date or checked_through > today:
            raise ValueError(f"{ticker}:停牌行情查核日期錯誤")
    if (today - checked_through).days > max_age_days:
        raise ValueError(f"{ticker}:行情已逾 {max_age_days} 天")
    expected_change = close - previous
    expected_pct = expected_change / previous * 100
    if abs(change - expected_change) > 0.0001 or abs(change_pct - expected_pct) > 0.0001:
        raise ValueError(f"{ticker}:單日漲跌公式不一致")


def _validate_snapshot_metadata(snapshot: dict) -> None:
    if not isinstance(snapshot, dict):
        raise ValueError("AI 台股行情快照格式錯誤")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("AI 台股行情快照 schema 不相容")
    if snapshot.get("source") != SOURCE:
        raise ValueError("AI 台股行情快照來源不相容")
    try:
        updated_at = datetime.fromisoformat(str(snapshot["updated_at"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("AI 台股行情快照更新時間格式錯誤") from e
    if updated_at.tzinfo is None:
        raise ValueError("AI 台股行情快照更新時間缺少時區")


def validate_tw_quote_snapshot(snapshot: dict, expected: set[str],
                               max_age_days: int = _MAX_AGE_DAYS,
                               now: datetime | None = None) -> None:
    _validate_snapshot_metadata(snapshot)
    quotes = snapshot.get("quotes")
    if not isinstance(quotes, dict):
        raise ValueError("AI 台股行情 quotes 格式錯誤")
    if set(quotes) != expected:
        raise ValueError(
            f"AI 台股行情集合不一致；缺少 {sorted(expected-set(quotes))}；"
            f"多餘 {sorted(set(quotes)-expected)}")
    market_now = (now or datetime.now(timezone.utc)).astimezone(_MARKET_TZ)
    for ticker in sorted(expected):
        _validate_quote(ticker, quotes[ticker], market_now, max_age_days)
    # 個股可能停牌或當日無成交；各檔保留自己的最近有效交易日。


def load_tw_quote_snapshot(path: str | Path, expected: set[str],
                           max_age_days: int = _MAX_AGE_DAYS) -> dict:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_tw_quote_snapshot(snapshot, expected, max_age_days)
    return snapshot


def quote_from_price_rows(ticker: str, rows: list[dict]) -> dict:
    """由已取得的日價列建立行情，供全市場價格更新共用同一批資料。"""
    by_date = {}
    for row in rows:
        try:
            dstr = date.fromisoformat(str(row["date"])).isoformat()
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            by_date[dstr] = close
    dates = sorted(by_date)
    if len(dates) < 2:
        raise RuntimeError(f"{ticker}:最近有效收盤不足兩日")
    previous_date, close_date = dates[-2], dates[-1]
    previous, close = by_date[previous_date], by_date[close_date]
    change = close - previous
    return {
        "currency": "TWD",
        "close": close,
        "close_date": close_date,
        "previous_close": previous,
        "previous_date": previous_date,
        "change": change,
        "change_pct": change / previous * 100,
    }
