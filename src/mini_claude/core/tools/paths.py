from __future__ import annotations

from pathlib import Path


# 将工具入参路径解析为工作区内的绝对路径。
# 绝对路径一律拒绝（工具契约要求相对路径），相对路径经 resolve 后必须仍落在工作区内，
# 因此同时拦截 `..` 穿越与「符号链接指向工作区外」的逃逸。
def resolve_within_workspace(raw: str, root: Path | None = None) -> Path:
    workspace = (root or Path.cwd()).resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        raise PermissionError(
            f"absolute path not allowed: {raw!r}; use a path relative to the workspace"
        )

    resolved = (workspace / candidate).resolve()
    if not resolved.is_relative_to(workspace):
        raise PermissionError(f"path escapes workspace: {raw!r}")
    return resolved
