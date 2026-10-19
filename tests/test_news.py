import unittest

from a_stock_agents.agents.news import classify_a_share_relevance, is_a_share_news, parse_eastmoney_payload
from a_stock_agents.errors import SchemaChangedError


class NewsParserTests(unittest.TestCase):
    def test_a_share_classifier(self):
        self.assertTrue(is_a_share_news("沪指收涨，A股成交放量"))
        self.assertTrue(is_a_share_news("浦发银行600000发布业绩预告"))
        self.assertFalse(is_a_share_news("纳斯达克美股科技板块收跌"))
        self.assertFalse(is_a_share_news("美国股票市场成交活跃", "海外证券个股普遍上涨", "https://finance.sina.com.cn/stock/usstock/a.html"))
        self.assertTrue(is_a_share_news("美联储释放降息信号，离岸人民币走强", feed_region="global"))
        self.assertTrue(is_a_share_news("国际原油大涨，布伦特油价突破关键位置", feed_region="global"))

    def test_global_relevance_tags(self):
        score, tags, region = classify_a_share_relevance(
            "美国收紧对华芯片出口管制",
            "半导体供应链或受影响",
            feed_region="global",
        )
        self.assertGreaterEqual(score, 0.55)
        self.assertEqual(region, "global")
        self.assertIn("trade_policy", tags)
        self.assertIn("global_supply_chain", tags)

    def test_parse_and_filter(self):
        payload = {
            "code": "1",
            "data": {
                "fastNewsList": [
                    {
                        "code": "202609040001",
                        "title": "浦发银行发布业绩预告",
                        "summary": "公司净利润增长",
                        "showTime": "2026-09-04 14:30:00",
                        "stockList": ["1.600000"],
                    },
                    {
                        "code": "202609040002",
                        "title": "纳斯达克美股收跌",
                        "summary": "海外科技股调整",
                        "showTime": "2026-09-04 14:29:00",
                        "stockList": [],
                    },
                ]
            }
        }
        items = parse_eastmoney_payload(payload)
        self.assertEqual([item.news_id for item in items], ["eastmoney:202609040001"])
        self.assertEqual(items[0].stock_codes, ["600000"])
        self.assertEqual(items[0].source, "东方财富快讯")
        self.assertEqual(items[0].url, "https://finance.eastmoney.com/a/202609040001.html")

    def test_parse_global_feed_adds_metadata_and_filters_noise(self):
        payload = {
            "code": 1,
            "data": {
                "fastNewsList": [
                    {
                        "code": "g1",
                        "title": "美联储降息预期升温",
                        "summary": "美元指数回落",
                        "showTime": "2026-09-04 14:30:00",
                    },
                    {
                        "code": "g2",
                        "title": "道琼斯指数小幅收涨",
                        "showTime": "2026-09-04 14:29:00",
                    },
                ]
            }
        }
        items = parse_eastmoney_payload(payload, feed="eastmoney_724", feed_region="global")
        self.assertEqual([item.news_id for item in items], ["eastmoney:g1"])
        self.assertEqual(items[0].region, "global")
        self.assertEqual(items[0].feed, "eastmoney_724")
        self.assertIn("global_monetary", items[0].relevance_tags)

    def test_schema_change_is_reported(self):
        with self.assertRaises(SchemaChangedError):
            parse_eastmoney_payload({"code": "1", "data": {"list": []}})


if __name__ == "__main__":
    unittest.main()
