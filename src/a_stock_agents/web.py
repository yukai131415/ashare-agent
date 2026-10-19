from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .llm import answer_question, public_config as llm_public_config, save_config as save_llm_config, test_connection as test_llm_connection
from .model_state import ModelState
from .recommendation_store import recommendation_store
from .storage import read_json
from .supervisor import Supervisor
from .agents.backtest import parse_close_time
from .trading_calendar import is_trading_weekday


STRATEGY_LABELS = {
    "balanced": "平衡多因子",
    "momentum": "动量趋势",
    "value_reversal": "价值反转",
}

POSITIVE_NEWS_WORDS = (
    "上涨",
    "涨超",
    "收涨",
    "大涨",
    "走高",
    "拉升",
    "突破",
    "增长",
    "增加",
    "盈利",
    "扭亏",
    "中标",
    "回购",
    "增持",
    "利好",
)
NEGATIVE_NEWS_WORDS = (
    "下跌",
    "跌超",
    "收跌",
    "大跌",
    "走低",
    "跳水",
    "亏损",
    "减持",
    "处罚",
    "立案",
    "退市",
    "警惕",
    "风险",
    "利空",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _date_of(value: Any) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else ""


def _is_trading_day_text(value: str) -> bool:
    try:
        return is_trading_weekday(date.fromisoformat(value))
    except ValueError:
        return False


def _parse_hhmm(value: Any, default: tuple[int, int] = (14, 30)) -> tuple[int, int]:
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":", 1)
        parsed = (int(hour), int(minute[:2]))
        if 0 <= parsed[0] <= 23 and 0 <= parsed[1] <= 59:
            return parsed
    except (TypeError, ValueError):
        pass
    return default


def _seconds_until_next_run(hour: int, minute: int, now: datetime | None = None) -> float:
    current = now or datetime.now().astimezone()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while target <= current or not is_trading_weekday(target.date()):
        target = target + timedelta(days=1)
    return max(1.0, (target - current).total_seconds())


def scheduler_config(settings: Settings) -> dict[str, Any]:
    config = settings.values.get("scheduler", {}) if isinstance(settings.values, dict) else {}
    if not isinstance(config, dict):
        config = {}
    hour, minute = _parse_hhmm(config.get("daily_run_time", "14:30"))
    return {
        "enabled": bool(config.get("enabled", True)),
        "daily_run_time": f"{hour:02d}:{minute:02d}",
        "dashboard_refresh_seconds": int(config.get("dashboard_refresh_seconds", 60)),
        "hour": hour,
        "minute": minute,
    }


def recommendation_window_open(settings: Settings, now: datetime | None = None) -> bool:
    config = scheduler_config(settings)
    current = now or datetime.now().astimezone()
    return (
        is_trading_weekday(current.date())
        and (current.hour, current.minute) >= (int(config["hour"]), int(config["minute"]))
    )


def backtest_window_open(settings: Settings, now: datetime | None = None) -> bool:
    close_at = parse_close_time(settings.section("backtest").get("close_time", "15:00"))
    current = now or datetime.now().astimezone()
    return is_trading_weekday(current.date()) and current.time() >= close_at


def refresh_backtest_after_close(settings: Settings) -> dict[str, Any]:
    if not is_trading_weekday():
        return {
            "ok": False,
            "skipped": True,
            "message": "周末休市，不刷新行情和回测数据",
        }
    if not backtest_window_open(settings):
        close_at = parse_close_time(settings.section("backtest").get("close_time", "15:00"))
        return {
            "ok": False,
            "skipped": True,
            "message": f"尚未到 {close_at.strftime('%H:%M')}，暂不刷新回测数据",
        }
    supervisor = Supervisor(settings)
    market = supervisor.run_agent("market")
    backtest = supervisor.run_agent("backtest") if market.ok else None
    return {
        "ok": bool(market.ok and backtest and backtest.ok),
        "skipped": False,
        "message": (
            f"收盘后刷新完成：行情 {'成功' if market.ok else '失败'}；"
            f"回测 {backtest.message if backtest else '因行情失败未运行'}"
        ),
        "market": market.to_dict(),
        "backtest": backtest.to_dict() if backtest else None,
    }


def _latest_per_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = _date_of(row.get("recommendation_date") or row.get("created_at"))
        if not day:
            continue
        current = by_day.get(day)
        if current is None or str(row.get("created_at", "")) >= str(current.get("created_at", "")):
            by_day[day] = row
    return sorted(by_day.values(), key=lambda row: str(row.get("created_at", "")), reverse=True)


def _is_close_based_result(row: dict[str, Any]) -> bool:
    from .agents.backtest import is_close_based_result
    return is_close_based_result(row)


def _news_tone(item: dict[str, Any]) -> str:
    raw_sentiment = item.get("sentiment") or item.get("news_sentiment")
    try:
        sentiment = float(raw_sentiment)
    except (TypeError, ValueError):
        sentiment = None
    if sentiment is not None:
        if sentiment > 0.25:
            return "positive"
        if sentiment < -0.25:
            return "negative"
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    if any(word in text for word in NEGATIVE_NEWS_WORDS):
        return "negative"
    if any(word in text for word in POSITIVE_NEWS_WORDS):
        return "positive"
    return "neutral"


def _news_tone_label(tone: str) -> str:
    return {"positive": "利好", "negative": "利空"}.get(tone, "中性")


def _matches_recommendation_news(item: dict[str, Any], recommendation: dict[str, Any] | None) -> bool:
    if not recommendation:
        return False
    code = str(recommendation.get("code", "")).strip()
    name = str(recommendation.get("name", "")).strip()
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    stock_codes = {str(value).strip() for value in (item.get("stock_codes") or [])}
    return bool((code and (code in stock_codes or code in text)) or (len(name) >= 2 and name in text))


def _dashboard_news(settings: Settings, recommendation: dict[str, Any] | None = None, limit: int = 20) -> dict[str, Any]:
    news = read_json(settings.runtime / "agents" / "news" / "data" / "latest.json", {})
    items = news.get("items", []) if isinstance(news, dict) else []
    related_items = [item for item in items if isinstance(item, dict) and _matches_recommendation_news(item, recommendation)]
    rows: list[dict[str, Any]] = []
    counts = {"positive": 0, "negative": 0, "neutral": 0}
    for item in related_items:
        tone = _news_tone(item)
        counts[tone] = counts.get(tone, 0) + 1
        if len(rows) >= limit:
            continue
        rows.append(
            {
                "title": item.get("title", ""),
                "summary": item.get("summary", ""),
                "url": item.get("url", ""),
                "source": item.get("source", ""),
                "published_at": item.get("published_at", ""),
                "region": item.get("region", ""),
                "relevance_score": item.get("relevance_score"),
                "tone": tone,
                "tone_label": _news_tone_label(tone),
            }
        )
    return {
        "fetched_at": news.get("fetched_at") if isinstance(news, dict) else None,
        "count": len(related_items),
        "total_source_count": len(items),
        "display_count": len(rows),
        "counts": counts,
        "items": rows,
    }


