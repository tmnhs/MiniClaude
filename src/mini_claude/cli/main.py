

import argparse
import sys

from mini_claude.cli.commands.chat import cmd_chat
from mini_claude.cli.commands.core import (
    cmd_core_restart,
    cmd_core_start,
    cmd_core_status,
    cmd_core_stop,
)
from mini_claude.cli.commands.trace import cmd_trace
from mini_claude.cli.commands.version import cmd_version
from mini_claude.cli.commands.ping import cmd_ping
from mini_claude.cli.commands.run import cmd_run

from mini_claude.core.conf.config import get_config
from mini_claude.core.log.logging_setup import setup_logging

# CLI 主入口：解析命令行参数并分发到对应子命令
def main() -> None:
    parser = argparse.ArgumentParser(prog="mc", description="MiniClaude CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")

    # 健康检查命令
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("ping", help="Ping the core daemon")
    subparsers.add_parser("chat", help="Start a multi-turn chat session")

    # run 命令：启动一次 agent run
    run_parser = subparsers.add_parser("run", help="Run an agent task")
    run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

    # core 命令：管理 mc-core 守护进程
    core_parser = subparsers.add_parser("core", help="Manage the mc-core daemon")
    core_sub = core_parser.add_subparsers(dest="core_command")
    core_sub.add_parser("start", help="Start the daemon in the background")
    core_sub.add_parser("stop", help="Stop the running daemon")
    core_sub.add_parser("restart", help="Restart the daemon")
    core_sub.add_parser("status", help="Show daemon status")

    # trace 命令
    trace_parser = subparsers.add_parser("trace", help="View system trace log")
    trace_parser.add_argument("run_id", nargs="?", default=None, help="Filter by run ID")
    trace_parser.add_argument("--layer", choices=["ipc", "event", "llm"], help="Filter by layer")
    trace_parser.add_argument("--direction", help="Filter by direction (e.g. CORE→LLM)")
    trace_parser.add_argument("--raw", action="store_true", help="Output raw NDJSON")
    trace_parser.add_argument("--follow", "-f", action="store_true", help="Follow new records")

    args = parser.parse_args()
    if args.version:
        print("MiniClaude CLI ",cmd_version())
        return

    config = get_config()
    setup_logging(config)

    match args.command:
        case "ping":
            cmd_ping(config)
        case "chat":
            cmd_chat(config)
        case "run":
            cmd_run(args.goal, config)
        case "core":
            match args.core_command:
                case "start":
                    cmd_core_start(config)
                case "stop":
                    cmd_core_stop(config)
                case "restart":
                    cmd_core_restart(config)
                case "status":
                    cmd_core_status(config)
                case _:
                    parser.print_help()
                    sys.exit(1)
        case "trace":
            cmd_trace(
                args.run_id,
                config,
                layer=args.layer,
                direction=args.direction,
                raw=args.raw,
                follow=args.follow,
            )
        case _:
            parser.print_help()
            sys.exit(1)
        