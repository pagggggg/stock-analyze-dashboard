import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from src.ai_quotes import (SOURCE, fetch_quote, load_quote_snapshot,
                           update_quote_snapshot, validate_quote_snapshot)
from src.ai_chain_html import _node_row, _quote_html


MARKET_TZ = ZoneInfo("America/New_York")


def quote(close_date, previous_date, close=101.0, previous=100.0):
    change = close - previous
    return {
        "currency": "USD",
        "close": close,
        "close_date": close_date.isoformat(),
        "previous_close": previous,
        "previous_date": previous_date.isoformat(),
        "change": change,
        "change_pct": change / previous * 100,
    }


def recent_dates():
    market_now = datetime.now(MARKET_TZ)
    close_date = market_now.date()
    if (market_now.hour, market_now.minute) < (16, 15):
        close_date -= timedelta(days=1)
    while close_date.weekday() >= 5:
        close_date -= timedelta(days=1)
    previous_date = close_date - timedelta(days=1)
    while previous_date.weekday() >= 5:
        previous_date -= timedelta(days=1)
    return close_date, previous_date


class FakeTicker:
    def __init__(self, frame):
        self.frame = frame

    def history(self, **_kwargs):
        return self.frame

    def get_history_metadata(self):
        return {"currency": "USD"}


class AIQuoteTests(unittest.TestCase):
    def test_fetch_ignores_in_progress_daily_bar(self):
        index = pd.DatetimeIndex([
            "2026-08-06 00:00", "2026-08-07 00:00", "2026-08-10 00:00"
        ], tz=MARKET_TZ)
        frame = pd.DataFrame({"Close": [98.0, 100.0, 105.0]}, index=index)
        module = SimpleNamespace(Ticker=lambda _ticker: FakeTicker(frame))
        before_close = datetime(2026, 8, 10, 15, 0, tzinfo=MARKET_TZ)
        after_close = datetime(2026, 8, 10, 17, 0, tzinfo=MARKET_TZ)

        with patch.dict("sys.modules", {"yfinance": module}):
            incomplete = fetch_quote("TEST", now=before_close)
            complete = fetch_quote("TEST", now=after_close)

        self.assertEqual(incomplete["close_date"], "2026-08-07")
        self.assertEqual(incomplete["previous_date"], "2026-08-06")
        self.assertEqual(complete["close_date"], "2026-08-10")
        self.assertEqual(complete["previous_date"], "2026-08-07")

    def test_snapshot_rejects_same_day_before_market_close(self):
        market_day = datetime(2026, 8, 10, 15, 0, tzinfo=MARKET_TZ)
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": market_day.astimezone(timezone.utc).isoformat(),
            "quotes": {"TEST": quote(market_day.date(), market_day.date() - timedelta(days=3))},
        }

        with self.assertRaisesRegex(ValueError, "交易尚未完成"):
            validate_quote_snapshot(snapshot, {"TEST"}, now=market_day)

    def test_snapshot_rejects_invalid_session_dates(self):
        market_now = datetime(2026, 8, 11, 17, 0, tzinfo=MARKET_TZ)
        cases = (
            (quote(market_now.date() - timedelta(days=2),
                   market_now.date() - timedelta(days=3)), "週末"),
            (quote(market_now.date(), market_now.date() - timedelta(days=11)), "間隔過長"),
        )
        for value, message in cases:
            with self.subTest(message=message):
                snapshot = {
                    "schema_version": 1,
                    "source": SOURCE,
                    "updated_at": market_now.astimezone(timezone.utc).isoformat(),
                    "quotes": {"TEST": value},
                }
                with self.assertRaisesRegex(ValueError, message):
                    validate_quote_snapshot(snapshot, {"TEST"}, now=market_now)

    def test_invalid_fetch_preserves_valid_old_quote(self):
        close_date, previous_date = recent_dates()
        old_quote = quote(close_date, previous_date)
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quotes": {"OTHER": old_quote, "TEST": old_quote},
        }
        cfg = {"cloud_capex": {"tickers": ["OTHER", "TEST"]}}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            invalid = {**old_quote, "currency": "EUR"}
            with patch("src.ai_quotes.fetch_quote",
                       side_effect=lambda ticker: invalid if ticker == "TEST" else old_quote):
                updated, warnings = update_quote_snapshot(cfg, path)

        self.assertEqual(updated["quotes"]["TEST"], old_quote)
        self.assertEqual(len(warnings), 1)
        self.assertIn("TEST:更新失敗", warnings[0])
        self.assertIn("沿用前次行情", warnings[0])

    def test_incompatible_old_source_is_not_relabelled(self):
        close_date, previous_date = recent_dates()
        snapshot = {
            "schema_version": 1,
            "source": "another source",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quotes": {"TEST": quote(close_date, previous_date)},
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "來源不相容"):
                load_quote_snapshot(path, {"TEST"})

    def test_total_fetch_failure_leaves_snapshot_untouched(self):
        close_date, previous_date = recent_dates()
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quotes": {"TEST": quote(close_date, previous_date)},
        }
        cfg = {"cloud_capex": {"tickers": ["TEST"]}}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.json"
            original = json.dumps(snapshot)
            path.write_text(original, encoding="utf-8")
            with patch("src.ai_quotes.fetch_quote", side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(RuntimeError, "全部更新失敗"):
                    update_quote_snapshot(cfg, path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_quote_links_and_unavailable_row_keep_ticker_context(self):
        close_date, previous_date = recent_dates()
        current = quote(close_date, previous_date)
        internal = _quote_html("ASML", current, True, "stock_ASML.html")
        external = _quote_html("PLTR", current)
        node = {
            "member": {"id": "PLTR", "name": "Palantir", "market": "us"},
            "result": None,
            "quote": current,
            "cycle": {"status": "unknown", "reason": "資料不足"},
            "unavailable": "資料不足",
        }
        row = _node_row(node, set(), {})

        self.assertIn('href="stock_ASML.html"', internal)
        self.assertNotIn('target="_blank"', internal)
        self.assertIn('href="https://finance.yahoo.com/quote/PLTR/"', external)
        self.assertIn('aria-label="PLTR ', external)
        self.assertIn('data-quote-ticker="PLTR"', row)
        self.assertIn("US$ 101.00", row)


if __name__ == "__main__":
    unittest.main()
