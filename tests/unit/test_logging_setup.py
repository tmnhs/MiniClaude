from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from mini_claude.core.conf.model import LoggingConfig, MiniClaudeConfig
from mini_claude.core.log.logging_setup import setup_logging


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakePipe(io.StringIO):
    def isatty(self) -> bool:
        return False


# 功能：保存/恢复 root logger 的 handlers，避免 setup_logging 清空 handler 后污染其他测试
@pytest.fixture()
def restore_root_handlers() -> Iterator[None]:
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        yield
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)


def _config(log_file: str) -> MiniClaudeConfig:
    config = MiniClaudeConfig()
    config.logging = LoggingConfig(level="INFO", file=log_file, format="text")
    return config


# 功能：daemon 后台运行（stderr 非 TTY）且配置了日志文件时，不应再挂 stderr handler
# 设计：stderr 被重定向到 core.stderr.log，若仍写 stderr 会与 core.log 重复；断言只有文件 handler
def test_no_stderr_handler_when_detached_and_file_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore_root_handlers: None
) -> None:
    fake = _FakePipe()
    monkeypatch.setattr("sys.stderr", fake)
    setup_logging(_config(str(tmp_path / "core.log")))

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    # RotatingFileHandler 也是 StreamHandler 子类，因此按 stream 身份判断是否写到了 stderr
    assert not any(getattr(h, "stream", None) is fake for h in handlers)


# 功能：交互终端（stderr 为 TTY）下即使配置了日志文件，也应同时输出到 stderr
# 设计：保证前台 `mc-core` 调试时仍能看到实时日志，断言同时存在 stderr 与文件 handler
def test_stderr_handler_kept_on_tty_with_file_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore_root_handlers: None
) -> None:
    fake = _FakeTTY()
    monkeypatch.setattr("sys.stderr", fake)
    setup_logging(_config(str(tmp_path / "core.log")))

    handlers = logging.getLogger().handlers
    assert len(handlers) == 2
    assert any(getattr(h, "stream", None) is fake for h in handlers)


# 功能：未配置日志文件时 stderr 是唯一输出，必须始终挂载
# 设计：log_file="" 表示仅 stderr，断言存在 stderr handler 且无文件 handler
def test_stderr_handler_always_added_without_file(
    monkeypatch: pytest.MonkeyPatch, restore_root_handlers: None
) -> None:
    fake = _FakePipe()
    monkeypatch.setattr("sys.stderr", fake)
    setup_logging(_config(""))

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
