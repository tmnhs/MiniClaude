from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mini_claude.core.tools.builtin.glob import GlobParams, GlobTool


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


# 功能：** 递归匹配并返回工作区相对路径
async def test_glob_recursive(workspace: Path) -> None:
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("", encoding="utf-8")
    (workspace / "b.py").write_text("", encoding="utf-8")

    result = await GlobTool().invoke({"pattern": "**/*.py"})

    assert set(result.content.splitlines()) == {"b.py", "src/a.py"}


# 功能：忽略依赖/缓存目录
async def test_glob_ignores_dependency_dirs(workspace: Path) -> None:
    (workspace / ".git").mkdir()
    (workspace / ".git" / "x.py").write_text("", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "y.py").write_text("", encoding="utf-8")
    (workspace / "keep.py").write_text("", encoding="utf-8")

    result = await GlobTool().invoke({"pattern": "**/*.py"})

    assert result.content.splitlines() == ["keep.py"]


# 功能：隐藏文件可被匹配
async def test_glob_includes_hidden(workspace: Path) -> None:
    (workspace / ".hidden.py").write_text("", encoding="utf-8")

    result = await GlobTool().invoke({"pattern": ".*.py"})

    assert ".hidden.py" in result.content


# 功能：目录不进入结果，只返回文件
async def test_glob_returns_files_only(workspace: Path) -> None:
    (workspace / "dir.py").mkdir()
    (workspace / "file.py").write_text("", encoding="utf-8")

    result = await GlobTool().invoke({"pattern": "*.py"})

    assert result.content.splitlines() == ["file.py"]


# 功能：max_results 达到上限后截断并标注
async def test_glob_max_results(workspace: Path) -> None:
    for i in range(3):
        (workspace / f"f{i}.py").write_text("", encoding="utf-8")

    result = await GlobTool().invoke({"pattern": "*.py", "max_results": 2})

    lines = result.content.splitlines()
    assert len([line for line in lines if not line.startswith("[")]) == 2
    assert "[truncated at 2 results]" in result.content


# 功能：子目录搜索的路径仍相对工作区展示
async def test_glob_search_subdirectory(workspace: Path) -> None:
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "m.py").write_text("", encoding="utf-8")

    result = await GlobTool().invoke({"pattern": "*.py", "path": "pkg"})

    assert result.content.splitlines() == ["pkg/m.py"]


# 功能：无匹配返回空内容且非错误
async def test_glob_no_matches(workspace: Path) -> None:
    result = await GlobTool().invoke({"pattern": "*.nomatch"})

    assert not result.is_error
    assert result.content == ""


# 功能：指向工作区外的符号链接文件被排除
async def test_glob_excludes_symlink_escape(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.py").write_text("", encoding="utf-8")
    try:
        (workspace / "link.py").symlink_to(outside / "secret.py")
    except OSError:
        pytest.skip("symlinks unavailable")

    result = await GlobTool().invoke({"pattern": "*.py"})

    assert "link.py" not in result.content


# 功能：path 指向非目录时拒绝
async def test_glob_rejects_non_directory(workspace: Path) -> None:
    (workspace / "file.txt").write_text("x", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        await GlobTool().invoke({"pattern": "*", "path": "file.txt"})


# 功能：非法 pattern 在参数校验阶段拒绝
@pytest.mark.parametrize("pattern", ["", "/abs/*.py", "~/*.py", "../*.py"])
def test_glob_rejects_bad_pattern(pattern: str) -> None:
    with pytest.raises(ValidationError):
        GlobParams(pattern=pattern)
