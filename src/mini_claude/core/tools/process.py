from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mini_claude.core.tools.limits import MAX_BASH_OUTPUT_BYTES


# 子进程执行结果：output 已合并 stderr 并按字节截断
@dataclass(frozen=True)
class ProcessResult:
    output: str
    returncode: int
    truncated: bool = False
    timed_out: bool = False


# 按字节上限截断后再解码，避免多字节字符按字符切片时突破字节上限
def decode_output(raw: bytes, max_bytes: int) -> tuple[str, bool]:
    truncated = len(raw) > max_bytes
    data = raw[:max_bytes] if truncated else raw
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text += "\n[truncated]"
    return text, truncated


# 以独立进程组执行 argv，合并 stdout/stderr；超时杀掉整个进程组并标记 timed_out。
# 调用方负责把 returncode / timed_out 映射为工具错误。
async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    max_output_bytes: int = MAX_BASH_OUTPUT_BYTES,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        start_new_session=True,
        env=dict(env) if env is not None else None,
    )

    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        # start_new_session=True 使子进程成为独立进程组，killpg 可清理整个进程树
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.communicate()
        return ProcessResult(
            output=f"[timeout after {timeout}s]",
            returncode=-1,
            timed_out=True,
        )

    output, truncated = decode_output(raw, max_output_bytes)
    return ProcessResult(
        output=output,
        returncode=proc.returncode or 0,
        truncated=truncated,
    )
