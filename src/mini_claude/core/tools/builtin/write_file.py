from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import MAX_WRITE_BYTES, resolve_within_workspace


class WriteFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    content: str

class WriteFileTool(BaseTool):
    params_model = WriteFileParams
    name = "write_file"
    description = (
        "Write text content to a file, creating it (and any parent directories) if it "
        "does not exist, or overwriting it if it does. "
        "Path must be relative to the current working directory and may not escape it. "
        f"Content size is limited to {MAX_WRITE_BYTES // (1024 * 1024)} MB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            },
            "content": {
                "type": "string",
                "description": "Text content to write.",
            },
        },
        "required": ["path", "content"],
    }

    # workspace 由 runner / spawn_agent 注入；直接构造时回退到进程 cwd
    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    # 写入文件内容；超 1MB 拒绝；禁止 .. 路径遍历；自动创建父目录
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        path_str = p.path
        content = p.content

        # 解析并校验路径：拒绝绝对路径、`..` 穿越与 symlink 逃逸
        workspace = (self._workspace or Path.cwd()).resolve()
        path = resolve_within_workspace(path_str, workspace)

        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            return ToolResult(
                content=(
                    f"content too large: {len(encoded)} bytes "
                    f"(limit {MAX_WRITE_BYTES // (1024 * 1024)} MB)"
                ),
                is_error=True,
                error_type="runtime_error",
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        return ToolResult(content=f"wrote {len(encoded)} bytes to {path_str}")