from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import (
    DEFAULT_BASH_TIMEOUT,
    MAX_BASH_OUTPUT_BYTES,
    MAX_BASH_TIMEOUT,
)


class BashParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    timeout: int = Field(default=DEFAULT_BASH_TIMEOUT, ge=1, le=MAX_BASH_TIMEOUT)


class BashTool(BaseTool):
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined). "
        "Non-interactive only — commands requiring user input will hang and time out. "
        f"Prefer short, focused commands. Output is truncated at "
        f"{MAX_BASH_OUTPUT_BYTES // 1024} KB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Maximum seconds to wait "
                    f"(default {DEFAULT_BASH_TIMEOUT}, max {MAX_BASH_TIMEOUT})."
                ),
            },
        },
        "required": ["command"],
    }

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        # FIXME: 如何确保bash命令是安全的？
        p = BashParams.model_validate(params)
        command = p.command
        timeout = p.timeout

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        output = stdout_bytes.decode("utf-8", errors="replace")
        truncated = len(stdout_bytes) > MAX_BASH_OUTPUT_BYTES
        if truncated:
            output = output[:MAX_BASH_OUTPUT_BYTES] + "\n[truncated]"

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )

        return ToolResult(content=output or "[no output]")
