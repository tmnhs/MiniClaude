from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import MAX_WRITE_BYTES, resolve_within_workspace


class EditFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditFileTool(BaseTool):
    params_model = EditFileParams
    name = "edit_file"
    description = (
        "Replace an exact string in a text file. "
        "By default old_string must occur exactly once; pass replace_all=true to replace "
        "every occurrence. Prefer this over write_file for small changes to existing files. "
        "Path must be relative to the current working directory and may not escape it."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to replace. Must occur once unless replace_all is set.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence of old_string (default false).",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    # 对文件做精确字符串替换；返回替换次数，输入不满足前置条件时返回不可重试错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = EditFileParams.model_validate(params)
        if p.old_string == "":
            return ToolResult(
                content="old_string must not be empty",
                is_error=True,
                error_type="schema_error",
            )

        # 解析并校验路径：拒绝绝对路径、`..` 穿越与 symlink 逃逸
        path = resolve_within_workspace(p.path)

        try:
            original = path.read_text(encoding="utf-8")  # 不存在时抛 FileNotFoundError
        except UnicodeDecodeError:
            return ToolResult(
                content=f"file is not valid UTF-8: {p.path}",
                is_error=True,
                error_type="schema_error",
            )

        occurrences = original.count(p.old_string)
        if occurrences == 0:
            return ToolResult(
                content=f"old_string not found in {p.path}",
                is_error=True,
                error_type="schema_error",
            )
        if occurrences > 1 and not p.replace_all:
            return ToolResult(
                content=(
                    f"old_string occurs {occurrences} times in {p.path}; "
                    "add more surrounding context or set replace_all=true"
                ),
                is_error=True,
                error_type="schema_error",
            )

        replacements = occurrences if p.replace_all else 1
        updated = original.replace(p.old_string, p.new_string, replacements)
        encoded = updated.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            return ToolResult(
                content=(
                    f"content too large: {len(encoded)} bytes "
                    f"(limit {MAX_WRITE_BYTES // (1024 * 1024)} MB)"
                ),
                is_error=True,
                error_type="schema_error",
            )

        path.write_text(updated, encoding="utf-8")

        suffix = "" if replacements == 1 else "s"
        return ToolResult(content=f"edited {p.path} ({replacements} replacement{suffix})")
