from __future__ import annotations

import asyncio
import sys
import threading
import uuid
from typing import Any

from mini_claude.core.conf.config import MiniClaudeConfig
from mini_claude.core.transport.socket_client import IpcError, SocketClient

_DECISION_MAP: dict[str, str] = {
    "y": "allow_once",
    "a": "always_allow",
    "n": "deny_once",
    "d": "always_deny",
}

class ChatPrinter:
    # 初始化 chat 模式的流式输出状态和待审批权限请求
    def __init__(self) -> None:
        self._inline = False
        self.pending_permission_id: str | None = None
        self.pending_risk: str = "low"

    # 当前可选审批项；高风险命令不提供 always allow
    def _options_text(self) -> str:
        if self.pending_risk == "high":
            return "  y=allow once  n=deny once  d=always deny"
        return "  y=allow once  a=always allow  n=deny once  d=always deny"

    # 若当前 LLM token 尚未换行，则补一个换行
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 按事件类型打印 chat 输出和等待提示
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True
        elif t == "tool.call_started":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "permission.requested":
            self._ensure_newline()
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            tool_use_id = str(event.get("tool_use_id", ""))
            risk_level = str(event.get("risk_level", "low"))
            affected = [str(p) for p in (event.get("affected_paths") or [])]
            print(f"[permission] {tool_name}  [{risk_level}]  {param_preview}")
            if affected:
                print(f"  paths: {', '.join(affected)}")
            self.pending_permission_id = tool_use_id
            self.pending_risk = risk_level
            print(self._options_text())
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            self.pending_permission_id = None
            self.pending_risk = "low"
            print("[waiting for input]")
        elif t == "session.closed":
            self._ensure_newline()
            print("session closed.")

# 在守护线程中读取 stdin，避免阻塞 socket event loop
# 不用 run_in_executor：默认线程池的线程是非守护线程，Ctrl+C 取消等待后仍阻塞在 input()，
# 解释器退出时 threading._shutdown 会 join 它们，导致挂起或 "Exception ignored" 报错
async def _readline(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def _deliver(result: str | BaseException) -> None:
        if future.done():
            return
        if isinstance(result, BaseException):
            future.set_exception(result)
        else:
            future.set_result(result)

    def _worker() -> None:
        try:
            line = input(prompt)
        except BaseException as exc:
            loop.call_soon_threadsafe(_deliver, exc)
        else:
            loop.call_soon_threadsafe(_deliver, line)

    threading.Thread(target=_worker, name="mc-chat-readline", daemon=True).start()
    return await future


# 异步核心：创建 chat session，循环读取用户输入并发送到 daemon
async def _chat_async(config: MiniClaudeConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = ChatPrinter()
    client.on_event(printer.handle)
    loop_task = asyncio.create_task(client.run_event_loop())

    # 预生成 id，先订阅再创建：create 期间的事件不会因订阅未就绪而丢失
    session_id = f"sess-{uuid.uuid4().hex[:12]}"
    exit_code = 0
    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["session.*", "run.*", "tool.*", "llm.token", "permission.*"],
                "scope": f"session:{session_id}",
            },
        )
        await client.send_command(
            "session.create", {"mode": "chat", "session_id": session_id}
        )
        print(f"[session: {session_id}]")

        while True:
            try:
                line = await _readline("> ")
            except (EOFError, KeyboardInterrupt):
                break

            content = line.strip()
            if not content:
                continue

            # 有待审批的权限请求时，将用户输入解释为决策而非聊天消息
            if printer.pending_permission_id:
                decision = _DECISION_MAP.get(content.lower())
                if decision == "always_allow" and printer.pending_risk == "high":
                    decision = None
                if decision is None:
                    print(printer._options_text())
                    continue
                tool_use_id = printer.pending_permission_id
                printer.pending_permission_id = None
                printer.pending_risk = "low"
                await client.send_command(
                    "permission.respond",
                    {
                        "tool_use_id": tool_use_id,
                        "decision": decision,
                        "session_id": session_id,
                    },
                )
                continue

            await client.send_command(
                "session.send_message",
                {"session_id": session_id, "content": content},
            )

        await client.send_command("session.close", {"session_id": session_id})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        exit_code = 1
    except asyncio.CancelledError:
        # pending RPC future 被取消 = 连接断开；真正的任务取消继续向上传播
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        print("error: connection closed", file=sys.stderr)
        exit_code = 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()

    return exit_code


# 执行 mc chat 命令
def cmd_chat(config: MiniClaudeConfig) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)