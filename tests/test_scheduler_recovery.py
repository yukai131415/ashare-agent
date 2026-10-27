from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from a_stock_agents.config import Settings, load_settings
from a_stock_agents.scheduler import DailyPipelineScheduler, SHANGHAI, process_lock
from a_stock_agents.storage import atomic_write_json, read_json


class SchedulerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        sync_patch=patch('a_stock_agents.dashboard_enrichment.start_sync')
        self.sync=sync_patch.start()
        self.addCleanup(sync_patch.stop)
        self.settings=Settings(Path(self.folder.name),load_settings().values)
        self.config={'enabled':True,'hour':14,'minute':30}
        self.scheduler=DailyPipelineScheduler(self.settings,self.config)
        self.now=datetime(2026,10,22,14,40,tzinfo=SHANGHAI)
        self.good={'ok':True,'degraded':False,'agents':{'recommendation':{'ok':True,'records':1}}}

    def test_before_cutoff_and_weekend_never_run(self):
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor:
            self.scheduler.run_once('test',self.now.replace(hour=14,minute=29))
            self.scheduler.run_once('test',self.now.replace(day=24))
            supervisor.assert_not_called()

    def test_late_start_catches_up_and_restart_does_not_repeat(self):
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor:
            supervisor.return_value.run_pipeline.return_value=self.good
            self.assertTrue(self.scheduler.run_once('test',self.now))
            self.assertFalse(DailyPipelineScheduler(self.settings,self.config).run_once('test',self.now))
            self.assertEqual(supervisor.return_value.run_pipeline.call_count,1)

    def test_failure_retries_after_delay(self):
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor,patch('a_stock_agents.scheduler.shanghai_now',return_value=self.now):
            supervisor.return_value.run_pipeline.side_effect=[RuntimeError('offline'),self.good]
            self.assertFalse(self.scheduler.run_once('test',self.now))
            self.assertFalse(self.scheduler.run_once('test',self.now+timedelta(seconds=30)))
            self.assertTrue(self.scheduler.run_once('test',self.now+timedelta(seconds=121)))
            self.assertEqual(supervisor.return_value.run_pipeline.call_count,2)

    def test_health_and_existing_recommendation_prevent_reselection(self):
        atomic_write_json(self.settings.runtime/'state/health.json',{'ok':True,'degraded':False,'finished_at':self.now.isoformat()})
        atomic_write_json(self.settings.runtime/'agents/recommendation/data/latest.json',{'recommendation_date':'2026-10-22','created_at':self.now.isoformat()})
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor:
            self.assertFalse(self.scheduler.run_once('test',self.now))
            supervisor.assert_not_called()

    def test_process_lock_blocks_second_instance(self):
        with process_lock(self.scheduler.path.with_suffix('.lock')) as first:
            self.assertTrue(first)
            with patch('a_stock_agents.scheduler.Supervisor') as supervisor:
                self.assertFalse(self.scheduler.run_once('test',self.now))
                supervisor.assert_not_called()

    def test_missing_recommendation_does_not_count_as_success(self):
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor,patch('a_stock_agents.scheduler.shanghai_now',return_value=self.now):
            supervisor.return_value.run_pipeline.return_value={'ok':True,'agents':{}}
            self.assertFalse(self.scheduler.run_once('test',self.now))
            self.assertEqual(read_json(self.scheduler.path,{})['status'],'retry_wait')

    def test_utc_clock_is_converted_to_shanghai(self):
        from datetime import timezone
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor:
            supervisor.return_value.run_pipeline.return_value=self.good
            self.assertTrue(self.scheduler.run_once('test',self.now.astimezone(timezone.utc)))

    def test_next_day_runs_after_previous_day_completed(self):
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor:
            supervisor.return_value.run_pipeline.return_value=self.good
            self.assertTrue(self.scheduler.run_once('test',self.now))
            self.assertTrue(self.scheduler.run_once('test',self.now+timedelta(days=1)))
            self.assertEqual(supervisor.return_value.run_pipeline.call_count,2)

    def test_degraded_report_is_retried(self):
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor,patch('a_stock_agents.scheduler.shanghai_now',return_value=self.now):
            supervisor.return_value.run_pipeline.return_value={**self.good,'degraded':True}
            self.assertFalse(self.scheduler.run_once('test',self.now))
            self.assertNotIn('completed_date',read_json(self.scheduler.path,{}))
