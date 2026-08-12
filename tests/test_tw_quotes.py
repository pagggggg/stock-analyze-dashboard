import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml

from src.ai_chain_html import _node_row, _quote_html, _quote_update_meta
from src.tw_quotes import (SOURCE, expected_tw_quote_tickers,
                           update_tw_quote_snapshot,
                           validate_tw_quote_snapshot)


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

    def test_validator_rejects_mixed_close_dates(self):
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": "2026-08-11T07:00:00+00:00",
            "quotes": {
                "CURRENT": quote("2026-08-11", "2026-08-10"),
                "STALE": quote("2026-08-10", "2026-08-07"),
            },
        }

        with self.assertRaisesRegex(ValueError, "收盤日不一致"):
            validate_tw_quote_snapshot(
                snapshot, {"CURRENT", "STALE"},
                now=datetime(2026, 8, 11, 15, 0, tzinfo=MARKET_TZ))

    def test_invalid_fetch_preserves_valid_old_quote(self):
        close_date, previous_date = recent_dates()
        old_quote = quote(close_date, previous_date)
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quotes": {"OTHER": old_quote, "TEST": old_quote},
        }
        cfg = {"layers": [{"members": [
            {"id": "OTHER", "market": "twse"},
            {"id": "TEST", "market": "tpex"},
        ]}]}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tw-quotes.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            invalid = {**old_quote, "currency": "USD"}
            with patch("src.tw_quotes.fetch_tw_quote",
                       side_effect=lambda ticker: invalid if ticker == "TEST" else old_quote):
                updated, warnings = update_tw_quote_snapshot(cfg, path)

        self.assertEqual(updated["quotes"]["TEST"], old_quote)
        self.assertEqual(len(warnings), 1)
        self.assertIn("沿用前次行情", warnings[0])

    def test_total_fetch_failure_leaves_snapshot_untouched(self):
        close_date, previous_date = recent_dates()
        snapshot = {
            "schema_version": 1,
            "source": SOURCE,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quotes": {"TEST": quote(close_date, previous_date)},
        }
        cfg = {"layers": [{"members": [{"id": "TEST", "market": "twse"}]}]}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tw-quotes.json"
            original = json.dumps(snapshot)
            path.write_text(original, encoding="utf-8")
            with patch("src.tw_quotes.fetch_tw_quote", side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(RuntimeError, "全部更新失敗"):
                    update_tw_quote_snapshot(cfg, path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

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
