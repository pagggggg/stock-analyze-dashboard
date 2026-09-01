import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import yaml

from src.us_data import _reported_eps_events_regressed
from src.us_price_refresh import (refresh_us_prices,
                                  refresh_us_record_price,
                                  _price_only_cache_wrapper,
                                  _publish_transaction,
                                  _splits_after,
                                  update_us_record_from_price_inputs)


def _history(end: str = "2026-08-31") -> pd.DataFrame:
    index = pd.date_range("2019-01-02", end, freq="B", tz="America/New_York")
    closes = [100.0 + i / 100 for i in range(len(index))]
    return pd.DataFrame({
        "Close": closes,
        "Volume": [1_000_000.0] * len(index),
        "Stock Splits": [0.0] * len(index),
    }, index=index)


def _events(end: date = date(2026, 8, 31)) -> list[tuple[date, float]]:
    rows = []
    current = date(2018, 1, 2)
    while current <= end:
        rows.append((current, 1.0))
        current += timedelta(days=90)
    return rows


def _record(sid: str, partial_key: str | None = None, detail: bool = False) -> dict:
    record = {
        "stock_id": sid,
        "name": sid,
        "market": "us",
        "currency": "USD",
        "industry": "Semiconductors",
        "errors": [],
        "annual": {"2025": {"revenue": 10.0, "gross_profit": 5.0,
                              "eps": 4.0, "parent_ni": 3.0}},
        "annual_bs": {"2025": {"liabilities": 4.0, "total_assets": 10.0,
                                 "nci": 0.0}},
        "annual_ocf": {"2025": 3.0},
        "latest_bs": {"date": "2025-12-31", "liabilities": 4.0,
                       "total_assets": 10.0, "short_borrow": 1.0,
                       "long_borrow": None, "bonds": None, "cash": 2.0,
                       "equity": 6.0},
        "ocf_q": [["2025-12-31", 1.0]],
        "first_report": "2010-01-01",
        "latest_report": "2026-06-30",
        "price_last": 119.0,
        "price_date": "2026-08-28",
        "liq_avg_value": 100_000_000.0,
        "liq_days": 60,
        "valuation": {"forward_pe": 20.0, "peg": 1.0,
                      "fcf_yield": 2.0, "growth_pct": 20.0,
                      "coverage": 10},
    }
    if partial_key:
        record["partial_update"] = True
        record["errors"] = [f"本次抓取缺漏,沿用前次資料:{partial_key}"]
    if detail:
        financial_currency = "EUR" if sid == "ASML" else "USD"
        record["detail"] = {
            "schema_version": 2,
            "quote_currency": "USD",
            "financial_currency": financial_currency,
            "fx_note": "old",
            "latest_fx": 1.0,
            "shares_bn": 1.0,
            "eps_y0": 10.0,
            "eps_y1": 12.0,
            "growth_pct": 20.0,
            "yf": {
                "eps_y0": 10.0,
                "eps_y1": 12.0,
                "n_y0": 10,
                "fcf_ttm": 2_000_000_000.0,
                "sharesOutstanding": 1_000_000_000.0,
            },
            "quarters": [{"period": "2026-06-30", "eps": 3.0}],
            "splits": [],
            "river": {},
        }
    return record


class USPriceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.cfg = yaml.safe_load(Path("config/screener.yaml").read_text())
        self.history = _history()
        self.quote = {
            "close_date": "2026-08-31",
            "close": float(self.history["Close"].iloc[-1]),
        }

    def _assert_financial_blocks_unchanged(self, old: dict, new: dict) -> None:
        for key in ("annual", "annual_bs", "annual_ocf", "latest_bs", "ocf_q",
                    "first_report", "latest_report"):
            self.assertEqual(new[key], old[key], key)

    def test_nvda_price_refresh_does_not_add_partial_state(self):
        old = _record("NVDA")

        new = update_us_record_from_price_inputs(
            old, self.quote, self.cfg, self.history, _events())

        self.assertIsNone(new.get("partial_update"))
        self.assertEqual(new["errors"], [])
        self.assertEqual(new["price_date"], "2026-08-31")
        self.assertEqual(new["pe_hist"]["status"], "ok")
        self._assert_financial_blocks_unchanged(old, new)
        self.assertEqual(old["price_date"], "2026-08-28")

    def test_googl_price_refresh_preserves_existing_partial_only(self):
        old = _record("GOOGL", "annual_bs")

        new = update_us_record_from_price_inputs(
            old, self.quote, self.cfg, self.history, _events())

        self.assertTrue(new["partial_update"])
        self.assertEqual(new["errors"], old["errors"])
        self.assertEqual(new["price_date"], "2026-08-31")
        self._assert_financial_blocks_unchanged(old, new)

    def test_asml_price_refresh_preserves_ocf_and_rebuilds_fx_river(self):
        old = _record("ASML", "annual_ocf", detail=True)
        fx = [(ts.date(), 1.1) for ts in self.history.index]

        new = update_us_record_from_price_inputs(
            old, self.quote, self.cfg, self.history, _events(), fx,
            "point-in-time EUR/USD", 1.1)

        self.assertTrue(new["partial_update"])
        self.assertEqual(new["errors"], old["errors"])
        self.assertEqual(
            new["pe_hist"]["currency_conversion"]["as_of"], "2026-08-31")
        self.assertEqual(new["detail"]["river"]["current_date"], "2026-08-31")
        self.assertEqual(new["detail"]["latest_fx"], 1.1)
        self.assertEqual(new["detail"]["quarters"][0]["eps"], 3.3)
        self._assert_financial_blocks_unchanged(old, new)

    def test_transaction_preserves_all_files_when_one_ticker_fails(self):
        records = {sid: _record(sid) for sid in ("NVDA", "TSLA")}
        snapshot = {
            "updated_at": "2026-09-01T00:00:00+00:00",
            "quotes": {
                "NVDA": {"close_date": "2026-08-31", "close": 120.0},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data/universe").mkdir(parents=True)
            (root / "config/universe.yaml").write_text(
                yaml.safe_dump({"us": [{"stock_id": sid} for sid in records]}))
            quote_path = root / "data/ai_chain_quotes.json"
            quote_path.write_text(json.dumps(snapshot))
            originals = {}
            for sid, record in records.items():
                path = root / f"data/universe/{sid}.json"
                path.write_text(json.dumps(record))
                originals[sid] = path.read_text()

            def update(record, *_args):
                if record["stock_id"] == "TSLA":
                    raise RuntimeError("history unavailable")
                changed = deepcopy(record)
                changed["price_date"] = "2026-08-31"
                return changed

            chain_cfg = {"layers": [{"members": [
                {"id": "NVDA", "market": "us"}]}]}
            with patch("src.us_price_refresh.update_quote_snapshot",
                       return_value=(snapshot, [])), \
                    patch("src.us_price_refresh.fetch_quote",
                          return_value={"close_date": "2026-08-31", "close": 121.0}), \
                    patch("src.us_price_refresh.refresh_us_record_price",
                          side_effect=update):
                with self.assertRaisesRegex(RuntimeError, "preserved all prior files"):
                    refresh_us_prices(root, self.cfg, chain_cfg)

            for sid in records:
                self.assertEqual(
                    (root / f"data/universe/{sid}.json").read_text(), originals[sid])
            self.assertEqual(json.loads(quote_path.read_text()), snapshot)

    def test_transaction_rejects_canonical_date_rollback(self):
        record = _record("NVDA")
        record["price_date"] = "2026-09-01"
        old_snapshot = {
            "updated_at": "2026-09-01T00:00:00+00:00",
            "quotes": {"NVDA": {"close_date": "2026-09-01", "close": 122.0}},
        }
        candidate = {
            "updated_at": "2026-09-01T01:00:00+00:00",
            "quotes": {"NVDA": {"close_date": "2026-08-31", "close": 120.0}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data/universe").mkdir(parents=True)
            (root / "config/universe.yaml").write_text(
                yaml.safe_dump({"us": [{"stock_id": "NVDA"}]}))
            quote_path = root / "data/ai_chain_quotes.json"
            quote_path.write_text(json.dumps(old_snapshot))
            record_path = root / "data/universe/NVDA.json"
            record_path.write_text(json.dumps(record))

            chain_cfg = {"layers": [{"members": [
                {"id": "NVDA", "market": "us"}]}]}
            with patch("src.us_price_refresh.update_quote_snapshot",
                       return_value=(candidate, [])):
                with self.assertRaisesRegex(RuntimeError, "older than stored quote"):
                    refresh_us_prices(root, self.cfg, chain_cfg)

            self.assertEqual(json.loads(quote_path.read_text()), old_snapshot)
            self.assertEqual(json.loads(record_path.read_text()), record)

    def test_transaction_rejects_record_date_rollback(self):
        record = _record("TSLA")
        record["price_date"] = "2026-09-01"
        snapshot = {
            "updated_at": "2026-09-01T00:00:00+00:00",
            "quotes": {"NVDA": {"close_date": "2026-08-31", "close": 120.0}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data/universe").mkdir(parents=True)
            (root / "config/universe.yaml").write_text(
                yaml.safe_dump({"us": [{"stock_id": "TSLA"}]}))
            quote_path = root / "data/ai_chain_quotes.json"
            quote_path.write_text(json.dumps(snapshot))
            record_path = root / "data/universe/TSLA.json"
            record_path.write_text(json.dumps(record))
            chain_cfg = {"layers": [{"members": [
                {"id": "NVDA", "market": "us"}]}]}

            with patch("src.us_price_refresh.update_quote_snapshot",
                       return_value=(snapshot, [])), \
                    patch("src.us_price_refresh.fetch_quote",
                          return_value={"close_date": "2026-08-31", "close": 121.0}):
                with self.assertRaisesRegex(RuntimeError, "preserved all prior files"):
                    refresh_us_prices(root, self.cfg, chain_cfg)

            self.assertEqual(json.loads(record_path.read_text()), record)

    def test_reported_eps_cache_is_deferred_until_global_publication(self):
        old = _record("NVDA")
        events = _events()
        pending = {}
        with patch("src.us_price_refresh.fetch_us_price_inputs",
                   return_value=(self.history, events, True)), \
                patch("src.us_price_refresh.update_us_record_from_price_inputs",
                      side_effect=ValueError("invalid PE snapshot")):
            with self.assertRaisesRegex(ValueError, "invalid PE snapshot"):
                refresh_us_record_price(old, self.quote, self.cfg, pending)
            self.assertEqual(pending, {})

        with patch("src.us_price_refresh.fetch_us_price_inputs",
                   return_value=(self.history, events, True)), \
                patch("src.us_price_refresh.update_us_record_from_price_inputs",
                      return_value=old):
            refresh_us_record_price(old, self.quote, self.cfg, pending)
            self.assertIn("us_reported_eps_events_v1_NVDA", pending)

    def test_publication_failure_rolls_back_replaced_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text("old-first")
            second.write_text("old-second")
            real_replace = os.replace
            calls = 0

            def fail_second(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("disk failure")
                return real_replace(source, target)

            with patch("src.us_price_refresh.os.replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    _publish_transaction({first: "new-first", second: "new-second"})

            self.assertEqual(first.read_text(), "old-first")
            self.assertEqual(second.read_text(), "old-second")

    def test_price_only_ai_cache_keeps_financial_age(self):
        wrapper = {
            "fetched_at": 100.0,
            "fetched_date": "2026-08-20",
            "data": _record("AMD"),
            "source": "full-financial-refresh",
        }
        updated = _record("AMD")
        updated["price_date"] = "2026-08-31"

        result = _price_only_cache_wrapper(wrapper, updated)

        self.assertEqual(result["fetched_at"], 100.0)
        self.assertEqual(result["fetched_date"], "2026-08-20")
        self.assertEqual(result["source"], "full-financial-refresh")
        self.assertEqual(result["data"]["price_date"], "2026-08-31")

    def test_shorter_reported_eps_response_is_a_regression(self):
        old = [(date(2025, 1, 1) + timedelta(days=90 * i), 1.0)
               for i in range(8)]

        self.assertTrue(_reported_eps_events_regressed(old, old[-4:]))
        self.assertTrue(_reported_eps_events_regressed(old, []))
        self.assertFalse(_reported_eps_events_regressed(old, old + [
            (date(2027, 1, 1), 1.0)]))

    def test_new_split_requires_full_refresh(self):
        old = _record("NVDA")
        split_history = self.history.copy()
        split_history.loc[split_history.index[-1], "Stock Splits"] = 10.0

        self.assertEqual(
            _splits_after(split_history, "2026-08-28", "2026-08-31"),
            [("2026-08-31", 10.0)])
        with self.assertRaisesRegex(ValueError, "stock split requires full"):
            update_us_record_from_price_inputs(
                old, self.quote, self.cfg, split_history, _events())


if __name__ == "__main__":
    unittest.main()
