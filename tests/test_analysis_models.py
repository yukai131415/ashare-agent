import unittest

from a_stock_agents.agents.analysis import build_features, sentiment
from a_stock_agents.models import get_model


class AnalysisModelTests(unittest.TestCase):
    def test_all_models_ignore_news_sentiment_and_count(self):
        features = {
            "momentum_z": 0.8, "volume_ratio_z": 0.5,
            "liquidity_z": 0.6, "turnover_z": 0.4,
            "valuation_z": 0.2, "amplitude_z": 0.3,
            "change_speed_z": 0.7,
        }
        for name in ("balanced", "momentum", "value_reversal"):
            model = get_model(name)
            neutral_score = model.score(features)
            for news_sentiment in (-1.0, 0.0, 1.0):
                with self.subTest(model=name, news_sentiment=news_sentiment):
                    self.assertEqual(
                        model.score({**features, "news_sentiment": news_sentiment, "news_count": 100}),
                        neutral_score,
                    )
        # 禁用新闻不重新归一化其他权重。
        self.assertAlmostEqual(get_model("balanced").score(features), 0.466)

    def test_sentiment_and_features(self):
        self.assertGreater(sentiment("业绩预增并回购，重大利好"), 0)
        market = [
            {"code": "600001", "name": "甲公司", "price": "10", "change_pct": "2", "amount": "200000000", "volume_ratio": "2", "turnover_pct": "3", "amplitude_pct": "4", "change_speed_pct": "0.1", "pe_ratio": "10"},
            {"code": "600002", "name": "乙公司", "price": "11", "change_pct": "-1", "amount": "100000000", "volume_ratio": "1", "turnover_pct": "2", "amplitude_pct": "3", "change_speed_pct": "-0.1", "pe_ratio": "30"},
        ]
        news = [{"title": "公司业绩预增", "summary": "利好", "stock_codes": ["600001"]}]
        features = build_features(market, news)
        self.assertEqual(features[0]["news_count"], 1)
        self.assertGreater(features[0]["momentum_z"], features[1]["momentum_z"])
        self.assertIsInstance(get_model("balanced").score(features[0]), float)


if __name__ == "__main__":
    unittest.main()
