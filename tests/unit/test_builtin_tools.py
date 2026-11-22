from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.tools.builtin.bash import BashTool
from mini_claude.core.tools.builtin.list_dir import ListDirTool
from mini_claude.core.tools.builtin.write_file import WriteFileTool

# ── bash ──────────────────────────────────────────────────────────────────────

# 功能：验证成功命令的 stdout 出现在 ToolResult.content 中，is_error 为 False
# 设计：用 echo 命令避免外部依赖，直接比较输出内容，无需 mock
@pytest.mark.asyncio
async def test_bash_success_stdout() -> None:
    result = await BashTool().invoke({"command": "echo hello"})
    assert not result.is_error
    assert "hello" in result.content


# 功能：验证非零退出码时 is_error=True 且 content 包含退出码标注
# 设计：`exit 2` 是最简单的非零退出；不依赖任何外部命令行为
@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_error() -> None:
    result = await BashTool().invoke({"command": "exit 2"})
    assert result.is_error
    assert "[exit 2]" in result.content


# 功能：验证命令超时后 is_error=True，error_type 为 "timeout"
# 设计：timeout=1s 搭配 sleep 2 必然超时；验证 error_type 而非 content，避免超时消息格式耦合
@pytest.mark.asyncio
async def test_bash_timeout() -> None:
    result = await BashTool().invoke({"command": "sleep 5", "timeout": 1})
    assert result.is_error
    assert result.error_type == "timeout"


# 功能：验证 stderr 被合并到 stdout 输出中
# 设计：只写 stderr 的命令（>&2 echo），输出应该出现在合并后的 content 里
@pytest.mark.asyncio
async def test_bash_stderr_merged() -> None:
    result = await BashTool().invoke({"command": "echo err >&2"})
    assert not result.is_error
    assert "err" in result.content


# ── write_file ────────────────────────────────────────────────────────────────

# 功能：验证 write_file 写入文件后内容可以被读取，返回字节数
# 设计：切换到临时工作区后以相对路径写入，断言文件存在且内容一致；用 tmp_path fixture 自动清理
@pytest.mark.asyncio
async def test_write_file_creates_and_returns_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = await WriteFileTool().invoke({"path": "out.txt", "content": "hello world"})
    assert not result.is_error
    assert "11" in result.content  # "hello world" = 11 bytes
    assert (tmp_path / "out.txt").read_text() == "hello world"


# 功能：验证 write_file 自动创建不存在的父目录
# 设计：路径包含两层不存在的子目录，确认写入后目录结构被创建
@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = await WriteFileTool().invoke({"path": "a/b/file.txt", "content": "x"})
    assert not result.is_error
    assert (tmp_path / "a" / "b" / "file.txt").exists()


# 功能：验证 write_file 拒绝逃出工作区的 `..` 路径并抛出 PermissionError
# 设计：工作区建在 tmp_path/ws，`../secret.txt` 指向工作区外，与 read_file 遵循相同规则
@pytest.mark.asyncio
async def test_write_file_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await WriteFileTool().invoke({"path": "../secret.txt", "content": "x"})


# 功能：验证 write_file 拒绝绝对路径
# 设计：即使目标在工作区内，绝对路径也违反工具契约，必须抛 PermissionError
@pytest.mark.asyncio
async def test_write_file_rejects_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionError):
        await WriteFileTool().invoke({"path": str(tmp_path / "x.txt"), "content": "x"})


# 功能：验证指向工作区外的符号链接无法写入外部文件
# 设计：工作区内建 symlink 指向外部文件，resolve 后越界，应抛 PermissionError
@pytest.mark.asyncio
async def test_write_file_rejects_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await WriteFileTool().invoke({"path": "link.txt", "content": "overwrite"})


# ── list_dir ──────────────────────────────────────────────────────────────────

# 功能：验证 list_dir 输出包含目录中的文件名
# 设计：在 tmp_path 创建已知结构并切换到该目录，断言文件名出现在 content 中；不约束格式细节
@pytest.mark.asyncio
async def test_list_dir_shows_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "foo.py").write_text("x")
    (tmp_path / "bar.md").write_text("y")
    monkeypatch.chdir(tmp_path)
    result = await ListDirTool().invoke({"path": "."})
    assert not result.is_error
    assert "foo.py" in result.content
    assert "bar.md" in result.content


# 功能：验证 list_dir 按 max_depth 限制递归深度（depth=1 时不展示孙级目录内容）
# 设计：创建 parent/child/grandchild 三层，depth=1 时 grandchild 不应出现在输出中
@pytest.mark.asyncio
async def test_list_dir_respects_max_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    grandchild = child / "grandchild"
    grandchild.mkdir()
    (grandchild / "deep.txt").write_text("x")

    monkeypatch.chdir(tmp_path)
    result = await ListDirTool().invoke({"path": ".", "max_depth": 1})
    assert not result.is_error
    assert "child" in result.content
    assert "deep.txt" not in result.content


# 功能：验证对不存在的路径 list_dir 抛出 FileNotFoundError
# 设计：在临时工作区内传入不存在的相对路径，预期抛出标准异常（由 invocation 层捕获）
@pytest.mark.asyncio
async def test_list_dir_missing_path_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        await ListDirTool().invoke({"path": "missing"})


# 功能：验证 list_dir 拒绝逃出工作区的 `..` 路径
# 设计：工作区建在 tmp_path/ws，`../` 指向工作区外，与 read_file 和 write_file 保持一致的安全规则
@pytest.mark.asyncio
async def test_list_dir_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError):
        await ListDirTool().invoke({"path": "../"})


# 功能：验证 list_dir 拒绝绝对路径
# 设计：绝对路径违反工具契约，即使目录存在也必须拒绝
@pytest.mark.asyncio
async def test_list_dir_rejects_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionError):
        await ListDirTool().invoke({"path": str(tmp_path)})


# 功能：验证 list_dir 不递归进入指向工作区外的符号链接目录
# 设计：工作区内建 symlink 指向外部目录，输出可包含链接名但不得包含外部目录内的文件
@pytest.mark.asyncio
async def test_list_dir_does_not_follow_symlinked_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaked.txt").write_text("x")
    (workspace / "link").symlink_to(outside, target_is_directory=True)

    monkeypatch.chdir(workspace)
    result = await ListDirTool().invoke({"path": ".", "max_depth": 3})
    assert not result.is_error
    assert "link" in result.content
    assert "leaked.txt" not in result.content
