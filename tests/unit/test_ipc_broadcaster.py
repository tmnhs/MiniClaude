from __future__ import annotations

import asyncio
import json
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from mini_claude.core.bus.events import (
    LlmTokenEvent,
    RunStartedEvent,
    SessionWaitingForInputEvent,
    StepStartedEvent,
)
from mini_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster


def _make_writer(*, drain_raises: Exception | None = None) -> asyncio.StreamWriter:
    writer = MagicMock(spec=asyncio.StreamWriter)
    if drain_raises is not None:
        writer.drain = AsyncMock(side_effect=drain_raises)
    else:
        writer.drain = AsyncMock()
    return cast(asyncio.StreamWriter, writer)


def _run_started(run_id: str = "r1") -> RunStartedEvent:
    return RunStartedEvent(run_id=run_id, goal="test", ts="2026-01-01T00:00:00Z")


# 功能：验证 subscribe 后 handle 将匹配 topic 的事件写入 writer，且内容是合法的 EventPushEnvelope
# 设计：用 MagicMock writer 捕获写入的字节，反序列化后断言 kind 和 event.type，排除对网络层的依赖
async def test_subscriber_receives_matching_event() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])

    await broadcaster.handle(_run_started())

    writer.write.assert_called_once()  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["kind"] == "event"
    assert data["event"]["type"] == "run.started"


# 功能：验证无订阅时 handle 不向任何 writer 写入数据
# 设计：创建 broadcaster 但不 subscribe，调用 handle 后断言 write 从未被调用，验证空 fan-out 的边界情况
async def test_no_subscription_no_write() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()

    await broadcaster.handle(_run_started())

    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证 topic glob "step.*" 匹配 step.started 但不匹配 run.started
# 设计：向同一 broadcaster 发布两种事件，断言 write 只被调用一次，验证 fnmatch 语义的 glob 边界行为
async def test_topic_glob_matches_step_not_run() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["step.*"])

    step_event = StepStartedEvent(run_id="r1", step=1, ts="2026-01-01T00:00:00Z")
    run_event = _run_started()

    await broadcaster.handle(step_event)
    await broadcaster.handle(run_event)

    assert writer.write.call_count == 1  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["event"]["type"] == "step.started"


# 功能：验证 scope="global" 的订阅能收到任意 run_id 的事件
# 设计：发布两个不同 run_id 的事件，断言两次都写入，确认 global scope 不过滤 run_id 字段
async def test_scope_global_receives_all_run_ids() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="global")

    await broadcaster.handle(_run_started("r1"))
    await broadcaster.handle(_run_started("r2"))

    assert writer.write.call_count == 2  # type: ignore[attr-defined]


# 功能：验证 scope="run:<id>" 只接收匹配 run_id 的事件，过滤其他 run_id
# 设计：订阅 scope="run:abc"，发布 run_id="abc" 和 run_id="xyz"，断言只写入一次，验证 run-specific scope 的过滤语义
async def test_scope_run_specific_filters_other_run_ids() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="run:abc")

    await broadcaster.handle(_run_started("abc"))
    await broadcaster.handle(_run_started("xyz"))

    assert writer.write.call_count == 1  # type: ignore[attr-defined]


# 功能：验证 unsubscribe 后 handle 不再向该 writer 发送事件
# 设计：先 subscribe 再 unsubscribe，再调用 handle，断言 write 从未被调用，验证订阅生命周期的正确性
async def test_unsubscribe_stops_delivery() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])
    broadcaster.unsubscribe(writer)

    await broadcaster.handle(_run_started())

    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证写入失败（ConnectionResetError）后订阅自动移除，下次 handle 不再尝试写入
# 设计：drain() 抛出 ConnectionResetError 触发死连接清理；断言第二次 handle 时 write 未被调用；
#       第一次 write 在 drain 前已执行，call_count==1 是预期行为而非被测点
async def test_dead_connection_removed_after_failure() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer(drain_raises=ConnectionResetError())
    broadcaster.subscribe(writer, topics=["run.*"])

    event = _run_started()
    await broadcaster.handle(event)  # 消费失败 → 订阅被移除

    assert writer.write.call_count == 1  # type: ignore[attr-defined]

    writer.write.reset_mock()  # type: ignore[attr-defined]
    await broadcaster.handle(event)  # 已无订阅者剩余
    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证 scope="session:<id>" 只接收 session_id 匹配的事件
# 设计：订阅 session:A，发布带 session A / B 的事件，断言只写入一次，确认会话级隔离
async def test_scope_session_specific_filters_other_sessions() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["llm.token"], scope="session:A")

    await broadcaster.handle(
        LlmTokenEvent(run_id="r1", token="a", session_id="A", ts="2026-01-01T00:00:00Z")
    )
    await broadcaster.handle(
        LlmTokenEvent(run_id="r2", token="b", session_id="B", ts="2026-01-01T00:00:00Z")
    )

    assert writer.write.call_count == 1  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["event"]["token"] == "a"


# 功能：验证 session scope 下没有 session_id 的事件被丢弃（旧事件不串屏）
# 设计：发布不带 session_id 的 run.started，订阅 session:A，断言不写入
async def test_scope_session_drops_event_without_session_id() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="session:A")

    await broadcaster.handle(_run_started("r1"))

    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证 session 事件（session.waiting_for_input）按 session_id 匹配
# 设计：该类事件只有 session_id 没有 run_id，确认 scope 对 session 事件同样生效
async def test_scope_session_matches_session_events() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["session.*"], scope="session:A")

    await broadcaster.handle(
        SessionWaitingForInputEvent(
            session_id="A", last_run_id="r1", ts="2026-01-01T00:00:00Z"
        )
    )
    await broadcaster.handle(
        SessionWaitingForInputEvent(
            session_id="B", last_run_id="r2", ts="2026-01-01T00:00:00Z"
        )
    )

    assert writer.write.call_count == 1  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["event"]["session_id"] == "A"


# 功能：验证未知 scope 不投递任何事件
# 设计：订阅 scope="bogus:x"，断言 handle 后没有写入，避免非法 scope 意外全通
async def test_unknown_scope_delivers_nothing() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="bogus:x")

    await broadcaster.handle(_run_started("r1"))

    writer.write.assert_not_called()  # type: ignore[attr-defined]
