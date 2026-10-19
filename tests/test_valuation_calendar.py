import unittest
from datetime import date
from a_stock_agents.models import BalancedModel, valuation_factor
from a_stock_agents.trading_calendar import next_weekday, is_trading_weekday


class ValuationCalendarTests(unittest.TestCase):
    def test_extreme_pe_neutral(self):
        self.assertEqual(BalancedModel().score({'pe_ratio': '0.36', 'valuation_z': 26}), 0)
        self.assertEqual(valuation_factor({'pe_ratio': -10, 'valuation_z': 5}), 0)

    def test_cap(self):
        self.assertAlmostEqual(BalancedModel().score({'pe_ratio': 10, 'valuation_z': 26}), .15)
        self.assertAlmostEqual(BalancedModel().score({'pe_ratio': 10, 'valuation_z': -26}), -.15)

    def test_holidays(self):
        self.assertEqual(next_weekday(date(2026, 9, 30)), date(2026, 10, 8))
        self.assertEqual(next_weekday(date(2026, 9, 24)), date(2026, 9, 28))
        self.assertFalse(is_trading_weekday(date(2026, 10, 10)))
        self.assertFalse(is_trading_weekday(date(2026, 9, 25)))
        with self.assertRaises(ValueError):
            is_trading_weekday(date(2027, 1, 4))
