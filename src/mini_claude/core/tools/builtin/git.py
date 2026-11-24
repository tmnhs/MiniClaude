from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import DEFAULT_BASH_TIMEOUT
from mini_claude.core.tools.process import run_process

GitSubcommand = Literal[
    "status", "diff", "log", "show", "add", "commit", "branch", "restore"
]

# 会改变 git 仓库定位/配置注入/输出重定向的选项，禁用以免逃出工作区或绕过安全语义
_DANGEROUS_FLAGS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--exec-path",
    "--config-env",
    "--output",
    "--ext-diff",
    "--textconv",
}

# 不弹分页器、不等待交互输入、不执行外部编辑器与 diff 驱动，保证非交互安全
_GIT_ENV = {
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": "true",
    "GIT_OPTIONAL_LOCKS": "0",
}

# 判断单个参数是否是禁用选项（支持 --flag=value 与 -c 粘连形式）
def _is_dangerous_arg(arg: str) -> bool:
    if arg.startswith("--force"):
        return True
    if arg.startswith("--"):
        return arg.split("=", 1)[0] in _DANGEROUS_FLAGS
    return arg in ("-C", "-c") or arg.startswith("-C") or arg.startswith("-c")


class GitParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subcommand: GitSubcommand
    args: list[str] = Field(default_factory=list)
    message: str | None = None

    # 拒绝危险选项、校验 commit/non-commit 的 message 约束
    @model_validator(mode="after")
    def _validate(self) -> GitParams:
        for arg in self.args:
            if _is_dangerous_arg(arg):
                raise ValueError(f"forbidden git option: {arg!r}")
        if self.subcommand == "commit":
            if self.message is None or not self.message.strip():
                raise ValueError("commit requires a non-empty 'message'")
        elif self.message is not None:
            raise ValueError("'message' is only allowed for commit")
        return self


# 构造 git argv：禁用 fsmonitor、只读展示关闭 textconv，参数数组直传不经 shell
def build_git_argv(
    subcommand: str, args: list[str], message: str | None = None
) -> list[str]:
    argv = ["git", "-c", "core.fsmonitor=false", subcommand]
    if subcommand in ("diff", "log", "show"):
        argv.append("--no-textconv")
    if subcommand == "commit" and message is not None:
        argv += ["-m", message]
    argv += args
    return argv


class GitTool(BaseTool):
    params_model = GitParams
    name = "git"
    description = (
        "Run a safe subset of git commands in the current working directory. "
        "Allowed subcommands: status, diff, log, show, add, commit, branch, restore. "
        "Read-only commands run without approval; mutating commands require approval. "
        "Note: restore discards uncommitted changes. "
        "Dangerous options (-C, -c, --git-dir, --work-tree, --output, --force*, ...) are rejected."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "subcommand": {
                "type": "string",
                "enum": [
                    "status",
                    "diff",
                    "log",
                    "show",
                    "add",
                    "commit",
                    "branch",
                    "restore",
                ],
                "description": "Git subcommand to run.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Extra arguments passed through verbatim (not via a shell).",
            },
            "message": {
                "type": "string",
                "description": "Commit message; required for commit and forbidden otherwise.",
            },
        },
        "required": ["subcommand"],
    }

    # workspace 由 runner / spawn_agent 注入；直接构造时回退到进程 cwd
    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    # 在 workspace 内执行受控 git 子命令，非零退出码或超时映射为工具错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = GitParams.model_validate(params)
        workspace = (self._workspace or Path.cwd()).resolve()

        argv = build_git_argv(p.subcommand, p.args, p.message)
        env = {**os.environ, **_GIT_ENV}

        try:
            result = await run_process(
                argv, cwd=workspace, timeout=DEFAULT_BASH_TIMEOUT, env=env
            )
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        if result.timed_out:
            return ToolResult(content=result.output, is_error=True, error_type="timeout")

        if result.returncode != 0:
            return ToolResult(
                content=f"[exit {result.returncode}]\n{result.output}",
                is_error=True,
                error_type="runtime_error",
            )

        return ToolResult(content=result.output or "[no output]")
