from __future__ import annotations

from pathlib import Path

# ── 工具资源上限 ──────────────────────────────────────────────────────────────
# 集中定义各内置工具的截断/范围限制，避免分散在工具文件中导致口径漂移。

MAX_READ_BYTES = 1 * 1024 * 1024     # read_file 最大读取字节数：1 MB
MAX_WRITE_BYTES = 1 * 1024 * 1024    # write_file / edit_file 最大写入字节数：1 MB
MAX_LIST_DEPTH = 4                   # list_dir 最大递归深度
MAX_LIST_ENTRIES = 200               # list_dir 最大输出条目数
MAX_BASH_OUTPUT_BYTES = 64 * 1024    # bash 最大输出字节数：64 KB
DEFAULT_BASH_TIMEOUT = 60            # bash 默认超时秒数
MAX_BASH_TIMEOUT = 120               # bash 最大超时秒数


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
