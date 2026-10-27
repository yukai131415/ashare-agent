from datetime import datetime,timezone
import unittest
from a_stock_agents.scheduler import scheduler_display


class SchedulerDisplayTests(unittest.TestCase):
    config={'enabled':True,'hour':14,'minute':30}
    def display(self,stamp,state=None):return scheduler_display(self.config,state or {},datetime.fromisoformat(stamp))

    def test_before_cutoff_targets_today(self):
        r=self.display('2026-10-22T14:00:00+08:00')
        self.assertEqual(r['next_run_at'],'2026-10-22T14:30:00+08:00')
        self.assertEqual(r['countdown_state'],'counting')

    def test_completed_targets_next_trading_day(self):
        r=self.display('2026-10-23T15:00:00+08:00',{'completed_date':'2026-10-23'})
        self.assertEqual(r['next_run_at'],'2026-10-26T14:30:00+08:00')

    def test_weekends_and_holidays_skipped(self):
        self.assertEqual(self.display('2026-10-24T12:00:00+08:00')['next_run_at'],'2026-10-26T14:30:00+08:00')
        self.assertEqual(self.display('2026-10-01T12:00:00+08:00')['next_run_at'],'2026-10-08T14:30:00+08:00')

    def test_running_and_overdue_not_false_tomorrow(self):
        r=self.display('2026-10-22T14:31:00+08:00',{'attempt_date':'2026-10-22','status':'running'})
        self.assertEqual(r['countdown_state'],'running');self.assertIsNone(r['next_run_at'])
        self.assertEqual(self.display('2026-10-22T14:30:00+08:00')['countdown_state'],'awaiting_dispatch')

    def test_retry_uses_actual_retry_timestamp(self):
        r=self.display('2026-10-22T14:31:00+08:00',{'attempt_date':'2026-10-22','status':'retry_wait','next_retry_at':'2026-10-22T14:32:00+08:00'})
        self.assertEqual(r['next_kind'],'retry');self.assertEqual(r['next_run_at'],'2026-10-22T14:32:00+08:00')
        self.assertEqual(self.display('2026-10-22T14:33:00+08:00',{'attempt_date':'2026-10-22','status':'retry_wait','next_retry_at':'2026-10-22T14:32:00+08:00'})['countdown_state'],'awaiting_dispatch')

    def test_disabled_unknown_calendar_and_utc(self):
        self.assertEqual(scheduler_display({**self.config,'enabled':False},{})['countdown_state'],'disabled')
        self.assertEqual(self.display('2027-01-01T12:00:00+08:00')['countdown_state'],'calendar_unavailable')
        r=scheduler_display(self.config,{},datetime(2026,10,22,6,0,tzinfo=timezone.utc))
        self.assertEqual(r['next_run_at'],'2026-10-22T14:30:00+08:00')


if __name__=='__main__':unittest.main()
