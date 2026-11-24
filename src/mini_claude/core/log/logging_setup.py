from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from mini_claude.core.conf.config import MiniClaudeConfig

_TEXT_FMT = 'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"'
_JSON_FMT = '{"level":"%(levelname)s","ts":"%(asctime)s","source":"%(name)s","msg":"%(message)s"}'


# 根据配置初始化 root logger：设置级别、格式，挂载滚动文件 handler 与（交互时的）stderr handler
def setup_logging(config: MiniClaudeConfig) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    fmt = _JSON_FMT if config.logging.format == "json" else _TEXT_FMT
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # 仅在终端交互（或未配置文件日志）时挂载 stderr：
    if not config.logging.file or (hasattr(sys.stderr, "isatty") and sys.stderr.isatty()):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root.addHandler(stderr_handler)

    if config.logging.file:
        log_path = Path(config.logging.file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
