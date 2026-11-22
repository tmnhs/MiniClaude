from __future__ import annotations

import json
from typing import Any

from mini_claude.core.app import CoreApp
from mini_claude.core.events.bus import EventBus
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore
from mini_claude.core.transport import socket_server


class _RecordingBroadcaster:
    # 记录 subscribe 调用并返回固定 subscription id
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def subscribe(self, writer: Any, topics: list[str], scope: str = "global") -> str:
        self._order.append("subscribe")
        return "sub-test"


class _RecordingWriter:
    # 记录写入顺序，模拟 StreamWriter 的最小接口
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self._order.append("write")
        self.chunks.append(data)

    async def drain(self) -> None:
        pass


# 功能：验证 event.subscribe 先注册订阅、后写回放事件
# 设计：用共享 order 列表记录 broadcaster.subscribe 与 writer.write 的调用顺序；
#       修复前是「先回放后订阅」，回放期间的实时事件会丢失，该断言可捕获回归
async def test_subscribe_registers_before_replay(
    monkeypatch: Any, tmp_path: Any
) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "run.started", "run_id": "r1"}),
                json.dumps({"type": "step.started", "run_id": "r1"}),
                "",
            ]
        )
    )
    monkeypatch.setattr("mini_claude.core.app.events_file", lambda run_id: events_path)

    order: list[str] = []
    app = CoreApp()
    app._broadcaster = _RecordingBroadcaster(order)  # type: ignore[assignment]
    writer = _RecordingWriter(order)

    token = socket_server._writer_var.set(writer)  # type: ignore[attr-defined]
    try:
        result = await app._subscribe_handler(
            {"topics": ["run.*"], "scope": "global", "replay_from_run": "r1"}
        )
    finally:
        socket_server._writer_var.reset(token)  # type: ignore[attr-defined]

    assert result.subscription_id == "sub-test"
    assert result.replayed_count == 1  # step.started 不匹配 run.*，被过滤
    assert order[0] == "subscribe"
    assert order[1:] == ["write"]


# 功能：验证 replay_from_session 按 run 时间顺序回放整个会话的历史事件
# 设计：在临时 sessions 根目录下构造两个 run 的 events.jsonl，monkeypatch _sessions_root；
#       断言跨 run 顺序拼接、topic 过滤生效，且订阅仍先于回放写入
async def test_subscribe_replays_from_session(
    monkeypatch: Any, tmp_path: Any
) -> None:
    sid = "sess-replay"
    runs = tmp_path / sid / "runs"
    run_a = runs / "20260101-000000-aaa"
    run_a.mkdir(parents=True)
    (run_a / "events.jsonl").write_text(
        json.dumps({"type": "run.started", "run_id": "a", "session_id": sid}) + "\n"
    )
    run_b = runs / "20260102-000000-bbb"
    run_b.mkdir(parents=True)
    (run_b / "events.jsonl").write_text(
        json.dumps({"type": "llm.token", "token": "x", "session_id": sid}) + "\n"
        + json.dumps({"type": "run.finished", "run_id": "b", "session_id": sid}) + "\n"
    )
    monkeypatch.setattr("mini_claude.core.app._sessions_root", lambda: tmp_path)

    order: list[str] = []
    app = CoreApp()
    app._broadcaster = _RecordingBroadcaster(order)  # type: ignore[assignment]
    writer = _RecordingWriter(order)

    token = socket_server._writer_var.set(writer)  # type: ignore[attr-defined]
    try:
        result = await app._subscribe_handler(
            {
                "topics": ["run.*"],
                "scope": f"session:{sid}",
                "replay_from_session": sid,
            }
        )
    finally:
        socket_server._writer_var.reset(token)  # type: ignore[attr-defined]

    assert result.replayed_count == 2  # llm.token 不匹配 run.*，被过滤
    assert order[0] == "subscribe"
    replayed_types = [json.loads(c)["event"]["type"] for c in writer.chunks]
    assert replayed_types == ["run.started", "run.finished"]


# 功能：验证 replay_from_session 指向不存在的会话时回放 0 条且不报错
# 设计：会话目录缺失，断言 replayed_count=0，保证首次/空会话也能正常订阅
async def test_subscribe_replays_missing_session_returns_zero(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setattr("mini_claude.core.app._sessions_root", lambda: tmp_path)

    order: list[str] = []
    app = CoreApp()
    app._broadcaster = _RecordingBroadcaster(order)  # type: ignore[assignment]
    writer = _RecordingWriter(order)

    token = socket_server._writer_var.set(writer)  # type: ignore[attr-defined]
    try:
        result = await app._subscribe_handler(
            {
                "topics": ["run.*"],
                "scope": "session:sess-none",
                "replay_from_session": "sess-none",
            }
        )
    finally:
        socket_server._writer_var.reset(token)  # type: ignore[attr-defined]

    assert result.replayed_count == 0
    assert writer.chunks == []


# 功能：验证 session.resume handler 从磁盘加载已关闭会话并重新打开
# 设计：先用一个 manager 创建并 close 会话，再用另一个内存为空的 manager 承载 handler，
#       断言返回 waiting_for_input，覆盖 core 重启后按 sid 恢复的核心路径
async def test_session_resume_handler_loads_from_disk(tmp_path: Any) -> None:
    store = SessionStore(tmp_path)
    first = SessionManager(store, lambda: None, EventBus())  # type: ignore[arg-type]
    session = await first.create("chat")
    await first.close(session.id)

    app = CoreApp()
    app._sessions = SessionManager(store, lambda: None, EventBus())  # type: ignore[arg-type]

    result = await app._session_resume_handler({"session_id": session.id})

    assert result.session_id == session.id
    assert result.status == "waiting_for_input"
    assert result.run_count == 0
