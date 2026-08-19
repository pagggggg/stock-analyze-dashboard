import unittest
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from build_universe import _validate_universe_update
from fetch_universe import _save
from src.data_layer import fetch_pe_history_twse
from src.data_layer import _twse_fetch_month
from src.scan_state import revision_momentum
from src.screener import (c4_debt_ratio, derive_trailing_price_levels,
                          q10_momentum)
from src.river import _filing_available_date, current_trailing_pe
from src.universe_builder import (_fetch_mops_year, evaluate as evaluate_universe,
                                  fetch_meeting_ids_tw)
from src.valuation_flag import pe_history_is_compatible, pe_history_stats


class UniverseSafetyTests(unittest.TestCase):
    def test_mops_parser_rejects_unexpected_html(self):
        response = Mock(text="<html><body>maintenance</body></html>")
        response.raise_for_status.return_value = None
        with patch("requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "頁面格式不符"):
                _fetch_mops_year(115)

    def test_mops_year_failure_does_not_cache_partial_result(self):
        valid = {str(1000 + i): "2026-06-01" for i in range(60)}
        with (patch("src.universe_builder.cache_get", return_value=None),
              patch("src.universe_builder.cache_set") as cache_set,
              patch("src.universe_builder._fetch_mops_year",
                    side_effect=[valid, RuntimeError("offline")])):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                fetch_meeting_ids_tw()
        cache_set.assert_not_called()

    def test_incomplete_mops_cache_is_rejected(self):
        with patch("src.universe_builder.cache_get",
                   return_value={"data": {"1101": "2026-06-01"}}):
            with patch("src.universe_builder._fetch_mops_year",
                       side_effect=RuntimeError("refetch expected")):
                with self.assertRaisesRegex(RuntimeError, "refetch expected"):
                    fetch_meeting_ids_tw()

    def test_one_year_mops_cache_is_not_accepted(self):
        cached = {
            "data": {str(1000 + i): "2026-06-01" for i in range(100)},
            "years": [115, 114],
            "year_counts": {"115": 100, "114": 0},
        }
        with (patch("src.universe_builder.date") as mocked_date,
              patch("src.universe_builder.cache_get", return_value=cached),
              patch("src.universe_builder._fetch_mops_year",
                    side_effect=RuntimeError("refetch expected"))):
            mocked_date.today.return_value = __import__("datetime").date(2026, 8, 18)
            with self.assertRaisesRegex(RuntimeError, "refetch expected"):
                fetch_meeting_ids_tw()

    def test_taiwan_universe_abnormal_shrink_is_rejected(self):
        results = [SimpleNamespace(ok=True) for _ in range(100)]
        passed = [SimpleNamespace() for _ in range(60)]
        old_doc = {"twse": [{"stock_id": str(i)} for i in range(100)]}

        with self.assertRaisesRegex(RuntimeError, "異常縮減"):
            _validate_universe_update("twse", results, passed, old_doc)

    def test_taiwan_universe_incomplete_snapshots_are_rejected(self):
        results = [SimpleNamespace(ok=i < 89, stock_id=str(i)) for i in range(100)]
        passed = [SimpleNamespace() for _ in range(89)]

        with self.assertRaisesRegex(RuntimeError, "必要快照抓取不完整"):
            _validate_universe_update("twse", results, passed, {})

    def test_new_listing_with_short_history_is_insufficient_not_fetch_failure(self):
        cfg = {"universe_builder": {
            "liquidity_days": 60,
            "coverage_gates": False,
            "tw": {"min_market_cap": 1, "min_analyst_coverage": 3,
                   "require_meeting": False, "min_avg_value": 1},
        }}
        snap = {"market_cap": 100, "liq_avg": 10, "liq_days": 20,
                "listed_date": (date.today() - timedelta(days=30)).isoformat()}

        result = evaluate_universe(
            {"stock_id": "9999", "name": "新股", "market": "twse"},
            snap, set(), cfg)

        self.assertTrue(result.ok)
        self.assertEqual(result.conds["u4"].status, "na")
        self.assertFalse(result.passed)

    def test_new_listing_falls_back_to_recent_price_start(self):
        cfg = {"universe_builder": {
            "liquidity_days": 60,
            "coverage_gates": False,
            "tw": {"min_market_cap": 1, "min_analyst_coverage": 3,
                   "require_meeting": False, "min_avg_value": 1},
        }}
        snap = {"market_cap": 100, "liq_avg": 10, "liq_days": 20,
                "price_start": (date.today() - timedelta(days=30)).isoformat()}

        result = evaluate_universe(
            {"stock_id": "9999", "name": "新股", "market": "twse"},
            snap, set(), cfg)

        self.assertTrue(result.ok)
        self.assertEqual(result.conds["u4"].status, "na")

    def test_established_stock_with_truncated_history_is_fetch_failure(self):
        cfg = {"universe_builder": {
            "liquidity_days": 60,
            "coverage_gates": False,
            "tw": {"min_market_cap": 1, "min_analyst_coverage": 3,
                   "require_meeting": False, "min_avg_value": 1},
        }}
        snap = {"market_cap": 100, "liq_avg": 10, "liq_days": 20,
                "listed_date": "2020-01-01"}

        result = evaluate_universe(
            {"stock_id": "9999", "name": "舊股", "market": "twse"},
            snap, set(), cfg)

        self.assertFalse(result.ok)
        self.assertEqual(result.conds["u4"].status, "na")


class DataSafetyTests(unittest.TestCase):
    def test_full_fetch_preserves_official_price_verification_metadata(self):
        import fetch_universe

        with tempfile.TemporaryDirectory() as directory:
            old_dir = fetch_universe.UNIVERSE_DIR
            fetch_universe.UNIVERSE_DIR = Path(directory)
            path = fetch_universe.UNIVERSE_DIR / "2330.json"
            path.write_text(json.dumps({
                "stock_id": "2330", "market": "twse",
                "price_last": 100.0, "price_date": "2026-08-18",
                "price_checked_through": "2026-08-18",
                "price_updated_at": "2026-08-18T07:00:00+00:00",
            }), encoding="utf-8")
            try:
                _save({
                    "stock_id": "2330", "market": "twse", "errors": [],
                    "price_last": 999.0, "price_date": "2026-08-19",
                })
                saved = json.loads(path.read_text())
            finally:
                fetch_universe.UNIVERSE_DIR = old_dir

        self.assertEqual(saved["price_checked_through"], "2026-08-18")
        self.assertEqual(saved["price_updated_at"], "2026-08-18T07:00:00+00:00")

    def test_financial_q2_deadline_keeps_q1_ttm_valid_through_august(self):
        income = {
            "2025-06-30": {"EPS": 1.0},
            "2025-09-30": {"EPS": 1.0},
            "2025-12-31": {"EPS": 1.0},
            "2026-03-31": {"EPS": 1.0},
        }
        prices = [{"date": "2026-08-18", "close": 40.0}]

        regular, _ = current_trailing_pe(
            prices, income, current_date="2026-08-18")
        financial, _ = current_trailing_pe(
            prices, income, current_date="2026-08-18", financial_company=True)

        self.assertIsNone(regular)
        self.assertEqual(financial, 10.0)
        self.assertEqual(
            _filing_available_date(date(2026, 6, 30), financial_company=True),
            date(2026, 9, 1))

    def test_malformed_successful_twse_response_is_rejected(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "stat": "OK", "fields": ["日期", "本益比"], "data": [["2026/01/02"]]
        }
        with patch("requests.get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "格式錯誤"):
                _twse_fetch_month("2330", 2026, 1)

    def test_failed_twse_month_is_not_cached(self):
        with (patch("src.data_layer.cache_get", return_value=None),
              patch("src.data_layer.cache_set") as cache_set,
              patch("src.data_layer._twse_fetch_month",
                    side_effect=RuntimeError("timeout"))):
            with self.assertRaisesRegex(RuntimeError, "月份抓取失敗"):
                fetch_pe_history_twse("2330", years=1, polite_sleep=0)
        cache_set.assert_not_called()

    def test_legacy_empty_month_cache_is_not_trusted(self):
        with (patch("src.data_layer.cache_get", return_value={"data": []}),
              patch("src.data_layer.cache_set") as cache_set,
              patch("src.data_layer._twse_fetch_month",
                    side_effect=RuntimeError("retry expected")) as fetch):
            with self.assertRaisesRegex(RuntimeError, "retry expected"):
                fetch_pe_history_twse("2330", years=1, polite_sleep=0)
        fetch.assert_called_once()
        cache_set.assert_not_called()

    def test_missing_debt_fields_are_insufficient(self):
        cfg = {"layer1": {"debt_ratio": {
            "exclude_financial": False,
            "default_max_pct": 40,
        }}}
        rec = {"industry": "半導體業", "market": "twse", "latest_bs": {
            "total_assets": 100.0,
            "liabilities": 80.0,
            "short_borrow": None,
            "long_borrow": None,
            "bonds": None,
        }}

        result = c4_debt_ratio(rec, cfg, "2404")

        self.assertEqual(result.status, "na")
        self.assertIn("欄位全缺", result.detail)

    def test_explicit_zero_debt_can_pass(self):
        cfg = {"layer1": {"debt_ratio": {
            "exclude_financial": False,
            "default_max_pct": 40,
        }}}
        rec = {"industry": "半導體業", "market": "twse", "latest_bs": {
            "total_assets": 100.0,
            "liabilities": 80.0,
            "short_borrow": 0.0,
            "long_borrow": 0.0,
            "bonds": 0.0,
        }}

        result = c4_debt_ratio(rec, cfg, "2404")

        self.assertEqual(result.status, "pass")
        self.assertIn("0.0%", result.detail)

    def test_latest_close_remains_visible_when_pe_is_missing(self):
        result = derive_trailing_price_levels(
            2395, "2026-08-11", None, None, None)

        self.assertEqual(result["close_price"], 2395.0)
        self.assertEqual(result["close_date"], "2026-08-11")
        self.assertIsNone(result["price_p50"])

    def test_analysis_uses_committed_close_when_price_cache_lags(self):
        from src import analysis

        income = {
            "2024-06-30": {"EPS": 1.0}, "2024-09-30": {"EPS": 1.0},
            "2024-12-31": {"EPS": 1.0}, "2025-03-31": {"EPS": 1.0},
        }
        cached_prices = [
            {"date": "2026-08-17", "close": 90.0},
        ]
        with tempfile.TemporaryDirectory() as directory:
            old_root = analysis.ROOT
            analysis.ROOT = Path(directory)
            (analysis.ROOT / "data/universe").mkdir(parents=True)
            (analysis.ROOT / "data/universe/9999.json").write_text(json.dumps({
                "stock_id": "9999", "industry": "半導體業",
                "price_date": "2026-08-18", "price_last": 100.0,
            }), encoding="utf-8")
            try:
                with (patch("src.analysis.fetch_income_pivot", return_value=(income, "2026-08-19")),
                      patch("src.analysis.quarters_from_income_pivot", return_value=[]),
                      patch("src.analysis.fetch_price_daily_finmind",
                            return_value=(cached_prices, "2026-08-19")),
                      patch("src.analysis.compute_pe_band_finmind", return_value=None),
                      patch("src.analysis.fetch_yfinance_metrics", side_effect=RuntimeError("offline")),
                      patch("src.data_layer.fetch_month_revenue", side_effect=RuntimeError("offline")),
                      patch("src.analysis.fetch_balance_pivot", side_effect=RuntimeError("offline")),
                      patch("src.analysis.fetch_cashflow_pivot", side_effect=RuntimeError("offline")),
                      patch("src.analysis.build_pe_river", return_value=None)):
                    result = analysis.analyze_stock("9999")
            finally:
                analysis.ROOT = old_root

        self.assertEqual(result.price, 100.0)
        self.assertEqual(result.price_date, "2026-08-18")

    def test_missing_current_eps_is_a_valid_insufficient_pe_snapshot(self):
        current = "2026-08-18"
        coverage = {
            "price_start": "2020-01-01", "price_end": current, "price_n": 1000,
            "eps_start": "2020-03-31", "eps_end": "2026-03-31", "eps_n": 20,
            "eps_max_gap_quarters": 1, "eps_pre_window_n": 4,
        }
        snapshot = pe_history_stats(
            [], None, years=5, current_date=current, market="twse",
            insufficient_reason="current_trailing_pe_unavailable",
            source_coverage=coverage)

        self.assertTrue(pe_history_is_compatible(snapshot, "twse", current, 5))

    def test_pe_history_never_reports_ok_without_current_pe(self):
        current = "2026-08-18"
        series = []
        start = date(2020, 1, 1)
        for i in range(1800):
            day = start + timedelta(days=i)
            if day.weekday() < 5 and day <= date.fromisoformat(current):
                series.append((day.isoformat(), 20.0))
        coverage = {
            "price_start": series[0][0], "price_end": current, "price_n": len(series),
            "eps_start": "2020-03-31", "eps_end": "2026-06-30", "eps_n": 24,
            "eps_max_gap_quarters": 1, "eps_pre_window_n": 4,
        }

        snapshot = pe_history_stats(
            series, None, years=5, current_date=current, market="twse",
            source_coverage=coverage)

        self.assertEqual(snapshot["status"], "insufficient")
        self.assertEqual(snapshot["reason"], "current_trailing_pe_unavailable")


class MomentumConfigTests(unittest.TestCase):
    def test_revision_momentum_honors_custom_threshold(self):
        history = [{"eps_y0": 100}, {"eps_y0": 101}]

        self.assertEqual(revision_momentum(history, min_pct=10), ("flat", 0.0))
        self.assertEqual(revision_momentum(history, min_pct=0.5), ("up", 1.0))

    def test_revision_momentum_uses_previous_value_and_handles_zero(self):
        self.assertEqual(
            revision_momentum([{"eps_y0": 100}, {"eps_y0": 110}], min_pct=10),
            ("up", 10.0))
        self.assertEqual(
            revision_momentum([{"eps_y0": 110}, {"eps_y0": 100}], min_pct=10),
            ("flat", 0.0))
        self.assertEqual(
            revision_momentum([{"eps_y0": 0}, {"eps_y0": 1}], min_pct=0.5),
            ("na", None))

    def test_screener_passes_configured_threshold(self):
        cfg = {"layer2": {"momentum": {"source": "history", "min_pct": 10}}}
        history = [{"eps_y0": 100}, {"eps_y0": 101}]

        with patch("src.data_layer.load_consensus_history", return_value=history):
            result = q10_momentum({}, cfg, "2330")

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.detail, "共識持平")


if __name__ == "__main__":
    unittest.main()
