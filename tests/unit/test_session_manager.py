from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.events.bus import EventBus
from mini_claude.core.runner import RunOutcome
from mini_claude.core.session.manager import (
    SESSION_CLOSED,
    SESSION_ID_CONFLICT,
    SESSION_ID_INVALID,
    SESSION_NOT_FOUND,
    SessionManager,
)
from mini_claude.core.session.model import Session
from mini_claude.core.session.store import SessionStore


class _Runner:
    # 模拟 AgentRunner，将 run 新消息写入 thread 后返回成功
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        assert run_id is not None
        assert session is not None
        assert store is not None
        store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": f"done {goal}"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


# 功能：验证 create 会创建 active session、写入 meta 并发布 session.created 事件
# 设计：用真实 SessionStore + EventBus 收集事件，覆盖 manager 与 store/bus 的协作边界
async def test_create_session_writes_meta_and_event(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    session = await manager.create("chat", "title")

    assert session.status == "active"
    assert store.read_meta(session.id).title == "title"
    assert [e.type for e in events] == ["session.created"]  # type: ignore[attr-defined]


# 功能：验证 chat session 处理一条消息后进入 waiting_for_input，并保留 user/assistant thread
# 设计：mock runner 主动追加 assistant 消息，确认 send_message 负责 user 消息、状态流转和 run_id 记录
async def test_send_message_chat_enters_waiting_and_writes_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(session.id, "hello")

    loaded = store.read_meta(session.id)
    assert loaded.status == "waiting_for_input"
    assert loaded.run_ids == [run_id]
    messages = store.read_messages(session.id)
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1]["role"] == "assistant"


# 功能：验证 one_shot session 在单次消息完成后自动 closed
# 设计：复用 mock runner 的成功路径，聚焦 mode 对最终状态的影响，保证 mc run 的统一路径正确
async def test_one_shot_auto_closes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    await manager.send_message(session.id, "hello")

    assert store.read_meta(session.id).status == "closed"


# 功能：验证不存在的 session_id 返回 session_not_found 错误码
# 设计：直接调用 get_history 的查找路径，断言 HandlerError code，覆盖 IPC handler 可结构化返回错误
async def test_missing_session_raises_handler_error(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.get_history("missing")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证 closed session 不能继续 send_message
# 设计：先显式 close，再发送消息，断言 session_closed 错误码，覆盖状态机拒绝路径
async def test_closed_session_rejects_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.close(session.id)

    with pytest.raises(HandlerError) as exc:
        await manager.send_message(session.id, "again")
    assert exc.value.code == SESSION_CLOSED


# 功能：验证客户端预生成的 session_id 被直接采用并持久化
# 设计：create 时传入固定 id，断言返回值和 meta 一致，覆盖"先订阅再创建"的 id 复用路径
async def test_create_uses_client_provided_session_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    session = await manager.create("chat", session_id="sess-client")

    assert session.id == "sess-client"
    assert store.read_meta("sess-client").id == "sess-client"


# 功能：验证重复的预生成 session_id 被拒绝，不会覆盖已有会话
# 设计：同一 id 创建两次，断言错误码为 SESSION_ID_CONFLICT
async def test_create_duplicate_session_id_raises(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    await manager.create("chat", session_id="sess-dup")

    with pytest.raises(HandlerError) as exc:
        await manager.create("chat", session_id="sess-dup")
    assert exc.value.code == SESSION_ID_CONFLICT


# 功能：验证 SessionManager 自身拒绝非法 session_id（绕过命令层时的兜底校验）
# 设计：直接调用 create 传入路径穿越字符串，断言 SESSION_ID_INVALID 且未创建目录
async def test_create_rejects_invalid_session_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    with pytest.raises(HandlerError) as exc:
        await manager.create("chat", session_id="../../escape")
    assert exc.value.code == SESSION_ID_INVALID
    assert not (tmp_path.parent / "escape").exists()


# 功能：验证 resume 能把磁盘上的 closed 会话重新加载进内存并重新打开
# 设计：同一 store 构造两个 manager 模拟 core 重启；第二个内存为空，resume 后应
#       从 meta 加载、状态回到 waiting_for_input、历史可读、并能继续追加消息
async def test_resume_loads_closed_session_from_disk(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    first = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await first.create("chat")
    await first.send_message(session.id, "hello")
    await first.close(session.id)
    assert store.read_meta(session.id).status == "closed"

    second = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    resumed = await second.resume(session.id)

    assert resumed.status == "waiting_for_input"
    assert store.read_meta(session.id).status == "waiting_for_input"
    assert len(await second.get_history(session.id)) >= 2
    run_id = await second.send_message(session.id, "again")
    assert store.read_meta(session.id).run_ids[-1] == run_id


# 功能：验证 resume 不存在的 session 返回 session_not_found
# 设计：内存和磁盘都没有该 id，断言结构化错误码，供 TUI 明确提示用户检查 sid
async def test_resume_missing_session_raises(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.resume("sess-missing")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证 resume 拒绝非法 session_id（防止路径穿越去读任意 meta.json）
# 设计：传入含 "../" 的 id，断言 SESSION_ID_INVALID 且不触发磁盘读取
async def test_resume_rejects_invalid_session_id(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.resume("../../etc")
    assert exc.value.code == SESSION_ID_INVALID


# 功能：验证 resume 会发布 session.resumed 事件
# 设计：订阅 EventBus，close 后清空事件，再 resume，断言只收到一条 session.resumed
async def test_resume_publishes_resumed_event(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    first = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]
    session = await first.create("chat")
    await first.close(session.id)
    events.clear()

    second = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]
    await second.resume(session.id)

    assert [e.type for e in events] == ["session.resumed"]  # type: ignore[attr-defined]
