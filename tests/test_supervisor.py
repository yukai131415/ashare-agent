import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from a_stock_agents.config import Settings, load_settings
from a_stock_agents.errors import SchemaChangedError, SourceBlockedError
from a_stock_agents.storage import ensure_runtime, write_csv
from a_stock_agents.supervisor import Supervisor, diagnose


class SupervisorTests(unittest.TestCase):
    def test_weekend_pipeline_is_skipped_before_collectors_run(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder), load_settings().values)
            with patch("a_stock_agents.supervisor.is_trading_weekday", return_value=False):
                report = Supervisor(settings).run_pipeline()
            self.assertTrue(report["ok"])
            self.assertTrue(report["skipped"])
            self.assertEqual(report["agents"], {})

    def test_failure_diagnosis(self):
        diagnosis, repair = diagnose(SourceBlockedError("403"))
        self.assertIn("反爬", diagnosis)
        self.assertIn("停止推荐", repair)
        diagnosis, repair = diagnose(SchemaChangedError("missing field"))
        self.assertIn("结构", diagnosis)
        self.assertIn("原始响应", repair)

    def test_failed_agent_does_not_use_cached_snapshot_when_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            values = load_settings().values
            values = {
                **values,
                "supervisor": {
                    **values["supervisor"],
                    "max_retries": 0,
                    "allow_cached_data": False,
                    "min_market_rows": 1,
                },
            }
            settings = Settings(Path(folder), values)
            ensure_runtime(settings.runtime)
            write_csv(
                settings.runtime / "agents" / "market" / "data" / "latest.csv",
                [{"code": "600000", "name": "缓存", "price": 1}],
                ["code", "name", "price"],
            )
            supervisor = Supervisor(settings)

            class BrokenMarket:
                def __init__(self, settings):
                    self.settings = settings

                def run(self):
                    raise SourceBlockedError("network blocked")

            supervisor.agent_types["market"] = BrokenMarket
            result = supervisor.run_agent("market")
            self.assertFalse(result.ok)
            self.assertIsNone(result.repair_action)
            self.assertFalse(result.metadata.get("degraded", False))


if __name__ == "__main__":
    unittest.main()
