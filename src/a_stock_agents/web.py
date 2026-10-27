from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .auth_web import AccessControl,bootstrap
from .auth import tenant_settings
from .scheduler import scheduler_display
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


def monthly_statistics(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate existing results by recommendation month, never fill missing returns."""
    from decimal import Decimal, InvalidOperation
    months = {}
    for row in history:
        month = str(row.get("date", ""))[:7]
        if len(month) != 7:
            continue
        item = months.setdefault(month, {"month": month, "total": 0, "evaluated": 0,
            "wins": 0, "losses": 0, "flat": 0, "pending": 0, "returns": []})
        item["total"] += 1
        try:
            value = Decimal(str(row.get("return_pct")))
            if not value.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            item["pending"] += 1
            continue
        item["evaluated"] += 1
        item["wins" if value > 0 else "losses" if value < 0 else "flat"] += 1
        item["returns"].append(value)
    result = []
    for month in sorted(months, reverse=True):
        item = months[month]
        values = item.pop("returns")
        item["return_sum_pct"] = float(round(sum(values), 2)) if values else None
        item["average_return_pct"] = float(round(sum(values) / len(values), 2)) if values else None
        item["win_rate"] = round(item["wins"] / item["evaluated"] * 100, 2) if values else None
        result.append(item)
    return result


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
        "monthly_statistics": monthly_statistics(history),
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
            "scheduler": {**scheduler_config(settings), **read_json(settings.runtime / 'state' / 'scheduler_state.json', {}), **scheduler_display(scheduler_config(settings),read_json(settings.runtime / 'state' / 'scheduler_state.json', {}))},
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
    from .llm_context import question_evidence
    return data, question_evidence(data,news_lines)


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
    .table-card { margin-top:16px; } .table-wrap { overflow:auto; } table { width:100%; border-collapse:collapse; min-width:700px; } th,td { padding:13px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; } th { color:var(--muted); font-size:12px; font-weight:500; } td { font-size:13px; } .positive { color:var(--profit); } .negative { color:var(--loss); } .pending { color:var(--yellow); }
    .status { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; } .status span { padding:5px 8px; border-radius:999px; background:#0b182a; color:var(--muted); font-size:11px; } .status .ok { color:var(--cyan); }
    .qa-grid { margin-top:16px; } textarea { width:100%; min-height:96px; resize:vertical; color:var(--text); background:#0b182a; border:1px solid var(--line); border-radius:10px; padding:12px; font:inherit; line-height:1.55; } .qa-actions { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:10px; } .primary { background:linear-gradient(135deg,#1c807e,#2562a6); border-color:#55cfc9; font-weight:700; } .answer { white-space:pre-wrap; line-height:1.7; color:#dbe8f7; background:#0b182a; border:1px solid var(--line); border-radius:12px; padding:14px; margin-top:14px; min-height:66px; } .answer.loading { color:var(--muted); } .config-form { display:grid; gap:10px; } .config-form input[type=text], .config-form input[type=password] { width:100%; } .config-form small { color:var(--muted); line-height:1.45; } .switch { display:flex; align-items:center; gap:8px; color:#c8d6e9; font-size:13px; }
    .empty { color:var(--muted); padding:22px 0; } footer { color:var(--muted); font-size:12px; margin-top:22px; line-height:1.6; }
    @media(max-width:850px) { .top,.pick { align-items:flex-start; flex-direction:column; } .grid { grid-template-columns:repeat(2,1fr); } .main { grid-template-columns:1fr; } .toolbar { width:100%; justify-content:space-between; } }
    @media(max-width:480px) { .wrap { width:min(100% - 22px,1220px); padding-top:22px; } .grid { gap:9px; } .card { padding:14px; border-radius:14px; } .metric .v { font-size:22px; } .pick-name { font-size:27px; } }
    /* Compact controls and restrained motion: data remains the visual priority. */
    body {background:radial-gradient(ellipse at 12% 8%,#12344366,transparent 48%),radial-gradient(ellipse at 90% 0%,#213f6966,transparent 55%),#08111f;}
    .card {transition:border-color .22s,box-shadow .22s;position:relative;}
    .card:hover {border-color:#3c607b;box-shadow:0 14px 40px #02081270,0 0 24px #54d6c708;}
    .metric {overflow:hidden;border-top-color:#428b9b;}
    .metric::after {content:'';position:absolute;top:0;left:18px;right:18px;height:1px;background:linear-gradient(90deg,transparent,#54d6c7,transparent);}
    .metric .v,.score strong,.factor-value {font-variant-numeric:tabular-nums;}
    .score strong {text-shadow:0 0 24px #ffd47925;}
    button {transition:background .18s,border-color .18s,box-shadow .18s,transform .18s;}
    button:not(:disabled):hover {background:#1b3850;box-shadow:0 0 15px #54d6c718;transform:translateY(-1px);}
    button:disabled {opacity:.35;cursor:not-allowed;box-shadow:none;transform:none;}
    button:focus-visible,summary:focus-visible {outline:2px solid var(--cyan);outline-offset:3px;}
    .pagination.qa-actions {justify-content:flex-end;gap:12px;flex-wrap:wrap;padding-top:12px;border-top:1px solid var(--line);}
    .page-info {font-size:12px;font-variant-numeric:tabular-nums;}
    .page-buttons {display:inline-flex;align-items:center;gap:5px;padding:4px;border:1px solid #315071;border-radius:11px;background:#081524;}
    .page-buttons button {padding:6px 10px;font-size:12px;line-height:1.4;border-color:transparent;background:transparent;border-radius:7px;white-space:nowrap;}
    .page-buttons button:not(:disabled):hover {border-color:#54d6c766;background:#163b49;}
    tbody tr {transition:background .18s;}
    tbody tr:hover {background:#54d6c709;}
    .factor-card,.strategy {transition:border-color .2s,background .2s;}
    .factor-card:hover,.strategy:hover {border-color:#3d697b;background:#0e2033;}
    .page-enter {animation:page-enter .24s ease-out;}
    @keyframes page-enter {from{opacity:.35;transform:translateY(5px);}to{opacity:1;transform:translateY(0);}}
    @media(max-width:600px) {.pagination.qa-actions {justify-content:center;gap:8px;} .page-info {width:100%;text-align:center;}}
    @media(prefers-reduced-motion:reduce) {*,*::before,*::after {animation:none!important;transition:none!important;scroll-behavior:auto!important;}}
    .monitor {margin-bottom:18px;border:1px solid #346073;border-radius:14px;background:linear-gradient(110deg,#0a2531,#101f36);padding:14px 16px;overflow:hidden;}
    .monitor-head,.monitor-footer {display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:12px;}
    .monitor-label {display:flex;align-items:center;gap:9px;letter-spacing:1px;color:#a5fff0;}
    .signal {width:7px;height:7px;border-radius:50%;background:var(--cyan);box-shadow:0 0 10px var(--cyan);animation:signal-pulse 2s ease-in-out infinite;}
    .monitor[data-state="error"] .signal {background:var(--red);box-shadow:0 0 10px var(--red);animation:none;}
    .monitor-clock {color:#c9ddfa;font:12px Consolas,monospace;font-variant-numeric:tabular-nums;}
    .monitor-grid {display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:14px;}
    .monitor-item {padding:9px 12px;border-left:2px solid #397f8e;background:#07162480;font-size:12px;color:var(--muted);}
    .monitor-item strong {display:block;margin-top:5px;font-size:13px;font-weight:500;color:var(--text);}
    .sync-track {height:2px;background:#1b354c;margin-top:14px;overflow:hidden;}
    .sync-track span {display:block;width:100%;height:100%;transform-origin:left;background:linear-gradient(90deg,#2766a1,#54d6c7);transition:transform 1s linear;}
    .monitor[data-state="loading"] .sync-track span {animation:monitor-scan 1.3s ease-in-out infinite;}
    .monitor-footer {margin-top:9px;color:var(--muted);font-size:11px;}
    .monitor-toggle {padding:2px 7px;font-size:11px;background:transparent;}
    .value-flash {animation:value-flash .7s ease-out;}
    @keyframes signal-pulse {50%{opacity:.4;box-shadow:0 0 2px #54d6c7;}}
    @keyframes monitor-scan {0%{transform:translateX(-100%);}100%{transform:translateX(100%);}}
    @keyframes value-flash {0%{color:#fff;text-shadow:0 0 16px #54d6c7;}100%{text-shadow:none;}}
    body.motion-paused *,body.motion-paused *::before,body.motion-paused *::after {animation:none!important;transition:none!important;}
    @media(max-width:600px) {.monitor-grid {grid-template-columns:1fr;gap:6px;} .monitor-item{padding:6px 10px;}}
    @media(prefers-reduced-motion:reduce) {.signal,.value-flash,.sync-track span {animation:none!important;transition:none!important;}}
    .model-picker {position:relative;display:inline-block;margin-left:auto;}
    .model-trigger {padding:8px 12px;border-radius:10px;background:#173247;min-width:140px;display:flex;gap:14px;align-items:center;justify-content:space-between;}
    .model-menu {position:absolute;bottom:calc(100% + 8px);right:0;width:min(310px,calc(100vw - 60px));z-index:20;background:#101f33;border:1px solid #365470;border-radius:14px;box-shadow:0 15px 45px #0008;padding:6px;}
    .model-menu[hidden] {display:none;}
    .model-options {max-height:260px;overflow:auto;display:grid;gap:3px;}
    .model-option {display:flex;align-items:center;gap:9px;text-align:left;border-color:transparent;background:transparent;padding:10px;min-width:0;}
    .model-option[aria-checked="true"] {background:#204054;color:var(--cyan);}
    .model-icon {display:grid;place-items:center;width:25px;height:25px;border-radius:7px;background:#284157;font-weight:700;flex-shrink:0;}
    .model-description {min-width:0;flex:1;overflow:hidden;}
    .model-description strong,.model-description small {display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .model-description strong {font-size:13px;font-weight:500;}.model-description small {font-size:10px;color:var(--muted);margin-top:3px;}
    .model-add {display:block;width:100%;text-align:left;background:transparent;border:0;border-top:1px solid var(--line);border-radius:0;padding:11px 10px;margin-top:5px;font-size:13px;}
    .model-picker-note {font-size:11px;color:var(--muted);padding:6px 10px;}
    .wrap {width:min(1440px,calc(100% - 32px));padding:24px 0 32px;}
    .top {margin-bottom:16px;gap:16px;} h1 {font-size:clamp(26px,3vw,38px);margin-bottom:5px;}
    .card {padding:16px;border-radius:14px;min-width:0;}
    .section-title {margin-bottom:12px;} .main {gap:12px;margin-top:12px;align-items:start;grid-template-columns:minmax(0,2fr) minmax(260px,1fr);}
    .grid {gap:12px;}.metric .v{font-size:26px;margin-top:5px;}
    .news-card,.table-card {margin-top:12px;}
    .monitor {padding:12px 14px;margin-bottom:12px;}.monitor-grid{margin-top:10px;gap:9px;}
    .pick-name {font-size:29px;}.price{font-size:22px;}.factor-title{margin-top:15px;padding-top:13px;}
    .basis {grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;}.factor-card{min-height:0;padding:11px;}.factor-card .factor-value{font-size:19px;}
    .qa-grid {grid-template-columns:minmax(0,1fr);gap:10px;}
    .qa-grid textarea {min-height:76px;margin-top:10px;}
    .qa-grid article>.muted {font-size:12px;}
    .qa-grid .qa-actions{flex-wrap:wrap;gap:8px;}.qa-grid #answer-status{font-size:12px;}
    .qa-grid #answer {min-height:48px;margin-top:12px;padding:12px;max-height:440px;overflow:auto;}
    .model-settings {padding:12px 16px;}
    .model-settings summary {cursor:pointer;list-style:none;gap:12px;margin-bottom:0;}
    .model-settings summary::-webkit-details-marker{display:none;}.model-settings h2{font-size:14px;}
    .model-settings h2::before{content:'▸';margin-right:8px;}.model-settings details[open] h2::before{content:'▾';}
    .settings-hint{font-size:11px;color:var(--muted);margin-left:10px;font-weight:400;}
    .model-settings details[open] summary{margin-bottom:14px;}
    .model-settings .config-form{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 14px;}
    .model-settings .config-form>.switch,.model-settings .config-form>.answer,.model-settings .config-form>small{grid-column:1/-1;}
    .model-settings .config-form>.answer{margin-top:0;min-height:0;font-size:12px;max-height:120px;overflow:auto;}
    .model-settings .config-form>label:not(.switch){display:grid;gap:5px;}
    @media(max-width:900px){.main{grid-template-columns:minmax(0,1fr);}.basis{grid-template-columns:repeat(3,minmax(0,1fr));}}
    @media(max-width:600px){.wrap{width:calc(100% - 20px);padding-top:16px;}.card{padding:13px;}.basis{grid-template-columns:repeat(2,minmax(0,1fr));}.model-settings .config-form{grid-template-columns:1fr;}.settings-hint{display:none;}}
    /* Keep the recommendation and strategy in independent full-width rows. */
    .main.featured-layout {grid-template-columns:minmax(0,1fr);align-items:start;}
    .featured-layout>.card {height:auto;min-height:0;align-self:start;}
    .featured-layout .basis {grid-template-columns:repeat(4,minmax(0,1fr));align-items:start;}
    .featured-layout .factor-card {min-height:0;height:auto;}
    .featured-layout #strategies {grid-template-columns:repeat(3,minmax(0,1fr));align-items:start;gap:10px;}
    .featured-layout #strategies .strategy {height:auto;min-height:0;}
    @media(max-width:1000px) {.featured-layout .basis {grid-template-columns:repeat(3,minmax(0,1fr));}}
    @media(max-width:700px) {.featured-layout .basis {grid-template-columns:repeat(2,minmax(0,1fr));}.featured-layout #strategies {grid-template-columns:1fr;}}
    /* Large-screen research command center, driven only by saved observations. */
    :root {--bg:#070e2b;--panel:#101e48;--line:#20456b;--cyan:#30e5e0;--blue:#459cff;}
    body {background:radial-gradient(ellipse at 50% 0,#142d56 0,#080f2c 60%);}
    .wrap {width:calc(100% - 32px);max-width:1920px;padding:18px 0 28px;display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px;}
    .wrap>*,.featured-layout>*{min-width:0;}
    .top {grid-column:1/-1;position:relative;align-items:center;margin:0;padding:15px 18px;border-top:2px solid #30e5e0;background:linear-gradient(90deg,#0e275077,#183666aa,#0e275077);box-shadow:0 -4px 20px #30e5e012;}
    .top::after {content:'';position:absolute;bottom:0;left:25%;right:25%;height:2px;background:linear-gradient(90deg,transparent,#30e5e0,transparent);}
    .top h1 {color:#a7fffb;font-size:clamp(24px,2.5vw,36px);letter-spacing:3px;text-shadow:0 0 20px #30e5e044;}
    .top .sub {font-size:11px;letter-spacing:2px;}.toolbar{background:#0b1937;border-color:#265272;}
    .monitor,.grid {grid-column:1/-1;margin:0;}.monitor{border-radius:2px;padding:10px 14px;background:#0b1c3c;border-color:#25617a;}
    .monitor-grid{margin-top:8px;}.monitor-item{padding:5px 9px;}.sync-track{margin-top:9px;}
    .card {border-radius:3px;background:linear-gradient(125deg,#12254be6,#101b3ee6);border:1px solid #24516c;box-shadow:inset 0 0 20px #0b385b20,0 4px 12px #0003;padding:14px;position:relative;}
    .card::before {content:'';position:absolute;left:-1px;top:-1px;width:22px;height:16px;border-left:2px solid #30e5e0;border-top:2px solid #30e5e0;pointer-events:none;}
    .card::after {content:'';position:absolute;right:-1px;bottom:-1px;width:18px;height:12px;border-right:2px solid #268fba;border-bottom:2px solid #268fba;pointer-events:none;}
    .metric {padding:12px 16px;}.metric .v{font:700 30px Consolas,'Microsoft YaHei',monospace;letter-spacing:1px;color:#c3ffff;}.metric::after{left:auto;top:auto;}
    .section-title h2 {font-size:15px;letter-spacing:1px;border-left:3px solid #30e5e0;padding-left:9px;}
    .main.featured-layout {display:contents;}.featured-layout>article{grid-column:span 8;}.featured-layout>aside{grid-column:span 4;}
    .featured-layout .basis{grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;}.factor-card{border-radius:3px;padding:9px;background:#0a1935;}.factor-card .factor-note{font-size:11px;}.factor-card .factor-value{font-size:18px;}
    .featured-layout #strategies {grid-template-columns:1fr;gap:8px;}.strategy{border-radius:3px;padding:11px;background:#0b1b39;}.strategy p{margin-top:5px;}.pick-name{font-size:28px;}
    .board-charts{grid-column:1/-1;display:grid;grid-template-columns:1fr 1.5fr 1.5fr;gap:12px;}
    .chart-panel {min-height:230px;}.chart-title{font-size:14px;color:#b0ffff;border-left:3px solid #30e5e0;padding-left:9px;margin:0 0 13px;letter-spacing:1px;}
    .ring-wrap{display:flex;align-items:center;justify-content:center;gap:20px;min-height:150px;}.win-ring{width:126px;height:126px;border-radius:50%;display:grid;place-items:center;position:relative;box-shadow:0 0 20px #30e5e012;}.win-ring::before{content:'';position:absolute;inset:12px;border-radius:50%;background:#101f43;}.ring-value{position:relative;text-align:center;}.ring-value strong{display:block;color:#70fff0;font:700 23px Consolas,monospace;}.ring-value small{font-size:10px;color:var(--muted);}.ring-legend{font-size:12px;line-height:2;color:#b1c9e4;}.chart-caption{font-size:10px;color:var(--muted);margin-top:7px;line-height:1.5;}
    .data-chart{width:100%;height:155px;display:block;}.data-chart text{font-size:10px;fill:#98b7d1;}.data-chart line{stroke:#2b4763;stroke-width:1;}.chart-empty{display:grid;place-items:center;height:155px;color:var(--muted);font-size:12px;}
    .news-panel,.research-panel {grid-column:span 6;margin:0;}.news-panel .news-list{max-height:240px;}.news-panel .empty{padding:18px;font-size:12px;}.research-panel .toolbar{flex-wrap:wrap;padding:8px;}.research-panel input{flex:1;min-width:100px;}.research-panel .news-list{max-height:240px;}
    .workflow-panel,.table-card,.qa-grid{grid-column:1/-1;margin:0;}.qa-grid{display:grid;grid-template-columns:1fr;gap:10px;}.qa-grid article,.model-settings{min-width:0;}
    .table-card th,.table-card td{padding:10px 9px;font-size:12px;}.table-card tr:hover{background:#30e5e00a;}.table-card>.muted{font-size:11px;line-height:1.5;}
    .wrap>footer{grid-column:1/-1;}.tag{border-radius:4px;font-size:11px;padding:4px 7px;}
    @media(max-width:1150px){.featured-layout>article{grid-column:span 7;}.featured-layout>aside{grid-column:span 5;}.featured-layout .basis{grid-template-columns:repeat(2,minmax(0,1fr));}.board-charts{grid-template-columns:repeat(2,minmax(0,1fr));}.board-charts>:last-child{grid-column:1/-1;}}
    @media(max-width:750px){.wrap{width:calc(100% - 20px);gap:10px;}.featured-layout>article,.featured-layout>aside,.news-panel,.research-panel{grid-column:1/-1;}.board-charts{grid-template-columns:1fr;}.board-charts>:last-child{grid-column:auto;}.top{padding:12px;}.top h1{letter-spacing:1px;font-size:24px;}}
    .model-launcher {display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;}
    .model-launcher-actions{display:flex;align-items:center;gap:8px;}.model-launcher h2{font-size:14px;}
    .model-dialog {width:min(700px,calc(100vw - 28px));max-height:calc(100dvh - 40px);overflow:auto;margin:auto;border:1px solid #387d96;border-radius:12px;background:linear-gradient(145deg,#142b4b,#0b1831);color:var(--text);padding:22px;box-shadow:0 30px 100px #0009;}
    .model-dialog::backdrop {background:#020a1bcc;backdrop-filter:blur(5px);}
    .dialog-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;}.dialog-header h2{font-size:18px;color:#b3fffa;}.dialog-close{padding:5px 10px;font-size:20px;line-height:1;background:transparent;}
    .dialog-help{font-size:12px;color:var(--muted);margin:0 0 16px;line-height:1.6;}.dialog-cancel{width:100%;margin-top:12px;background:transparent;}
    .model-dialog .config-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;}.model-dialog .config-form>label:not(.switch){display:grid;gap:6px;}
    .model-dialog .config-form>.switch,.model-dialog .config-form>.answer,.model-dialog .config-form>small{grid-column:1/-1;}
    .model-dialog .answer{font-size:12px;min-height:0;max-height:140px;overflow:auto;margin-top:0;}
    @media(max-width:600px){.model-dialog{padding:16px;}.model-dialog .config-form{grid-template-columns:1fr;}}
    /* Motion layer: ambient decoration, never simulated price ticks. */
    body::before {content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;background-image:linear-gradient(#30e5e006 1px,transparent 1px),linear-gradient(90deg,#30e5e006 1px,transparent 1px);background-size:56px 56px;animation:grid-drift 32s linear infinite;}
    .top::after {animation:header-flow 5s ease-in-out infinite;}
    .card {animation:panel-breathe 7s ease-in-out infinite;}
    .card:nth-child(2n) {animation-delay:-2s;}.card:nth-child(3n){animation-delay:-4s;}
    .card::before {animation:corner-glow 4.5s ease-in-out infinite;}.card::after{animation:corner-glow 4.5s ease-in-out -2s infinite;}
    .win-ring::after {content:'';position:absolute;inset:-7px;border-radius:50%;border:1px dashed #30e5e06b;border-top:2px solid #81fff6;border-bottom-color:transparent;animation:ring-orbit 16s linear infinite;pointer-events:none;}
    .chart-panel {overflow:hidden;}
    .chart-panel::after {width:35%;height:100%;inset:0 auto auto -40%;border:0;background:linear-gradient(100deg,transparent,#40dfed0b,transparent);animation:chart-sweep 9s linear infinite;}
    .data-chart rect {transform-box:fill-box;transform-origin:center bottom;}
    .chart-enter .data-chart rect {animation:bar-draw .9s cubic-bezier(.2,.75,.25,1) both;}
    .chart-enter .data-chart rect:nth-of-type(2n){animation-delay:.08s;}.chart-enter .data-chart rect:nth-of-type(3n){animation-delay:.16s;}
    .chart-enter .win-ring {animation:ring-arrive 1.1s ease-out both;}
    .monitor-label .signal {animation:signal-pulse 2s ease-in-out infinite;}
    @keyframes grid-drift {to{background-position:56px 56px;}}
    @keyframes header-flow {0%,100%{opacity:.5;transform:scaleX(.7);}50%{opacity:1;transform:scaleX(1.35);}}
    @keyframes panel-breathe {0%,100%{box-shadow:inset 0 0 18px #0b385b20,0 0 5px #30e5e005;}50%{box-shadow:inset 0 0 28px #15567830,0 0 12px #30e5e015;}}
    @keyframes corner-glow {0%,100%{opacity:.45;}50%{opacity:1;filter:drop-shadow(0 0 3px #30e5e0);}}
    @keyframes ring-orbit {to{transform:rotate(360deg);}}
    @keyframes chart-sweep {to{transform:translateX(420%);}}
    @keyframes bar-draw {from{transform:scaleY(0);opacity:.2;}to{transform:scaleY(1);opacity:1;}}
    @keyframes ring-arrive {from{transform:scale(.88);opacity:.2;}to{transform:scale(1);opacity:1;}}
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;}}
    /* Equal factor tiles and a strategy column that fills the recommendation row. */
    .featured-layout .basis {align-items:stretch;grid-auto-rows:1fr;}
    .featured-layout .factor-card {height:100%;min-height:132px;display:flex;flex-direction:column;}
    .featured-layout .factor-card .factor-label {flex-shrink:0;}
    .featured-layout .factor-card .factor-value {overflow-wrap:anywhere;}
    .featured-layout .factor-card .factor-note {margin-top:auto;padding-top:7px;}
    .main.featured-layout>.card {align-self:stretch;}
    .featured-layout>aside {display:flex;flex-direction:column;}
    .featured-layout>aside>.section-title {flex-shrink:0;}
    .featured-layout #strategies {flex:1;grid-template-rows:repeat(3,minmax(0,1fr));align-items:stretch;}
    .featured-layout #strategies .strategy {height:100%;display:flex;flex-direction:column;justify-content:center;}
    @media(max-width:750px){.main.featured-layout>.card{align-self:start;}.featured-layout #strategies{flex:initial;grid-template-rows:none;grid-auto-rows:1fr;}.featured-layout .factor-card{min-height:132px;}}
  </style>
</head>
<body><main class="wrap">
  <div class="top"><div><div class="sub">A-STOCK MULTI-AGENT RESEARCH</div><h1>明日精选投研看板</h1><div class="sub">行情、新闻、模型与回测结果集中展示</div></div><div class="toolbar"><label for="date">推荐日期</label><input id="date" type="date"><button id="refresh">刷新</button></div></div>
  <section class="grid"><div class="card metric"><div class="k">当前策略</div><div class="v" id="metric-model">—</div><div class="hint" id="metric-model-key">—</div></div><div class="card metric"><div class="k">回测胜率</div><div class="v" id="metric-win">—</div><div class="hint" id="metric-count">—</div></div><div class="card metric"><div class="k">已评估推荐</div><div class="v" id="metric-total">—</div><div class="hint">目标日已到期记录</div></div><div class="card metric"><div class="k">数据健康</div><div class="v" id="metric-health">—</div><div class="hint" id="metric-health-time">—</div></div></section>
  <section class="main featured-layout"><article class="card"><div class="section-title"><h2>日期对应的精选股票</h2><span class="tag" id="pick-target">—</span></div><div id="pick-content"></div></article><aside class="card"><div class="section-title"><h2>选股策略</h2><span class="tag" id="active-strategy">—</span></div><div class="strategies" id="strategies"></div></aside></section>
  <section class="card news-card"><div class="section-title"><h2>新闻观察</h2><span class="muted" id="news-count">—</span></div><div class="news-summary" id="news-summary"></div><div class="news-list" id="news-list"></div></section>
  <style>.workflow-fold > summary {cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:12px;list-style:none;} .workflow-fold > summary::-webkit-details-marker {display:none;} .workflow-fold > summary h2::before {content:'▸';display:inline-block;margin-right:8px;} .workflow-fold[open] > summary h2::before {content:'▾';} .workflow-fold[open] > summary {margin-bottom:16px;} .workflow-fold > summary:focus-visible {outline:2px solid var(--cyan);outline-offset:6px;border-radius:4px;} .workflow-fold .fold-hint {font-size:12px;color:var(--muted);margin-left:10px;} .workflow-fold[open] .fold-hint {display:none;}</style>
  <section class="card news-card"><details class="workflow-fold" id="workflow-details"><summary><h2>多 Agent 协作记录<span class="fold-hint">点击展开</span></h2><span class="tag" id="workflow-status">LangGraph</span></summary><div class="muted">数据获取 → 基本面分析 → 技术面分析 → 风控审查 → 决策汇总</div><div id="workflow-roles" class="strategies"></div><div id="workflow-research" class="muted"></div></details></section>
  <section class="card news-card"><div class="section-title"><h2>研报知识库</h2><span class="tag" id="research-status">正在检查 Milvus</span></div><p class="muted" id="research-stats">真实研报按股票代码和所选日期检索，保留来源。</p><div class="toolbar"><input id="research-query" aria-label="研报问题" placeholder="例如：公司盈利与主要风险"><button id="research-search">搜索研报</button><button id="research-sync">获取该股研报</button></div><div id="research-results" class="news-list"></div></section>
  <section class="card table-card"><div class="section-title"><h2>推荐历史与回测</h2><span class="muted" id="history-count">—</span></div><p class="muted">模拟收益＝（目标日最高价 ÷ 前一交易日收盘价 − 1）×100% − 1个百分点。收盘后判定；非实际成交收益，不保证能在最高价卖出。缺少有效数据时保持待回测。</p><div class="table-wrap"><table><thead><tr><th>推荐日期</th><th>代码</th><th>股票</th><th>策略</th><th>参考价</th><th>目标日</th><th>模拟收益</th><th>状态</th></tr></thead><tbody id="history"></tbody></table></div></section>
  <section class="main qa-grid"><article class="card"><div class="section-title"><h2>投研问答</h2><span class="tag" id="answer-source">本地规则</span></div><div class="muted">可以询问“为什么推荐这只股票？”、“有哪些主要风险？”或“当前因子如何影响评分？”</div><textarea id="question" placeholder="请输入关于当天推荐股票的问题…">为什么今天推荐这只股票？</textarea><div class="qa-actions"><span class="muted" id="answer-status">回答基于当前选择日期</span><button class="primary" id="ask">获取解释</button></div><div class="answer" id="answer">等待你的问题。</div></article><aside class="card model-settings"><details id="model-settings"><summary class="section-title"><h2>模型连接设置<span class="settings-hint">按需展开配置</span></h2><span class="tag" id="llm-status">未配置</span></summary><div class="config-form"><label class="switch"><input id="llm-enabled" type="checkbox"> 启用大模型回答</label><label class="switch"><input id="llm-agents" type="checkbox"> 同时启用各 Agent 大模型解释（不改变选股模型）</label><label>兼容接口地址<input id="llm-base" type="text" placeholder="https://api.openai.com/v1"></label><label>模型名称<input id="llm-model" type="text" placeholder="gpt-5.6-luna"></label><label>API Key<input id="llm-key" type="password" placeholder="留空则保持现有配置"></label><label>代理地址<input id="llm-proxy" type="text" placeholder="例如 http://127.0.0.1:7890，留空则直连"></label><button id="save-llm">保存连接配置</button><button id="test-llm">测试连接</button><div class="answer" id="llm-test-result">保存后可点击测试连接，确认是否真的连到大模型。</div><small>OpenAI 请填写 https://api.openai.com/v1（误填 platform.openai.com 时会自动纠正）；其他服务支持 OpenAI-compatible 的 /chat/completions 接口。密钥仅保存在本机 runtime/state，不会在页面回显。</small></div></details></aside></section>
  <footer>风险提示：本页面仅用于数据工程与量化研究，不构成投资建议。公开行情可能存在延迟，回测结果不代表未来收益。</footer>
</main><script>
const el = id => document.getElementById(id);
el('news-list').closest('section').classList.add('news-panel');
el('research-results').closest('section').classList.add('research-panel');
el('workflow-details').closest('section').classList.add('workflow-panel');
el('research-results').closest('section').after(el('workflow-details').closest('section'));
document.querySelector('.top h1').textContent='A股多 Agent 投研监控中心';
document.querySelector('.featured-layout').insertAdjacentHTML('afterend',`<section class="board-charts" aria-label="回测数据可视化"><article class="card chart-panel"><h2 class="chart-title">已回测胜负分布</h2><div id="board-win"></div><div class="chart-caption">基于已完成回测；待回测记录不计入胜率。</div></article><article class="card chart-panel"><h2 class="chart-title">月度模拟盈亏</h2><div id="board-month"></div><div class="chart-caption">每笔模拟收益百分比相加 · 非复利、非实际账户收益</div></article><article class="card chart-panel"><h2 class="chart-title">最近10笔已回测收益</h2><div id="board-recent"></div><div class="chart-caption">日期为推荐日期 · 红色盈利 / 绿色亏损</div></article></section>`);
function drawReturnBars(id,rows){
  if(!rows.length){el(id).innerHTML='<div class="chart-empty">暂无已完成回测数据</div>';return;}
  const values=rows.map(row=>Number(row.value)), min=Math.min(0,...values),max=Math.max(0,...values),range=max-min||1;
  const y=value=>116-(value-min)/range*92,zero=y(0),width=360,slot=320/rows.length;
  el(id).innerHTML=`<svg class="data-chart" viewBox="0 0 ${width} 155" role="img" aria-label="${esc(rows.map(row=>row.label+' '+Number(row.value).toFixed(2)+'%').join('，'))}"><line x1="24" y1="${zero}" x2="350" y2="${zero}"/><text x="2" y="${zero+4}">0</text>${rows.map((row,index)=>{const value=Number(row.value),x=30+index*slot,w=Math.min(34,slot*.6),top=Math.min(y(value),zero),height=Math.max(1,Math.abs(y(value)-zero));return `<rect x="${x}" y="${top}" width="${w}" height="${height}" rx="1" fill="${value<0?'var(--loss)':value>0?'var(--profit)':'var(--muted)'}"><title>${esc(row.label)}：${value.toFixed(2)}%</title></rect><text x="${x+w/2}" y="${value<0?Math.min(131,top+height+11):Math.max(11,top-5)}" text-anchor="middle">${value.toFixed(1)}</text><text x="${x+w/2}" y="149" text-anchor="middle">${esc(row.label)}</text>`;}).join('')}</svg>`;
}
function renderBoardCharts(data){
  const chartSignature=JSON.stringify([data.backtest?.total,data.backtest?.wins,data.monthly_statistics,(data.history||[]).map(row=>[row.date,row.return_pct])]);
  if(renderBoardCharts.signature===chartSignature)return;
  renderBoardCharts.signature=chartSignature;
  const bt=data.backtest,total=Number(bt.total)||0,wins=Number(bt.wins)||0;
  el('board-win').innerHTML=total?`<div class="ring-wrap"><div class="win-ring" style="background:conic-gradient(var(--profit) ${wins/total*360}deg,var(--loss) 0)"><div class="ring-value"><strong>${Number(bt.win_rate).toFixed(1)}%</strong><small>回测胜率</small></div></div><div class="ring-legend"><span class="positive">盈利 ${wins} 笔</span><br><span class="negative">亏损 ${Number(bt.losses)||0} 笔</span><br>已回测 ${total} 笔</div></div>`:'<div class="chart-empty">暂无已完成回测</div>';
  drawReturnBars('board-month',(data.monthly_statistics||[]).filter(row=>row.return_sum_pct!==null).slice(0,6).reverse().map(row=>({label:row.month.slice(2),value:row.return_sum_pct})));
  drawReturnBars('board-recent',[...(data.history||[])].filter(row=>row.return_pct!==null&&Number.isFinite(Number(row.return_pct))).sort((a,b)=>b.date.localeCompare(a.date)).slice(0,10).reverse().map(row=>({label:row.date.slice(5),value:row.return_pct})));
  document.querySelectorAll('.chart-panel').forEach(panel=>{panel.classList.remove('chart-enter');if(panel.getBoundingClientRect().top<innerHeight&&panel.getBoundingClientRect().bottom>0)requestAnimationFrame(()=>panel.classList.add('chart-enter'));});
}
let addingModel=false, switchingModel=false;
const settingsPanel=document.querySelector('.model-settings');
const settingsForm=settingsPanel.querySelector('.config-form');
const settingsStatus=el('llm-status');
const modelDialog=document.createElement('dialog');modelDialog.id='model-dialog';modelDialog.className='model-dialog';modelDialog.setAttribute('aria-labelledby','model-dialog-title');
modelDialog.innerHTML='<div class="dialog-header"><h2 id="model-dialog-title">添加模型</h2><button type="button" id="model-dialog-close" class="dialog-close" aria-label="关闭模型设置">×</button></div><p class="dialog-help">连接配置仅保存在本机。添加后可从问答区的模型菜单切换，不改变选股模型。</p>';
modelDialog.appendChild(settingsForm);modelDialog.insertAdjacentHTML('beforeend','<button type="button" class="dialog-cancel" id="model-dialog-cancel">取消</button>');document.body.appendChild(modelDialog);
settingsPanel.innerHTML='<div class="model-launcher"><h2>模型管理</h2><div class="model-launcher-actions"><span id="model-status-slot"></span><button type="button" id="model-edit">配置当前模型</button><button type="button" id="model-new" class="primary">＋ 添加模型</button></div></div>';
el('model-status-slot').appendChild(settingsStatus);
function closeModelDialog(){modelDialog.close();el('llm-key').value='';}
el('model-dialog-close').addEventListener('click',closeModelDialog);el('model-dialog-cancel').addEventListener('click',closeModelDialog);
modelDialog.addEventListener('cancel',()=>{el('llm-key').value='';});
modelDialog.addEventListener('click',event=>{if(event.target===modelDialog){const box=modelDialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)closeModelDialog();}});
async function openModelDialog(add){
  if(modelDialog.open)return;
  closeModelPicker();await loadLlmConfig();addingModel=add;el('llm-key').value='';
  el('model-dialog-title').textContent=add?'添加模型':'配置当前模型';
  el('save-llm').textContent=add?'添加并保存':'保存修改';
  el('llm-key').placeholder=add?'填写新服务的 API Key':'留空则保持现有配置';
  if(add){for(const id of ['llm-base','llm-model','llm-proxy'])el(id).value='';el('llm-test-result').textContent='填写接口地址、模型名和密钥后保存。新模型不会沿用其他模型的密钥。';}
  modelDialog.showModal();el('llm-base').focus();
}
el('model-new').addEventListener('click',()=>openModelDialog(true).catch(error=>{el('llm-status').textContent=error.message;}));
el('model-edit').addEventListener('click',()=>openModelDialog(false).catch(error=>{el('llm-status').textContent=error.message;}));
el('ask').insertAdjacentHTML('beforebegin',`<div class="model-picker" id="model-picker"><button type="button" id="model-trigger" class="model-trigger" aria-haspopup="menu" aria-expanded="false" aria-controls="model-menu"><span id="model-current">选择模型</span><span aria-hidden="true">⌃</span></button><div id="model-menu" class="model-menu" hidden><div class="model-picker-note">已保存的模型连接</div><div class="model-options" id="model-options" role="menu" aria-label="选择大模型"></div><button class="model-add" type="button" id="model-add">＋ 添加模型</button><div class="model-picker-note" id="model-picker-message" role="status"></div></div></div>`);
function closeModelPicker(){el('model-menu').hidden=true;el('model-trigger').setAttribute('aria-expanded','false');}
el('model-trigger').addEventListener('click',()=>{const open=el('model-menu').hidden;el('model-menu').hidden=!open;el('model-trigger').setAttribute('aria-expanded',String(open));if(open)el('model-options').querySelector('button')?.focus();});
document.addEventListener('click',event=>{if(!el('model-picker').contains(event.target))closeModelPicker();});
el('model-picker').addEventListener('keydown',event=>{if(event.key==='Escape'){closeModelPicker();el('model-trigger').focus();}if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){const options=Array.from(el('model-options').querySelectorAll('button'));if(!options.length)return;event.preventDefault();const index=options.indexOf(document.activeElement);options[event.key==='Home'?0:event.key==='End'?options.length-1:(index+(event.key==='ArrowDown'?1:-1)+options.length)%options.length].focus();}});
el('model-add').addEventListener('click',()=>openModelDialog(true).catch(error=>{el('model-picker-message').textContent=error.message;}));
function renderModelPicker(config){
  el('model-current').textContent=config.model || '选择模型';el('model-options').innerHTML='';
  for(const profile of config.profiles || []){
    const selected=profile.id===config.active_profile_id,button=document.createElement('button');button.type='button';button.className='model-option';button.setAttribute('role','menuitemradio');button.setAttribute('aria-checked',String(selected));
    let host='自定义接口';try{host=new URL(profile.base_url).host;}catch{}
    button.innerHTML=`<span class="model-icon" aria-hidden="true">${esc((profile.model || 'M').slice(0,1).toUpperCase())}</span><span class="model-description"><strong>${esc(profile.model)}</strong><small>${esc(host)} · ${profile.api_key_configured?'已配置密钥':'未配置密钥'}</small></span><span aria-hidden="true">${selected?'✓':''}</span>`;
    button.addEventListener('click',async()=>{
      if(switchingModel||selected)return;switchingModel=true;el('model-trigger').disabled=true;el('ask').disabled=true;
      el('model-picker-message').textContent='正在切换…';
      try{const response=await fetch('/api/llm-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({select_profile_id:profile.id})});const data=await response.json();if(!response.ok)throw new Error(data.error || '模型切换失败');addingModel=false;el('llm-key').value='';await loadLlmConfig();el('model-picker-message').textContent='';closeModelPicker();el('answer-source').textContent='已切换，等待提问';}
      catch(error){el('model-picker-message').textContent=error.message;}finally{switchingModel=false;el('model-trigger').disabled=false;el('ask').disabled=false;}
    });el('model-options').appendChild(button);
  }
}
document.querySelector('.top').insertAdjacentHTML('afterend',`<section class="monitor" id="monitor" data-state="loading" aria-label="系统监控"><div class="monitor-head"><div class="monitor-label"><span class="signal" aria-hidden="true"></span><span id="monitor-state" role="status">正在连接看板服务</span></div><time class="monitor-clock" id="monitor-clock"></time></div><div class="monitor-grid"><div class="monitor-item">页面同步<strong id="monitor-sync">等待首次响应</strong></div><div class="monitor-item">推荐 / 数据状态<strong id="monitor-data">核对数据中</strong></div><div class="monitor-item">协作记录 / 已完成角色<strong id="monitor-agents">等待记录</strong></div></div><div class="sync-track" aria-hidden="true"><span id="monitor-progress"></span></div><div class="monitor-footer"><span id="monitor-log">只读取已有结果，不代表实时行情推送</span><button type="button" class="monitor-toggle" id="motion-toggle" aria-pressed="false">暂停动效</button></div></section>`);
let nextPollAt=Date.now()+__DASHBOARD_REFRESH_MS__, lastSyncAt=null, monitorLoading=false, loadSequence=0;
const monitorClockFormat=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
function monitorTick() {
  el('monitor-clock').textContent=monitorClockFormat.format(new Date())+' 北京时间';
  const remaining=Math.max(0,Math.ceil((nextPollAt-Date.now())/1000));
  el('monitor-sync').textContent=monitorLoading?'正在读取已有结果…':lastSyncAt?`${remaining} 秒后同步 · 上次 ${lastSyncAt}`:'等待首次响应';
  el('monitor-progress').style.transform=`scaleX(${Math.max(0,Math.min(1,(nextPollAt-Date.now())/__DASHBOARD_REFRESH_MS__))})`;
}
el('motion-toggle').addEventListener('click',event=>{const paused=document.body.classList.toggle('motion-paused');event.currentTarget.textContent=paused?'开启动效':'暂停动效';event.currentTarget.setAttribute('aria-pressed',String(paused));});
monitorTick();setInterval(monitorTick,1000);
const chartObserver=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){entry.target.classList.add('chart-enter');}},{threshold:.15});
document.querySelectorAll('.chart-panel').forEach(panel=>chartObserver.observe(panel));
const counterFrames=new WeakMap();
function animateMetric(node,previous){
  const target=node.textContent,number=Number(target.replace('%','')),startNumber=Number(String(previous).replace('%',''));
  if(!Number.isFinite(number)||!target.trim()||target==='—')return;
  if(counterFrames.has(node))cancelAnimationFrame(counterFrames.get(node));
  const decimals=(target.split('.')[1]||'').replace('%','').length,suffix=target.endsWith('%')?'%':'',start=performance.now(),from=Number.isFinite(startNumber)?startNumber:0;
  const frame=now=>{if(document.body.classList.contains('motion-paused')||matchMedia('(prefers-reduced-motion: reduce)').matches||document.hidden){node.textContent=target;counterFrames.delete(node);return;}
    const progress=Math.min(1,(now-start)/750);node.textContent=(from+(number-from)*(1-Math.pow(1-progress,3))).toFixed(decimals)+suffix;
    if(progress<1)counterFrames.set(node,requestAnimationFrame(frame));else{node.textContent=target;counterFrames.delete(node);}};
  counterFrames.set(node,requestAnimationFrame(frame));
}
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
let historyPage = 1, monthlyPage = 1, monthlyRows = [];
const pageSize = 10;
const historyCard = el('history').closest('section');
historyCard.insertAdjacentHTML('beforebegin', `<section class="card table-card"><div class="section-title"><h2>月度盈亏统计</h2><span class="muted" id="monthly-count"></span></div><p class="muted">按推荐日期所属月份统计，仅计已完成回测。累计盈亏为每笔模拟收益百分比之和（非复利、非账户实际收益）；没有本金与仓位数据，不计算人民币盈亏。待回测不按零收益计入。</p><div class="table-wrap"><table><thead><tr><th>月份</th><th>推荐数</th><th>已回测</th><th>盈利 / 亏损 / 持平</th><th>待回测</th><th>胜率</th><th>累计盈亏百分比</th><th>平均单笔收益</th></tr></thead><tbody id="monthly-body"></tbody></table></div><div id="monthly-pages" class="qa-actions"></div></section>`);
historyCard.insertAdjacentHTML('beforeend', '<div id="history-pages" class="qa-actions"></div>');
function renderPager(id, page, count, change) {
  const pages = Math.max(1, Math.ceil(count / pageSize));
  el(id).classList.add('pagination');
  el(id).setAttribute('role','navigation');
  el(id).setAttribute('aria-label',id==='history-pages'?'推荐历史分页':'月度统计分页');
  el(id).innerHTML = `<span class="muted page-info" aria-live="polite">第 ${page} / ${pages} 页 · 共 ${count} 条 · 每页10条</span><div class="page-buttons"></div>`;
  const buttons=el(id).querySelector('.page-buttons');
  for (const [label, target, disabled] of [['首页',1,page===1],['上一页',page-1,page===1],['下一页',page+1,page===pages],['最后一页',pages,page===pages]]) {
    const button=document.createElement('button'); button.type='button'; button.textContent=label;
    button.disabled=disabled; button.addEventListener('click',()=>{
      change(target);
      const body=el(id==='history-pages'?'history':'monthly-body');
      body.classList.remove('page-enter'); void body.offsetWidth; body.classList.add('page-enter');
      const focus=Array.from(el(id).querySelectorAll('button')).find(item=>item.textContent===label&&!item.disabled);
      if(focus) focus.focus({preventScroll:true});
    }); buttons.appendChild(button);
  }
}
function renderMonthly() {
  monthlyPage=Math.max(1,Math.min(monthlyPage,Math.ceil(monthlyRows.length/pageSize)||1));
  el('monthly-count').textContent=`${monthlyRows.length} 个月`;
  el('monthly-body').innerHTML=monthlyRows.slice((monthlyPage-1)*pageSize,monthlyPage*pageSize).map(row=>{
    const cls=row.return_sum_pct===null?'pending':row.return_sum_pct>0?'positive':row.return_sum_pct<0?'negative':'';
    return `<tr><td>${esc(row.month)}</td><td>${row.total}</td><td>${row.evaluated}</td><td><span class="outcome-counts"><span class="positive">${row.wins}</span><span>/</span><span class="negative">${row.losses}</span><span>/</span><span>${row.flat}</span></span></td><td>${row.pending}</td><td>${row.win_rate===null?'—':pct(row.win_rate)}</td><td class="${cls}">${row.return_sum_pct===null?'—':pct(row.return_sum_pct)}</td><td class="${row.average_return_pct===null?'pending':row.average_return_pct>0?'positive':row.average_return_pct<0?'negative':''}">${row.average_return_pct===null?'—':pct(row.average_return_pct)}</td></tr>`;
  }).join('') || '<tr><td colspan="8" class="empty">暂无月度统计。</td></tr>';
  renderPager('monthly-pages',monthlyPage,monthlyRows.length,page=>{monthlyPage=page;renderMonthly();});
}
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
  historyPage=Math.max(1,Math.min(historyPage,Math.ceil(historyRows.length/pageSize)||1));
  el('history').innerHTML = sortedHistory(historyRows, scoreSort).slice((historyPage-1)*pageSize,historyPage*pageSize).map(row => {
    const cls = row.return_pct === null ? 'pending' : (Number(row.return_pct)>0 ? 'positive' : Number(row.return_pct)<0 ? 'negative' : '');
    const status = row.return_pct === null ? '待回测' : (Number(row.return_pct)>0 ? '盈利' : Number(row.return_pct)<0 ? '亏损' : '持平');
    const score = scoreValue(row.score);
    return `<tr><td>${esc(row.date)}</td><td>${esc(row.code)}</td><td>${esc(row.name)}</td><td>${esc(row.model_label)}</td><td>${score === null ? '—' : score.toFixed(3)}</td><td>¥${money(row.reference_price)}</td><td>${esc(row.target_date)}</td><td class="${cls}">${row.return_pct === null ? '—' : pct(row.return_pct)}</td><td class="${cls}">${status}</td></tr>`;
  }).join('') || '<tr><td colspan="9" class="empty">暂无历史推荐。</td></tr>';
  el('sort-score').textContent = '模型评分 ' + (scoreSort === 'desc' ? '↓' : scoreSort === 'asc' ? '↑' : '↕');
  scoreHeader.setAttribute('aria-sort', scoreSort === 'desc' ? 'descending' : scoreSort === 'asc' ? 'ascending' : 'none');
  renderPager('history-pages',historyPage,historyRows.length,page=>{historyPage=page;renderHistory();});
}
el('sort-score').addEventListener('click', () => {
  scoreSort = scoreSort === null ? 'desc' : scoreSort === 'desc' ? 'asc' : null;
  historyPage = 1;
  renderHistory();
});
function render(data) {
  window.renderQuantMonitor?.(data);
  renderBoardCharts(data);
  const rec = data.recommendation;
  const researchKey=`${data.selected_date}:${rec?.code || ''}`;
  if(el('research-results').dataset.context!==researchKey) { el('research-results').textContent=''; el('research-results').dataset.context=researchKey; }
  const bt = data.backtest;
  el('date').value = data.selected_date || '';
  el('metric-model').textContent = data.strategy.current_label || '—';
  el('metric-model-key').textContent = '内部策略保密 · 真实结果展示';
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
  monthlyRows = data.monthly_statistics || [];
  renderMonthly();
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
async function load(date) {
  const sequence=++loadSequence; monitorLoading=true; el('monitor').dataset.state='loading'; el('monitor-state').textContent='正在同步看板';
  const before=Object.fromEntries(['metric-win','metric-total','metric-health'].map(id=>[id,el(id).textContent]));
  try {
    const query=date?`?date=${encodeURIComponent(date)}`:'';
    const response=await fetch(`/api/dashboard${query}`,{cache:'no-store',signal:AbortSignal.timeout(20000)});
    if(!response.ok) throw new Error('数据加载失败');
    const data=await response.json(); if(sequence!==loadSequence)return;
    render(data); lastSyncAt=new Date().toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
    el('monitor').dataset.state='connected';el('monitor-state').textContent='看板服务已连接 · 自动监测中';
    el('monitor-data').textContent=`推荐 ${data.selected_date || '—'} · ${data.health.ok===true?'数据正常':data.health.ok===false?'数据异常':'尚未运行'}`;
    const roles=Object.values(data.workflow?.roles || {}), completed=roles.filter(role=>role.status==='success').length;
    el('monitor-agents').textContent=roles.length?`${completed} / ${roles.length} 已完成 · 非实时执行状态`:'暂无协作记录';
    el('monitor-log').textContent=`${lastSyncAt} 已读取已有结果 · 未重新选股 · 每 ${__DASHBOARD_REFRESH_MS__/1000} 秒同步`;
    for(const [id,value] of Object.entries(before))if(value!==el(id).textContent){el(id).classList.remove('value-flash');void el(id).offsetWidth;el(id).classList.add('value-flash');animateMetric(el(id),value);}
  } catch(error) {
    if(sequence!==loadSequence)return;
    el('monitor').dataset.state='error';el('monitor-state').textContent='看板同步失败 · 等待重试';
    el('monitor-log').textContent='连接中断或超时；保留上次展示，不代表最新结果。';throw error;
  } finally {if(sequence===loadSequence){monitorLoading=false;monitorTick();}}
}
async function refreshAfterClose() { const button = el('refresh'); button.disabled = true; button.textContent = '刷新中…'; try { const response = await fetch('/api/refresh-backtest', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); const result = await response.json(); if (!response.ok && !result.skipped) throw new Error(result.error || result.message || '刷新失败'); await load(''); if (result.message) el('metric-health-time').textContent = result.message; } catch (error) { showError(error); } finally { button.disabled = false; button.textContent = '刷新'; } }
el('date').addEventListener('change', event => load(event.target.value).catch(showError)); el('refresh').addEventListener('click', () => refreshAfterClose());
function showError(error) { el('pick-content').innerHTML = `<div class="empty">${esc(error.message)}，请检查服务是否仍在运行。</div>`; }
async function loadLlmConfig() { const response = await fetch('/api/llm-config', {cache:'no-store'}); if (!response.ok) return; const config = await response.json(); renderModelPicker(config); el('llm-enabled').checked = !!config.enabled; el('llm-agents').checked = !!config.agent_explanations_enabled; el('llm-base').value = config.base_url || ''; el('llm-model').value = config.model || ''; el('llm-proxy').value = config.proxy_url || ''; el('llm-status').textContent = config.api_key_configured ? (config.last_test?.ok===false ? '上次连接失败' : (config.enabled ? '已启用' : '已配置')) : '未配置'; if(config.last_test?.message) el('llm-test-result').textContent=`上次测试：${config.last_test.message}`; }
async function askQuestion() { const question = el('question').value.trim(); if (!question) { el('answer').textContent = '请先输入问题。'; return; } el('ask').disabled = true; el('answer').className = 'answer loading'; el('answer').textContent = '正在分析当前推荐…'; try { const response = await fetch('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question, date:el('date').value})}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '问答失败'); el('answer-source').textContent = data.source === 'llm' ? `大模型：${data.model}` : data.source === 'fallback' ? '模型失败，已降级' : '本地规则'; el('answer').className = 'answer'; if(window.renderResearchAnswer)window.renderResearchAnswer(data.answer);else el('answer').textContent = data.answer; el('answer-status').textContent = `回答日期：${data.selected_date}`; } catch (error) { el('answer').className = 'answer'; el('answer').textContent = error.message; } finally { el('ask').disabled = false; } }
async function saveLlmConfig() { const button = el('save-llm'); button.disabled = true; try { const response = await fetch('/api/llm-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({add_profile:addingModel, enabled:el('llm-enabled').checked, agent_explanations_enabled:el('llm-agents').checked, base_url:el('llm-base').value, model:el('llm-model').value, api_key:el('llm-key').value, proxy_url:el('llm-proxy').value})}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '配置保存失败'); addingModel=false; renderModelPicker(data); if(modelDialog.open)closeModelDialog(); el('llm-key').value = ''; el('llm-base').value = data.base_url || el('llm-base').value; el('llm-status').textContent = data.api_key_configured ? (data.enabled ? '已启用' : '已配置') : '未配置'; el('llm-test-result').textContent = '配置已保存，请点击“测试连接”。'; } catch (error) { el('llm-status').textContent = error.message; } finally { button.disabled = false; } }
async function testLlmConfig() { const button = el('test-llm'); button.disabled = true; el('llm-test-result').textContent = '正在测试连接…'; try { const response = await fetch('/api/llm-test', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); const data = await response.json(); if (!response.ok) throw new Error(data.error || '测试失败'); el('llm-status').textContent = data.ok ? '连接成功' : '连接失败'; el('llm-test-result').textContent = `${data.status}：${data.message}${data.proxy_url ? `\n代理：${data.proxy_url}` : '\n代理：未配置，当前为直连'}`; } catch (error) { el('llm-status').textContent = '连接失败'; el('llm-test-result').textContent = error.message; } finally { button.disabled = false; } }
el('ask').addEventListener('click', () => askQuestion()); el('question').addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') askQuestion(); }); el('save-llm').addEventListener('click', () => saveLlmConfig()); el('test-llm').addEventListener('click', () => testLlmConfig());
load('').catch(showError); loadLlmConfig().catch(() => {});
setInterval(() => {if(Date.now()>=nextPollAt&&!monitorLoading){nextPollAt=Date.now()+__DASHBOARD_REFRESH_MS__;load(el('date').value).catch(() => {});}},1000);
</script></body></html>'''


def page_html(settings: Settings, principal=None) -> str:
    scheduler = scheduler_config(settings)
    refresh_seconds = int(scheduler.get("dashboard_refresh_seconds", 60))
    refresh_ms = max(10, refresh_seconds) * 1000
    assets = Path(__file__).with_name("static")
    style = (assets / "quant-monitor.css").read_text(encoding="utf-8")
    script = (assets / "quant-monitor.js").read_text(encoding="utf-8")
    style += '\n' + (assets / 'research-workspace.css').read_text(encoding='utf-8')
    script += '\n' + (assets / 'research-workspace.js').read_text(encoding='utf-8')
    script += '\n' + (assets / 'account-workspace.js').read_text(encoding='utf-8')
    script += '\n' + (assets / 'shadow-usage.js').read_text(encoding='utf-8')
    style += '\n' + (assets / 'answer-view.css').read_text(encoding='utf-8')
    style += '\n' + (assets / 'score-details.css').read_text(encoding='utf-8')
    style += '\n' + (assets / 'scheduler-countdown.css').read_text(encoding='utf-8')
    script += '\n' + (assets / 'answer-view.js').read_text(encoding='utf-8')
    script += '\n' + (assets / 'scheduler-countdown.js').read_text(encoding='utf-8')
    style += '\n' + (assets / 'stock-observations.css').read_text(encoding='utf-8')
    script += '\n' + (assets / 'stock-observations.js').read_text(encoding='utf-8')
    style += '\n' + (assets / 'shadow.css').read_text(encoding='utf-8')
    script += '\n' + (assets / 'shadow.js').read_text(encoding='utf-8')
    style += '\n' + (assets / 'intraday.css').read_text(encoding='utf-8')
    script += '\n' + (assets / 'intraday.js').read_text(encoding='utf-8')
    script += '\n' + (assets / 'agent-control.js').read_text(encoding='utf-8')
    from .strategy_privacy import public_page
    return public_page(PAGE.replace("__DASHBOARD_REFRESH_MS__", str(refresh_ms))
            .replace("</head>", f"<style>{style}</style></head>")
            .replace('</main><script>', '</main>'+ (bootstrap(principal) if principal else '') + '<script>',1)
            .replace("</body>", f"<script>{script}</script></body>"))


from .http_logging import HTTPLogging


class DashboardHandler(HTTPLogging, AccessControl, BaseHTTPRequestHandler):
    settings: Settings

    def _dashboard_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path=='/api/agent-control':
            from .harness.control.master import view
            self._json(view(self.settings,self.principal['role']=='admin'))
            return
        if parsed.path=='/api/intraday':
            from .intraday import view
            try:self._json(view(self.settings,parse_qs(parsed.query).get('date',[None])[0],self.principal['role']=='admin'))
            except ValueError as exc:self._json({'error':str(exc)},400)
            return
        if parsed.path == '/api/shadow':
            from .shadow import view
            self._send(json.dumps(view(self.settings,self.principal['role']=='admin'),ensure_ascii=False,allow_nan=False).encode('utf-8'),'application/json; charset=utf-8')
            return
        if parsed.path == '/api/stock-observation':
            from .external_stock_mcp import stock_observation
            params = parse_qs(parsed.query)
            try:
                result = stock_observation(self.settings, params.get('kind', [''])[0], params)
                self._send(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'), 'application/json; charset=utf-8')
            except ValueError as exc:
                self._send(json.dumps({'error': str(exc)}, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8', 400)
            return
        if parsed.path in ("/", "/index.html"):
            self._send(page_html(self.settings,self.principal).encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/dashboard":
            selected = parse_qs(parsed.query).get("date", [None])[0]
            payload = json.dumps(dashboard_data(self.settings, selected), ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8")
            return
        if parsed.path == '/api/research-dashboard':
            from .dashboard_enrichment import enrichment_view
            params=parse_qs(parsed.query)
            try:
                buy=float(params.get('buy_cost_pct',['0'])[0]);sell=float(params.get('sell_cost_pct',['0'])[0])
                result=enrichment_view(self.settings,dashboard_data(self.settings,params.get('date',[None])[0]),buy,sell)
                self._send(json.dumps(result,ensure_ascii=False,allow_nan=False).encode('utf-8'),'application/json; charset=utf-8')
            except (ValueError,TypeError) as exc:
                self._send(json.dumps({'error':str(exc)},ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8',400)
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

    def _dashboard_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            self._send(b'{"error":"request too large"}', "application/json; charset=utf-8", 413)
            return
        try:
            values = json.loads(self.rfile.read(length).decode("utf-8"))
            from .console_log import console_event
            console_event('http_request_body',request=values)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(b'{"error":"invalid JSON"}', "application/json; charset=utf-8", 400)
            return
        if not isinstance(values, dict):
            self._send(b'{"error":"JSON object required"}', 'application/json; charset=utf-8',400)
            return
        if parsed.path=='/api/intraday/config':
            if self.principal['role']!='admin':self._json({'error':'仅管理员可配置自动当日分析'},403);return
            from .intraday import set_enabled,config
            try:set_enabled(self.settings,values.get('enabled'));self._json(config(self.settings))
            except ValueError as exc:self._json({'error':str(exc)},400)
            return
        if parsed.path in ('/api/shadow/start','/api/shadow/config','/api/shadow/financial-check','/api/shadow/repair','/api/shadow/refresh'):
            if self.principal['role']!='admin':
                self._send(json.dumps({'error':'仅管理员可启动或配置系统影子任务'},ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8',403);return
            from .shadow import start_shadow,set_enabled,recheck_financials
            try:
                if parsed.path.endswith('/financial-check'):
                    result={**recheck_financials(self.settings),'message':'财务复核完成；原模型结论未改写，请查看复核结果。'}
                elif parsed.path.endswith('/refresh'):
                    if not isinstance(values.get('profile_id'),str) or not values['profile_id']:raise ValueError('请先选择影子模型')
                    result=start_shadow(self.settings,profile_id=values['profile_id'])
                elif parsed.path.endswith('/repair'):
                    result=start_shadow(self.settings,repair=True)
                elif parsed.path.endswith('/config'):
                    set_enabled(self.settings,values.get('enabled'));result={'ok':True}
                else:result=start_shadow(self.settings)
                self._send(json.dumps(result,ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8',202)
            except (ValueError,TypeError,KeyError) as exc:
                self._send(json.dumps({'error':str(exc)},ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8',400)
            return
        if parsed.path == '/api/research-dashboard/sync':
            from .dashboard_enrichment import start_sync
            result=start_sync(self.settings)
            self._send(json.dumps(result).encode('utf-8'),'application/json; charset=utf-8',202)
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
                payload = json.dumps(save_llm_config(tenant_settings(self.settings,self.principal['tenant_id']), values), ensure_ascii=False).encode("utf-8")
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
            from .strategy_privacy import public_question_context,public_local_answer,protected_question,protect_answer
            data,context=public_question_context(data)
            local=public_local_answer(question,data)
            if protected_question(question):answer,source,model=local,'local','策略保密'
            else:answer, source, model = answer_question(tenant_settings(self.settings,self.principal['tenant_id']), question, context, local)
            answer=protect_answer(answer)
            payload = json.dumps({"answer": answer, "source": source, "model": model, "selected_date": data.get("selected_date")}, ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/llm-test":
            payload = json.dumps(test_llm_connection(tenant_settings(self.settings,self.principal['tenant_id'])), ensure_ascii=False).encode("utf-8")
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
    from .console_log import configure_console_file,console_event
    configure_console_file(settings.runtime/'logs/backend.jsonl')
    console_event('web_service_starting',host=host,port=port,log_file=str(settings.runtime/'logs/backend.jsonl'))
    if settings.values.get('research',{}).get('enabled'):
        import pymilvus  # Warm native dependencies before request threads are started.
    from .milvus_service import ensure_local_milvus
    try:
        milvus_status=ensure_local_milvus(settings)
        print(f"Milvus：{milvus_status['status']}")
    except Exception as exc:
        print(f"Milvus启动失败：{type(exc).__name__}；研报检索将在后续调用时重试")
    scheduler = PipelineScheduler(settings)
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {"settings": settings})
    from .http_server import ExclusiveHTTPServer
    try:
        server=ExclusiveHTTPServer((host,port),handler)
    except OSError:
        console_event('web_bind_failed',host=host,port=port,message='端口绑定失败；请检查旧实例或端口权限，服务未启动，不自动切换端口')
        raise
    console_event('web_service_listening',host=host,port=server.server_port,log_file=str(settings.runtime/'logs/backend.jsonl'))
    print(f"A 股投研看板已启动：http://{host}:{server.server_port}；控制台与本地日志同步输出",flush=True)
    from .intraday import IntradayScheduler
    intraday=IntradayScheduler(settings)
    try:
        scheduler.start()
        intraday.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        intraday.stop()
        scheduler.stop()
        server.server_close()


from .scheduler import DailyPipelineScheduler


class PipelineScheduler(DailyPipelineScheduler):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, scheduler_config(settings))
