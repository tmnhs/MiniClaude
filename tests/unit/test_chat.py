from __future__ import annotations

import asyncio
from typing import Any

from mini_claude.cli.commands import chat as chat_mod
from mini_claude.core.conf.config import MiniClaudeConfig


class _FakeClient:
    # 记录命令顺序，并按方法返回最小可用结果
    def __init__(self, *, fail_on_subscribe: bool = False) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self._done: asyncio.Event = asyncio.Event()
        self._fail_on_subscribe = fail_on_subscribe

    async def connect(self) -> None:
        pass

    def on_event(self, handler: Any) -> None:
        pass

    async def run_event_loop(self) -> None:
        await self._done.wait()

    async def send_command(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.commands.append((method, params))
        if method == "event.subscribe" and self._fail_on_subscribe:
            raise asyncio.CancelledError()
        if method == "session.create":
            return {"session_id": params.get("session_id"), "status": "active"}
        return {}

    async def close(self) -> None:
        pass


# 功能：验证 chat 订阅发生在 session.create 之前，且 create 复用预生成的 session_id
# 设计：fake client 记录命令顺序；_readline 立即 EOF 结束循环，
#       断言顺序为 subscribe → create → close，且 create 参数携带同一 id
async def test_subscribe_before_create_uses_pregenerated_id(
    monkeypatch: Any,
) -> None:
    fake = _FakeClient()

    async def _eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(chat_mod, "SocketClient", lambda host, port: fake)
    monkeypatch.setattr(chat_mod, "_readline", _eof)

    code = await chat_mod._chat_async(MiniClaudeConfig())

    assert code == 0
    methods = [m for m, _ in fake.commands]
    assert methods == ["event.subscribe", "session.create", "session.close"]

    sub_params = fake.commands[0][1]
    create_params = fake.commands[1][1]
    session_id = create_params["session_id"]
    assert session_id.startswith("sess-")
    assert sub_params["scope"] == f"session:{session_id}"
    assert fake.commands[2][1]["session_id"] == session_id


# 功能：验证订阅阶段断连不抛 CancelledError，而是返回错误码
# 设计：event.subscribe 抛 CancelledError（pending future 被取消的等价路径），
#       断言 _chat_async 返回 1；此时尚未 create，不应发送 session.close
async def test_disconnect_during_subscribe_returns_error(monkeypatch: Any) -> None:
    fake = _FakeClient(fail_on_subscribe=True)
    monkeypatch.setattr(chat_mod, "SocketClient", lambda host, port: fake)

    code = await chat_mod._chat_async(MiniClaudeConfig())

    assert code == 1
    assert [m for m, _ in fake.commands] == ["event.subscribe"]
