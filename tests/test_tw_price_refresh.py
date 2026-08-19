import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml
from zoneinfo import ZoneInfo

from src.tw_price_refresh import (_fetch_tpex_day, _fetch_twse_day,
                                  _carry_forward_pe_history,
                                  _expected_income_period,
                                  _income_for_price_date, _publish_records,
                                  refresh_tw_prices)


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def iterrows(self):
        return enumerate(self.rows)


def twse_payload(day="20260817", targets=None, rows=500):
    targets = targets or [["2330", "2,400.00", "32,423,014,050"]]
    filler = [[f"X{i:04d}", "1.00", "1"] for i in range(rows - len(targets))]
    return {"stat": "OK", "date": day, "tables": [{
        "fields": ["證券代號", "收盤價", "成交金額"],
        "data": targets + filler,
    }]}


def tpex_payload(day="20260817", targets=None, rows=500):
    targets = targets or [["3324", "1005.00", "1,535,590,310"]]
    filler = [[f"X{i:04d}", "1.00", "1"] for i in range(rows - len(targets))]
    data = targets + filler
    return {"stat": "ok", "date": day, "tables": [{
        "fields": ["代號", "收盤", "成交金額(元)"],
        "data": data, "totalCount": len(data),
    }]}


class TaiwanPriceRefreshTests(unittest.TestCase):
    def test_twse_market_parser_extracts_target_stock(self):
        payload = twse_payload()
        with (patch("src.tw_price_refresh.cache_get", return_value=None),
              patch("src.tw_price_refresh.cache_set"),
              patch("src.tw_price_refresh._json_get", return_value=payload)):
            rows = _fetch_twse_day(date(2026, 8, 17), {"2330"})

        self.assertEqual(rows["2330"]["close"], 2400.0)
        self.assertEqual(rows["2330"]["value"], 32423014050.0)

    def test_tpex_market_parser_extracts_target_stock(self):
        payload = tpex_payload()
        with (patch("src.tw_price_refresh.cache_get", return_value=None),
              patch("src.tw_price_refresh.cache_set"),
              patch("src.tw_price_refresh._json_get", return_value=payload)):
            rows = _fetch_tpex_day(date(2026, 8, 17), {"3324"})

        self.assertEqual(rows["3324"]["close"], 1005.0)
        self.assertEqual(rows["3324"]["value"], 1535590310.0)

    def test_empty_market_response_is_not_persisted(self):
        payload = {"stat": "很抱歉，沒有符合條件的資料!", "tables": []}
        with (patch("src.tw_price_refresh.cache_get", return_value=None),
              patch("src.tw_price_refresh.cache_set") as cache_set,
              patch("src.tw_price_refresh._json_get", return_value=payload)):
            rows = _fetch_twse_day(date(2026, 8, 18), {"2330"})

        self.assertEqual(rows, {})
        cache_set.assert_not_called()

    def test_day_cache_must_cover_requested_tickers(self):
        cached = {"status": "ok", "target_ids": ["2308"],
                  "data": {"2308": {"date": "2026-08-17", "close": 1885.0, "value": 1}}}
        payload = twse_payload()
        with (patch("src.tw_price_refresh.cache_get", return_value=cached),
              patch("src.tw_price_refresh.cache_set"),
              patch("src.tw_price_refresh._json_get", return_value=payload) as fetch):
            rows = _fetch_twse_day(date(2026, 8, 17), {"2330"})

        fetch.assert_called_once()
        self.assertIn("2330", rows)

    def test_cached_superset_is_filtered_to_current_universe(self):
        cached = {"status": "ok", "target_ids": ["2308", "2330"], "data": {
            "2308": {"date": "2026-08-17", "close": 100.0, "value": 1},
            "2330": {"date": "2026-08-17", "close": 200.0, "value": 1},
        }}
        with patch("src.tw_price_refresh.cache_get", return_value=cached):
            rows = _fetch_twse_day(date(2026, 8, 17), {"2330"})

        self.assertEqual(set(rows), {"2330"})

    def test_wrong_exchange_date_is_rejected(self):
        with (patch("src.tw_price_refresh.cache_get", return_value=None),
              patch("src.tw_price_refresh._json_get",
                    return_value=twse_payload(day="20260816"))):
            with self.assertRaisesRegex(RuntimeError, "日期不符"):
                _fetch_twse_day(date(2026, 8, 17), {"2330"})

    def test_tpex_total_count_must_match_rows(self):
        payload = tpex_payload()
        payload["tables"][0]["totalCount"] += 1
        with (patch("src.tw_price_refresh.cache_get", return_value=None),
              patch("src.tw_price_refresh._json_get", return_value=payload)):
            with self.assertRaisesRegex(RuntimeError, "原始表不完整"):
                _fetch_tpex_day(date(2026, 8, 17), {"3324"})

    def test_truncated_market_table_is_rejected(self):
        payload = {"stat": "OK", "date": "20260817", "tables": [{
            "fields": ["證券代號", "收盤價", "成交金額"],
            "data": [["2330", "2,400.00", "32,423,014,050"]],
        }]}
        with (patch("src.tw_price_refresh.cache_get", return_value=None),
              patch("src.tw_price_refresh.cache_set") as cache_set,
              patch("src.tw_price_refresh._json_get", return_value=payload)):
            with self.assertRaisesRegex(RuntimeError, "原始表不完整"):
                _fetch_twse_day(date(2026, 8, 17), {"2330", "2308", "2383"})
        cache_set.assert_not_called()

    def test_twse_target_coverage_is_checked_after_full_table_parse(self):
        payload = twse_payload(targets=[
            ["2330", "2,400.00", "32,423,014,050"],
        ])
        with (patch("src.tw_price_refresh.cache_get", return_value=None),
              patch("src.tw_price_refresh.cache_set") as cache_set,
              patch("src.tw_price_refresh._json_get", return_value=payload)):
            with self.assertRaisesRegex(RuntimeError, "覆蓋不足"):
                _fetch_twse_day(date(2026, 8, 17), {"2330", "2308", "2383"})
        cache_set.assert_not_called()

    def test_cached_quarter_without_eps_is_refetched(self):
        cached = {
            "2026-03-31": {"EPS": 1.0},
            "2026-06-30": {"Revenue": 100.0},
        }
        loader = unittest.mock.Mock()
        loader.taiwan_stock_financial_statement.return_value = FakeFrame([
            {"date": "2026-03-31", "type": "EPS", "value": 1.0},
            {"date": "2026-06-30", "type": "EPS", "value": 1.2},
        ])
        with patch("src.tw_price_refresh._load_cache_data", return_value=cached):
            income, refreshed, attempted = _income_for_price_date(
                loader, "2330", {"name": "台積電", "industry": "半導體業"},
                "2026-08-18", "2018-01-01")

        self.assertTrue(refreshed)
        self.assertTrue(attempted)
        self.assertEqual(income["2026-06-30"]["EPS"], 1.2)
        self.assertEqual(income["2026-06-30"]["Revenue"], 100.0)

    def test_financial_company_uses_later_q2_deadline(self):
        self.assertEqual(_expected_income_period(date(2026, 8, 18)), "2026-06-30")
        self.assertEqual(
            _expected_income_period(date(2026, 8, 18), financial_company=True),
            "2026-03-31")

    def test_tpex_member_can_use_same_income_refresh_path(self):
        cached = {"2026-03-31": {"EPS": 1.0}}
        loader = unittest.mock.Mock()
        loader.taiwan_stock_financial_statement.return_value = FakeFrame([
            {"date": "2026-06-30", "type": "EPS", "value": 1.2},
        ])
        with patch("src.tw_price_refresh._load_cache_data", return_value=cached):
            income, refreshed, attempted = _income_for_price_date(
                loader, "3324", {"name": "雙鴻", "industry": "", "market": "tpex"},
                "2026-08-18", "2018-01-01")

        self.assertTrue(refreshed)
        self.assertTrue(attempted)
        self.assertEqual(income["2026-06-30"]["EPS"], 1.2)

    def test_persisted_ttm_basis_reprices_after_cache_regression(self):
        old = {
            "status": "ok", "current_ttm_eps": 10.0,
            "current_trailing_pe": 10.0, "current_date": "2026-08-17",
            "as_of": "2026-08-17", "window_start": "2021-08-17",
            "p10": 8.0, "median": 12.0, "p90": 20.0, "n": 1000,
            "source_coverage": {
                "price_start": "2020-01-01", "price_end": "2026-08-17",
                "price_n": 1000, "eps_start": "2020-03-31",
                "eps_end": "2026-06-30", "eps_n": 24,
                "eps_max_gap_quarters": 1, "eps_pre_window_n": 4,
            },
        }
        rows = [
            {"date": "2026-08-17", "close": 100.0},
            {"date": "2026-08-18", "close": 110.0},
        ]

        carried = _carry_forward_pe_history(
            old, rows, rows[-1], years=5, expected_period="2026-06-30")

        self.assertEqual(carried["current_trailing_pe"], 11.0)
        self.assertEqual(carried["current_date"], "2026-08-18")
        self.assertEqual(carried["source_coverage"]["eps_end"], "2026-06-30")
        self.assertTrue(carried["source_cache_regressed"])

    def test_persisted_ttm_basis_expires_when_next_quarter_is_due(self):
        old = {
            "status": "ok", "current_ttm_eps": 10.0,
            "source_coverage": {"eps_end": "2026-06-30"},
        }
        latest = {"date": "2026-11-17", "close": 110.0}

        carried = _carry_forward_pe_history(
            old, [latest], latest, years=5, expected_period="2026-09-30")

        self.assertIsNone(carried)

    def test_refetch_without_expected_eps_preserves_old_cache(self):
        cached = {"2026-03-31": {"EPS": 1.0}}
        loader = unittest.mock.Mock()
        loader.taiwan_stock_financial_statement.return_value = FakeFrame([
            {"date": "2026-06-30", "type": "Revenue", "value": 100.0},
        ])
        with (patch("src.tw_price_refresh._load_cache_data", return_value=cached),
              patch("src.tw_price_refresh.cache_set") as cache_set):
            income, refreshed, attempted = _income_for_price_date(
                loader, "2330", {"name": "台積電", "industry": "半導體業"},
                "2026-08-18", "2018-01-01")

        self.assertEqual(income, cached)
        self.assertFalse(refreshed)
        self.assertTrue(attempted)
        cache_set.assert_not_called()

    def test_truncated_refetch_with_expected_eps_preserves_history(self):
        cached = {
            "2020-03-31": {"EPS": 0.5, "Revenue": 10.0},
            "2026-03-31": {"EPS": 1.0},
        }
        loader = unittest.mock.Mock()
        loader.taiwan_stock_financial_statement.return_value = FakeFrame([
            {"date": "2026-06-30", "type": "EPS", "value": 1.2},
        ])
        with patch("src.tw_price_refresh._load_cache_data", return_value=cached):
            income, refreshed, attempted = _income_for_price_date(
                loader, "2330", {"name": "台積電", "industry": "半導體業"},
                "2026-08-18", "2018-01-01")

        self.assertTrue(refreshed)
        self.assertTrue(attempted)
        self.assertEqual(income["2020-03-31"], cached["2020-03-31"])
        self.assertEqual(income["2026-06-30"]["EPS"], 1.2)

    def test_one_price_batch_updates_records_and_quotes_together(self):
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        close_date = now.date()
        if (now.hour, now.minute) < (14, 0):
            close_date -= timedelta(days=1)
        while close_date.weekday() >= 5:
            close_date -= timedelta(days=1)
        previous_date = close_date - timedelta(days=1)
        while previous_date.weekday() >= 5:
            previous_date -= timedelta(days=1)
        recent = [
            {"date": previous_date.isoformat(), "close": 100.0,
             "Trading_money": 1_000_000_000},
            {"date": close_date.isoformat(), "close": 101.0,
             "Trading_money": 1_100_000_000},
        ]
        history = []
        d = close_date - timedelta(days=90)
        while len(history) < 60:
            if d.weekday() < 5:
                history.append({"date": d.isoformat(), "close": 90.0})
            d += timedelta(days=1)
        chain_ids = [f"{1000+i}" for i in range(17)]
        universe_ids = chain_ids[:2]
        chain_cfg = {"layers": [{"members": [
            {"id": sid, "market": "twse" if i < 15 else "tpex"}
            for i, sid in enumerate(chain_ids)
        ]}]}
        screener_cfg = {
            "layer1": {"liquidity": {"days": 2}},
            "valuation_flag": {"pe_history_years": 5},
            "fetch": {"sleep_seconds": 0},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data/universe").mkdir(parents=True)
            (root / "config/universe.yaml").write_text(
                yaml.safe_dump({"twse": [{"stock_id": sid} for sid in universe_ids]}),
                encoding="utf-8")
            (root / "data/ai_chain_tw_quotes.json").write_text(
                json.dumps({"schema_version": 0, "quotes": {}}), encoding="utf-8")
            for sid in universe_ids:
                (root / f"data/universe/{sid}.json").write_text(
                    json.dumps({"stock_id": sid, "name": sid, "market": "twse",
                                "price_last": 90.0, "price_date": history[-1]["date"]}),
                    encoding="utf-8")

            value_history = [{**row, "value": 900_000_000} for row in history]

            def cache_get(key, ttl_seconds=None):
                if key.startswith("finmind_price_"):
                    return {"data": history}
                if key.startswith("finmind_pxv_"):
                    return {"data": value_history}
                return None

            def updated(record, rows, recent_rows, _cfg, income, **_kwargs):
                out = dict(record)
                out.update(price_last=rows[-1]["close"], price_date=rows[-1]["date"])
                return out

            exchange_rows = {sid: [
                {"date": previous_date.isoformat(), "close": 100.0,
                 "value": 1_000_000_000},
                {"date": close_date.isoformat(), "close": 101.0,
                 "value": 1_100_000_000},
            ] for sid in chain_ids}

            with (patch("src.tw_price_refresh.cache_get", side_effect=cache_get),
                  patch("src.tw_price_refresh.cache_set"),
                  patch("src.tw_price_refresh._fetch_exchange_rows",
                        return_value=exchange_rows),
                  patch("src.tw_price_refresh._income_for_price_date",
                        return_value=({}, False, False)),
                  patch("src.tw_price_refresh._updated_record", side_effect=updated)):
                result = refresh_tw_prices(
                    root, screener_cfg, chain_cfg, loader=object(), sleep_seconds=0,
                    refresh_supporting=False, verbose=False)

            snapshot = json.loads(
                (root / "data/ai_chain_tw_quotes.json").read_text(encoding="utf-8"))
            self.assertEqual(result["records"], 2)
            self.assertEqual(result["quotes"], 17)
            self.assertTrue(result["updated"])
            for sid in universe_ids:
                record = json.loads(
                    (root / f"data/universe/{sid}.json").read_text(encoding="utf-8"))
                self.assertEqual(record["price_date"], snapshot["quotes"][sid]["close_date"])
                self.assertEqual(record["price_last"], snapshot["quotes"][sid]["close"])

    def test_publish_rolls_back_all_files_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/universe").mkdir(parents=True)
            old_record = {"stock_id": "2330", "price_last": 100}
            old_quote = {"schema_version": 1, "quotes": {"2330": {"close": 100}}}
            (root / "data/universe/2330.json").write_text(
                json.dumps(old_record), encoding="utf-8")
            (root / "data/ai_chain_tw_quotes.json").write_text(
                json.dumps(old_quote), encoding="utf-8")
            real_replace = __import__("os").replace
            calls = 0

            def fail_second_publish(source, target):
                nonlocal calls
                if ".tw-price-stage-" in str(source):
                    calls += 1
                    if calls == 2:
                        raise OSError("simulated interruption")
                return real_replace(source, target)

            with patch("src.tw_price_refresh.os.replace", side_effect=fail_second_publish):
                with self.assertRaisesRegex(OSError, "simulated"):
                    _publish_records(
                        root, {"2330": {"stock_id": "2330", "price_last": 200}},
                        {"schema_version": 1, "quotes": {"2330": {"close": 200}}})

            self.assertEqual(
                json.loads((root / "data/universe/2330.json").read_text()), old_record)
            self.assertEqual(
                json.loads((root / "data/ai_chain_tw_quotes.json").read_text()), old_quote)
            self.assertFalse((root / "data/.tw-price-publish.json").exists())

    def test_cache_ahead_of_artifacts_repairs_snapshot(self):
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        close_date = now.date()
        if (now.hour, now.minute) < (14, 0):
            close_date -= timedelta(days=1)
        while close_date.weekday() >= 5:
            close_date -= timedelta(days=1)
        previous_date = close_date - timedelta(days=1)
        while previous_date.weekday() >= 5:
            previous_date -= timedelta(days=1)
        history = []
        d = close_date - timedelta(days=90)
        while len(history) < 60:
            if d.weekday() < 5:
                history.append({"date": d.isoformat(), "close": 90, "value": 1})
            d += timedelta(days=1)
        history.extend([
            {"date": previous_date.isoformat(), "close": 99, "value": 1},
            {"date": close_date.isoformat(), "close": 100, "value": 1},
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data/universe").mkdir(parents=True)
            (root / "config/universe.yaml").write_text(
                yaml.safe_dump({"twse": [{"stock_id": "2330"}]}), encoding="utf-8")
            record = {"stock_id": "2330", "market": "twse",
                      "price_last": 90, "price_date": history[-3]["date"]}
            quote = {"schema_version": 0, "quotes": {}}
            (root / "data/universe/2330.json").write_text(json.dumps(record), encoding="utf-8")
            quote_path = root / "data/ai_chain_tw_quotes.json"
            quote_path.write_text(json.dumps(quote), encoding="utf-8")
            chain_cfg = {"layers": [{"members": [
                {"id": str(1000+i), "market": "twse"} for i in range(16)
            ] + [{"id": "2330", "market": "twse"}]}]}
            def updated(old, rows, _recent, _cfg, _income, **_kwargs):
                return {**old, "price_last": rows[-1]["close"], "price_date": rows[-1]["date"]}
            def exchange(_twse, _tpex, _start_twse, _start_tpex, _end, verified=None):
                verified["twse"] = close_date.isoformat()
                verified["tpex"] = close_date.isoformat()
                return {}
            with (patch("src.tw_price_refresh.cache_get", return_value={"data": history}),
                  patch("src.tw_price_refresh.cache_set"),
                  patch("src.tw_price_refresh._fetch_exchange_rows", side_effect=exchange),
                  patch("src.tw_price_refresh._income_for_price_date",
                        return_value=({}, False, False)),
                  patch("src.tw_price_refresh._updated_record", side_effect=updated)):
                result = refresh_tw_prices(
                    root, {}, chain_cfg, loader=object(), refresh_supporting=False,
                    verbose=False)

            self.assertTrue(result["updated"])
            repaired = json.loads(quote_path.read_text())
            self.assertEqual(repaired["quotes"]["2330"]["close_date"], close_date.isoformat())

    def test_unverified_cache_date_is_not_published(self):
        verified_date = date(2026, 8, 18)
        future_date = date(2026, 8, 19)
        history = []
        d = verified_date - timedelta(days=90)
        while len(history) < 60:
            if d.weekday() < 5:
                history.append({"date": d.isoformat(), "close": 100, "value": 1})
            d += timedelta(days=1)
        history.extend([
            {"date": verified_date.isoformat(), "close": 101, "value": 1},
            {"date": future_date.isoformat(), "close": 999, "value": 1},
        ])
        chain_ids = [f"{1000+i}" for i in range(17)]
        chain_cfg = {"layers": [{"members": [
            {"id": sid, "market": "twse"} for sid in chain_ids
        ]}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data/universe").mkdir(parents=True)
            (root / "config/universe.yaml").write_text(
                yaml.safe_dump({"twse": [{"stock_id": "1000"}]}), encoding="utf-8")
            record = {"stock_id": "1000", "market": "twse", "price_last": 100,
                      "price_date": verified_date.isoformat(),
                      "price_checked_through": verified_date.isoformat()}
            (root / "data/universe/1000.json").write_text(json.dumps(record), encoding="utf-8")
            (root / "data/ai_chain_tw_quotes.json").write_text(
                json.dumps({"schema_version": 0, "quotes": {}}), encoding="utf-8")

            def exchange(_twse, _tpex, _start_twse, _start_tpex, _end, verified=None):
                verified["twse"] = verified_date.isoformat()
                return {}
            def updated(old, rows, _recent, _cfg, _income, **_kwargs):
                return {**old, "price_last": rows[-1]["close"], "price_date": rows[-1]["date"]}
            with (patch("src.tw_price_refresh.cache_get", return_value={"data": history}),
                  patch("src.tw_price_refresh.cache_set"),
                  patch("src.tw_price_refresh._fetch_exchange_rows", side_effect=exchange),
                  patch("src.tw_price_refresh._income_for_price_date",
                        return_value=({}, False, False)),
                  patch("src.tw_price_refresh._updated_record", side_effect=updated)):
                refresh_tw_prices(root, {}, chain_cfg, loader=object(),
                                  refresh_supporting=False, verbose=False)

            repaired = json.loads((root / "data/ai_chain_tw_quotes.json").read_text())
            self.assertEqual(repaired["quotes"]["1000"]["close_date"],
                             verified_date.isoformat())
            self.assertNotEqual(repaired["quotes"]["1000"]["close"], 999)

    def test_duplicate_exchange_rows_do_not_trigger_republish(self):
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        close_date = now.date()
        if (now.hour, now.minute) < (14, 0):
            close_date -= timedelta(days=1)
        while close_date.weekday() >= 5:
            close_date -= timedelta(days=1)
        existing = []
        d = close_date - timedelta(days=90)
        while len(existing) < 60:
            if d.weekday() < 5:
                existing.append({"date": d.isoformat(), "close": 100, "value": 1})
            d += timedelta(days=1)
        chain_ids = [f"{1000+i}" for i in range(17)]
        chain_cfg = {"layers": [{"members": [
            {"id": sid, "market": "twse"} for sid in chain_ids
        ]}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data/universe").mkdir(parents=True)
            (root / "config/universe.yaml").write_text(
                yaml.safe_dump({"twse": [{"stock_id": "1000"}]}), encoding="utf-8")
            record = {"stock_id": "1000", "market": "twse",
                      "price_last": 100, "price_date": existing[-1]["date"]}
            (root / "data/universe/1000.json").write_text(json.dumps(record), encoding="utf-8")
            quote_path = root / "data/ai_chain_tw_quotes.json"
            quotes = {sid: {
                "currency": "TWD", "close": 100, "close_date": existing[-1]["date"],
                "previous_close": 100, "previous_date": existing[-2]["date"],
                "change": 0, "change_pct": 0,
            } for sid in chain_ids}
            snapshot = {"schema_version": 1, "source": "TWSE/TPEx official daily close",
                        "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(), "quotes": quotes}
            quote_path.write_text(json.dumps(snapshot), encoding="utf-8")
            duplicates = {sid: list(existing) for sid in chain_ids}
            def updated(old, _rows, _recent, _cfg, _income, **_kwargs):
                return dict(old)
            with (patch("src.tw_price_refresh.cache_get", return_value={"data": existing}),
                  patch("src.tw_price_refresh.cache_set"),
                  patch("src.tw_price_refresh._fetch_exchange_rows", return_value=duplicates),
                  patch("src.tw_price_refresh._income_for_price_date",
                        return_value=({}, False, False)),
                  patch("src.tw_price_refresh._updated_record", side_effect=updated)):
                result = refresh_tw_prices(
                    root, {}, chain_cfg, loader=object(), refresh_supporting=False,
                    verbose=False)
            self.assertFalse(result["updated"])


if __name__ == "__main__":
    unittest.main()
