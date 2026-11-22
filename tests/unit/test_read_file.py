from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.tools.builtin.read_file import ReadFileTool
from mini_claude.core.tools.limits import MAX_READ_BYTES


# 功能：验证读取存在的文件时返回完整内容且 is_error 为 False
# 设计：切换到临时工作区后以相对路径读取，断言 content 和 is_error，覆盖正常路径（happy path）
async def test_read_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = await ReadFileTool().invoke({"path": "hello.txt"})
    assert not result.is_error
    assert result.content == "hello world"


# 功能：验证文件不存在时抛出 FileNotFoundError 而非返回错误 ToolResult
# 设计：在临时工作区内传入不存在的相对路径，确认 ReadFileTool 不吞掉异常，由调用方负责错误分类
async def test_file_not_found_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        await ReadFileTool().invoke({"path": "missing.txt"})


# 功能：验证包含 `..` 且逃出工作区的路径被拒绝并抛出 PermissionError
# 设计：工作区建在 tmp_path/ws，传入 `"../secret.txt"` 指向工作区外，确认安全边界有效
async def test_path_traversal_dotdot_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": "../secret.txt"})


# 功能：验证多级路径中嵌入的 `..` 经过路径解析后也被正确检测
# 设计：使用 `"subdir/../../etc/passwd"` 测试 resolve 后跳出工作区的情况，确认单层过滤不足
async def test_path_traversal_nested_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": "subdir/../../etc/passwd"})


# 功能：验证绝对路径被拒绝
# 设计：即使目标存在，绝对路径也违反工具契约，必须抛 PermissionError
async def test_absolute_path_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": "/etc/passwd"})


# 功能：验证「工作区内的绝对路径」同样被拒绝（契约要求相对路径）
# 设计：传入 tmp_path 下真实存在的绝对路径，确认拒绝逻辑不因路径在工作区内而放行
async def test_absolute_path_inside_workspace_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "inside.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": str(target)})


# 功能：验证指向工作区外的符号链接无法读取外部文件
# 设计：在工作区内建一条指向外部文件的 symlink，resolve 后越界，应抛 PermissionError
async def test_symlink_escape_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": "link.txt"})


# 功能：验证超过读取上限的文件被截断并在末尾追加 [truncated] 标记
# 设计：基于 MAX_READ_BYTES 动态构造超限文件，断言内容以限制长度的 x 开头、以 [truncated] 结尾
async def test_truncation_over_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "big.txt").write_bytes(b"x" * (MAX_READ_BYTES + 88 * 1024))
    monkeypatch.chdir(tmp_path)
    result = await ReadFileTool().invoke({"path": "big.txt"})
    assert not result.is_error
    assert result.content.endswith("[truncated]")
    # 实际文本内容恰好是 MAX_READ_BYTES 个 'x' 字符
    assert result.content.startswith("x" * MAX_READ_BYTES)


# 功能：验证恰好等于读取上限的文件不被截断（边界值：超过而非大于等于）
# 设计：boundary check，确认截断阈值为"严格超过上限"，防止 off-by-one 错误
async def test_exact_limit_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "exact.txt").write_bytes(b"y" * MAX_READ_BYTES)
    monkeypatch.chdir(tmp_path)
    result = await ReadFileTool().invoke({"path": "exact.txt"})
    assert not result.is_error
    assert not result.content.endswith("[truncated]")
    assert len(result.content) == MAX_READ_BYTES


# 功能：验证空文件返回空字符串而非 None 或错误
# 设计：零字节文件确认 content="" 的正常返回，避免调用方（LLM prompt 组装）对空内容做额外 None 判断
async def test_empty_file_returns_empty_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = await ReadFileTool().invoke({"path": "empty.txt"})
    assert not result.is_error
    assert result.content == ""
