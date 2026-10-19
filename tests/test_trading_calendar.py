import unittest
from datetime import date

from a_stock_agents.trading_calendar import is_trading_weekday, next_weekday


class TradingCalendarTests(unittest.TestCase):
    def test_weekends_are_closed(self):
        self.assertTrue(is_trading_weekday(date(2026, 9, 4)))
        self.assertFalse(is_trading_weekday(date(2026, 9, 5)))
        self.assertFalse(is_trading_weekday(date(2026, 9, 6)))

    def test_friday_targets_monday(self):
        self.assertEqual(next_weekday(date(2026, 9, 4)), date(2026, 9, 7))


if __name__ == "__main__":
    unittest.main()
