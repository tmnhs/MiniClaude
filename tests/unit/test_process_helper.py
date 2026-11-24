from __future__ import annotations

import os
import sys
from pathlib import Path

from mini_claude.core.tools.process import decode_output, run_process


# 功能：成功命令返回 stdout、退出码 0，且不标记截断/超时
async def test_run_process_success(tmp_path: Path) -> None:
    result = await run_process(
        [sys.executable, "-c", "print('hello')"],
        cwd=tmp_path,
        timeout=10,
    )

    assert result.output.strip() == "hello"
    assert result.returncode == 0
    assert not result.truncated
    assert not result.timed_out


# 功能：stderr 合并进 output
async def test_run_process_merges_stderr(tmp_path: Path) -> None:
    result = await run_process(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        cwd=tmp_path,
        timeout=10,
    )

    assert "out" in result.output
    assert "err" in result.output


# 功能：非零退出码原样返回，由调用方映射为错误
async def test_run_process_nonzero(tmp_path: Path) -> None:
    result = await run_process(
        [sys.executable, "-c", "raise SystemExit(3)"],
        cwd=tmp_path,
        timeout=10,
    )

    assert result.returncode == 3
    assert not result.timed_out


# 功能：超时后杀掉进程组并标记 timed_out
async def test_run_process_timeout(tmp_path: Path) -> None:
    result = await run_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout=0.2,
    )

    assert result.timed_out
    assert "[timeout after 0.2s]" in result.output


# 功能：环境变量可覆盖
async def test_run_process_env(tmp_path: Path) -> None:
    env = {**os.environ, "MC_PROCESS_TEST": "ok"}
    result = await run_process(
        [sys.executable, "-c", "import os; print(os.environ['MC_PROCESS_TEST'])"],
        cwd=tmp_path,
        timeout=10,
        env=env,
    )

    assert result.output.strip() == "ok"


# 功能：多字节字符按字节截断，解码后追加截断标记
def test_decode_output_multibyte_truncation() -> None:
    raw = "你好世界".encode()  # 12 bytes

    text, truncated = decode_output(raw, 7)

    assert truncated
    assert text.endswith("\n[truncated]")
    # 7 bytes 会落在第 3 个字符中间，用 replace 解码不应抛错
    assert "你" in text


# 功能：未超限时原样解码，不追加标记
def test_decode_output_not_truncated() -> None:
    text, truncated = decode_output(b"hello", 10)

    assert text == "hello"
    assert not truncated
