from __future__ import annotations

import asyncio
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
    session = await first.create("chat", workspace=str(tmp_path))
    await first.close(session.id)

    app = CoreApp()
    app._sessions = SessionManager(store, lambda: None, EventBus())  # type: ignore[arg-type]

    result = await app._session_resume_handler(
        {"session_id": session.id, "workspace": str(tmp_path)}
    )

    assert result.session_id == session.id
    assert result.status == "waiting_for_input"
    assert result.run_count == 0


# 功能：run 任务异常逃逸时补发 failed 终态事件，而非静默丢弃
# 设计：fake sessions 的 send_message 立即抛 RuntimeError；订阅 _bus 捕获 run.finished，
#       用 Event 等待而非 sleep 轮询，断言 status=failed 且 reason 带错误类名
async def test_run_task_exception_publishes_failed_event() -> None:
    class _Session:
        id = "sess-1"

    class _FakeSessions:
        async def create(self, **kwargs: Any) -> _Session:
            return _Session()

        async def send_message(
            self, session_id: str, content: str, run_id: str
        ) -> None:
            raise RuntimeError("boom")

    finished: list[Any] = []
    found = asyncio.Event()

    async def _handler(event: Any) -> None:
        if getattr(event, "type", "") == "run.finished":
            finished.append(event)
            found.set()

    app = CoreApp()
    app._config = object()  # type: ignore[assignment]
    app._sessions = _FakeSessions()  # type: ignore[assignment]
    app._bus.subscribe(_handler)

    await app._agent_run_handler({"goal": "x", "workspace": "/tmp"})
    await asyncio.wait_for(found.wait(), timeout=2.0)

    assert finished[0].status == "failed"
    assert finished[0].reason.startswith("run_task_error:")
    assert finished[0].run_id


# 功能：验证 run.cancel 取消登记中的 run 任务并返回 cancelled=True
# 设计：登记一个长眠任务到 _active_runs，调用 handler 后断言任务被取消
async def test_run_cancel_cancels_active_run() -> None:
    started = asyncio.Event()

    async def _long_running() -> None:
        started.set()
        await asyncio.sleep(60)

    app = CoreApp()
    task = asyncio.create_task(_long_running())
    app._active_runs["sess-1"] = task
    await started.wait()

    result = await app._run_cancel_handler({"session_id": "sess-1"})

    assert result.cancelled is True
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


# 功能：验证无运行中 run 时 run.cancel 返回 cancelled=False
# 设计：空 _active_runs 直接调用 handler，确保不会误报取消成功
async def test_run_cancel_without_active_run_returns_false() -> None:
    app = CoreApp()

    result = await app._run_cancel_handler({"session_id": "sess-none"})

    assert result.cancelled is False


# 功能：验证 run.cancel 取消内层 send_message 后，RPC handler 仍返回结果而非被取消
# 设计：fake send_message 长时间挂起，模拟取消流程；断言 handler 正常返回空 run_id，
#       避免客户端 pending 请求永久挂起
async def test_session_send_handler_survives_run_cancel() -> None:
    class _FakeSessions:
        async def send_message(self, sid: str, content: str) -> str:
            await asyncio.sleep(60)
            return "run-never"

    app = CoreApp()
    app._sessions = _FakeSessions()  # type: ignore[assignment]

    handler = asyncio.create_task(
        app._session_send_handler({"session_id": "sess-1", "content": "hi"})
    )
    for _ in range(200):
        if "sess-1" in app._active_runs:
            break
        await asyncio.sleep(0.01)

    cancel = await app._run_cancel_handler({"session_id": "sess-1"})
    result = await asyncio.wait_for(handler, timeout=2.0)

    assert cancel.cancelled is True
    assert result.run_id == ""
    assert "sess-1" not in app._active_runs
