from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from ..contracts import AgentResult, MarketRow, iso_now
from ..errors import DataUnavailableError, StateCorruptedError
from ..model_state import ModelState
from ..models import get_model
from ..recommendation_store import recommendation_store
from ..selection import rank_candidates, eligible_for_selection
from ..storage import atomic_write_json, read_json
from ..trading_calendar import is_trading_weekday, next_weekday
from .base import BaseAgent
from .market import is_allowed_stock


def validate_recommendation_data(payload, now, cutoff, config=None):
    if now.strftime('%H:%M') < cutoff:
        raise DataUnavailableError(f'尚未到 {cutoff}，禁止生成当天推荐')
    generated = datetime.fromisoformat(str(payload.get('generated_at', '')))
    if generated.date() != now.date() or generated > now:
        raise DataUnavailableError('分析数据不是当天有效结果')
    rows=payload['features']
    checked=[row for row in rows if config is None or eligible_for_selection(row,config)]
    if not checked:
        raise DataUnavailableError('没有满足原有成交额和涨跌幅条件的有效候选')
    for row in checked:
        try:
            quoted = datetime.strptime(str(row.get('quote_time', '')), '%Y%m%d%H%M%S')
        except ValueError as exc:
            raise DataUnavailableError('行情缺少可验证的时间，禁止推荐') from exc
        if quoted.date() != now.date() or quoted.strftime('%H:%M') < cutoff or quoted > now.replace(tzinfo=None):
            raise DataUnavailableError(f"{row.get('code','')} {row.get('name','')} 报价 {row.get('quote_time')} 不符合当天推荐窗口，禁止使用旧行情")
    return {'scope':'eligible_candidates','checked_rows':len(checked),'excluded_rows':len(rows)-len(checked),
            'excluded_codes':[str(r.get('code','')) for r in rows if config is not None and not eligible_for_selection(r,config)]}


def recommendation_basis(features: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: features.get(key) for key in (
                "change_pct", "amount", "volume_ratio", "turnover_pct", "pe_ratio",
                "news_count", "news_sentiment", "momentum_z", "liquidity_z", "quote_time",
            )
        },
        "news_factor_enabled": False,
    }


class RecommendationAgent(BaseAgent):
    name = "recommendation"

    def run(self) -> AgentResult:
        started = iso_now()
        now = datetime.now().astimezone()
        today = now.date()
        if not is_trading_weekday(today):
            return AgentResult(
                agent=self.name,
                ok=True,
                started_at=started,
                finished_at=iso_now(),
                records=0,
                message="周末休市，已跳过股票推荐",
                metadata={"skipped": True, "skip_reason": "weekend"},
            )
        config = self.settings.section("recommendation")
        analysis_path = self.settings.runtime / "agents" / "analysis" / "data" / "latest.json"
        payload = read_json(analysis_path)
        if not isinstance(payload, dict) or not payload.get("features"):
            raise DataUnavailableError("缺少可用于推荐的分析特征")
        market_config = self.settings.section("market")
        def allowed(item):
            return is_allowed_stock(MarketRow(code=str(item.get("code", "")), name=str(item.get("name", "")), price=1.0, change_pct=0.0), market_config)
        payload = {**payload, "features": [item for item in payload["features"] if allowed(item)]}
        cutoff = str(self.settings.values.get('scheduler', {}).get('daily_run_time', '14:30'))
        freshness=validate_recommendation_data(payload, now, cutoff, config)
        store = recommendation_store(self.settings.runtime)
        latest_path = self.settings.runtime / "agents" / "recommendation" / "data" / "latest.json"
        today_existing = next(
            (
                row for row in store.history()
                if str(row.get("recommendation_date") or row.get("created_at", ""))[:10] == today.isoformat()
            ),
            None,
        )
        if today_existing and str(today_existing.get('created_at', ''))[11:16] < cutoff:
            backup = self.settings.runtime / 'backups' / f"invalid_recommendation_{today}_{today_existing['recommendation_id']}.json"
            if not backup.exists():
                atomic_write_json(backup, today_existing)
            today_existing = None
        if today_existing and not allowed(today_existing):
            raise DataUnavailableError("今日已锁定股票不符合当前板块排除规则，已停止输出；历史记录保留，请明确重新选股后再替换")
        if today_existing:
            locked_code = str(today_existing.get("code", ""))
            selected = next((features for features in payload["features"] if str(features.get("code")) == locked_code), None)
            if selected is None:
                raise DataUnavailableError('今日锁定股票缺少本轮行情，停止刷新推荐')
            validate_recommendation_data({**payload,'features':[selected]},now,cutoff)
            if selected is not None and today_existing.get('model_version') == get_model(str(today_existing.get('model') or config['models'][0])).version:
                model_name = str(today_existing.get("model") or config["models"][0])
                model = get_model(model_name)
                today_existing = {
                    **today_existing,
                    "reference_price": float(selected["price"]),
                    "score": round(model.score(selected), 6),
                    "model_version": model.version,
                    "basis": recommendation_basis(selected),
                }
                store.save(today_existing)
            atomic_write_json(latest_path, today_existing)
            return AgentResult(
                agent=self.name,
                ok=True,
                started_at=started,
                finished_at=iso_now(),
                records=1,
                output_files=[str(store.path), str(latest_path)],
                message=(
                    f"今日推荐已锁定：{today_existing['code']} {today_existing['name']}；"
                    "已刷新锁定股票的最新行情因子"
                ),
                metadata={
                    "recommendation": today_existing,
                    "candidate_count": len(payload["features"]),
                    "history_inserted": False,
                    "database": str(store.path),
                    "locked_daily_recommendation": True,
                    "freshness": freshness,
                },
            )
        state_manager = ModelState(self.settings.runtime / "state" / "model_state.json", list(config["models"]))
        repair_action = None
        try:
            model_name = state_manager.current()
        except StateCorruptedError as exc:
            state_manager.repair(str(exc))
            model_name = state_manager.current()
            repair_action = "模型状态损坏，已备份并恢复默认模型"
        model = get_model(model_name)
        candidates = rank_candidates(payload["features"], config, model_name)
        if not candidates:
            raise DataUnavailableError("流动性和涨跌幅过滤后没有推荐候选")
        score, selected = candidates[0]
        target_date = next_weekday(today).isoformat()
        identifier = hashlib.sha256(f"{today}:{target_date}:{selected['code']}:{model.name}".encode()).hexdigest()[:20]
        recommendation = {
            "recommendation_id": identifier,
            "created_at": iso_now(),
            "target_date": target_date,
            "code": selected["code"],
            "name": selected["name"],
            "reference_price": float(selected["price"]),
            "model": model.name,
            "model_version": model.version,
            "score": round(score, 6),
            "basis": recommendation_basis(selected),
            "disclaimer": "仅供研究，不构成投资建议；公开行情可能延迟。",
        }
        history_inserted = store.save(recommendation)
        database = store.path
        atomic_write_json(latest_path, recommendation)
        persistence_message = "今日首次生成" if history_inserted else "今日已重新计算并更新"
        return AgentResult(
            agent=self.name, ok=True, started_at=started, finished_at=iso_now(), records=1,
            output_files=[str(database), str(latest_path)],
            message=f"明日精选：{selected['code']} {selected['name']}（模型 {model.name}）；{persistence_message}",
            repair_action=repair_action,
            metadata={
                "recommendation": recommendation, "candidate_count": len(candidates),
                "history_inserted": history_inserted, "database": str(database),
                "freshness": freshness,
            },
        )
