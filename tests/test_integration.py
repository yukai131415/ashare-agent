import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from a_stock_agents.agents.analysis import AnalysisAgent
from a_stock_agents.agents.backtest import BacktestAgent
from a_stock_agents.agents.recommendation import RecommendationAgent
from a_stock_agents.config import Settings, load_settings
from a_stock_agents.models import SCORING_VERSION
from a_stock_agents.trading_calendar import is_trading_weekday
from a_stock_agents.recommendation_store import recommendation_store
from a_stock_agents.storage import atomic_write_json, ensure_runtime, write_csv


class OfflinePipelineTests(unittest.TestCase):
    def setUp(self):
        # Synthetic ranking fixtures have no feed timestamps; freshness is tested separately.
        validator = patch('a_stock_agents.agents.recommendation.validate_recommendation_data')
        validator.start()
        self.addCleanup(validator.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.weekday_patcher = patch(
            "a_stock_agents.agents.recommendation.is_trading_weekday",
            return_value=True,
        )
        self.weekday_patcher.start()
        values = load_settings().values
        self.settings = Settings(Path(self.temp.name), values)
        ensure_runtime(self.settings.runtime)
        self.market_fields = [
            "code", "name", "price", "change_pct", "change", "change_speed_pct",
            "turnover_pct", "volume_ratio", "amplitude_pct", "amount",
            "circulating_shares", "circulating_market_cap", "pe_ratio",
        ]
        self.market_rows = [
            {"code": "600001", "name": "甲公司", "price": 10, "change_pct": 2, "change": 0.2, "change_speed_pct": 0.1, "turnover_pct": 3, "volume_ratio": 2, "amplitude_pct": 4, "amount": 500000000, "circulating_shares": 1000000000, "circulating_market_cap": 10000000000, "pe_ratio": 12},
            {"code": "600002", "name": "乙公司", "price": 20, "change_pct": -1, "change": -0.2, "change_speed_pct": -0.1, "turnover_pct": 2, "volume_ratio": 1, "amplitude_pct": 3, "amount": 400000000, "circulating_shares": 1000000000, "circulating_market_cap": 20000000000, "pe_ratio": 25},
        ]
        write_csv(self.settings.runtime / "agents" / "market" / "data" / "latest.csv", self.market_rows, self.market_fields)
        atomic_write_json(
            self.settings.runtime / "agents" / "news" / "data" / "latest.json",
            {"items": [{"title": "甲公司业绩预增", "summary": "回购利好", "url": "https://example.test"}]},
        )

    def tearDown(self):
        self.weekday_patcher.stop()
        self.temp.cleanup()

    def test_weekend_recommendation_is_skipped(self):
        with patch("a_stock_agents.agents.recommendation.is_trading_weekday", return_value=False):
            result = RecommendationAgent(self.settings).run()
        self.assertTrue(result.ok)
        self.assertEqual(result.records, 0)
        self.assertTrue(result.metadata["skipped"])
        self.assertEqual(recommendation_store(self.settings.runtime).history(), [])

    def test_star_board_cannot_win_from_existing_features(self):
        atomic_write_json(
            self.settings.runtime / 'agents/analysis/data/latest.json',
            {'features': [
                {'code': '688837', 'name': '科创测试', 'price': 10, 'amount': 500000000, 'change_pct': 2, 'momentum_z': 100},
                {'code': '689009', 'name': '科创存托', 'price': 10, 'amount': 500000000, 'change_pct': 2, 'momentum_z': 90},
                {'code': '600001', 'name': '主板测试', 'price': 10, 'amount': 500000000, 'change_pct': 2, 'momentum_z': 1},
            ]},
        )
        result = RecommendationAgent(self.settings).run()
        self.assertEqual(result.metadata['recommendation']['code'], '600001')
        self.assertEqual(result.metadata['candidate_count'], 1)

    def write_close_snapshot(self, day: date) -> None:
        payload = []
        for row in self.market_rows:
            fields = ['0'] * 46
            fields[1:5] = [row['name'], row['code'], str(row['price']), str(row['price'])]
            fields[30] = day.strftime('%Y%m%d') + '150100'
            fields[33] = str(row['price'])
            payload.append('v_sh' + row['code'] + '="' + '~'.join(fields) + '";')
        raw = self.settings.runtime / 'agents' / 'market' / 'raw' / day.strftime('%Y%m%d_150100')
        raw.mkdir(parents=True, exist_ok=True)
        atomic_write_json(raw / 'batch_001.json', {'payload': '\n'.join(payload)})
        write_csv(
            self.settings.runtime / "agents" / "market" / "data" / f"market_{day.strftime('%Y%m%d')}_150100.csv",
            self.market_rows,
            self.market_fields,
        )

    def test_analysis_recommendation_and_single_negative_backtest_does_not_rotate(self):
        self.assertTrue(AnalysisAgent(self.settings).run().ok)
        recommendation_result = RecommendationAgent(self.settings).run()
        self.assertTrue(recommendation_result.ok)
        recommendation = recommendation_result.metadata["recommendation"]
        # 构造一条已到期且价格下跌的历史推荐；单次亏损不应切换模型。
        recommendation["recommendation_id"] = f"expired-{recommendation['recommendation_id']}"
        rec_day = date.today() - timedelta(days=2)
        while rec_day.weekday() >= 5:
            rec_day -= timedelta(days=1)
        recommendation["created_at"] = datetime.combine(rec_day, datetime.min.time()).astimezone().isoformat()
        target = date.today() - timedelta(days=1)
        recommendation["target_date"] = target.isoformat()
        recommendation["reference_price"] = float(recommendation["reference_price"]) * 1.1
        self.write_close_snapshot(target)
        self.assertTrue(recommendation_store(self.settings.runtime).insert_history(recommendation))
        result = BacktestAgent(self.settings).run()
        self.assertEqual(result.records, 1)
        self.assertEqual(result.metadata["negative_streak"], 1)
        self.assertEqual(len(result.metadata["model_rotations"]), 0)

    def test_same_day_recommendation_is_locked_after_first_pick(self):
        self.assertTrue(AnalysisAgent(self.settings).run().ok)
        first = RecommendationAgent(self.settings).run().metadata["recommendation"]
        first_price = first["reference_price"]
        self.market_rows[0]["amount"] = 100000000
        self.market_rows[0]["price"] = 10.5
        self.market_rows[1]["amount"] = 900000000
        write_csv(self.settings.runtime / "agents" / "market" / "data" / "latest.csv", self.market_rows, self.market_fields)
        self.assertTrue(AnalysisAgent(self.settings).run().ok)
        second = RecommendationAgent(self.settings).run()
        self.assertTrue(second.metadata["locked_daily_recommendation"])
        self.assertEqual(second.metadata["recommendation"]["recommendation_id"], first["recommendation_id"])
        self.assertEqual(second.metadata["recommendation"]["code"], first["code"])
        self.assertNotEqual(second.metadata["recommendation"]["reference_price"], first_price)
        self.assertEqual(second.metadata["recommendation"]["model_version"], SCORING_VERSION)
        self.assertFalse(second.metadata["recommendation"]["basis"]["news_factor_enabled"])

    def test_recommendation_ranks_market_factors_even_with_opposing_news(self):
        atomic_write_json(
            self.settings.runtime / "agents" / "analysis" / "data" / "latest.json",
            {"features": [
                {"code": "600001", "name": "甲公司", "price": 10,
                 "amount": 500000000, "change_pct": 2, "momentum_z": 1.0,
                 "news_sentiment": -1.0, "news_count": 1},
                {"code": "600002", "name": "乙公司", "price": 20,
                 "amount": 400000000, "change_pct": 1, "momentum_z": 0.8,
                 "news_sentiment": 1.0, "news_count": 1},
            ]},
        )
        result = RecommendationAgent(self.settings).run()
        self.assertTrue(result.ok)
        saved = recommendation_store(self.settings.runtime).realtime()
        self.assertEqual(saved["code"], "600001")
        self.assertEqual(saved["score"], 0.28)
        self.assertEqual(saved["model_version"], SCORING_VERSION)
        self.assertFalse(saved["basis"]["news_factor_enabled"])
        self.assertEqual(saved["basis"]["news_sentiment"], -1.0)

    def test_five_consecutive_negative_backtests_rotate_once(self):
        store = recommendation_store(self.settings.runtime)
        today = date.today()
        days = []
        candidate = today - timedelta(days=1)
        while len(days) < 5:
            if is_trading_weekday(candidate):
                days.append(candidate)
            candidate -= timedelta(days=1)
        for index, day in enumerate(reversed(days)):
            self.write_close_snapshot(day)
            self.assertTrue(
                store.insert_history(
                    {
                        "recommendation_id": f"loss-{index}",
                        "created_at": datetime.combine(day, datetime.min.time()).astimezone().isoformat(),
                        "target_date": day.isoformat(),
                        "code": "600001",
                        "name": "甲公司",
                        "reference_price": 11.0,
                        "model": "balanced",
                        "model_version": "1.0",
                        "score": 1.0,
                        "basis": {},
                        "disclaimer": "test",
                    }
                )
            )
        result = BacktestAgent(self.settings).run()
        self.assertEqual(result.metadata["negative_streak"], 5)
        self.assertEqual(len(result.metadata["model_rotations"]), 1)


if __name__ == "__main__":
    unittest.main()
