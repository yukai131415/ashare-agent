import json
import tempfile
import unittest
from pathlib import Path
from datetime import date, time

from a_stock_agents.agents.backtest import daily_high_prices, high_return_pct, is_close_based_result, parse_daily_high


class HighReturnTests(unittest.TestCase):
    def test_historical_unadjusted_daily(self):
        payload = {'code': 0, 'data': {'sh600371': {'day': [
            ['2026-09-08', '15.370', '15.830', '16.580', '15.370'],
            ['2026-09-09', '14.610', '15.400', '16.100', '14.610'],
        ]}}}
        self.assertEqual(parse_daily_high(payload, 'sh600371', date(2026, 9, 9)), (16.1, 15.83))
        with self.assertRaises(ValueError):
            parse_daily_high(payload, 'sh600371', date(2026, 9, 10))
        with self.assertRaises(ValueError):
            parse_daily_high(payload, 'sh600371', date(2026, 9, 8))

    def test_formula(self):
        self.assertAlmostEqual(high_return_pct(10.8, 10), 7)
        self.assertAlmostEqual(high_return_pct(10, 10), -1)
        self.assertAlmostEqual(high_return_pct(9.8, 10), -3)
        for value in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                high_return_pct(value, 10)

    def test_old_method_excluded(self):
        self.assertFalse(is_close_based_result({'exit_close_price': 10, 'method': '目标日收盘价（使用 market_x）'}))

    def test_actual_timestamp_required(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'data'
            data.mkdir()
            raw = Path(temp) / 'raw' / '20260911_160000'
            raw.mkdir(parents=True)
            def write(stamp):
                fields = ['0'] * 46
                fields[1:5] = ['测试', '600354', '10.97', '11.45']
                fields[30], fields[32], fields[33] = stamp, '-4.19', '12.32'
                (raw / 'batch_001.json').write_text(json.dumps({'payload': 'v_sh600354="' + '~'.join(fields) + '";'}), encoding='utf-8')
            for stamp in ('20260911145959', '20260910160000'):
                write(stamp)
                self.assertEqual(daily_high_prices(data, date(2026, 9, 11), time(15)), {})
            write('20260911155014')
            row = daily_high_prices(data, date(2026, 9, 11), time(15))['600354']
            self.assertAlmostEqual(high_return_pct(row[0], row[1]), 6.598253275, places=6)
