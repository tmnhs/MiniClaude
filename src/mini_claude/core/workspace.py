from __future__ import annotations

from pathlib import Path

from mini_claude.core.bus.envelope import HandlerError

# workspace 非法时返回的 JSON-RPC 错误码（与 session 相关错误码区间保持一致）
INVALID_WORKSPACE = -32030


# 将客户端传入的 workspace 归一化为真实绝对目录；未提供时直接报错（不做 cwd 兜底）。
def normalize_workspace(raw: str | None) -> Path:
    if raw is None or raw == "":
        raise HandlerError(INVALID_WORKSPACE, "workspace is required")

    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HandlerError(
            INVALID_WORKSPACE, f"workspace must be an absolute path, got: {raw!r}"
        )

    try:
        resolved = path.resolve(strict=True)
    except OSError as e:
        raise HandlerError(
            INVALID_WORKSPACE, f"workspace does not exist: {raw!r}"
        ) from e

    if not resolved.is_dir():
        raise HandlerError(
            INVALID_WORKSPACE, f"workspace is not a directory: {raw!r}"
        )
    return resolved