def _strategy_info(settings: Settings, current: str) -> list[dict[str, str]]:
    configured = settings.section("recommendation").get("models", [])
    descriptions = {
        "balanced": "综合涨幅、量比、流动性、换手和估值，考虑振幅风险；新闻因子已禁用。",
        "momentum": "提高涨幅、量比和涨速权重，偏向短期趋势强势股；新闻因子已禁用。",
        "value_reversal": "提高低估值与短期反转权重，寻找超跌修复机会；新闻因子已禁用。",
    }
    return [
        {
            "key": str(name),
            "label": STRATEGY_LABELS.get(str(name), str(name)),
            "description": descriptions.get(str(name), "自定义选股模型。"),
            "active": "true" if str(name) == current else "false",
        }
        for name in configured
    ]


def dashboard_data(settings: Settings, selected_date: str | None = None) -> dict[str, Any]:
    root = settings.runtime / "agents"
    market_latest = root / "market" / "data" / "latest.csv"
    store = recommendation_store(settings.runtime)
    recommendation_rows = [
        row for row in _latest_per_day(store.history())
        if _is_trading_day_text(_date_of(row.get("recommendation_date") or row.get("created_at")))
    ]
    realtime_recommendation = store.realtime()
    if realtime_recommendation and not _is_trading_day_text(
        _date_of(realtime_recommendation.get("recommendation_date") or realtime_recommendation.get("created_at"))
    ):
        realtime_recommendation = None
    valid_recommendation_ids = {
        str(row.get("recommendation_id"))
        for row in recommendation_rows
        if row.get("recommendation_id")
    }
    result_rows = [
        row for row in _read_jsonl(root / "backtest" / "data" / "results.jsonl")
        if _is_close_based_result(row)
        and str(row.get("recommendation_id", "")) in valid_recommendation_ids
    ]
    result_by_id = {str(row.get("recommendation_id")): row for row in result_rows}
    dates = sorted(
        {_date_of(row.get("recommendation_date") or row.get("created_at")) for row in recommendation_rows if _date_of(row.get("recommendation_date") or row.get("created_at"))},
        reverse=True,
    )
    realtime_date = _date_of(realtime_recommendation.get("recommendation_date") or realtime_recommendation.get("created_at")) if realtime_recommendation else ""
    if realtime_date and realtime_date not in dates:
        dates.insert(0, realtime_date)
    today = date.today().isoformat()
    visible_dates = dates if recommendation_window_open(settings) else [item for item in dates if item != today]
    default_date = visible_dates[0] if visible_dates else today
    selected = selected_date if selected_date in visible_dates else default_date
    selected_recommendation = (
        realtime_recommendation
        if realtime_recommendation and realtime_date == selected and selected in visible_dates
        else next(
            (row for row in recommendation_rows if _date_of(row.get("recommendation_date") or row.get("created_at")) == selected and selected in visible_dates),
            None,
        )
    )
    history = []
    visible_recommendation_rows = [
        row for row in recommendation_rows if _date_of(row.get("recommendation_date") or row.get("created_at")) in visible_dates
    ]
    for row in visible_recommendation_rows:
        rec_id = str(row.get("recommendation_id", ""))
        result = result_by_id.get(rec_id)
        history.append(
            {
                "recommendation_id": rec_id,
                "date": _date_of(row.get("recommendation_date") or row.get("created_at")),
                "target_date": _date_of(row.get("target_date")),
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "model": row.get("model", ""),
                "model_label": STRATEGY_LABELS.get(str(row.get("model", "")), row.get("model", "")),
                "reference_price": row.get("reference_price"),
                "score": row.get("score"),
                "return_pct": result.get("return_pct") if result else None,
                "profitable": result.get("profitable") if result else None,
            }
        )
    wins = sum(1 for row in result_rows if bool(row.get("profitable")))
    losses = sum(1 for row in result_rows if row.get("profitable") is False)
    total = wins + losses
    try:
        model_state = ModelState(
            settings.runtime / "state" / "model_state.json",
            list(settings.section("recommendation").get("models", [])),
        ).load()
    except Exception:
        model_state = {"current_model": settings.section("recommendation").get("models", ["balanced"])[0]}
    health = read_json(settings.runtime / "state" / "health.json", {})
    health_agents = health.get("agents", {}) if isinstance(health, dict) else {}
    selected_result = result_by_id.get(str(selected_recommendation.get("recommendation_id"))) if selected_recommendation else None
    recommendation = None
    if selected_recommendation:
        recommendation = {
            **selected_recommendation,
            "model_label": STRATEGY_LABELS.get(
                str(selected_recommendation.get("model", "")), selected_recommendation.get("model", "")
            ),
            "backtest": selected_result,
        }
    dashboard_news = _dashboard_news(settings, recommendation)
    workflow = read_json(settings.runtime / 'workflows' / 'latest.json', {})
    if recommendation and recommendation.get('recommendation_id'):
        workflow = read_json(settings.runtime / 'workflows' / 'recommendations' / (recommendation['recommendation_id'] + '.json'), {})
    return {
        "selected_date": selected,
        "available_dates": visible_dates,
        "recommendation": recommendation,
        "history": history,
        "strategy": {
            "news_factor_enabled": False,
            "current_model": model_state.get("current_model", "balanced"),
            "current_label": STRATEGY_LABELS.get(str(model_state.get("current_model", "")), model_state.get("current_model", "")),
            "models": _strategy_info(settings, str(model_state.get("current_model", ""))),
        },
        "backtest": {
            "wins": wins,
            "losses": losses,
            "total": total,
            "win_rate": round(wins / total * 100, 2) if total else None,
            "results": result_rows,
        },
        "news": dashboard_news,
        "workflow": workflow,
        "health": {
            "ok": health.get("ok") if isinstance(health, dict) else None,
            "degraded": health.get("degraded") if isinstance(health, dict) else None,
            "finished_at": health.get("finished_at") if isinstance(health, dict) else None,
            "market_snapshot_at": datetime.fromtimestamp(market_latest.stat().st_mtime).astimezone().isoformat()
            if market_latest.exists()
            else None,
            "agents": {
                name: {"ok": value.get("ok"), "records": value.get("records", 0)}
                for name, value in health_agents.items()
                if isinstance(value, dict)
            },
            "scheduler": health.get("scheduler") if isinstance(health, dict) else None,
            "recommendation_window_open": recommendation_window_open(settings),
            "failure_details": [{"agent":name,"message":value.get("message","")} for name,value in health_agents.items()
                if isinstance(value,dict) and value.get("ok") is False],
        },
    }


