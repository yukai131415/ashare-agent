import json
import tempfile
import unittest
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

from a_stock_agents.config import Settings, load_settings
from a_stock_agents.llm import (
    _build_request,
    _extract_answer,
    answer_question,
    normalize_base_url,
    public_config as llm_public_config,
    save_config as save_llm_config,
)
from a_stock_agents.storage import atomic_write_json, ensure_runtime
from a_stock_agents.web import _local_answer, _seconds_until_next_run, backtest_window_open, dashboard_data, page_html, recommendation_window_open


class DashboardDataTests(unittest.TestCase):
    def test_scheduler_countdown_uses_server_time_and_header(self):
        html=page_html(load_settings())
        self.assertIn('aria-label="推荐任务倒计时"',html)
        self.assertNotIn('id="countdown-label"',html)
        self.assertNotIn('id="countdown-next"',html)
        self.assertIn("actions.append(widget,logout)",html)
        self.assertIn('欢迎：',html)
        self.assertIn('performance.now()-received',html)
        self.assertIn("Date.parse(snapshot.server_now)",html)
        self.assertIn('awaiting_dispatch',html)

    def test_score_ring_has_confidential_dialog(self):
        html=page_html(load_settings())
        self.assertIn("scoreDialog.showModal()",html)
        self.assertIn('查看评分流程与结果（保密版）',html)
        self.assertIn('已存前五名位置',html)
        self.assertIn('不是可复刻的逐项计算明细',html)
        self.assertIn('score-details-dialog::backdrop',html)

    def test_selected_stock_left_and_mood_right_with_horizontal_bars(self):
        html=page_html(load_settings())
        self.assertIn('.workspace-duo.workspace-pick-mood{grid-template-columns:minmax(0,2fr) minmax(0,1fr)',html)
        self.assertIn("overview.prepend(document.querySelector('.featured-layout>article'))",html)
        self.assertIn("overview.classList.add('workspace-pick-mood')",html)
        self.assertIn('class="mood-horizontal"',html)
        self.assertIn('class="mood-bar-row"',html)
        self.assertNotIn('row.count/max*60+2',html)

    def test_strategy_sections_hidden_but_market_mood_kept(self):
        html=page_html(load_settings())
        self.assertIn("$('metric-model').closest('.metric').hidden=true",html)
        self.assertIn("$('pick-contributions').closest('article').hidden=true",html)
        self.assertIn("document.querySelector('.featured-layout>aside').hidden=true",html)
        self.assertIn('id="pool-mood"',html)
        self.assertIn('workspace-mood-only',html)

    def test_navigation_is_vertical_sidebar(self):
        html=page_html(load_settings())
        self.assertIn("nav.setAttribute('aria-orientation','vertical')",html)
        self.assertIn("document.body.classList.add('workspace-sidebar')",html)
        self.assertIn("'ArrowUp','ArrowDown'",html)
        self.assertIn('.workspace-sidebar .workspace-nav',html)

    def test_question_panel_is_in_monitor_overview(self):
        html=page_html(load_settings())
        self.assertIn("move('overview',document.querySelector('.qa-grid'))",html)
        self.assertNotIn("'.workflow-panel','.qa-grid'])move('brief'",html)

    def test_explanation_distinguishes_disabled_news_from_historical_scoring(self):
        rec = {
            "code": "600001", "name": "甲公司", "model": "balanced",
            "basis": {"momentum_z": 1.0, "news_sentiment": 1.0, "news_factor_enabled": False},
        }
        answer = _local_answer("为什么推荐？", {"recommendation": rec})
        self.assertIn("新闻因子已禁用", answer)
        self.assertNotIn("新闻情绪为正", answer)
        del rec["basis"]["news_factor_enabled"]
        historical = _local_answer("为什么推荐？", {"recommendation": rec})
        self.assertIn("新闻情绪为正", historical)
        self.assertIn("参与该历史版本评分", historical)

    def test_dashboard_exposes_recommendation_strategy_and_win_rate(self):
        with tempfile.TemporaryDirectory() as folder:
            values = load_settings().values
            settings = Settings(Path(folder), values)
            ensure_runtime(settings.runtime)
            rec_dir = settings.runtime / "agents" / "recommendation" / "data"
            bt_dir = settings.runtime / "agents" / "backtest" / "data"
            rec_dir.joinpath("recommendations.jsonl").write_text(
                '{"recommendation_id":"r1","created_at":"2026-08-19T10:00:00+08:00","target_date":"2026-08-20","code":"600000","name":"浦发银行","model":"balanced","reference_price":10,"score":1.2,"basis":{"change_pct":"2"}}\n',
                encoding="utf-8",
            )
            bt_dir.joinpath("results.jsonl").write_text(
                '{"recommendation_id":"r1","return_pct":3.5,"profitable":true,"target_high_price":10.45,"previous_close_price":10,"calculation_version":"target-high-prev-close-minus-1-v1"}\n',
                encoding="utf-8",
            )
            atomic_write_json(settings.runtime / "state" / "model_state.json", {"current_model": "balanced", "rotation_index": 0, "history": []})
            payload = dashboard_data(settings, "2026-08-19")
            self.assertEqual(payload["recommendation"]["code"], "600000")
            self.assertFalse(payload["strategy"]["news_factor_enabled"])
            self.assertEqual(payload["backtest"]["win_rate"], 100.0)
            self.assertTrue(any(item["active"] == "true" for item in payload["strategy"]["models"]))

    def test_local_question_and_llm_config_never_echoes_key(self):
        with tempfile.TemporaryDirectory() as folder:
            values = load_settings().values
            settings = Settings(Path(folder), values)
            ensure_runtime(settings.runtime)
            rec_dir = settings.runtime / "agents" / "recommendation" / "data"
            rec_dir.joinpath("recommendations.jsonl").write_text(
                '{"recommendation_id":"r1","created_at":"2026-08-19T10:00:00+08:00","target_date":"2026-08-20","code":"600000","name":"浦发银行","model":"balanced","reference_price":10,"score":1.2,"basis":{"momentum_z":"0.4","liquidity_z":"0.2","turnover_pct":"3","pe_ratio":"10","news_sentiment":"0.1"}}\n',
                encoding="utf-8",
            )
            saved = save_llm_config(settings, {"enabled": False, "model": "demo", "api_key": "secret-key"})
            self.assertTrue(saved["api_key_configured"])
            self.assertNotIn("secret-key", str(llm_public_config(settings)))
            data = dashboard_data(settings, "2026-08-19")
            answer, source, _ = answer_question(settings, "为什么推荐？", "{}", f"{data['recommendation']['name']} 本地解释")
            self.assertEqual(source, "local")
            self.assertIn("浦发银行", answer)

    def test_dashscope_native_request_shape(self):
        endpoint, payload, provider = _build_request(
            "https://dashscope.aliyuncs.com/api/v1",
            "你好",
            {"model": "qwen3.8-max", "max_tokens": 100},
        )
        self.assertEqual(provider, "dashscope_native")
        self.assertTrue(endpoint.endswith("/services/aigc/multimodal-generation/generation"))
        self.assertIn('"content": [{"text": "你好"}]', payload.decode("utf-8"))

    def test_openai_compatible_request_shape(self):
        endpoint, payload, provider = _build_request(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "你好",
            {"model": "qwen-plus", "max_tokens": 100},
        )
        self.assertEqual(provider, "openai_compatible")
        self.assertTrue(endpoint.endswith("/chat/completions"))
        self.assertIn('"messages"', payload.decode("utf-8"))

    def test_openai_dashboard_url_is_normalized_and_uses_responses_api(self):
        endpoint, payload, provider = _build_request(
            "https://platform.openai.com",
            "你好",
            {"model": "gpt-5.6-luna", "max_tokens": 100},
        )
        self.assertEqual(normalize_base_url("platform.openai.com/api-keys"), "https://api.openai.com/v1")
        self.assertEqual(provider, "openai_responses")
        self.assertEqual(endpoint, "https://api.openai.com/v1/responses")
        self.assertIn('"max_output_tokens": 100', payload.decode("utf-8"))

    def test_save_config_persists_normalized_openai_url(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder), load_settings().values)
            ensure_runtime(settings.runtime)
            saved = save_llm_config(
                settings,
                {"enabled": True, "base_url": "https://platform.openai.com", "api_key": "secret-key"},
            )
            self.assertEqual(saved["base_url"], "https://api.openai.com/v1")

    def test_extract_openai_responses_text(self):
        answer = _extract_answer(
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": "连接成功"}]}]},
            "openai_responses",
        )
        self.assertEqual(answer, "连接成功")

    def test_dashboard_auto_refresh_interval_comes_from_config(self):
        with tempfile.TemporaryDirectory() as folder:
            values = load_settings().values
            values = {**values, "scheduler": {**values.get("scheduler", {}), "dashboard_refresh_seconds": 30}}
            settings = Settings(Path(folder), values)
            html = page_html(settings)
            self.assertIn("nextPollAt=Date.now()+30000", html)
            self.assertIn("Date.now()>=nextPollAt&&!monitorLoading", html)
            self.assertNotIn("__DASHBOARD_REFRESH_MS__", html)

    def test_scheduler_next_run_rolls_to_tomorrow_after_daily_time(self):
        now = datetime(2026, 9, 1, 14, 31, tzinfo=timezone(timedelta(hours=8)))
        self.assertGreater(_seconds_until_next_run(14, 30, now), 23 * 60 * 60)

    def test_recommendation_window_opens_at_configured_time(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {**load_settings().values, "scheduler": {"daily_run_time": "14:30"}}
            settings = Settings(Path(folder), values)
            tz = timezone(timedelta(hours=8))
            self.assertFalse(recommendation_window_open(settings, datetime(2026, 9, 1, 14, 29, tzinfo=tz)))
            self.assertTrue(recommendation_window_open(settings, datetime(2026, 9, 1, 14, 30, tzinfo=tz)))

    def test_weekend_never_opens_recommendation_window(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {**load_settings().values, "scheduler": {"daily_run_time": "14:30"}}
            settings = Settings(Path(folder), values)
            tz = timezone(timedelta(hours=8))
            self.assertFalse(recommendation_window_open(settings, datetime(2026, 9, 5, 20, 0, tzinfo=tz)))

    def test_scheduler_skips_weekend_and_targets_monday(self):
        tz = timezone(timedelta(hours=8))
        friday_after_close = datetime(2026, 9, 4, 15, 0, tzinfo=tz)
        self.assertEqual(_seconds_until_next_run(14, 30, friday_after_close), 71.5 * 60 * 60)

    def test_backtest_window_opens_at_close_time(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {**load_settings().values, "backtest": {**load_settings().values["backtest"], "close_time": "15:00"}}
            settings = Settings(Path(folder), values)
            tz = timezone(timedelta(hours=8))
            self.assertFalse(backtest_window_open(settings, datetime(2026, 9, 1, 14, 59, tzinfo=tz)))
            self.assertTrue(backtest_window_open(settings, datetime(2026, 9, 1, 15, 0, tzinfo=tz)))
            self.assertFalse(backtest_window_open(settings, datetime(2026, 9, 5, 16, 0, tzinfo=tz)))

    def test_dashboard_excludes_weekend_recommendations_and_their_results(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder), load_settings().values)
            ensure_runtime(settings.runtime)
            rec_dir = settings.runtime / "agents" / "recommendation" / "data"
            bt_dir = settings.runtime / "agents" / "backtest" / "data"
            rec_dir.joinpath("recommendations.jsonl").write_text(
                "\n".join(
                    [
                        '{"recommendation_id":"weekend","created_at":"2026-08-30T15:00:00+08:00","target_date":"2026-08-31","code":"600127","name":"周末错误记录","model":"balanced","reference_price":10,"score":2}',
                        '{"recommendation_id":"weekday","created_at":"2026-08-31T15:00:00+08:00","target_date":"2026-09-01","code":"600000","name":"工作日记录","model":"balanced","reference_price":10,"score":1}',
                    ]
                ) + "\n",
                encoding="utf-8",
            )
            bt_dir.joinpath("results.jsonl").write_text(
                "\n".join(
                    [
                        '{"recommendation_id":"weekend","target_date":"2026-08-31","return_pct":5,"profitable":true,"target_high_price":10.6,"previous_close_price":10,"calculation_version":"target-high-prev-close-minus-1-v1"}',
                        '{"recommendation_id":"weekday","target_date":"2026-09-01","return_pct":-1,"profitable":false,"target_high_price":10,"previous_close_price":10,"calculation_version":"target-high-prev-close-minus-1-v1"}',
                    ]
                ) + "\n",
                encoding="utf-8",
            )

            payload = dashboard_data(settings, "2026-08-31")

            self.assertNotIn("2026-08-30", payload["available_dates"])
            self.assertTrue(all(row["date"] != "2026-08-30" for row in payload["history"]))
            self.assertEqual(payload["backtest"]["total"], 1)
            self.assertEqual(payload["backtest"]["losses"], 1)

    def test_dashboard_hides_today_recommendation_before_daily_time(self):
        with tempfile.TemporaryDirectory() as folder:
            values = {**load_settings().values, "scheduler": {"daily_run_time": "23:59"}}
            settings = Settings(Path(folder), values)
            ensure_runtime(settings.runtime)
            today = date.today()
            yesterday = today - timedelta(days=1)
            from a_stock_agents.trading_calendar import is_trading_weekday
            while not is_trading_weekday(yesterday):
                yesterday -= timedelta(days=1)
            rec_dir = settings.runtime / "agents" / "recommendation" / "data"
            rec_dir.joinpath("recommendations.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "recommendation_id": "old",
                                "created_at": f"{yesterday.isoformat()}T15:00:00+08:00",
                                "target_date": today.isoformat(),
                                "code": "600000",
                                "name": "昨日股票",
                                "model": "balanced",
                                "reference_price": 10,
                                "score": 1.0,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "recommendation_id": "today",
                                "created_at": f"{today.isoformat()}T10:00:00+08:00",
                                "target_date": (today + timedelta(days=1)).isoformat(),
                                "code": "000001",
                                "name": "今日股票",
                                "model": "balanced",
                                "reference_price": 11,
                                "score": 2.0,
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            payload = dashboard_data(settings)
            self.assertEqual(payload["selected_date"], yesterday.isoformat())
            self.assertNotIn(today.isoformat(), payload["available_dates"])
            self.assertTrue(all(row["date"] != today.isoformat() for row in payload["history"]))

    def test_dashboard_exposes_news_with_readable_tone(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder), load_settings().values)
            ensure_runtime(settings.runtime)
            rec_dir = settings.runtime / "agents" / "recommendation" / "data"
            rec_dir.joinpath("recommendations.jsonl").write_text(
                '{"recommendation_id":"r1","created_at":"2026-08-19T10:00:00+08:00","target_date":"2026-08-20","code":"600000","name":"浦发银行","model":"balanced","reference_price":10,"score":1.2}\n',
                encoding="utf-8",
            )
            news_dir = settings.runtime / "agents" / "news" / "data"
            atomic_write_json(
                news_dir / "latest.json",
                {
                    "fetched_at": "2026-09-01T14:30:00+08:00",
                    "items": [
                        {"title": "银行业绩增长 股价涨超5%", "stock_codes": ["600000"], "source": "东方财富快讯", "published_at": "2026-09-01T14:00:00+08:00"},
                        {"title": "机构提示600000存在风险", "source": "东方财富快讯", "published_at": "2026-09-01T13:00:00+08:00"},
                        {"title": "市场午后震荡整理", "source": "东方财富快讯", "published_at": "2026-09-01T12:00:00+08:00"},
                    ],
                },
            )
            payload = dashboard_data(settings, "2026-08-19")
            self.assertEqual(payload["news"]["count"], 2)
            self.assertEqual(payload["news"]["total_source_count"], 3)
            self.assertEqual([item["tone_label"] for item in payload["news"]["items"]], ["利好", "利空"])
            self.assertTrue(all("东方财富" in item["source"] for item in payload["news"]["items"]))


if __name__ == "__main__":
    unittest.main()
