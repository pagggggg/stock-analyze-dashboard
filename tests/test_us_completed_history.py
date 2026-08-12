import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.us_data import _completed_history


MARKET_TZ = ZoneInfo("America/New_York")


class USCompletedHistoryTests(unittest.TestCase):
    def test_drops_current_bar_before_close_and_keeps_it_after_close(self):
        index = pd.DatetimeIndex([
            "2026-08-07 00:00", "2026-08-10 00:00"
        ], tz=MARKET_TZ)
        frame = pd.DataFrame({"Close": [100.0, 105.0]}, index=index)

        before = _completed_history(
            frame, datetime(2026, 8, 10, 15, 0, tzinfo=MARKET_TZ))
        after = _completed_history(
            frame, datetime(2026, 8, 10, 17, 0, tzinfo=MARKET_TZ))

        self.assertEqual(len(before), 1)
        self.assertEqual(before.index[-1].date().isoformat(), "2026-08-07")
        self.assertEqual(len(after), 2)
        self.assertEqual(after.index[-1].date().isoformat(), "2026-08-10")


if __name__ == "__main__":
    unittest.main()
