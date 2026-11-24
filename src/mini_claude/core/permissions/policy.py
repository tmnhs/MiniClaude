from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mini_claude.core.permissions.analysis import analyze_command


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"

# 判断 bash 命令是否操作 cwd 之外路径（强制触发 ASK，不可被 allow_patterns 绕过）。
# 基于 shell AST 分析，能识别带引号路径、$HOME 与 cd，避免把 --grep=/x 之内的选项值误判为路径。
def matches_outside_cwd(command: str) -> bool:
    return analyze_command(command).outside

@dataclass
class ToolPolicy:
    default: PermissionDecision
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)

DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "bash":       ToolPolicy(default=PermissionDecision.ASK),
    "write_file": ToolPolicy(default=PermissionDecision.ASK),
    "edit_file":  ToolPolicy(default=PermissionDecision.ASK),
    "read_file":  ToolPolicy(default=PermissionDecision.ALLOW),
    "list_dir":   ToolPolicy(default=PermissionDecision.ALLOW),
    "note_save":  ToolPolicy(default=PermissionDecision.ALLOW),
}

# 未在 DEFAULT_POLICIES 中登记的工具的兜底策略
_UNKNOWN_TOOL_DEFAULT = PermissionDecision.ASK

# bash 参数中展示用的关键字段映射
_PREVIEW_KEY: dict[str, str] = {
    "bash":       "command",
    "read_file":  "path",
    "write_file": "path",
    "edit_file":  "path",
    "list_dir":   "path",
    "note_save":  "content",
}
_PREVIEW_MAX = 60

# 为权限审批事件生成人类可读的参数摘要
def param_preview(tool_name: str, params: dict[str, Any]) -> str:
    key = _PREVIEW_KEY.get(tool_name)
    if key and key in params:
        val = str(params[key])
        if len(val) > _PREVIEW_MAX:
            val = val[:_PREVIEW_MAX] + "…"
        return f"{key}={val!r}"

    snippet = str(params)
    return snippet[:_PREVIEW_MAX] if len(snippet) > _PREVIEW_MAX else snippet

# 对工具 + 参数执行 4 层静态策略评估，返回 ALLOW/DENY/ASK
def evaluate(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy | None = None,
) -> PermissionDecision:
    if policy is None:
        policy = DEFAULT_POLICIES.get(tool_name)

    if policy is None:
        return _UNKNOWN_TOOL_DEFAULT

    # 针对bash工具严格校验
    command = str(params.get("command", "")) if tool_name == "bash" else ""
    if command:
        for pat in policy.deny_patterns:
            if re.search(pat, command):
                return PermissionDecision.DENY

    if command and matches_outside_cwd(command):
        return PermissionDecision.ASK

    if command:
        for pat in policy.allow_patterns:
            if re.search(pat, command):
                return PermissionDecision.ALLOW

    return policy.default