def _question_context(settings: Settings, selected_date: str | None) -> tuple[dict[str, Any], str]:
    data = dashboard_data(settings, selected_date)
    news = read_json(settings.runtime / "agents" / "news" / "data" / "latest.json", {})
    items = news.get("items", []) if isinstance(news, dict) else []
    news_lines = [
        (
            f"- [{item.get('region', 'unknown')}]"
            f"[{','.join(str(tag) for tag in item.get('relevance_tags', []))}] "
            f"{item.get('title', '')}: {item.get('summary', '')}"
        )
        for item in items[:8]
        if isinstance(item, dict)
    ]
    rec = data.get("recommendation") or {}
    context = {
        "date": data.get("selected_date"),
        "recommendation": rec,
        "strategy": data.get("strategy"),
        "agent_research": data.get("workflow"),
        "backtest": data.get("backtest"),
        "news": news_lines,
        "news_scoring_note": (
            "所选推荐使用历史版本，新闻情绪参与当时评分；当前新推荐已禁用新闻因子。"
            if (rec.get("basis") or {}).get("news_factor_enabled", True)
            else "所选推荐已禁用新闻因子；新闻仅供阅读，不是本次选股加分或扣分的依据。"
        ),
    }
    return data, json.dumps(context, ensure_ascii=False, indent=2)


def _local_answer(question: str, data: dict[str, Any]) -> str:
    rec = data.get("recommendation") or {}
    if not rec:
        return "所选日期没有推荐记录，暂时无法解释选股原因。请先运行推荐 Agent，或切换到有推荐记录的日期。"
    basis = rec.get("basis") or {}
    news_factor_enabled = basis.get("news_factor_enabled", True)
    factors = []
    if float(basis.get("momentum_z") or 0) > 0:
        factors.append("短期动量为正")
    if float(basis.get("liquidity_z") or 0) > 0:
        factors.append("流动性表现较好")
    if float(basis.get("turnover_pct") or 0) >= 2:
        factors.append("换手率达到活跃水平")
    if float(basis.get("pe_ratio") or 0) > 0 and float(basis.get("pe_ratio") or 0) < 20:
        factors.append("估值指标相对温和")
    if news_factor_enabled and float(basis.get("news_sentiment") or 0) > 0:
        factors.append("新闻情绪为正")
    factor_text = "、".join(factors) if factors else "综合因子评分进入候选集"
    news_text = (
        f"新闻情绪 {basis.get('news_sentiment', '—')}（参与该历史版本评分）"
        if news_factor_enabled else "新闻因子已禁用，相关新闻仅供阅读"
    )
    result = rec.get("backtest")
    backtest_text = "该推荐尚未到达目标日，暂无实际回测收益。"
    if isinstance(result, dict):
        backtest_text = f"历史回测收益为 {result.get('return_pct', '—')}%，{'盈利' if result.get('profitable') else '亏损'}。"
    return (
        f"{data.get('selected_date')} 推荐 {rec.get('code')} {rec.get('name')}，使用“{rec.get('model_label', rec.get('model', ''))}”策略，"
        f"参考价 ¥{rec.get('reference_price', '—')}，模型评分 {rec.get('score', '—')}。\n"
        f"主要依据：{factor_text}。原始因子：涨跌幅 {basis.get('change_pct', '—')}%、换手率 {basis.get('turnover_pct', '—')}%、"
        f"市盈率 {basis.get('pe_ratio', '—')}。{news_text}。\n"
        f"{backtest_text}\n"
        "以上是基于当前快照和透明因子的研究解释，不代表未来收益或投资建议。"
    )


PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>A股多 Agent 投研看板</title>
  <style>
    :root { --bg:#08111f; --panel:#101d31; --panel2:#14253d; --text:#edf4ff; --muted:#91a4bf; --line:#243a58; --cyan:#54d6c7; --blue:#70a7ff; --red:#ff7d8e; --yellow:#ffd479; }
    * { box-sizing:border-box; } body { margin:0; color:var(--text); background:radial-gradient(circle at 80% 0,#163558 0,#08111f 42%); font-family:Inter,"Microsoft YaHei",sans-serif; min-height:100vh; }
    .wrap { width:min(1220px,calc(100% - 36px)); margin:0 auto; padding:34px 0 56px; }
    .top { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; margin-bottom:24px; } h1 { margin:0 0 8px; letter-spacing:-.04em; font-size:clamp(28px,4vw,46px); } .sub,.muted { color:var(--muted); }
    .toolbar { display:flex; align-items:center; gap:10px; background:#0e1b2e; border:1px solid var(--line); padding:10px 12px; border-radius:14px; } label { color:var(--muted); font-size:13px; } input,button { color:var(--text); background:#14253d; border:1px solid #315071; border-radius:8px; padding:9px 11px; font:inherit; } button { cursor:pointer; } button:hover { border-color:var(--cyan); }
    .grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; } .card { background:linear-gradient(145deg,rgba(20,37,61,.95),rgba(12,24,42,.95)); border:1px solid var(--line); border-radius:18px; padding:19px; box-shadow:0 14px 40px #02081250; } .metric .k { color:var(--muted); font-size:13px; } .metric .v { margin-top:8px; font-size:28px; font-weight:750; } .metric .hint { color:var(--muted); margin-top:5px; font-size:12px; }
    .main { display:grid; grid-template-columns:1.32fr .68fr; gap:16px; margin-top:16px; } .section-title { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; } h2 { margin:0; font-size:18px; } .tag { display:inline-flex; border:1px solid #376e78; color:var(--cyan); background:#12313b; border-radius:999px; padding:5px 10px; font-size:12px; }
    .pick { display:grid; grid-template-columns:1fr auto; gap:18px; align-items:center; } .pick-code { font-size:14px; color:var(--cyan); } .pick-name { font-size:32px; font-weight:800; margin:6px 0; } .price { font-size:24px; font-weight:700; } .score { text-align:right; } .score strong { display:block; font-size:34px; color:var(--yellow); } .snapshot { margin-top:8px; color:var(--muted); font-size:12px; } .factor-title { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:22px; padding-top:18px; border-top:1px solid var(--line); } .factor-title strong { font-size:16px; } .factor-title span { color:var(--muted); font-size:12px; } .basis { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:10px; margin-top:12px; } .factor-card { background:#0b182a; border:1px solid var(--line); border-radius:12px; padding:12px; min-height:102px; } .factor-card .factor-label { color:#dce8f7; font-size:14px; font-weight:700; } .factor-card .factor-value { margin-top:7px; color:var(--cyan); font-size:20px; font-weight:800; } .factor-card .factor-value.down { color:var(--red); } .factor-card .factor-value.up { color:var(--cyan); } .factor-card .factor-note { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.45; }
    .strategies { display:grid; gap:10px; } .strategy { padding:13px; background:#0b182a; border:1px solid var(--line); border-radius:12px; } .strategy.active { border-color:var(--cyan); box-shadow:inset 3px 0 var(--cyan); } .strategy-head { display:flex; justify-content:space-between; font-weight:700; } .strategy p { color:var(--muted); line-height:1.55; font-size:12px; margin:8px 0 0; }
    .news-card { margin-top:16px; } .news-summary { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; } .news-pill { border:1px solid var(--line); border-radius:999px; padding:5px 10px; color:var(--muted); background:#0b182a; font-size:12px; } .news-pill.good { color:#ffb3bd; border-color:#9b3947; background:#3b111b; } .news-pill.bad { color:#f7f7f7; border-color:#111827; background:#020509; } .news-list { display:grid; gap:10px; max-height:520px; overflow:auto; padding-right:4px; } .news-item { background:#0b182a; border:1px solid var(--line); border-radius:14px; padding:13px; } .news-item.good { border-color:#9b3947; box-shadow:inset 4px 0 #e5485d; } .news-item.bad { border-color:#111827; background:#05080e; box-shadow:inset 4px 0 #000; } .news-meta { display:flex; flex-wrap:wrap; align-items:center; gap:8px; color:var(--muted); font-size:12px; margin-bottom:7px; } .news-tone { border-radius:999px; padding:3px 8px; font-weight:700; } .news-tone.good { color:#fff; background:#d9364f; } .news-tone.bad { color:#fff; background:#000; border:1px solid #2d3748; } .news-tone.neutral { color:var(--cyan); background:#12313b; border:1px solid #376e78; } .news-title { color:#edf4ff; font-weight:750; line-height:1.45; text-decoration:none; } .news-title:hover { color:var(--cyan); } .news-summary-text { margin-top:7px; color:#b8c8dd; font-size:13px; line-height:1.6; }
    .table-card { margin-top:16px; } .table-wrap { overflow:auto; } table { width:100%; border-collapse:collapse; min-width:700px; } th,td { padding:13px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; } th { color:var(--muted); font-size:12px; font-weight:500; } td { font-size:13px; } .positive { color:var(--cyan); } .negative { color:var(--red); } .pending { color:var(--yellow); }
    .status { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; } .status span { padding:5px 8px; border-radius:999px; background:#0b182a; color:var(--muted); font-size:11px; } .status .ok { color:var(--cyan); }
    .qa-grid { margin-top:16px; } textarea { width:100%; min-height:96px; resize:vertical; color:var(--text); background:#0b182a; border:1px solid var(--line); border-radius:10px; padding:12px; font:inherit; line-height:1.55; } .qa-actions { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:10px; } .primary { background:linear-gradient(135deg,#1c807e,#2562a6); border-color:#55cfc9; font-weight:700; } .answer { white-space:pre-wrap; line-height:1.7; color:#dbe8f7; background:#0b182a; border:1px solid var(--line); border-radius:12px; padding:14px; margin-top:14px; min-height:66px; } .answer.loading { color:var(--muted); } .config-form { display:grid; gap:10px; } .config-form input[type=text], .config-form input[type=password] { width:100%; } .config-form small { color:var(--muted); line-height:1.45; } .switch { display:flex; align-items:center; gap:8px; color:#c8d6e9; font-size:13px; }
    .empty { color:var(--muted); padding:22px 0; } footer { color:var(--muted); font-size:12px; margin-top:22px; line-height:1.6; }
    @media(max-width:850px) { .top,.pick { align-items:flex-start; flex-direction:column; } .grid { grid-template-columns:repeat(2,1fr); } .main { grid-template-columns:1fr; } .toolbar { width:100%; justify-content:space-between; } }
    @media(max-width:480px) { .wrap { width:min(100% - 22px,1220px); padding-top:22px; } .grid { gap:9px; } .card { padding:14px; border-radius:14px; } .metric .v { font-size:22px; } .pick-name { font-size:27px; } }
  </style>
</head>
<body><main class="wrap">
  <div class="top"><div><div class="sub">A-STOCK MULTI-AGENT RESEARCH</div><h1>明日精选投研看板</h1><div class="sub">行情、新闻、模型与回测结果集中展示</div></div><div class="toolbar"><label for="date">推荐日期</label><input id="date" type="date"><button id="refresh">刷新</button></div></div>
  <section class="grid"><div class="card metric"><div class="k">当前策略</div><div class="v" id="metric-model">—</div><div class="hint" id="metric-model-key">—</div></div><div class="card metric"><div class="k">回测胜率</div><div class="v" id="metric-win">—</div><div class="hint" id="metric-count">—</div></div><div class="card metric"><div class="k">已评估推荐</div><div class="v" id="metric-total">—</div><div class="hint">目标日已到期记录</div></div><div class="card metric"><div class="k">数据健康</div><div class="v" id="metric-health">—</div><div class="hint" id="metric-health-time">—</div></div></section>
  <section class="main"><article class="card"><div class="section-title"><h2>日期对应的精选股票</h2><span class="tag" id="pick-target">—</span></div><div id="pick-content"></div></article><aside class="card"><div class="section-title"><h2>选股策略</h2><span class="tag" id="active-strategy">—</span></div><div class="strategies" id="strategies"></div></aside></section>
  <section class="card news-card"><div class="section-title"><h2>新闻观察</h2><span class="muted" id="news-count">—</span></div><div class="news-summary" id="news-summary"></div><div class="news-list" id="news-list"></div></section>
  <section class="card news-card"><div class="section-title"><h2>多 Agent 协作记录</h2><span class="tag" id="workflow-status">LangGraph</span></div><div class="muted">数据获取 → 基本面分析 → 技术面分析 → 风控审查 → 决策汇总</div><div id="workflow-roles" class="strategies"></div><div id="workflow-research" class="muted"></div></section>
  <section class="card news-card"><div class="section-title"><h2>研报知识库</h2><span class="tag" id="research-status">正在检查 Milvus</span></div><p class="muted" id="research-stats">真实研报按股票代码和所选日期检索，保留来源。</p><div class="toolbar"><input id="research-query" aria-label="研报问题" placeholder="例如：公司盈利与主要风险"><button id="research-search">搜索研报</button><button id="research-sync">获取该股研报</button></div><div id="research-results" class="news-list"></div></section>
  <section class="card table-card"><div class="section-title"><h2>推荐历史与回测</h2><span class="muted" id="history-count">—</span></div><p class="muted">模拟收益＝（目标日最高价 ÷ 前一交易日收盘价 − 1）×100% − 1个百分点。收盘后判定；非实际成交收益，不保证能在最高价卖出。缺少有效数据时保持待回测。</p><div class="table-wrap"><table><thead><tr><th>推荐日期</th><th>代码</th><th>股票</th><th>策略</th><th>参考价</th><th>目标日</th><th>模拟收益</th><th>状态</th></tr></thead><tbody id="history"></tbody></table></div></section>
  <section class="main qa-grid"><article class="card"><div class="section-title"><h2>投研问答</h2><span class="tag" id="answer-source">本地规则</span></div><div class="muted">可以询问“为什么推荐这只股票？”、“有哪些主要风险？”或“当前因子如何影响评分？”</div><textarea id="question" placeholder="请输入关于当天推荐股票的问题…">为什么今天推荐这只股票？</textarea><div class="qa-actions"><span class="muted" id="answer-status">回答基于当前选择日期</span><button class="primary" id="ask">获取解释</button></div><div class="answer" id="answer">等待你的问题。</div></article><aside class="card"><div class="section-title"><h2>大模型连接配置</h2><span class="tag" id="llm-status">未配置</span></div><div class="config-form"><label class="switch"><input id="llm-enabled" type="checkbox"> 启用大模型回答</label><label class="switch"><input id="llm-agents" type="checkbox"> 同时启用各 Agent 大模型解释（不改变选股模型）</label><label>兼容接口地址<input id="llm-base" type="text" placeholder="https://api.openai.com/v1"></label><label>模型名称<input id="llm-model" type="text" placeholder="gpt-5.6-luna"></label><label>API Key<input id="llm-key" type="password" placeholder="留空则保持现有配置"></label><label>代理地址<input id="llm-proxy" type="text" placeholder="例如 http://127.0.0.1:7890，留空则直连"></label><button id="save-llm">保存连接配置</button><button id="test-llm">测试连接</button><div class="answer" id="llm-test-result">保存后可点击测试连接，确认是否真的连到大模型。</div><small>OpenAI 请填写 https://api.openai.com/v1（误填 platform.openai.com 时会自动纠正）；其他服务支持 OpenAI-compatible 的 /chat/completions 接口。密钥仅保存在本机 runtime/state，不会在页面回显。</small></div></aside></section>
  <footer>风险提示：本页面仅用于数据工程与量化研究，不构成投资建议。公开行情可能存在延迟，回测结果不代表未来收益。</footer>
</main><script>
const el = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = value => value === null || value === undefined || value === '' ? '—' : `${Number(value).toFixed(2)}%`;
const money = value => value === null || value === undefined || value === '' ? '—' : Number(value).toFixed(2);
const pickSnapshot = rec => { const basis = rec?.basis || {}; if (basis.source_trading_date) return `${basis.source_trading_date} 收盘`; const q = String(basis.quote_time || ''); return /^\d{14}$/.test(q) ? `${q.slice(0,4)}-${q.slice(4,6)}-${q.slice(6,8)} ${q.slice(8,10)}:${q.slice(10,12)}` : '未记录行情时间'; };
const num = value => { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : null; };
const signedPct = value => { const parsed = num(value); return parsed === null ? '—' : `${parsed > 0 ? '+' : ''}${parsed.toFixed(2)}%`; };
const valueTone = value => { const parsed = num(value); return parsed === null ? '' : (parsed < 0 ? 'down' : (parsed > 0 ? 'up' : '')); };
const shortTime = value => value ? String(value).replace('T',' ').slice(0,16) : '—';
function amountText(value) { const parsed = num(value); if (parsed === null) return '—'; return parsed >= 100000000 ? `${(parsed / 100000000).toFixed(2)} 亿元` : `${(parsed / 10000).toFixed(2)} 万元`; }
function levelText(value, positive, neutral, negative) { const parsed = num(value); if (parsed === null) return '暂无数据'; if (parsed > 0.6) return positive; if (parsed < -0.6) return negative; return neutral; }
function friendlyFactors(basis) {
  const rows = [];
  const push = (key, label, value, note, tone='') => { if (basis[key] !== undefined && basis[key] !== null && basis[key] !== '') rows.push({label, value, note, tone}); };
  push('change_pct', '当天涨跌', signedPct(basis.change_pct), '看股票当天价格是上涨还是下跌。', valueTone(basis.change_pct));
  push('amount', '成交金额', amountText(basis.amount), '金额越大，通常说明市场关注度和买卖活跃度越高。');
  push('volume_ratio', '量比', num(basis.volume_ratio) === null ? '—' : `${num(basis.volume_ratio).toFixed(2)} 倍`, '大于 1 表示当前成交比平时更活跃。');
  push('turnover_pct', '换手率', pct(basis.turnover_pct), '反映股票被买卖的活跃程度，过高也可能意味着波动较大。');
  push('pe_ratio', '市盈率', num(basis.pe_ratio) === null ? '—' : num(basis.pe_ratio).toFixed(2), '估值参考指标，越高通常代表市场预期更强，也可能更贵。');
  push('news_count', '相关新闻', `${Number(basis.news_count || 0)} 条`, '系统在新闻源中匹配到的相关消息数量。');
  push('news_sentiment', '新闻倾向', levelText(basis.news_sentiment, '偏正面', '中性或影响较小', '偏负面'), basis.news_factor_enabled === false ? '新闻因子已禁用，仅供阅读，不计入本次选股评分。' : `情绪分 ${num(basis.news_sentiment) === null ? '—' : num(basis.news_sentiment).toFixed(3)}，参与该历史版本评分。`);
  push('momentum_z', '短期走势强度', levelText(basis.momentum_z, '明显强于候选股票平均水平', '接近候选股票平均水平', '弱于候选股票平均水平'), '由涨跌表现换算而来，用来比较同一批股票里的相对强弱。');
  push('liquidity_z', '资金活跃度', levelText(basis.liquidity_z, '明显更活跃', '活跃度一般', '相对不活跃'), '由成交金额换算而来，用来判断买卖是否顺畅。');
  return rows;
}
function renderFactors(basis) {
  const factors = friendlyFactors(basis || {});
  if (!factors.length) return '<div class="empty">暂无可解释的选股指标。</div>';
  return factors.map(item => `<div class="factor-card"><div class="factor-label">${esc(item.label)}</div><div class="factor-value ${esc(item.tone || '')}">${esc(item.value)}</div><div class="factor-note">${esc(item.note)}</div></div>`).join('');
}
function newsClass(tone) { return tone === 'positive' ? 'good' : (tone === 'negative' ? 'bad' : 'neutral'); }
function renderNews(news) {
  const data = news || {items:[], counts:{}};
  const counts = data.counts || {};
  el('news-count').textContent = `${data.count || 0} 条新闻 · 展示 ${data.display_count || 0} 条`;
  el('news-summary').innerHTML = `<span class="news-pill good">利好 ${counts.positive || 0}</span><span class="news-pill bad">利空 ${counts.negative || 0}</span><span class="news-pill">中性 ${counts.neutral || 0}</span><span class="news-pill">更新 ${esc(shortTime(data.fetched_at))}</span>`;
  el('news-list').innerHTML = (data.items || []).map(item => {
    const cls = newsClass(item.tone);
    const href = item.url ? ` href="${esc(item.url)}" target="_blank" rel="noopener noreferrer"` : '';
    const summary = item.summary ? `<div class="news-summary-text">${esc(item.summary)}</div>` : '';
    return `<article class="news-item ${cls}"><div class="news-meta"><span class="news-tone ${cls}">${esc(item.tone_label || '中性')}</span><span>${esc(shortTime(item.published_at))}</span><span>${esc(item.source || '东方财富快讯')}</span><span>${esc(item.region || '')}</span></div><a class="news-title"${href}>${esc(item.title || '未命名新闻')}</a>${summary}</article>`;
  }).join('') || '<div class="empty">当前推荐股票暂无匹配新闻；这里只展示标题或摘要中包含该股票代码/名称的新闻。</div>';
}
let historyRows = [];
let scoreSort = null;
const scoreHeader = document.createElement('th');
scoreHeader.setAttribute('aria-sort', 'none');
scoreHeader.innerHTML = '<button type="button" id="sort-score" title="按模型评分排序；评分不是胜率">模型评分 ↕</button>';
document.querySelector('#history').closest('table').querySelector('thead tr').insertBefore(scoreHeader, document.querySelector('#history').closest('table').querySelector('thead tr').children[4]);
function scoreValue(value) {
  return value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);
}
function sortedHistory(rows, direction) {
  if (!direction) return [...rows];
  return [...rows].sort((a, b) => {
    const x = scoreValue(a.score), y = scoreValue(b.score);
    if (x === null) return y === null ? 0 : 1;
    if (y === null) return -1;
    return direction === 'desc' ? y - x : x - y;
  });
}
function renderHistory() {
  el('history').innerHTML = sortedHistory(historyRows, scoreSort).map(row => {
    const cls = row.return_pct === null ? 'pending' : (row.profitable ? 'positive' : 'negative');
    const status = row.return_pct === null ? '待回测' : (row.profitable ? '盈利' : '亏损');
    const score = scoreValue(row.score);
    return `<tr><td>${esc(row.date)}</td><td>${esc(row.code)}</td><td>${esc(row.name)}</td><td>${esc(row.model_label)}</td><td>${score === null ? '—' : score.toFixed(3)}</td><td>¥${money(row.reference_price)}</td><td>${esc(row.target_date)}</td><td class="${cls}">${row.return_pct === null ? '—' : pct(row.return_pct)}</td><td class="${cls}">${status}</td></tr>`;
  }).join('') || '<tr><td colspan="9" class="empty">暂无历史推荐。</td></tr>';
  el('sort-score').textContent = '模型评分 ' + (scoreSort === 'desc' ? '↓' : scoreSort === 'asc' ? '↑' : '↕');
  scoreHeader.setAttribute('aria-sort', scoreSort === 'desc' ? 'descending' : scoreSort === 'asc' ? 'ascending' : 'none');
}
el('sort-score').addEventListener('click', () => {
  scoreSort = scoreSort === null ? 'desc' : scoreSort === 'desc' ? 'asc' : null;
  renderHistory();
});
function render(data) {
  const rec = data.recommendation;
  const researchKey=`${data.selected_date}:${rec?.code || ''}`;
  if(el('research-results').dataset.context!==researchKey) { el('research-results').textContent=''; el('research-results').dataset.context=researchKey; }
  const bt = data.backtest;
  el('date').value = data.selected_date || '';
  el('metric-model').textContent = data.strategy.current_label || '—';
  el('metric-model-key').textContent = `${data.strategy.current_model || '—'} · 新闻因子已禁用`;
  el('metric-win').textContent = bt.win_rate === null ? '暂无' : pct(bt.win_rate);
  el('metric-count').textContent = bt.total ? `${bt.wins} 胜 / ${bt.losses} 负` : '暂无已到期回测';
  el('metric-total').textContent = bt.total;
  el('metric-health').textContent = data.health.degraded ? '降级' : (data.health.ok === true ? '正常' : (data.health.ok === false ? '异常' : '未运行'));
  el('metric-health-time').textContent = data.health.market_snapshot_at ? `行情 ${shortTime(data.health.market_snapshot_at)}` : (data.health.finished_at ? `更新于 ${shortTime(data.health.finished_at)}` : '—');
  el('metric-health').title=(data.health.failure_details || []).map(x=>`${x.agent}：${x.message}`).join('\n');
  el('active-strategy').textContent = data.strategy.current_label || '—';
  el('pick-target').textContent = rec ? `目标日 ${esc(rec.target_date)}` : '无推荐';
  el('pick-content').innerHTML = rec ? `<div class="pick"><div><div class="pick-code">${esc(rec.code)} · ${esc(rec.model_label)}</div><div class="pick-name">${esc(rec.name)}</div><div class="price">参考价 ¥${money(rec.reference_price)}</div><div class="snapshot">行情快照：${esc(pickSnapshot(rec))}</div></div><div class="score"><div class="muted">模型评分</div><strong>${Number(rec.score || 0).toFixed(3)}</strong></div></div><div class="factor-title"><strong>选股理由拆解</strong><span>把模型参数翻译成可理解的市场语言</span></div><div class="basis">${renderFactors(rec.basis)}</div>` : '<div class="empty">该日期暂无推荐记录，请选择其他日期。</div>';
  el('strategies').innerHTML = data.strategy.models.map(item => `<div class="strategy ${item.active === 'true' ? 'active' : ''}"><div class="strategy-head"><span>${esc(item.label)}</span>${item.active === 'true' ? '<span class="tag">当前</span>' : ''}</div><p>${esc(item.description)}</p></div>`).join('') || '<div class="empty">暂无策略配置。</div>';
  renderNews(data.news);
  const workflow = data.workflow || {};
  const roleEntries = Object.entries(workflow.roles || {});
  el('workflow-status').textContent = roleEntries.length ? (workflow.mode === 'snapshot_replay' ? '历史快照验证' : (workflow.ok ? '协作完成' : '任务未完成')) : '等待首次运行';
  el('workflow-roles').innerHTML = roleEntries.length ? roleEntries.map(([key, role]) => `<div class="strategy"><div class="strategy-head"><span>${esc(role.title)}</span><span class="tag">${esc(({success:'完成',failed:'失败',skipped:'跳过'})[role.status] || role.status)}</span></div><p>${esc(role.explanation?.text || role.message || (role.market_rows ? `行情 ${role.market_rows} 条` : '完成数据核验'))}</p></div>`).join('') : '<p class="muted">此历史推荐尚无多 Agent 协作记录，新流水线运行后会展示每个角色的分析和研报引用。</p>';
  const research = workflow.roles?.decision?.research || {};
  el('workflow-research').innerHTML = research.hits?.length ? research.hits.map(hit => `<p>研报引用：${esc(hit.title)} · ${esc(hit.source)}<br>${esc(hit.text)}</p>`).join('') : `研报检索：${esc(({disabled:'未启用 Milvus',empty_collection:'尚未导入研报',unavailable:'Milvus 暂不可用',not_requested:'本次未检索',ok:'未匹配到研报'})[research.status] || '暂无记录')}。财务与研报用于解释，选股评分沿用原模型。`;
  el('history-count').textContent = `${data.history.length} 条推荐记录`;
  historyRows = data.history || [];
  renderHistory();
}
async function loadResearchStatus() { try { const response=await fetch('/api/research-status',{cache:'no-store'}); const data=await response.json(); el('research-status').textContent=({running:'Milvus 已连接',disabled:'未启用',unavailable:'连接异常'})[data.status] || data.status; el('research-stats').textContent=data.status==='running' ? `已保存 ${data.chunks} 个研报片段 · ${data.embedding} · 按当前股票与日期检索` : '请检查研报服务配置或本机服务日志。'; } catch(error) { el('research-status').textContent='连接异常'; } }
async function queryResearch(sync) {
  const button=el(sync?'research-sync':'research-search'), requestedDate=el('date').value;
  button.disabled=true; el('research-results').textContent=sync?'正在获取真实研报并入库…':'正在检索…';
  try {
    const response=await fetch(sync?'/api/research-sync':'/api/research-search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:requestedDate,query:el('research-query').value})});
    const data=await response.json(); if(!response.ok) throw new Error(data.error || '研报请求失败');
    if(el('date').value!==requestedDate) return;
    el('research-results').innerHTML=data.hits?.length ? data.hits.map(hit=>{
      const url=String(hit.source || '');
      const source=/^https?:\/\//.test(url) ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看原文</a>` : esc(url);
      return `<article class="strategy"><strong>${esc(hit.title)}</strong><p>${esc(hit.text)}</p><small>${source} · 发布时间 ${esc(new Date(hit.published_at*1000).toLocaleString('zh-CN'))}</small></article>`;
    }).join('') : '<p class="muted">所选日期之前没有匹配的该股研报，不用其他股票的资料替代。</p>';
    await loadResearchStatus();
  } catch(error) { if(el('date').value===requestedDate) el('research-results').textContent=error.message; }
  finally { button.disabled=false; }
}
el('research-search').addEventListener('click',()=>queryResearch(false)); el('research-sync').addEventListener('click',()=>queryResearch(true)); loadResearchStatus();
async function load(date) { const query = date ? `?date=${encodeURIComponent(date)}` : ''; const response = await fetch(`/api/dashboard${query}`, {cache:'no-store'}); if (!response.ok) throw new Error('数据加载失败'); render(await response.json()); }
async function refreshAfterClose() { const button = el('refresh'); button.disabled = true; button.textContent = '刷新中…'; try { const response = await fetch('/api/refresh-backtest', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); const result = await response.json(); if (!response.ok && !result.skipped) throw new Error(result.error || result.message || '刷新失败'); await load(''); if (result.message) el('metric-health-time').textContent = result.message; } catch (error) { showError(error); } finally { button.disabled = false; button.textContent = '刷新'; } }
el('date').addEventListener('change', event => load(event.target.value).catch(showError)); el('refresh').addEventListener('click', () => refreshAfterClose());
function showError(error) { el('pick-content').innerHTML = `<div class="empty">${esc(error.message)}，请检查服务是否仍在运行。</div>`; }
async function loadLlmConfig() { const response = await fetch('/api/llm-config', {cache:'no-store'}); if (!response.ok) return; const config = await response.json(); el('llm-enabled').checked = !!config.enabled; el('llm-agents').checked = !!config.agent_explanations_enabled; el('llm-base').value = config.base_url || ''; el('llm-model').value = config.model || ''; el('llm-proxy').value = config.proxy_url || ''; el('llm-status').textContent = config.api_key_configured ? (config.last_test?.ok===false ? '上次连接失败' : (config.enabled ? '已启用' : '已配置')) : '未配置'; if(config.last_test?.message) el('llm-test-result').textContent=`上次测试：${config.last_test.message}`; }
async function askQuestion() { const question = el('question').value.trim(); if (!question) { el('answer').textContent = '请先输入问题。'; return; } el('ask').disabled = true; el('answer').className = 'answer loading'; el('answer').textContent = '正在分析当前推荐…'; try { const response = await fetch('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question, date:el('date').value})}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '问答失败'); el('answer-source').textContent = data.source === 'llm' ? `大模型：${data.model}` : data.source === 'fallback' ? '模型失败，已降级' : '本地规则'; el('answer').className = 'answer'; el('answer').textContent = data.answer; el('answer-status').textContent = `回答日期：${data.selected_date}`; } catch (error) { el('answer').className = 'answer'; el('answer').textContent = error.message; } finally { el('ask').disabled = false; } }
async function saveLlmConfig() { const button = el('save-llm'); button.disabled = true; try { const response = await fetch('/api/llm-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:el('llm-enabled').checked, agent_explanations_enabled:el('llm-agents').checked, base_url:el('llm-base').value, model:el('llm-model').value, api_key:el('llm-key').value, proxy_url:el('llm-proxy').value})}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '配置保存失败'); el('llm-key').value = ''; el('llm-base').value = data.base_url || el('llm-base').value; el('llm-status').textContent = data.api_key_configured ? (data.enabled ? '已启用' : '已配置') : '未配置'; el('llm-test-result').textContent = '配置已保存，请点击“测试连接”。'; } catch (error) { el('llm-status').textContent = error.message; } finally { button.disabled = false; } }
async function testLlmConfig() { const button = el('test-llm'); button.disabled = true; el('llm-test-result').textContent = '正在测试连接…'; try { const response = await fetch('/api/llm-test', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '测试失败'); el('llm-status').textContent = data.ok ? '连接成功' : '连接失败'; el('llm-test-result').textContent = `${data.status}：${data.message}${data.proxy_url ? `\n代理：${data.proxy_url}` : '\n代理：未配置，当前为直连'}`; } catch (error) { el('llm-status').textContent = '连接失败'; el('llm-test-result').textContent = error.message; } finally { button.disabled = false; } }
el('ask').addEventListener('click', () => askQuestion()); el('question').addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') askQuestion(); }); el('save-llm').addEventListener('click', () => saveLlmConfig()); el('test-llm').addEventListener('click', () => testLlmConfig());
load('').catch(showError); loadLlmConfig().catch(() => {});
setInterval(() => load(el('date').value).catch(() => {}), __DASHBOARD_REFRESH_MS__);
</script></body></html>'''


def page_html(settings: Settings) -> str:
    scheduler = scheduler_config(settings)
    refresh_seconds = int(scheduler.get("dashboard_refresh_seconds", 60))
    refresh_ms = max(10, refresh_seconds) * 1000
    return PAGE.replace("__DASHBOARD_REFRESH_MS__", str(refresh_ms))


class DashboardHandler(BaseHTTPRequestHandler):
    settings: Settings

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(page_html(self.settings).encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/dashboard":
            selected = parse_qs(parsed.query).get("date", [None])[0]
            payload = json.dumps(dashboard_data(self.settings, selected), ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/llm-config":
            payload = json.dumps(llm_public_config(self.settings), ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8")
            return
        if parsed.path == '/api/research-status':
            from .research import ResearchIndex
            index=ResearchIndex(self.settings)
            try:
                result=index.status()
            except Exception as exc:
                result={'status':'unavailable','error_type':type(exc).__name__}
            finally:
                index.close()
            self._send(json.dumps(result,ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8')
            return
        self._send(b"Not Found", "text/plain; charset=utf-8", 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            self._send(b'{"error":"request too large"}', "application/json; charset=utf-8", 413)
            return
        try:
            values = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(b'{"error":"invalid JSON"}', "application/json; charset=utf-8", 400)
            return
        if not isinstance(values, dict):
            self._send(b'{"error":"JSON object required"}', 'application/json; charset=utf-8',400)
            return
        if parsed.path in ('/api/research-sync','/api/research-search'):
            from .agent_tools import AgentTools
            from .contracts import iso_now
            selected=dashboard_data(self.settings,values.get('date'))
            rec=selected.get('recommendation')
            if not rec:
                self._send(json.dumps({'error':'请选择已有推荐记录的日期'},ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8',400)
                return
            as_of=min(datetime.fromisoformat(selected['selected_date']+'T23:59:59+08:00'),datetime.now().astimezone()).isoformat()
            tools=AgentTools(self.settings)
            try:
                synced=None
                if parsed.path=='/api/research-sync':
                    synced=tools.call('sync_research',{'code':rec['code'],'as_of':as_of})
                query=str(values.get('query') or f"{rec['name']} 盈利 估值 风险")
                result=tools.call('search_research',{'code':rec['code'],'as_of':as_of,'query':query})
                result.update(code=rec['code'],synced=synced,queried_at=iso_now())
                self._send(json.dumps(result,ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8')
            except Exception as exc:
                self._send(json.dumps({'error':'研报服务请求失败','error_type':type(exc).__name__},ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8',502)
            finally:
                tools.close()
            return
        if parsed.path == "/api/llm-config":
            try:
                payload = json.dumps(save_llm_config(self.settings, values), ensure_ascii=False).encode("utf-8")
                self._send(payload, "application/json; charset=utf-8")
            except (OSError, ValueError) as exc:
                self._send(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", 400)
            return
        if parsed.path == "/api/ask":
            question = str(values.get("question", "")).strip()
            if not question or len(question) > 2000:
                self._send(b'{"error":"question must be 1-2000 characters"}', "application/json; charset=utf-8", 400)
                return
            selected_date = values.get("date")
            data, context = _question_context(self.settings, str(selected_date) if selected_date else None)
            answer, source, model = answer_question(self.settings, question, context, _local_answer(question, data))
            payload = json.dumps({"answer": answer, "source": source, "model": model, "selected_date": data.get("selected_date")}, ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/llm-test":
            payload = json.dumps(test_llm_connection(self.settings), ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/refresh-backtest":
            try:
                result = refresh_backtest_after_close(self.settings)
                status = 200 if result.get("ok") or result.get("skipped") else 500
                payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self._send(payload, "application/json; charset=utf-8", status)
            except Exception as exc:
                self._send(
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                    500,
                )
            return
        self._send(b"Not Found", "text/plain; charset=utf-8", 404)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8001) -> None:
    if settings.values.get('research',{}).get('enabled'):
        import pymilvus  # Warm native dependencies before request threads are started.
    from .milvus_service import ensure_local_milvus
    try:
        milvus_status=ensure_local_milvus(settings)
        print(f"Milvus：{milvus_status['status']}")
    except Exception as exc:
        print(f"Milvus启动失败：{type(exc).__name__}；研报检索将在后续调用时重试")
    scheduler = PipelineScheduler(settings)
    scheduler.start()
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {"settings": settings})
    server = None
    selected_port = port
    for candidate in range(port, port + 10):
        try:
            server = ThreadingHTTPServer((host, candidate), handler)
            selected_port = candidate
            break
        except OSError as exc:
            if exc.errno not in (10048, 98):
                raise
    if server is None:
        raise OSError(f"端口 {port}-{port + 9} 都已被占用，请关闭旧服务或指定其他 --port")
    if selected_port != port:
        print(f"端口 {port} 已被占用，已自动改用 {selected_port}")
    print(f"A 股投研看板已启动：http://{host}:{selected_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        server.server_close()


class PipelineScheduler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        config = scheduler_config(settings)
        self.enabled = bool(config["enabled"])
        self.hour, self.minute = int(config["hour"]), int(config["minute"])
        self._stop = threading.Event()
        self._running = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            print("定时任务未启用")
            return
        self._thread = threading.Thread(target=self._loop, name="daily-pipeline-scheduler", daemon=True)
        self._thread.start()
        print(f"定时任务已启用：每个工作日 {self.hour:02d}:{self.minute:02d} 自动刷新数据并生成推荐")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            wait_seconds = _seconds_until_next_run(self.hour, self.minute)
            if self._stop.wait(wait_seconds):
                return
            self.run_once("scheduled_daily")

    def run_once(self, trigger: str) -> None:
        if not is_trading_weekday():
            print(f"定时任务跳过：周末休市（{trigger}）")
            return
        if not self._running.acquire(blocking=False):
            return
        try:
            print(f"定时任务开始运行：{trigger}")
            report = Supervisor(self.settings).run_pipeline()
            report["scheduler"] = {
                "enabled": self.enabled,
                "daily_run_time": f"{self.hour:02d}:{self.minute:02d}",
                "last_trigger": trigger,
                "last_run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "last_ok": bool(report.get("ok")),
                "last_degraded": bool(report.get("degraded")),
            }
            from .storage import atomic_write_json

            atomic_write_json(self.settings.runtime / "state" / "health.json", report)
            print(f"定时任务完成：{'成功' if report.get('ok') else '失败'}")
        except Exception as exc:
            print(f"定时任务失败：{exc}")
        finally:
            self._running.release()
