from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import MAX_READ_BYTES, resolve_within_workspace


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str

class ReadFileTool(BaseTool):
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path must be relative to the current working directory and may not escape it. "
        f"Files larger than {MAX_READ_BYTES // 1024} KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            }
        },
        "required": ["path"],
    }

    # 读取文件内容；超 512KB 截断；禁止 .. 路径遍历
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = ReadFileParams.model_validate(params).path

        # 解析并校验路径：拒绝绝对路径、`..` 穿越与 symlink 逃逸
        path = resolve_within_workspace(path_str)
        raw = path.read_bytes()  # 文件不存在时抛出 FileNotFoundError
        truncated = len(raw) > MAX_READ_BYTES
        text = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)