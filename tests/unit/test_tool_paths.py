from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.tools.limits import resolve_within_workspace


# 功能：验证工作区内的普通相对路径被解析为工作区下的绝对路径
# 设计：tmp_path 作为工作区，传入 "sub/file.txt"，断言结果为 resolve 后的绝对路径
def test_relative_path_resolves_inside_workspace(tmp_path: Path) -> None:
    resolved = resolve_within_workspace("sub/file.txt", root=tmp_path)
    assert resolved == (tmp_path / "sub" / "file.txt").resolve()
    assert resolved.is_relative_to(tmp_path.resolve())


# 功能：验证 `..` 仍在工作区内时不被误杀
# 设计：传入 "a/../b.txt"，resolve 后仍等于工作区下的 b.txt，应正常返回
def test_dotdot_that_stays_inside_is_allowed(tmp_path: Path) -> None:
    resolved = resolve_within_workspace("a/../b.txt", root=tmp_path)
    assert resolved == (tmp_path / "b.txt").resolve()


# 功能：验证 `..` 逃出工作区时抛 PermissionError
# 设计：传入 "../outside.txt"，resolve 后越界，应被拒绝
def test_dotdot_escape_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(PermissionError):
        resolve_within_workspace("../outside.txt", root=workspace)


# 功能：验证绝对路径一律被拒绝（即使位于工作区内）
# 设计：传入工作区下真实存在的绝对路径，确认契约要求相对路径
def test_absolute_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        resolve_within_workspace(str(tmp_path / "x.txt"), root=tmp_path)


# 功能：验证符号链接指向工作区外时抛 PermissionError
# 设计：工作区内建一条指向外部文件的 symlink，resolve 后越界，应被拒绝
def test_symlink_escape_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)

    with pytest.raises(PermissionError):
        resolve_within_workspace("link.txt", root=workspace)


# 功能：验证未显式传 root 时以当前工作目录为边界
# 设计：把 cwd 切到 tmp_path，相对路径应落在 tmp_path 下
def test_default_root_is_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_within_workspace("file.txt")
    assert resolved == (tmp_path / "file.txt").resolve()


# 功能：验证显式 root 优先于 cwd
# 设计：cwd 在 tmp_path，但显式指定另一个工作区，结果应落在指定工作区
def test_explicit_root_overrides_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(tmp_path)
    resolved = resolve_within_workspace("file.txt", root=other)
    assert resolved == (other / "file.txt").resolve()
