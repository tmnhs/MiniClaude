from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

import pytest

from mini_claude.core.conf.model import SandboxConfig
from mini_claude.core.tools import sandbox as sandbox_mod
from mini_claude.core.tools.builtin.bash import BashTool
from mini_claude.core.tools.sandbox import SandboxUnavailableError, build_argv

# ── 纯参数构造测试（不依赖真实沙箱后端）─────────────────────────────────────────


def _fake_which(name: str) -> str | None:
    return f"/usr/bin/{name}"


# 功能：off 模式直接复用 /bin/sh -c，不探测沙箱后端
# 设计：把 which 置为 None，确认 off 分支仍能构造 argv，证明关闭沙箱时不 fail-closed
def test_off_mode_returns_plain_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: None)
    argv = build_argv("echo hi", tmp_path, SandboxConfig(mode="off"))
    assert argv == ["/bin/sh", "-c", "echo hi"]


# 功能：macOS workspace 模式生成 sandbox-exec + 写隔离 profile
# 设计：monkeypatch 平台与 which，断言 argv 结构与 profile 关键规则；不真正执行
def test_darwin_workspace_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_mod.shutil, "which", _fake_which)
    workspace = tmp_path.resolve()

    argv = build_argv("echo hi", workspace, SandboxConfig(mode="workspace"))

    assert argv[0] == "sandbox-exec"
    assert argv[1] == "-p"
    assert argv[3:] == ["/bin/sh", "-c", "echo hi"]
    profile = argv[2]
    assert "(deny file-read*" in profile
    assert '(deny file-write* (subpath "/"))' in profile
    assert f'(allow file-write* (subpath "{workspace}")' in profile
    assert "(deny network*)" not in profile


# 功能：strict 模式在 workspace 基础上追加网络禁用
def test_darwin_strict_disables_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_mod.shutil, "which", _fake_which)

    argv = build_argv("echo hi", tmp_path, SandboxConfig(mode="strict"))

    assert "(deny network*)" in argv[2]


# 功能：allow_write 相对路径按工作区解析，allow_read 支持绝对路径
def test_darwin_extra_paths_are_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_mod.shutil, "which", _fake_which)
    workspace = tmp_path.resolve()
    read_path = (tmp_path / "readme.txt").resolve()

    argv = build_argv(
        "echo hi",
        workspace,
        SandboxConfig(
            mode="workspace",
            allow_write=["scratch"],
            allow_read=[str(read_path)],
        ),
    )

    profile = argv[2]
    assert f'(allow file-write* (subpath "{(workspace / "scratch").resolve()}")' in profile
    assert f'(allow file-read* (subpath "{read_path}")' in profile


# 功能：Linux workspace 模式生成 bwrap argv，工作区可写、根只读、不禁网
def test_linux_workspace_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sandbox_mod.sys, "platform", "linux")
    monkeypatch.setattr(sandbox_mod.shutil, "which", _fake_which)
    workspace = tmp_path.resolve()

    argv = build_argv("echo hi", workspace, SandboxConfig(mode="workspace"))

    assert argv[0] == "bwrap"
    assert "--die-with-parent" in argv
    assert argv[argv.index("--ro-bind") + 1 : argv.index("--ro-bind") + 3] == ["/", "/"]
    bind_at = argv.index("--bind")
    assert argv[bind_at + 1 : bind_at + 3] == [str(workspace), str(workspace)]
    assert argv[argv.index("--chdir") + 1] == str(workspace)
    assert "--unshare-net" not in argv
    assert argv[-4:] == ["--", "/bin/sh", "-c", "echo hi"]


# 功能：Linux strict 模式追加 --unshare-net
def test_linux_strict_unshares_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod.sys, "platform", "linux")
    monkeypatch.setattr(sandbox_mod.shutil, "which", _fake_which)

    argv = build_argv("echo hi", tmp_path, SandboxConfig(mode="strict"))

    assert "--unshare-net" in argv


# 功能：缺失后端时 fail-closed 抛出 SandboxUnavailableError
# 设计：分别覆盖 macOS 与 Linux，确认错误信息可定位到需要安装的后端
@pytest.mark.parametrize(
    ("platform", "needle"),
    [("darwin", "sandbox-exec"), ("linux", "bwrap")],
)
def test_missing_backend_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    needle: str,
) -> None:
    monkeypatch.setattr(sandbox_mod.sys, "platform", platform)
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: None)

    with pytest.raises(SandboxUnavailableError) as exc_info:
        build_argv("echo hi", tmp_path, SandboxConfig(mode="workspace"))

    assert needle in str(exc_info.value)


# 功能：不支持的平台在非 off 模式下也 fail-closed
def test_unsupported_platform_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_mod.sys, "platform", "sunos5")

    with pytest.raises(SandboxUnavailableError):
        build_argv("echo hi", tmp_path, SandboxConfig(mode="workspace"))


# 功能：BashTool 遇到不可用沙箱时返回 sandbox_error，而非抛出异常
async def test_bash_tool_reports_sandbox_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_mod.sys, "platform", "sunos5")
    tool = BashTool(sandbox=SandboxConfig(mode="workspace"))

    result = await tool.invoke({"command": "echo hi"})

    assert result.is_error
    assert result.error_type == "sandbox_error"


# ── macOS 真实沙箱行为（需要 sandbox-exec）────────────────────────────────────────

_DARWIN_SANDBOX = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
darwin_only = pytest.mark.skipif(
    not _DARWIN_SANDBOX, reason="requires macOS sandbox-exec"
)


# 功能：workspace 模式下工作区内命令可用、工作区外写入被 OS 拒绝
# 设计：真实执行 sandbox-exec；工作区建在 tmp_path/ws，外部目标在 tmp_path，验证写隔离边界
@darwin_only
async def test_darwin_sandbox_confines_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = BashTool(sandbox=SandboxConfig(mode="workspace"), workspace=workspace)

    ok = await tool.invoke({"command": "echo hello"})
    assert not ok.is_error
    assert "hello" in ok.content

    inside = workspace / "in.txt"
    written = await tool.invoke({"command": f"echo in > {shlex.quote(str(inside))}"})
    assert not written.is_error
    assert inside.read_text().strip() == "in"

    outside = tmp_path / "out.txt"
    blocked = await tool.invoke({"command": f"echo out > {shlex.quote(str(outside))}"})
    assert blocked.is_error
    assert not outside.exists()
