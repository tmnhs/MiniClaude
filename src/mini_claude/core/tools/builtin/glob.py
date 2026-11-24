from __future__ import annotations

import asyncio
import glob as globlib
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import (
    DEFAULT_GLOB_MAX_RESULTS,
    IGNORED_DIRS,
    MAX_BASH_OUTPUT_BYTES,
    MAX_GLOB_MAX_RESULTS,
    resolve_within_workspace,
)
from mini_claude.core.tools.process import decode_output


class GlobParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str
    path: str = "."
    max_results: int = Field(default=DEFAULT_GLOB_MAX_RESULTS, ge=1, le=MAX_GLOB_MAX_RESULTS)

    # 只接受相对 glob，拒绝绝对路径、`~` 与 `..`，避免逃出搜索根
    @model_validator(mode="after")
    def _validate_pattern(self) -> GlobParams:
        if not self.pattern:
            raise ValueError("pattern must not be empty")
        if os.path.isabs(self.pattern) or self.pattern.startswith("~"):
            raise ValueError("pattern must be relative and must not start with '~'")
        if ".." in Path(self.pattern).parts:
            raise ValueError("pattern must not contain '..'")
        return self


# 递归匹配 glob，过滤忽略目录、越界与符号链接，仅保留工作区内的文件
def _glob_sync(
    pattern: str, root: Path, workspace: Path, max_results: int
) -> tuple[list[str], bool]:
    raw_matches = globlib.glob(pattern, root_dir=root, recursive=True, include_hidden=True)

    found: set[str] = set()
    limit_hit = False
    for rel_raw in raw_matches:
        candidate = root / rel_raw
        if any(part in IGNORED_DIRS for part in Path(rel_raw).parts):
            continue
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(workspace) or not resolved.is_file():
            continue
        try:
            found.add(resolved.relative_to(workspace).as_posix())
        except ValueError:
            continue
        if len(found) > max_results:
            limit_hit = True
            break

    ordered = sorted(found)
    if limit_hit:
        return ordered[:max_results], True
    return ordered, False


class GlobTool(BaseTool):
    params_model = GlobParams
    name = "glob"
    description = (
        "Find files by glob pattern relative to a directory (recursive when the pattern "
        "contains '**'). Returns workspace-relative file paths, one per line. "
        "Path must be relative to the current working directory and may not escape it. "
        f"Hidden files are included; common dependency/cache directories are ignored; "
        f"at most {DEFAULT_GLOB_MAX_RESULTS} results are returned by default."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py' or 'src/*.toml'. Must be relative.",
            },
            "path": {
                "type": "string",
                "description": "Relative directory to search from (default '.').",
            },
            "max_results": {
                "type": "integer",
                "description": (
                    f"Maximum paths to return (default {DEFAULT_GLOB_MAX_RESULTS}, "
                    f"max {MAX_GLOB_MAX_RESULTS})."
                ),
            },
        },
        "required": ["pattern"],
    }

    # 在 workspace 内按 glob 匹配文件，返回排序后的相对路径
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = GlobParams.model_validate(params)

        workspace = Path.cwd().resolve()
        root = resolve_within_workspace(p.path, workspace)
        if not root.exists():
            raise FileNotFoundError(f"no such directory: {p.path}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {p.path}")

        results, limit_hit = await asyncio.to_thread(
            _glob_sync, p.pattern, root, workspace, p.max_results
        )

        content = "\n".join(results)
        if limit_hit:
            content += f"\n[truncated at {p.max_results} results]"
        content, _ = decode_output(content.encode("utf-8"), MAX_BASH_OUTPUT_BYTES)
        return ToolResult(content=content)
