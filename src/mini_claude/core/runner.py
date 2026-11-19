from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass

from mini_claude.core.bus.events import RunFinishedEvent, RunStartedEvent, StepFinishedEvent, StepStartedEvent
from mini_claude.core.compact.compactor import Compactor
from mini_claude.core.conf.config import MiniClaudeConfig
from mini_claude.core.context import ExecutionContext
from mini_claude.core.events.bus import EventBus , EventHandler
from mini_claude.core.events.writer import EventWriter
from mini_claude.core.llm.base import LLMProvider
from mini_claude.core.llm.provider import AnthropicProvider
from mini_claude.core.memory.loader import load_context_file
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.session.model import Session
from mini_claude.core.session.store import SessionStore
from mini_claude.core.task.manager import TaskManager
from mini_claude.core.tools.builtin import (
    BashTool,
    ListDirTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
    NoteSaveTool,
)
from mini_claude.core.tools.invocation import invoke_tool
from mini_claude.core.tools.registry import ToolRegistry
from mini_claude.core.trace.provider import TracingProvider
from mini_claude.core.trace.writer import TraceWriter
from mini_claude.core.utils.runs_dir import RUNS_DIR, new_run_id
from mini_claude.core.utils.time import now_iso

logger = logging.getLogger(__name__)

_AGENT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)

@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: MiniClaudeConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._runs_dir = runs_dir or RUNS_DIR
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._trace = trace
        self._permission_manager = permission_manager

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
    ) -> ToolRegistry:
        registry = ToolRegistry()

        registry.register(TaskCreateTool(task_manager))
        registry.register(TaskUpdateTool(task_manager))
        registry.register(TaskListTool(task_manager))
        registry.register(TaskGetTool(task_manager))

        registry.register(ReadFileTool())
        registry.register(BashTool())
        registry.register(WriteFileTool())
        registry.register(ListDirTool())

        if session is not None and store is not None and run_id is not None:
            registry.register(NoteSaveTool(store, session.id, run_id))

        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
    ) -> RunOutcome:
        # 创建run_id对应的目录
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        # 建立事件总线
        bus = self._bus if self._bus is not None else EventBus()
        for handler in self._extra_handlers:
            bus.subscribe(handler)

        global_ctx = load_context_file(Path("~/.mc/context.md").expanduser())
        project_ctx = load_context_file(Path(".mc/context.md"))

        # 创建工作记忆
        ctx = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
        )

        # 打开事件文件，然后正式开始
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=now_iso()))

            # 准备 LLM、工具注册表、循环控制器
            provider : LLMProvider = self._provider or AnthropicProvider(self._config.llm.default_model)
            if self._trace is not None:
                provider = TracingProvider(
                    provider,
                    self._trace,
                    include_payload=self._config.trace.include_llm_payload,
                )

            # 注册工具
            task_manager = TaskManager(run_path / ".tasks")
            registry = self._build_registry(
                task_manager,
                session=session,
                store=store,
                run_id=run_id,
            )

            session_dir = (
                store.session_dir(session.id)
                if session is not None and store is not None
                else run_path
            )
            session_id_str = session.id if session is not None else ""
            compactor = Compactor(bus, session_dir, session_id_str)
            loop = AgentLoop(
                    provider, registry, bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                )

            cancelled = False
            try:
                await loop.run(ctx)
            except asyncio.CancelledError:
                cancelled = True
                if not ctx.is_done():
                    ctx.mark_failed("cancelled")
            except Exception:
                logger.exception(
                    "agent run failed run_id=%s step=%d", run_id, ctx.step
                )
                if not ctx.is_done():
                    ctx.mark_failed("llm_error")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=ctx.status,
                    reason=ctx.reason,
                    steps=ctx.step,
                    ts=now_iso(),
                )
            )

        prefill_len = len(history)
        if session is not None and store is not None:
            store.append_messages(session.id, ctx.messages[prefill_len:], run_id=run_id)

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=ctx.status,
            result=ctx.result,
            reason=ctx.reason,
        )

        

class AgentLoop:
    # 初始化循环所需的三个依赖：LLM provider、工具注册表、事件总线
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        session_id: str = "",
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._session_id = session_id
        self._compactor = compactor
        self._compact_threshold = compact_threshold

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, ctx: ExecutionContext) -> None:
        while not ctx.is_done():
            ctx.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=ctx.run_id, step=ctx.step, ts=now_iso())
            )

            # [plan] call LLM — API errors terminate the run
            try:
                response = await self._provider.chat(
                    messages=ctx.messages,
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=ctx.run_id,
                    step=ctx.step,
                    system=ctx.system_prompt(_AGENT_SYSTEM_PROMPT),
                )
            except asyncio.CancelledError:
                ctx.mark_failed("cancelled")
                logger.info("LLM call cancelled: run_id: %s, step: %d", ctx.run_id, ctx.step)
                raise
            except Exception as e:
                ctx.mark_failed("llm_error")
                logger.error("LLM call error: %s, run_id: %s, step: %d", e, ctx.run_id, ctx.step)
                break

            # [observe] append assistant content blocks to ctx
            blocks: list[dict[str, object]] = []
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            ctx.add_assistant_message(blocks)

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                for tc in response.tool_calls:
                    result = await invoke_tool(
                        self._registry, tc, self._bus, ctx.run_id,
                        permission_manager=self._permission_manager,
                        session_id=self._session_id,
                    )
                    ctx.add_tool_result(tc.id, result.content, is_error=result.is_error)
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for tc in response.tool_calls:
                    ctx.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call could be completed. "
                        "Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            if response.stop_reason == "end_turn":
                ctx.result = response.text or ""
                ctx.mark_success()
            elif response.stop_reason != "tool_use":
                logger.warning(
                    "LLM stopped with unsupported reason %s: run_id: %s, step: %d",
                    response.stop_reason,
                    ctx.run_id,
                    ctx.step,
                )
                # ctx.mark_failed(f"unsupported_stop_reason:{response.stop_reason}")
            elif ctx.step >= ctx.max_steps:
                logger.error("LLM call exceeded max steps: %s, run_id: %s", ctx.max_steps, ctx.run_id)
                ctx.mark_failed("exceeded_max_steps")

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not ctx.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._compactor.compact(ctx, self._provider)

            await self._bus.publish(
                StepFinishedEvent(run_id=ctx.run_id, step=ctx.step, ts=now_iso())
            )