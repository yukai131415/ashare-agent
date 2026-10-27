from __future__ import annotations

import json
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents.analysis import AnalysisAgent
from .agents.backtest import BacktestAgent
from .agents.base import BaseAgent
from .agents.market import MarketDataAgent
from .agents.news import NewsAgent
from .agents.recommendation import RecommendationAgent
from .config import Settings
from .contracts import AgentResult, iso_now
from .errors import DataUnavailableError, SchemaChangedError, SourceBlockedError, StateCorruptedError
from .storage import atomic_write_json, ensure_runtime
from .trading_calendar import is_trading_weekday


def diagnose(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, SourceBlockedError):
        return "数据源连接、限流或反爬失败", "退避重试；仍失败时停止推荐，等待真实数据拉取成功"
    if isinstance(exc, SchemaChangedError):
        return "上游页面或 JSON 字段结构发生变化", "保留原始响应用于定位；修复解析前停止推荐"
    if isinstance(exc, DataUnavailableError):
        return "输入数据缺失或过滤后样本不足", "检查依赖 Agent；数据不足时停止推荐"
    if isinstance(exc, StateCorruptedError):
        return "本地状态文件损坏或不合法", "备份损坏状态并重建默认状态"
    return "未分类运行异常", "记录完整堆栈并有限重试，避免无限故障循环"


class Supervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        ensure_runtime(settings.runtime)
        self.config = settings.section("supervisor")
        from .harness.control.master import MasterAgent
        self.master = MasterAgent(settings)
        self.agent_types: dict[str, type[BaseAgent]] = {
            "market": MarketDataAgent,
            "news": NewsAgent,
            "analysis": AnalysisAgent,
            "backtest": BacktestAgent,
            "recommendation": RecommendationAgent,
        }

    @property
    def health_path(self) -> Path:
        return self.settings.runtime / "state" / "health.json"

    @property
    def event_path(self) -> Path:
        return self.settings.runtime / "agents" / "supervisor" / "logs" / "events.jsonl"

    def _event(self, event: dict[str, Any]) -> None:
        from .console_log import console_event
        console_event('agent_event', component='supervisor', **event)
        from .harness.control.master import record
        record(self.settings,event.get('run_id',self.master.run_id),event.get('agent') or event.get('role') or 'pipeline',
               event['status'],**{k:v for k,v in event.items() if k not in ('agent','role','run_id','status','traceback','error','message')})
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": iso_now(), **event}, ensure_ascii=False) + "\n")

    def run_agent(self, name: str) -> AgentResult:
        if name not in self.agent_types:
            raise ValueError(f"未知 Agent: {name}")
        attempts: list[dict[str, Any]] = []
        from .harness.policy import policy
        recovery_policy=policy(self.settings)
        max_attempts = recovery_policy.max_worker_retries+1
        for attempt in range(1, max_attempts + 1):
            attempt_started = time.monotonic()
            self._event({'agent': name, 'status': 'started', 'attempt': attempt})
            try:
                result = self.agent_types[name](self.settings).run()
                if not result.ok:
                    self.master.recover(name,DataUnavailableError('worker返回失败'),attempt,can_retry=False)
                if name == "market" and result.records < int(self.config["min_market_rows"]):
                    raise DataUnavailableError(
                        f"行情记录仅 {result.records} 条，低于安全阈值 {self.config['min_market_rows']}，拒绝生成推荐"
                    )
                result.metadata["attempt"] = attempt
                if attempts:
                    result.repair_action = result.repair_action or f"第 {attempt} 次尝试恢复成功"
                    result.metadata["failed_attempts"] = attempts
                self._event({"agent": name, "status": "success" if result.ok else "failed", "attempt": attempt, "records": result.records,
                             "duration_ms": round((time.monotonic()-attempt_started)*1000, 1), "message": result.message})
                return result
            except Exception as exc:
                diagnosis, proposed_repair = diagnose(exc)
                failure = {
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "diagnosis": diagnosis,
                    "proposed_repair": proposed_repair,
                    "traceback": traceback.format_exc(limit=8),
                }
                attempts.append(failure)
                self._event({"agent": name, "status": "failed_attempt", **failure})
                action=self.master.recover(name,exc,attempt,can_retry=attempt<max_attempts and name!='recommendation')
                if action=='retry':
                    time.sleep(recovery_policy.backoff_seconds * attempt)
                else:break
        return AgentResult(
            agent=name, ok=False, started_at=iso_now(), finished_at=iso_now(), message=attempts[-1]["error"],
            error_type=attempts[-1]["error_type"], repair_action=None,
            metadata={"failed_attempts": attempts},
        )

    def run_pipeline(self) -> dict[str, Any]:
        run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        started = iso_now()
        self.master.run_id=run_id
        results: dict[str, AgentResult] = {}
        self._event({"run_id": run_id, "status": "pipeline_started"})
        if not is_trading_weekday():
            report = {
                "run_id": run_id,
                "started_at": started,
                "finished_at": iso_now(),
                "ok": True,
                "degraded": False,
                "skipped": True,
                "skip_reason": "周末休市，不抓取行情、不生成推荐",
                "agents": {},
            }
            run_report = self.settings.runtime / "state" / f"run_{run_id}.json"
            atomic_write_json(run_report, report)
            atomic_write_json(self.health_path, report)
            self._event({
                "run_id": run_id,
                "status": "pipeline_skipped",
                "reason": report["skip_reason"],
            })
            return report
        from .workflow import AgentWorkflow
        workflow, _ = AgentWorkflow(self.settings, self.run_agent, self._event).invoke(run_id,before_run=self.master.dispatch)
        agent_results = workflow['agents']
        for name in self.agent_types:
            if name not in agent_results:
                agent_results[name] = self._skipped(name, workflow.get('blocked') or '图节点未运行').to_dict()
        ok = workflow['ok'] and all(result['ok'] for result in agent_results.values())
        degraded = any(result.get('metadata', {}).get('degraded') for result in agent_results.values())
        report = {
            "run_id": run_id, "started_at": started, "finished_at": iso_now(),
            "ok": ok, "degraded": degraded,
            "agents": agent_results,
            "workflow": {key: workflow[key] for key in ('engine', 'roles', 'tool_calls', 'blocked')},
        }
        run_report = self.settings.runtime / "state" / f"run_{run_id}.json"
        atomic_write_json(run_report, report)
        atomic_write_json(self.health_path, report)
        self._event({"run_id": run_id, "status": "pipeline_finished", "ok": ok, "degraded": degraded})
        self.master.finish(ok)
        if ok and not degraded and self.settings.values.get('shadow',{}).get('enabled',False):
            try:
                from .shadow import start_shadow
                start_shadow(self.settings)
            except Exception as exc:
                self._event({'run_id':run_id,'status':'shadow_start_failed','error_type':type(exc).__name__})
        return report

    @staticmethod
    def _skipped(name: str, reason: str) -> AgentResult:
        return AgentResult(
            agent=name, ok=False, started_at=iso_now(), finished_at=iso_now(),
            message=f"未运行：{reason}", error_type="DependencyUnavailable",
        )
