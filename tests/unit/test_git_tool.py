from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from mini_claude.core.tools.builtin import git as git_mod
from mini_claude.core.tools.builtin.git import GitParams, GitTool, build_git_argv
from mini_claude.core.tools.process import ProcessResult

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires git")


def _init_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# 功能：status 返回未跟踪文件
async def test_git_status(repo: Path) -> None:
    (repo / "f.txt").write_text("hi", encoding="utf-8")

    result = await GitTool().invoke({"subcommand": "status", "args": ["--porcelain"]})

    assert not result.is_error
    assert "f.txt" in result.content


# 功能：add + commit 后 log 可见提交信息
async def test_git_add_commit_log(repo: Path) -> None:
    (repo / "f.txt").write_text("hi", encoding="utf-8")
    tool = GitTool()

    add = await tool.invoke({"subcommand": "add", "args": ["f.txt"]})
    commit = await tool.invoke({"subcommand": "commit", "message": "initial"})
    log = await tool.invoke({"subcommand": "log", "args": ["--oneline"]})

    assert not add.is_error
    assert not commit.is_error
    assert not log.is_error
    assert "initial" in log.content


# 功能：diff 展示未暂存的修改
async def test_git_diff(repo: Path) -> None:
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    tool = GitTool()
    await tool.invoke({"subcommand": "add", "args": ["f.txt"]})
    await tool.invoke({"subcommand": "commit", "message": "base"})
    (repo / "f.txt").write_text("hi2\n", encoding="utf-8")

    result = await tool.invoke({"subcommand": "diff", "args": ["f.txt"]})

    assert not result.is_error
    assert "hi2" in result.content


# 功能：show 展示 HEAD 提交内容
async def test_git_show(repo: Path) -> None:
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    tool = GitTool()
    await tool.invoke({"subcommand": "add", "args": ["f.txt"]})
    await tool.invoke({"subcommand": "commit", "message": "base"})

    result = await tool.invoke({"subcommand": "show", "args": ["--stat", "HEAD"]})

    assert not result.is_error
    assert "f.txt" in result.content


# 功能：branch 列表可读（分支名随 git 默认值不同）
async def test_git_branch(repo: Path) -> None:
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    tool = GitTool()
    await tool.invoke({"subcommand": "add", "args": ["f.txt"]})
    await tool.invoke({"subcommand": "commit", "message": "base"})

    result = await tool.invoke({"subcommand": "branch"})

    assert not result.is_error
    assert result.content.strip()


# 功能：不在 git 仓库中返回可读错误
async def test_git_outside_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "plain"
    work.mkdir()
    monkeypatch.chdir(work)

    result = await GitTool().invoke({"subcommand": "status"})

    assert result.is_error
    assert "not a git repository" in result.content.lower()


# 功能：非白名单子命令在参数校验阶段拒绝
async def test_git_rejects_unknown_subcommand() -> None:
    with pytest.raises(ValidationError):
        GitParams(**{"subcommand": "push"})

    with pytest.raises(ValidationError):
        await GitTool().invoke({"subcommand": "push"})


# 功能：危险选项在参数校验阶段拒绝
@pytest.mark.parametrize(
    "arg",
    [
        "-C",
        "-c",
        "-C/tmp",
        "--git-dir=/x",
        "--work-tree=/x",
        "--exec-path=/x",
        "--config-env=a=b",
        "--output=/x",
        "--force",
        "--force-with-lease",
        "--ext-diff",
        "--textconv",
    ],
)
def test_git_rejects_dangerous_flags(arg: str) -> None:
    with pytest.raises(ValidationError):
        GitParams(subcommand="status", args=[arg])


# 功能：commit 必须有非空 message，非 commit 禁止 message
def test_git_message_constraints() -> None:
    with pytest.raises(ValidationError):
        GitParams(subcommand="commit")
    with pytest.raises(ValidationError):
        GitParams(subcommand="commit", message="   ")
    with pytest.raises(ValidationError):
        GitParams(subcommand="status", message="nope")


# 功能：参数不经 shell，注入片段不会被执行
async def test_git_args_not_shell_interpreted(repo: Path) -> None:
    sentinel = repo / "pwned"
    (repo / "f.txt").write_text("hi", encoding="utf-8")

    await GitTool().invoke({"subcommand": "add", "args": [f"; touch {sentinel}"]})

    assert not sentinel.exists()


# 功能：argv 结构与安全环境变量正确
async def test_git_argv_and_env(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> ProcessResult:
        captured["argv"] = argv
        captured["env"] = env
        return ProcessResult(output="ok", returncode=0)

    monkeypatch.setattr(git_mod, "run_process", fake_run)

    await GitTool().invoke({"subcommand": "diff", "args": ["f.txt"]})

    argv = captured["argv"]
    env = captured["env"]
    assert isinstance(argv, list)
    assert argv[:4] == ["git", "-c", "core.fsmonitor=false", "diff"]
    assert "--no-textconv" in argv
    assert isinstance(env, dict)
    assert env["GIT_PAGER"] == "cat"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# 功能：build_git_argv 纯函数结构
def test_build_git_argv() -> None:
    assert build_git_argv("diff", ["f.txt"]) == [
        "git",
        "-c",
        "core.fsmonitor=false",
        "diff",
        "--no-textconv",
        "f.txt",
    ]
    assert build_git_argv("commit", [], "msg") == [
        "git",
        "-c",
        "core.fsmonitor=false",
        "commit",
        "-m",
        "msg",
    ]
    assert build_git_argv("status", ["--porcelain"]) == [
        "git",
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain",
    ]
