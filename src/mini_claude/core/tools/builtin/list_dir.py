from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import (
    MAX_LIST_DEPTH,
    MAX_LIST_ENTRIES,
    resolve_within_workspace,
)


class ListDirParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    max_depth: int = Field(default=2, ge=1, le=MAX_LIST_DEPTH)

class ListDirTool(BaseTool):
    params_model = ListDirParams
    name = "list_dir"
    description = (
        "List the contents of a directory as a tree. "
        "Path must be relative to the current working directory and may not escape it. "
        "Hidden entries (starting with .) are included. "
        f"Maximum depth is {MAX_LIST_DEPTH}, maximum total entries is {MAX_LIST_ENTRIES}."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the directory (default '.').",
            },
            "max_depth": {
                "type": "integer",
                "description": (
                    f"How many levels deep to recurse (default 2, max {MAX_LIST_DEPTH})."
                ),
            },
        },
        "required": [],
    }

    # workspace 由 runner / spawn_agent 注入；直接构造时回退到进程 cwd
    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    # 以树状格式列出目录内容，深度和条数有上限
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ListDirParams.model_validate(params)
        path_str = p.path
        max_depth = p.max_depth

        # 解析并校验路径：拒绝绝对路径、`..` 穿越与 symlink 逃逸
        workspace = (self._workspace or Path.cwd()).resolve()
        root = resolve_within_workspace(path_str, workspace)
        if not root.exists():
            raise FileNotFoundError(f"no such directory: {path_str}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {path_str}")

        lines: list[str] = [str(root) + "/"]
        count = 0

        def _walk(directory: Path, depth: int, prefix: str) -> None:
            nonlocal count
            if depth > max_depth or count >= MAX_LIST_ENTRIES:
                return
            entries = sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name))
            for i, entry in enumerate(entries):
                if count >= MAX_LIST_ENTRIES:
                    lines.append(f"{prefix}... (truncated)")
                    return
        
                connector = "└── " if i == len(entries) - 1 else "├── "
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")

                count += 1
                # 不递归进入符号链接目录，避免通过 symlink 列出工作区外的内容
                if entry.is_dir() and not entry.is_symlink() and depth < max_depth:
                    extension = "    " if i == len(entries) - 1 else "│   "
                    _walk(entry, depth + 1, prefix + extension)

        _walk(root, 1, "")
        return ToolResult(content="\n".join(lines))
