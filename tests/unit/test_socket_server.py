from __future__ import annotations

import asyncio
import json
import socket

import pytest

from mini_claude.core.transport import socket_server as socket_server_mod
from mini_claude.core.transport.socket_server import SocketServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 功能：验证客户端断开后 SocketServer 调用 broadcaster.unsubscribe(writer) 清理订阅
# 设计：用内联 MockBroadcaster 捕获 unsubscribe 调用并设置 asyncio.Event，避免 sleep 轮询；
#       等待 Event 而非断言调用次数，确保时序正确性而不依赖竞态假设
async def test_broadcaster_unsubscribe_called_on_disconnect() -> None:
    unsubscribed = asyncio.Event()

    class MockBroadcaster:
        def unsubscribe(self, writer: object) -> None:
            unsubscribed.set()

    port = _free_port()
    server = SocketServer("127.0.0.1", port, broadcaster=MockBroadcaster())  # type: ignore[arg-type]
    await server.start()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()

        await asyncio.wait_for(unsubscribed.wait(), timeout=2.0)
    finally:
        await server.stop()


# 功能：验证不传入 broadcaster 时 SocketServer 仍可正常启动和停止（backward-compatible 默认值）
# 设计：直接实例化 SocketServer(host, port)（无 broadcaster），start/stop 不抛异常即为通过；
#       回归测试确保新参数的默认值 None 不破坏现有调用方
async def test_no_broadcaster_server_starts_and_stops() -> None:
    port = _free_port()
    server = SocketServer("127.0.0.1", port)
    await server.start()
    await server.stop()


# 功能：超过帧上限的行应收到 INVALID_REQUEST 错误帧，随后连接被关闭
# 设计：把模块级 _MAX_LINE_BYTES 调小后再启动服务器（start_server(limit=...) 在 start 时求值），
#       发送无换行的超长数据触发 readline 的 ValueError 路径，验证错误帧 + EOF 而非静默断开
async def test_oversize_frame_returns_error_then_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket_server_mod, "_MAX_LINE_BYTES", 1024)
    port = _free_port()
    server = SocketServer("127.0.0.1", port)
    await server.start()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x" * 4096)
        await writer.drain()

        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        msg = json.loads(line)
        assert msg["error"]["code"] == -32600
        assert "Request too large" in msg["error"]["message"]

        rest = await asyncio.wait_for(reader.read(), timeout=2.0)
        assert rest == b""

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
