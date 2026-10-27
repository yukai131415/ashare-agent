from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    package_dir = Path(__file__).resolve().parent
    src_dir = str(package_dir.parent)
    sys.path = [item for item in sys.path if Path(item or ".").resolve() != package_dir]
    sys.path.insert(0, src_dir)
    from a_stock_agents.config import load_settings
    from a_stock_agents.model_state import ModelState
    from a_stock_agents.storage import ensure_runtime, read_json
    from a_stock_agents.supervisor import Supervisor
    from a_stock_agents.trading_calendar import is_trading_weekday
else:
    from .config import load_settings
    from .model_state import ModelState
    from .storage import ensure_runtime, read_json
    from .supervisor import Supervisor
    from .trading_calendar import is_trading_weekday


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A 股多 Agent 分析系统")
    parser.add_argument("--config", help="自定义配置 JSON，会覆盖默认配置")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="运行完整流水线")
    plan=commands.add_parser('plan',help='查看 Harness 计划工具的步骤与验收条件')
    plan.add_argument('task_type',choices=['formal','shadow','intraday'],nargs='?',default='formal')
    resume=commands.add_parser('resume',help='从检查点恢复当日正式任务；发布结果待确认时停止')
    resume.add_argument('run_id')
    audit=commands.add_parser('audit',help='离线验收 Harness 运行轨迹，不调用模型')
    audit.add_argument('run_id')
    agent = commands.add_parser("agent", help="单独运行一个子 Agent")
    agent.add_argument("name", choices=["market", "news", "analysis", "backtest", "recommendation"])
    commands.add_parser("status", help="查看总控最近健康状态")
    commands.add_parser("model", help="查看当前推荐模型和切换历史")
    commands.add_parser("doctor", help="检查本地目录、配置和运行环境")
    commands.add_parser("mcp", help="启动 MCP stdio 行情、财务和研报工具服务")
    graph = commands.add_parser("graph", help="显示实际编译的 LangGraph 架构")
    replay = commands.add_parser("replay", help="离线回放历史快照，不写入推荐历史")
    replay.add_argument('--snapshot', required=True)
    replay.add_argument('--model', choices=['balanced','momentum','value_reversal'], default='balanced')
    rag = commands.add_parser('research', help='Milvus 研报导入/查询')
    rag.add_argument('action', choices=['ingest','search','sync'])
    rag.add_argument('--file', help='包含 title/source/published_at/code/text 的 UTF-8 JSONL')
    rag.add_argument('--query')
    rag.add_argument('--code')
    rag.add_argument('--as-of', help='带时区的检索截止时间')
    web = commands.add_parser("web", help="启动本地前端投研看板")
    web.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机访问")
    web.add_argument("--port", type=int, default=8001, help="监听端口，默认 8001")
    web.add_argument(
        "--refresh-on-start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="启动看板前先运行一次完整流水线，默认开启",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["web"]
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    if args.command=='audit':
        from a_stock_agents.harness.evaluation import audit_run
        result=audit_run(settings,args.run_id);_print(result);return 0 if result['structural_checks_passed'] else 1
    if args.command=='plan':
        from a_stock_agents.harness.planning.tool import execution_plan_tool
        _print(execution_plan_tool.invoke({'task_type':args.task_type}));return 0
    if args.command=='resume':
        from a_stock_agents.workflow import AgentWorkflow
        supervisor=Supervisor(settings);supervisor.master.run_id=args.run_id
        report,_=AgentWorkflow(settings,supervisor.run_agent).invoke(args.run_id,resume=True)
        supervisor.master.finish(report['ok'])
        _print(report);return 0 if report['ok'] else 1
    if args.command == 'mcp':
        from a_stock_agents.mcp_server import create_server
        from a_stock_agents.console_log import console_output
        with console_output(sys.stderr):create_server(settings).run(transport='stdio')
        return 0
    if args.command in ('graph','replay'):
        from a_stock_agents.workflow import AgentWorkflow
        from a_stock_agents.storage import read_csv
        from a_stock_agents.contracts import iso_now
        workflow=AgentWorkflow(settings)
        if args.command=='graph':
            print(workflow.graph.get_graph().draw_mermaid())
        else:
            report,_=workflow.invoke('replay_'+iso_now().replace(':','').replace('+','_'),replay_rows=read_csv(Path(args.snapshot)),model=args.model)
            _print(report)
            return 0 if report['ok'] else 1
        return 0
    if args.command=='research':
        from a_stock_agents.research import ResearchIndex
        index=ResearchIndex(settings)
        try:
            if args.action=='ingest':
                if not args.file:
                    raise ValueError('ingest requires --file')
                with Path(args.file).open(encoding='utf-8-sig') as handle:
                    result=index.ingest([json.loads(line) for line in handle if line.strip()])
            else:
                if not all((args.code,args.as_of)) or (args.action=='search' and not args.query):
                    raise ValueError('search requires --query --code --as-of')
                from a_stock_agents.agent_tools import AgentTools
                tools=AgentTools(settings)
                try:
                    result=tools.call('sync_research',{'code':args.code,'as_of':args.as_of}) if args.action=='sync' else tools.call('search_research',{'query':args.query,'code':args.code,'as_of':args.as_of})
                finally:
                    tools.close()
            _print(result)
            return 0
        finally:
            index.close()
    ensure_runtime(settings.runtime)
    supervisor = Supervisor(settings)
    if args.command == "run":
        report = supervisor.run_pipeline()
        _print(report)
        return 0 if report["ok"] else 1
    if args.command == "agent":
        result = supervisor.run_agent(args.name)
        _print(result.to_dict())
        return 0 if result.ok else 1
    if args.command == "status":
        _print(read_json(supervisor.health_path, {"status": "尚未运行"}))
        return 0
    if args.command == "model":
        config = settings.section("recommendation")
        state = ModelState(settings.runtime / "state" / "model_state.json", list(config["models"]))
        try:
            _print(state.load())
        except Exception as exc:
            _print({"ok": False, "error": str(exc), "repair": "下次推荐时自动备份并重建"})
            return 1
        return 0
    if args.command == "doctor":
        _print({
            "ok": True,
            "python": sys.version.split()[0],
            "project_root": str(settings.root),
            "runtime_writable": settings.runtime.exists(),
            "configured_agents": list(supervisor.agent_types),
            "note": "doctor 只做本地检查；使用 agent market/news 验证上游连通性。",
        })
        return 0
    if args.command == "web":
        if __package__ in (None, ""):
            from a_stock_agents.web import recommendation_window_open, scheduler_config, serve
        else:
            from .web import recommendation_window_open, scheduler_config, serve
        if args.refresh_on_start:
            schedule = scheduler_config(settings)
            if not is_trading_weekday():
                print("周末休市，启动时不抓取当天行情，不生成当天推荐")
            elif recommendation_window_open(settings):
                from a_stock_agents.web import PipelineScheduler
                completed = PipelineScheduler(settings).run_once('startup')
                print('启动任务检查完成：' + ('已生成推荐' if completed else '已有结果、等待重试或其他进程正在执行'))
            else:
                print(f"尚未到 {schedule['daily_run_time']}，启动时不抓取当天数据，不生成当天推荐")
        serve(settings, args.host, args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
