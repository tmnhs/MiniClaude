from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mini_claude.core.conf.model import SandboxConfig
from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import (
    DEFAULT_BASH_TIMEOUT,
    MAX_BASH_OUTPUT_BYTES,
    MAX_BASH_TIMEOUT,
)
from mini_claude.core.tools.process import run_process
from mini_claude.core.tools.sandbox import SandboxUnavailableError, build_argv


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

    # sandbox 由运行时（runner / spawn_agent）注入；直接构造时默认使用 workspace 隔离
    def __init__(
        self,
        sandbox: SandboxConfig | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._sandbox = sandbox or SandboxConfig()
        self._workspace = workspace

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        command = p.command
        timeout = p.timeout
        workspace = (self._workspace or Path.cwd()).resolve()

        try:
            argv = build_argv(command, workspace, self._sandbox)
        except SandboxUnavailableError as exc:
            return ToolResult(
                content=str(exc), is_error=True, error_type="sandbox_error"
            )

        try:
            result = await run_process(argv, cwd=workspace, timeout=timeout)
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        if result.timed_out:
            return ToolResult(
                content=result.output,
                is_error=True,
                error_type="timeout",
            )

        if result.returncode != 0:
            return ToolResult(
                content=f"[exit {result.returncode}]\n{result.output}",
                is_error=True,
                error_type="runtime_error",
            )

        return ToolResult(content=result.output or "[no output]")
