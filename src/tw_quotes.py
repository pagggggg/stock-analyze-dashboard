"""AI 產業鏈台股行情快照：最近兩個交易日收盤與單日漲跌。"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
SOURCE = "FinMind TaiwanStockPrice Close"
_MARKET_TZ = ZoneInfo("Asia/Taipei")
_DAILY_BAR_READY = (14, 0)
_MAX_AGE_DAYS = 14
_MAX_SESSION_GAP_DAYS = 21


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
    if (close_date - previous_date).days > _MAX_SESSION_GAP_DAYS:
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
    close_dates = {str(quote["close_date"]) for quote in quotes.values()}
    if len(close_dates) != 1:
        raise ValueError(f"AI 台股行情收盤日不一致:{sorted(close_dates)}")


def load_tw_quote_snapshot(path: str | Path, expected: set[str],
                           max_age_days: int = _MAX_AGE_DAYS) -> dict:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_tw_quote_snapshot(snapshot, expected, max_age_days)
    return snapshot


def fetch_tw_quote(ticker: str) -> dict:
    """從 FinMind 日線取最近兩個有效交易日。"""
    from .cache import _path
    from .data_layer import fetch_price_daily_finmind

    # 沿用河流圖的長日線 cache，避免短區間資料覆蓋同一 cache key 後又重抓。
    market_now = datetime.now(_MARKET_TZ)
    cache_path = _path(f"finmind_price_{ticker}")
    if cache_path.exists() and (market_now.hour, market_now.minute) >= _DAILY_BAR_READY:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            fetched = datetime.fromtimestamp(float(cached.get("fetched_at", 0)), _MARKET_TZ)
            market_close = market_now.replace(hour=14, minute=0, second=0, microsecond=0)
            if fetched < market_close:
                cache_path.unlink()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            cache_path.unlink(missing_ok=True)
    rows, _ = fetch_price_daily_finmind(ticker, start_date="2015-01-01")
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


def update_tw_quote_snapshot(cfg: dict, path: str | Path) -> tuple[dict, list[str]]:
    """逐檔更新；失敗保留前次有效值，全部失敗時不覆寫原快照。"""
    output = Path(path)
    expected = expected_tw_quote_tickers(cfg)
    market_now = datetime.now(timezone.utc).astimezone(_MARKET_TZ)
    old_quotes, warnings = {}, []
    if output.exists():
        try:
            old_snapshot = json.loads(output.read_text(encoding="utf-8"))
            _validate_snapshot_metadata(old_snapshot)
            candidates = old_snapshot.get("quotes")
            if not isinstance(candidates, dict):
                raise ValueError("AI 台股行情 quotes 格式錯誤")
            for ticker in sorted(expected & set(candidates)):
                try:
                    _validate_quote(ticker, candidates[ticker], market_now, _MAX_AGE_DAYS)
                except ValueError as e:
                    warnings.append(f"{ticker}:舊行情無法沿用:{e}")
                else:
                    old_quotes[ticker] = candidates[ticker]
        except (json.JSONDecodeError, OSError, ValueError) as e:
            warnings.append(f"舊台股行情快照無法沿用:{e}")

    quotes, fetched = {}, 0
    for ticker in sorted(expected):
        try:
            quote = fetch_tw_quote(ticker)
            _validate_quote(ticker, quote, market_now, _MAX_AGE_DAYS)
            quotes[ticker] = quote
            fetched += 1
        except Exception as e:  # noqa: BLE001 - preserve prior quote per ticker
            if ticker in old_quotes:
                quotes[ticker] = old_quotes[ticker]
                warnings.append(f"{ticker}:更新失敗:{e}；沿用前次行情")
            else:
                warnings.append(f"{ticker}:更新失敗:{e}")
    if expected and fetched == 0:
        raise RuntimeError("AI 台股行情全部更新失敗；保留原快照")

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "quotes": quotes,
    }
    validate_tw_quote_snapshot(snapshot, expected)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot, warnings
