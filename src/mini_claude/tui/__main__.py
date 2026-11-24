from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
from pathlib import Path

from mini_claude.cli.commands.core import ensure_daemon
from mini_claude.core.conf.config import get_config
from mini_claude.tui.app import MiniClaudeTuiApp

_DEFAULT_TUI_LOG = "~/.mc/logs/tui.log"

# TUI 文件日志初始化：不写 stderr（避免干扰 Textual 渲染），只写滚动文件
def _setup_logging(level: str) -> None:
    log_path = Path(os.environ.get("MINICLAUDE_TUI_LOG_FILE", _DEFAULT_TUI_LOG)).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"',
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    root.handlers.clear()
    root.addHandler(handler)


# mc-tui 入口：解析 --replay / --resume 参数后启动 TUI 应用
def main() -> None:
    parser = argparse.ArgumentParser(prog="mc-tui", description="MiniClaude TUI")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="Replay events from a past run on connect",
    )
    mode.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Resume a past session by id: replay its history and keep chatting",
    )
    args = parser.parse_args()

    config = get_config()
    _setup_logging(config.logging.level)
    # 任意目录启动：daemon 未运行则后台拉起（单实例，工作区由会话参数决定）
    ensure_daemon(config)
    app = MiniClaudeTuiApp(
        config.host,
        config.port,
        replay_run_id=args.replay,
        resume_session_id=args.resume,
        model=config.llm.default_model,
    )
    # 无论正常退出（Ctrl+Q）还是被中断，都提示用户可恢复的 session id
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.print_resume_hint()


if __name__ == "__main__":
    main()
