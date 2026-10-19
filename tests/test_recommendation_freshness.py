import unittest
from datetime import datetime
from a_stock_agents.agents.recommendation import validate_recommendation_data
from a_stock_agents.errors import DataUnavailableError


class FreshnessTests(unittest.TestCase):
    def test_old_and_early_quotes_rejected(self):
        now = datetime.fromisoformat('2026-09-29T14:56:00+08:00')
        for stamp in ['20260928161454', '20260929120000', '', '20260930150000']:
            with self.assertRaises(DataUnavailableError):
                validate_recommendation_data({'generated_at': now.isoformat(), 'features': [{'quote_time': stamp}]}, now, '14:30')

    def test_valid_quote(self):
        now = datetime.fromisoformat('2026-09-29T14:56:00+08:00')
        validate_recommendation_data({'generated_at': now.isoformat(), 'features': [{'quote_time': '20260929144253'}]}, now, '14:30')

    def test_early_run(self):
        with self.assertRaises(DataUnavailableError):
            validate_recommendation_data({}, datetime.fromisoformat('2026-09-29T03:20:00+08:00'), '14:30')

    def test_zero_turnover_early_quotes_do_not_block_eligible_universe(self):
        now=datetime.fromisoformat('2026-10-19T14:45:00+08:00')
        payload={'generated_at':now.isoformat(),'features':[
            {'code':'601059','quote_time':'20261019090000','amount':0,'change_pct':0},
            {'code':'000823','quote_time':'20261019143900','amount':2e8,'change_pct':3}]}
        audit=validate_recommendation_data(payload,now,'14:30',{'min_turnover_amount':1e8,'max_daily_change_pct':9.8})
        self.assertEqual(audit['checked_rows'],1)
        self.assertEqual(audit['excluded_codes'],['601059'])
        self.assertEqual(len(payload['features']),2)

    def test_active_candidate_with_stale_quote_still_stops_recommendation(self):
        now=datetime.fromisoformat('2026-10-19T14:45:00+08:00')
        with self.assertRaises(DataUnavailableError):
            validate_recommendation_data({'generated_at':now.isoformat(),'features':[
                {'code':'000823','quote_time':'20261019090000','amount':2e8,'change_pct':3}]},now,'14:30',
                {'min_turnover_amount':1e8,'max_daily_change_pct':9.8})
