from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.conf.config import MiniClaudeConfig
from mini_claude.core.permissions.policy import DEFAULT_POLICIES, PermissionDecision
from mini_claude.core.runner import AgentRunner
from mini_claude.core.task.manager import TaskManager
from mini_claude.core.tools.builtin.edit_file import EditFileTool


# 功能：验证唯一匹配的字符串被替换，文件其余内容保持不变
# 设计：写入多行文件，替换其中一行，断言只有目标片段变化
async def test_edit_unique_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "code.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await EditFileTool().invoke(
        {"path": "code.py", "old_string": "b = 2", "new_string": "b = 20"}
    )

    assert not result.is_error
    assert "1 replacement" in result.content
    assert (tmp_path / "code.py").read_text() == "a = 1\nb = 20\nc = 3\n"


# 功能：验证 old_string 不存在时返回不可重试错误且文件不变
# 设计：传入不存在的片段，断言 is_error / error_type，并确认文件保持原样
async def test_edit_missing_old_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "code.py"
    target.write_text("hello\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await EditFileTool().invoke(
        {"path": "code.py", "old_string": "nope", "new_string": "x"}
    )

    assert result.is_error
    assert result.error_type == "schema_error"
    assert target.read_text() == "hello\n"


# 功能：验证多处匹配且未开 replace_all 时拒绝编辑，避免误改
# 设计：文件中出现两次相同片段，断言报错并提示出现次数，文件不变
async def test_edit_multiple_matches_without_replace_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "code.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await EditFileTool().invoke(
        {"path": "code.py", "old_string": "x = 1", "new_string": "x = 2"}
    )

    assert result.is_error
    assert result.error_type == "schema_error"
    assert "2 times" in result.content
    assert target.read_text() == "x = 1\nx = 1\n"


# 功能：验证 replace_all=true 时替换全部匹配
# 设计：文件中出现三次相同片段，开启 replace_all 后断言全部被替换并返回次数
async def test_edit_replace_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "code.py"
    target.write_text("x\nx\nx\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await EditFileTool().invoke(
        {"path": "code.py", "old_string": "x", "new_string": "y", "replace_all": True}
    )

    assert not result.is_error
    assert "3 replacements" in result.content
    assert target.read_text() == "y\ny\ny\n"


# 功能：验证空 old_string 被拒绝，避免 str.replace 的异常语义
# 设计：old_string 为空串时直接返回错误，且不读写文件
async def test_edit_empty_old_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "code.py"
    target.write_text("hello\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await EditFileTool().invoke(
        {"path": "code.py", "old_string": "", "new_string": "x"}
    )

    assert result.is_error
    assert result.error_type == "schema_error"
    assert target.read_text() == "hello\n"


# 功能：验证绝对路径被拒绝
# 设计：与 read_file / write_file 一致的安全边界，绝对路径抛 PermissionError
async def test_edit_rejects_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "code.py"
    target.write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PermissionError):
        await EditFileTool().invoke(
            {"path": str(target), "old_string": "x", "new_string": "y"}
        )


# 功能：验证指向工作区外的符号链接无法被编辑
# 设计：工作区内建 symlink 指向外部文件，resolve 后越界，应抛 PermissionError
async def test_edit_rejects_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    monkeypatch.chdir(workspace)

    with pytest.raises(PermissionError):
        await EditFileTool().invoke(
            {"path": "link.txt", "old_string": "secret", "new_string": "pwned"}
        )
    assert outside.read_text() == "secret\n"


# 功能：验证文件不存在时抛出 FileNotFoundError（与 read_file 行为一致）
# 设计：传入不存在的相对路径，让调用方负责错误分类
async def test_edit_missing_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        await EditFileTool().invoke(
            {"path": "missing.py", "old_string": "a", "new_string": "b"}
        )


# 功能：验证 edit_file 默认权限策略为 ASK，且已被 runner 注册
# 设计：直接断言默认策略与 _build_registry 结果，防止工具「实现了但没接上」
async def test_edit_file_registered_and_requires_approval(tmp_path: Path) -> None:
    assert DEFAULT_POLICIES["edit_file"].default is PermissionDecision.ASK

    runner = AgentRunner(MiniClaudeConfig(), runs_dir=tmp_path / "runs")
    registry = runner._build_registry(TaskManager(tmp_path / "tasks"))
    assert registry.get("edit_file") is not None
