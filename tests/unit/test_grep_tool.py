from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mini_claude.core.tools.builtin import grep as grep_mod
from mini_claude.core.tools.builtin.grep import GrepTool
from mini_claude.core.tools.limits import MAX_GREP_FILE_BYTES


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 强制走 Python 兜底，避免 CI 上 rg 存在/缺失导致输出差异
    monkeypatch.setattr(grep_mod.shutil, "which", lambda name: None)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# 功能：返回 relpath:line:text，覆盖根目录与子目录
async def test_grep_finds_matches(workspace: Path) -> None:
    (workspace / "a.txt").write_text("alpha\nBeta\n", encoding="utf-8")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.py").write_text("x = 1\nbeta again\n", encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "beta"})

    assert not result.is_error
    lines = result.content.splitlines()
    assert "a.txt:2:Beta" in lines
    assert "sub/b.py:2:beta again" in lines


# 功能：默认大小写不敏感，case_sensitive=true 时区分
async def test_grep_case_sensitivity(workspace: Path) -> None:
    (workspace / "a.txt").write_text("MixedCase\n", encoding="utf-8")
    tool = GrepTool()

    insensitive = await tool.invoke({"pattern": "mixedcase"})
    sensitive = await tool.invoke({"pattern": "mixedcase", "case_sensitive": True})

    assert "a.txt:1:MixedCase" in insensitive.content
    assert sensitive.content == ""


# 功能：glob 过滤只保留匹配文件
async def test_grep_glob_filter(workspace: Path) -> None:
    (workspace / "a.py").write_text("needle\n", encoding="utf-8")
    (workspace / "a.txt").write_text("needle\n", encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "needle", "glob": "*.py"})

    assert "a.py" in result.content
    assert "a.txt" not in result.content


# 功能：忽略依赖/缓存目录
async def test_grep_ignores_dependency_dirs(workspace: Path) -> None:
    for d in (".git", "node_modules", "src"):
        (workspace / d).mkdir()
        (workspace / d / "f.txt").write_text("needle\n", encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "needle"})

    assert "src/f.txt" in result.content
    assert ".git" not in result.content
    assert "node_modules" not in result.content


# 功能：无匹配返回空内容且非错误
async def test_grep_no_matches(workspace: Path) -> None:
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "absent"})

    assert not result.is_error
    assert result.content == ""


# 功能：非法正则与空 pattern 返回 schema_error
async def test_grep_invalid_regex(workspace: Path) -> None:
    tool = GrepTool()

    bad = await tool.invoke({"pattern": "("})
    empty = await tool.invoke({"pattern": ""})

    assert bad.is_error and bad.error_type == "schema_error"
    assert empty.is_error and empty.error_type == "schema_error"


# 功能：搜索路径逃出工作区时拒绝
async def test_grep_rejects_escape(workspace: Path) -> None:
    with pytest.raises(PermissionError):
        await GrepTool().invoke({"pattern": "x", "path": ".."})


# 功能：指向工作区的符号链接文件不泄漏外部内容
async def test_grep_skips_symlink_escape(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("secret-token\n", encoding="utf-8")
    try:
        (workspace / "link.txt").symlink_to(outside / "secret.txt")
    except OSError:
        pytest.skip("symlinks unavailable")

    result = await GrepTool().invoke({"pattern": "secret"})

    assert "secret-token" not in result.content


# 功能：max_results 达到上限后截断并标注
async def test_grep_max_results(workspace: Path) -> None:
    (workspace / "a.txt").write_text("\n".join(f"hit {i}" for i in range(5)), encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "hit", "max_results": 2})

    lines = result.content.splitlines()
    assert len([line for line in lines if not line.startswith("[")]) == 2
    assert "[truncated at 2 matches]" in result.content


# 功能：max_line_len 截断单行并追加省略号
async def test_grep_max_line_len(workspace: Path) -> None:
    (workspace / "a.txt").write_text("needle_aaaaaaaaaa\n", encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "needle", "max_line_len": 6})

    assert "…" in result.content


# 功能：path 指向单个文件时只搜索该文件
async def test_grep_single_file(workspace: Path) -> None:
    (workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    (workspace / "b.txt").write_text("needle\n", encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "needle", "path": "a.txt"})

    assert "a.txt:1:needle" in result.content
    assert "b.txt" not in result.content


# 功能：二进制文件被跳过
async def test_grep_skips_binary(workspace: Path) -> None:
    (workspace / "bin.dat").write_bytes(b"\x00needle\x00")
    (workspace / "text.txt").write_text("needle\n", encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "needle"})

    assert "text.txt" in result.content
    assert "bin.dat" not in result.content


# 功能：超过大小上限的文件被跳过
async def test_grep_skips_large_file(workspace: Path) -> None:
    (workspace / "big.txt").write_bytes(b"needle\n" + b"a" * MAX_GREP_FILE_BYTES)
    (workspace / "small.txt").write_text("needle\n", encoding="utf-8")

    result = await GrepTool().invoke({"pattern": "needle"})

    assert "small.txt" in result.content
    assert "big.txt" not in result.content


@pytest.mark.skipif(shutil.which("rg") is None, reason="requires ripgrep")
# 功能：rg 后端同样返回 relpath:line:text
async def test_grep_rg_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("needle\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await GrepTool().invoke({"pattern": "needle"})

    assert "sub/a.py:1:needle" in result.content
