import unittest

from a_stock_agents.web import monthly_statistics, page_html
from a_stock_agents.config import load_settings


class MonthlyStatisticsTests(unittest.TestCase):
    def test_groups_signed_returns_and_excludes_pending(self):
        rows = [
            {"date": "2026-10-19", "return_pct": 3.5},
            {"date": "2026-10-16", "return_pct": -2},
            {"date": "2026-10-15", "return_pct": 0},
            {"date": "2026-10-14", "return_pct": None},
            {"date": "2026-09-30", "return_pct": -1},
        ]
        result = monthly_statistics(rows)
        self.assertEqual([r['month'] for r in result], ['2026-10', '2026-09'])
        october = result[0]
        self.assertEqual((october['total'], october['evaluated'], october['pending']), (4, 3, 1))
        self.assertEqual((october['wins'], october['losses'], october['flat']), (1, 1, 1))
        self.assertEqual(october['return_sum_pct'], 1.5)
        self.assertEqual(october['average_return_pct'], .5)
        self.assertEqual(october['win_rate'], 33.33)

    def test_empty_invalid_and_pending_are_not_zero_returns(self):
        self.assertEqual(monthly_statistics([]), [])
        result = monthly_statistics([{'date': '2026-10-19', 'return_pct': v} for v in [None, 'NaN', 'Infinity', 'bad']])[0]
        self.assertEqual(result['pending'], 4)
        self.assertIsNone(result['return_sum_pct'])
        self.assertIsNone(result['win_rate'])

    def test_page_contains_pagination_for_both_tables(self):
        html = page_html(load_settings())
        for text in ['月度盈亏统计', 'monthly-pages', 'history-pages', 'const pageSize = 10', '首页', '最后一页', '下一页', '上一页']:
            self.assertIn(text, html)


if __name__ == '__main__':
    unittest.main()
