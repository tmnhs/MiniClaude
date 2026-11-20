from __future__ import annotations

import json
from typing import Any

from mini_claude.core.app import CoreApp
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
