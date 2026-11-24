from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import mini_claude
from mini_claude.core.bus.commands import (
    AgentRunCommand, 
    AgentRunResult, 
    EventSubscribeCommand, 
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult, 
    PongResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionResumeCommand,
    SessionResumeResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
)
from mini_claude.core.bus.envelope import EventPushEnvelope
from mini_claude.core.bus.events import RunFinishedEvent
from mini_claude.core.conf.config import MiniClaudeConfig, get_config
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.provider import AnthropicProvider
from mini_claude.core.log.logging_setup import setup_logging
from mini_claude.core.mcp.server import McpServerManager
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.permissions.store import load_policy_file
from mini_claude.core.runner import AgentRunner
from mini_claude.core.session.manager import SessionManager
from mini_claude.core.session.store import SessionStore
from mini_claude.core.trace.record import TraceRecord
from mini_claude.core.trace.writer import TraceWriter
from mini_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from mini_claude.core.transport.socket_server import SocketServer, get_connection_writer
from mini_claude.core.utils.runs_dir import events_file, new_run_id
from mini_claude.core.utils.time import now_iso

logger = logging.getLogger(__name__)


# session 文件根目录；抽成函数便于测试注入临时目录
def _sessions_root() -> Path:
    return Path("~/.mc/sessions").expanduser()


class CoreApp():
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: MiniClaudeConfig | None = None
        self._running_runs: set[asyncio.Task[None]] = set()
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)

        return PongResult(
            server_version=mini_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=now_iso(),
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._config is not None
        assert self._sessions is not None

        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(mode="one_shot", title=cmd.goal[:40])

        run_id = new_run_id()
        run_task = asyncio.create_task(
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
        )

        self._running_runs.add(run_task)
        run_task.add_done_callback(lambda t: self._on_run_done(t, run_id, session.id))
        return AgentRunResult(run_id=run_id)

    # 清理已完成的 run 任务；若异常逃逸，记录日志并补发终态事件，避免 run 静默失败
    def _on_run_done(
        self, task: asyncio.Task[Any], run_id: str, session_id: str
    ) -> None:
        self._running_runs.discard(task)

        # 取消由 runner 内部自行标记终态，这里无需处理
        if task.cancelled():
            return

        exc = task.exception()
        if exc is None:
            return

        logger.error("run task failed run_id=%s", run_id, exc_info=exc)
        # 正常路径下 send_message 已发布终态；此处仅异常逃逸时补发，
        # 极端情况下可能与 runner 的终态重复，属可接受的错误路径冗余
        asyncio.create_task(
            self._publish_run_failed(run_id, session_id, type(exc).__name__)
        )

    # 为异常终止的 run 补发 failed 终态事件
    async def _publish_run_failed(
        self, run_id: str, session_id: str, err_name: str
    ) -> None:
        await self._bus.publish(
            RunFinishedEvent(
                run_id=run_id,
                status="failed",
                reason=f"run_task_error:{err_name}",
                steps=0,
                session_id=session_id,
                ts=now_iso(),
            )
        )

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(
            mode=cmd.mode, title=cmd.title, session_id=cmd.session_id
        )
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = await self._sessions.send_message(cmd.session_id, cmd.content)
        return SessionSendMessageResult(run_id=run_id)

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    # 恢复已有 session（从磁盘加载 / 重新打开 closed 会话）并返回其状态
    async def _session_resume_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionResumeCommand.model_validate(params)
        session = await self._sessions.resume(cmd.session_id)
        return SessionResumeResult(
            session_id=session.id,
            status=session.status,
            run_count=len(session.run_ids),
        )

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info("permission.respond received tool_use_id=%s decision=%s", cmd.tool_use_id, cmd.decision)

        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()

        resolved = self._permission_manager.respond(
            cmd.tool_use_id, cmd.decision, cmd.session_id
        )
        return PermissionRespondResult(ok=resolved)
    
    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()
        assert self._broadcaster is not None

        # 快照必须在注册订阅前同步读取：读快照与注册订阅之间没有 await，
        # 因此不会插入新事件——既不会丢失回放窗口内的实时事件，
        # 也不会重复回放订阅之后才写入文件的事件
        replay_events: list[dict[str, Any]] = []
        if cmd.replay_from_run is not None:
            replay_events = self._load_replay_events(cmd.replay_from_run, cmd.topics)
        elif cmd.replay_from_session is not None:
            replay_events = self._load_session_replay_events(cmd.replay_from_session, cmd.topics)

        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        replayed_count = await self._write_replay_events(writer, replay_events)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=now_iso(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 读取 events.jsonl 中匹配 topic 的历史事件快照（同步，保证与注册订阅之间无 await）
    def _load_replay_events(self, run_id: str, topics: list[str]) -> list[dict[str, Any]]:
        path = events_file(run_id)
        if not path.exists():
            for candidate in _sessions_root().glob(f"*/runs/{run_id}/events.jsonl"):
                path = candidate
                break
        return self._read_topic_events(path, topics)

    # 按 session 回放：按 run 目录时间顺序拼接该会话所有 events.jsonl，再按 topic 过滤
    def _load_session_replay_events(
        self, session_id: str, topics: list[str]
    ) -> list[dict[str, Any]]:
        runs_dir = _sessions_root() / session_id / "runs"
        if not runs_dir.is_dir():
            return []

        events: list[dict[str, Any]] = []
        # run_id 以时间戳开头，按目录名排序即为时间顺序
        for run_path in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
            events.extend(self._read_topic_events(run_path / "events.jsonl", topics))
        return events

    # 读取单个 events.jsonl，过滤掉不匹配 topics 或损坏的行
    @staticmethod
    def _read_topic_events(path: Path, topics: list[str]) -> list[dict[str, Any]]:
        if not path.exists():
            return []

        events: list[dict[str, Any]] = []
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            events.append(event)
        return events

    # 将回放快照写入 writer，返回已回放条数
    async def _write_replay_events(
        self,
        writer: asyncio.StreamWriter,
        events: list[dict[str, Any]],
    ) -> int:
        for event in events:
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
        if events:
            await writer.drain()
        return len(events)


    # 启动守护进程：加载配置、初始化日志、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        # trace 追踪记录
        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        # 初始化权限审批
        policy_file = Path("~/.mc/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )
    
        # IPC 客户端订阅
        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)

        # 初始化Session管理器
        store = SessionStore(_sessions_root())
        compact_provider = AnthropicProvider(self._config.llm.default_model)

        self._mcp_manager = McpServerManager()
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner( 
                self._config,
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
            ),  
            bus=self._bus,
            provider=compact_provider,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            broadcaster=self._broadcaster,
            trace=self._trace,
        )

        # 注册 RPC 方法
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)

        addr = await server.start()
        logger.info("mini-core %s listening addr=%s", mini_claude.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        loop.add_signal_handler(signal.SIGINT, shutdown.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown.set)

        await shutdown.wait()

        logger.info("shutting down")
        for run_task in list(self._running_runs):
            run_task.cancel()

        if self._running_runs:
            await asyncio.gather(*self._running_runs, return_exceptions=True)

        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()

        await server.stop()

        if self._trace is not None:
            await self._trace.stop()

    
# 同步入口：启动 CoreApp 事件循环
def run():
    asyncio.run(CoreApp().run())