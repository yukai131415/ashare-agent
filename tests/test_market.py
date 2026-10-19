import unittest
from unittest.mock import patch

from a_stock_agents.agents.market import (
    MarketDataAgent,
    is_allowed_stock,
    parse_eastmoney_payload,
    parse_market_html,
    parse_tencent_quotes,
)
from a_stock_agents.contracts import MarketRow
from a_stock_agents.errors import SourceBlockedError


HTML = """
<table><thead><tr><th>序号</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅(%)</th>
<th>涨跌</th><th>涨速(%)</th><th>换手(%)</th><th>量比</th><th>振幅(%)</th>
<th>成交额</th><th>流通股</th><th>流通市值</th><th>市盈率</th></tr></thead>
<tbody><tr><td>1</td><td><a>600000</a></td><td><a>浦发银行</a></td><td>10.50</td><td>2.00</td>
<td>0.21</td><td>0.10</td><td>1.20</td><td>1.50</td><td>2.50</td>
<td>12.34亿</td><td>100.00亿</td><td>1050.00亿</td><td>6.80</td></tr></tbody></table>
"""


class MarketParserTests(unittest.TestCase):
    def test_parses_and_converts_units(self):
        rows = parse_market_html(HTML)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "600000")
        self.assertEqual(rows[0].amount, 1_234_000_000)
        self.assertEqual(rows[0].change_pct, 2.0)

    def test_required_filters(self):
        config = {"exclude_st": True, "exclude_gem": True, "exclude_bse": True}
        base = dict(price=1.0, change_pct=0.0)
        self.assertTrue(is_allowed_stock(MarketRow(code="600000", name="浦发银行", **base), config))
        self.assertFalse(is_allowed_stock(MarketRow(code="600001", name="*ST测试", **base), config))
        self.assertFalse(is_allowed_stock(MarketRow(code="300001", name="创业测试", **base), config))
        self.assertFalse(is_allowed_stock(MarketRow(code="920001", name="北交测试", **base), config))
        self.assertFalse(is_allowed_stock(MarketRow(code="688001", name="科创测试", **base), config))
        self.assertFalse(is_allowed_stock(MarketRow(code="689009", name="科创存托", **base), config))

    def test_eastmoney_payload_mapping(self):
        rows = parse_eastmoney_payload(
            {
                "data": {
                    "diff": [
                        {
                            "f12": "600000", "f14": "浦发银行", "f2": 10.5, "f3": 2.0,
                            "f4": 0.21, "f6": 1234000000, "f7": 2.5, "f8": 1.2,
                            "f9": 6.8, "f10": 1.5, "f21": 105000000000, "f22": 0.1,
                        }
                    ]
                }
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "600000")
        self.assertEqual(rows[0].amount, 1_234_000_000)
        self.assertEqual(rows[0].circulating_market_cap, 105_000_000_000)

    def test_fetch_rotates_nodes_and_reports_all_failures(self):
        class BlockedClient:
            def __init__(self):
                self.urls = []

            def get_json(self, url, params):
                self.urls.append(url)
                raise SourceBlockedError("blocked")

        client = BlockedClient()
        config = {
            "base_urls": ["https://node-a.test", "https://node-b.test"],
            "page_size": 100,
            "sort_field": "f3",
            "market_filter": "test",
            "fields": "f2,f3,f12,f14",
            "public_token": "test-token",
            "page_retries": 2,
            "page_retry_backoff_seconds": 0,
        }
        with patch("a_stock_agents.agents.market.time.sleep"), patch(
            "a_stock_agents.agents.market.random.uniform", return_value=0
        ):
            with self.assertRaises(SourceBlockedError) as captured:
                MarketDataAgent._fetch_page(None, client, config, page=1)
        self.assertEqual(
            client.urls,
            ["https://node-a.test", "https://node-b.test", "https://node-b.test", "https://node-a.test"],
        )
        self.assertIn("2 个节点均不可用", str(captured.exception))
        self.assertIn("node-a.test", str(captured.exception))
        self.assertIn("node-b.test", str(captured.exception))

    def test_tencent_quote_mapping(self):
        fields = [""] * 49
        fields[1] = "浦发银行"
        fields[2] = "600000"
        fields[3] = "10.50"
        fields[31] = "0.21"
        fields[32] = "2.04"
        fields[37] = "123400"
        fields[38] = "1.20"
        fields[39] = "6.80"
        fields[43] = "2.50"
        fields[44] = "1050.00"
        rows = parse_tencent_quotes(f'v_sh600000="{"~".join(fields)}";')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "600000")
        self.assertEqual(rows[0].amount, 1_234_000_000)
        self.assertEqual(rows[0].circulating_market_cap, 105_000_000_000)


if __name__ == "__main__":
    unittest.main()
