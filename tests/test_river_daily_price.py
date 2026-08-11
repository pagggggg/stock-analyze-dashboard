import unittest
from unittest.mock import patch

from src.dashboard_html import _fig_river
from src.river import RiverSeries, _daily_price_line


class RiverDailyPriceTests(unittest.TestCase):
    def test_daily_price_line_keeps_each_trading_day_and_latest_override(self):
        rows = [
            {"date": "2026-08-03", "close": 100.0},
            {"date": "2026-08-04", "close": 101.0},
            {"date": "2026-08-05", "close": 102.0},
        ]

        dates, prices = _daily_price_line(
            rows, "2026-08-03", "2026-08-06",
            current_date="2026-08-06", current_price=103.0, decimals=1)

        self.assertEqual(dates, ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"])
        self.assertEqual(prices, [100.0, 101.0, 102.0, 103.0])

    def test_chart_uses_daily_dates_for_black_line(self):
        river = RiverSeries(
            dates=["2026-07-31", "2026-08-31"],
            price_dates=["2026-08-03", "2026-08-04", "2026-08-05"],
            price=[100.0, 101.0, 102.0],
            band_low=[80.0, 82.0], band_mid=[100.0, 102.0], band_high=[120.0, 122.0],
            pe_low=10.0, pe_mid=15.0, pe_high=20.0,
            current_date="2026-08-05", current_price=102.0, current_pe=15.0,
        )

        with patch("src.dashboard_html._fig_div", side_effect=lambda fig: fig):
            figure = _fig_river(river)

        self.assertEqual(figure.data[3].name, "日收盤價")
        self.assertEqual(list(figure.data[3].x), river.price_dates)
        self.assertEqual(list(figure.data[3].y), river.price)


if __name__ == "__main__":
    unittest.main()
