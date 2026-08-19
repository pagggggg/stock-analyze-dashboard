import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from src.ai_chain_html import _node_row, _quote_html, _quote_update_meta
from src.tw_quotes import SOURCE, expected_tw_quote_tickers, validate_tw_quote_snapshot


MARKET_TZ = ZoneInfo("Asia/Taipei")


def quote(close_date="2026-08-11", previous_date="2026-08-10",
          close=101.0, previous=100.0):
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


def recent_dates():
    now = datetime.now(MARKET_TZ)
    close_date = now.date()
    if (now.hour, now.minute) < (14, 0):
        close_date -= timedelta(days=1)
    while close_date.weekday() >= 5:
        close_date -= timedelta(days=1)
    previous_date = close_date - timedelta(days=1)
    while previous_date.weekday() >= 5:
        previous_date -= timedelta(days=1)
    return close_date.isoformat(), previous_date.isoformat()


class TaiwanQuoteTests(unittest.TestCase):
    def test_config_has_seventeen_taiwan_tickers(self):
        cfg = yaml.safe_load(Path("config/ai_chain.yaml").read_text(encoding="utf-8"))
        tickers = expected_tw_quote_tickers(cfg)

        self.assertEqual(len(tickers), 17)
        self.assertIn("2330", tickers)
        self.assertIn("3324", tickers)
        self.assertIn("6488", tickers)

    def test_validator_accepts_long_exchange_holiday_gap(self):
        current = quote("2026-02-20", "2026-02-05")
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": "2026-02-20T06:30:00+00:00",
            "quotes": {"TEST": current},
        }

        validate_tw_quote_snapshot(
            snapshot, {"TEST"}, now=datetime(2026, 2, 20, 15, 0, tzinfo=MARKET_TZ))

    def test_validator_accepts_mixed_close_dates_for_suspended_stock(self):
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": "2026-08-11T07:00:00+00:00",
            "quotes": {
                "CURRENT": quote("2026-08-11", "2026-08-10"),
                "STALE": quote("2026-08-10", "2026-08-07"),
            },
        }

        validate_tw_quote_snapshot(
            snapshot, {"CURRENT", "STALE"},
            now=datetime(2026, 8, 11, 15, 0, tzinfo=MARKET_TZ))

    def test_validator_accepts_long_suspension_when_recently_checked(self):
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": "2026-08-18T07:00:00+00:00",
            "quotes": {"TEST": {
                **quote("2026-07-01", "2026-05-01"),
                "checked_through": "2026-08-18",
                "stale_reason": "no_official_trade",
            }},
        }

        validate_tw_quote_snapshot(
            snapshot, {"TEST"},
            now=datetime(2026, 8, 18, 15, 0, tzinfo=MARKET_TZ))

    def test_taiwan_quote_card_and_tpex_link(self):
        close_date, previous_date = recent_dates()
        current = quote(close_date, previous_date)
        external = _quote_html("3324", current, True, market="tpex")
        node = {
            "member": {"id": "3324", "name": "雙鴻", "market": "tpex"},
            "result": None,
            "quote": current,
            "cycle": {"status": "unknown", "reason": "資料不足"},
            "unavailable": "資料不足",
        }
        row = _node_row(node, set(), {})

        self.assertIn("NT$ 101.00", external)
        self.assertIn('data-quote-market="tpex"', external)
        self.assertIn("https://tw.stock.yahoo.com/quote/3324.TWO", external)
        self.assertIn('data-quote-ticker="3324"', row)
        self.assertIn("NT$ 101.00", row)

    def test_update_time_converts_to_taipei(self):
        text = _quote_update_meta(
            "美股", "2026-08-11T22:17:00+00:00", {"TEST": quote("2026-08-11")})

        self.assertIn("2026-08-12 06:17", text)
        self.assertIn("收盤日 2026-08-11", text)


if __name__ == "__main__":
    unittest.main()
