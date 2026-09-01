import unittest
from datetime import datetime, timezone

from scripts.deployment_health import (deployed_market_dates, evaluate_health,
                                       ready_session_lag)


class DeploymentHealthTests(unittest.TestCase):
    def test_parses_deployed_market_dates(self):
        html = ("台股行情更新 2026-08-31 23:05（收盤日 2026-08-31）<br>"
                "美股行情更新 2026-08-29 11:45（收盤日 2026-08-28）")
        self.assertEqual(
            deployed_market_dates(html),
            {"tw": "2026-08-31", "us": "2026-08-28"})

    def test_uses_latest_date_from_mixed_taiwan_range(self):
        html = ("台股行情更新 2026-09-01 23:05（收盤日 2026-08-28–2026-09-01）<br>"
                "美股行情更新 2026-09-01 06:30（收盤日 2026-08-31）")
        self.assertEqual(
            deployed_market_dates(html),
            {"tw": "2026-09-01", "us": "2026-08-31"})

    def test_weekends_and_holidays_do_not_count_as_stale_sessions(self):
        saturday = datetime(2026, 9, 5, 18, tzinfo=timezone.utc)
        labor_day = datetime(2026, 9, 7, 22, tzinfo=timezone.utc)

        self.assertEqual(
            ready_session_lag("us", "2026-09-04", saturday)["sessions"], 0)
        self.assertEqual(
            ready_session_lag("us", "2026-09-04", labor_day)["sessions"], 0)

    def test_stale_alert_waits_for_more_than_one_session(self):
        monday_after_close = datetime(2026, 8, 31, 21, tzinfo=timezone.utc)
        tuesday_after_close = datetime(2026, 9, 1, 21, tzinfo=timezone.utc)

        self.assertEqual(
            ready_session_lag("us", "2026-08-28", monday_after_close)["sessions"], 1)
        self.assertEqual(
            ready_session_lag("us", "2026-08-28", tuesday_after_close)["sessions"], 2)

    def test_quality_alert_opens_once_on_second_skip_and_resolves(self):
        now = datetime(2026, 9, 1, 3, tzinfo=timezone.utc)
        deployed = {"tw": "2026-08-31", "us": "2026-08-31"}

        first, new, _ = evaluate_health(
            {}, False, deployed, now, run_id="run-1")
        second, second_new, _ = evaluate_health(
            first, False, deployed, now, run_id="run-2")
        third, third_new, _ = evaluate_health(
            second, False, deployed, now, run_id="run-3")
        recovered, _, resolved = evaluate_health(
            third, True, deployed, now, run_id="run-4")

        self.assertEqual(new, set())
        self.assertEqual(second_new, {"quality-gate"})
        self.assertEqual(third_new, set())
        self.assertEqual(resolved, {"quality-gate"})
        self.assertEqual(recovered["quality_skip_streak"], 0)

    def test_rerun_does_not_increment_skip_streak(self):
        now = datetime(2026, 9, 1, 3, tzinfo=timezone.utc)
        deployed = {"tw": "2026-08-31", "us": "2026-08-31"}

        first, _, _ = evaluate_health({}, False, deployed, now, run_id="123")
        rerun, new, _ = evaluate_health(first, False, deployed, now, run_id="123")

        self.assertEqual(first["quality_skip_streak"], 1)
        self.assertEqual(rerun["quality_skip_streak"], 1)
        self.assertEqual(new, set())


if __name__ == "__main__":
    unittest.main()
