from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "rate_limited"
    error_type: str | None = None


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    # 可选参数模型；设置后 invoke_tool 会在执行前做 schema 校验
    params_model: ClassVar[type[BaseModel] | None] = None

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...
