from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.events.bus import EventBus
from mini_claude.core.runner import RunOutcome
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.model import Session
from mini_claude.core.session.store import SessionStore
from mini_claude.core.tools.builtin.read_file import ReadFileTool
from mini_claude.core.tools.builtin.write_file import WriteFileTool
from mini_claude.core.workspace import normalize_workspace


# 功能：未提供 workspace 时直接报错，不回退到 daemon cwd
def test_normalize_workspace_requires_value() -> None:
    for bad in (None, ""):
        with pytest.raises(HandlerError):
            normalize_workspace(bad)


# 功能：normalize_workspace 拒绝相对路径
def test_normalize_workspace_rejects_relative() -> None:
    with pytest.raises(HandlerError):
        normalize_workspace("relative/dir")


# 功能：normalize_workspace 拒绝不存在的绝对路径
def test_normalize_workspace_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(HandlerError):
        normalize_workspace(str(tmp_path / "nope"))


# 功能：normalize_workspace 返回解析后的绝对目录
def test_normalize_workspace_resolves(tmp_path: Path) -> None:
    assert normalize_workspace(str(tmp_path)) == tmp_path.resolve()


# 功能：注入 workspace 的写/读工具以该目录为根，而非进程 cwd
async def test_tools_use_injected_workspace(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    write = WriteFileTool(workspace=tmp_path)
    read = ReadFileTool(workspace=tmp_path)

    await write.invoke({"path": "sub/hello.txt", "content": "hi"})
    assert (tmp_path / "sub" / "hello.txt").read_text() == "hi"

    result = await read.invoke({"path": "sub/hello.txt"})
    assert result.content == "hi"


# 功能：注入 workspace 后仍拒绝越界路径
async def test_tools_reject_escape(tmp_path: Path) -> None:
    read = ReadFileTool(workspace=tmp_path)
    with pytest.raises(PermissionError):
        await read.invoke({"path": "../outside.txt"})


class _RecordingRunner:
    # 记录 manager 传给 runner 的 workspace，并写入最小 thread 以完成 run
    def __init__(self) -> None:
        self.workspaces: list[Path | None] = []

    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
        workspace: Path | None = None,
    ) -> RunOutcome:
        self.workspaces.append(workspace)
        assert session is not None and store is not None and run_id is not None
        store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="ok", reason=None)


# 功能：create 持久化 workspace，send_message 把它传给 runner
async def test_session_persists_and_passes_workspace(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: runner, EventBus())  # type: ignore[arg-type]

    session = await manager.create("chat", workspace=str(tmp_path))
    assert session.workspace == str(tmp_path.resolve())
    assert store.read_meta(session.id).workspace == str(tmp_path.resolve())

    await manager.send_message(session.id, "hello")
    assert runner.workspaces == [tmp_path.resolve()]
