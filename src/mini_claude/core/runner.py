from __future__ import annotations

import asyncio
from pathlib import Path

from mini_claude.core.bus.events import RunFinishedEvent, RunStartedEvent, StepFinishedEvent, StepStartedEvent
from mini_claude.core.conf.config import MiniClaudeConfig
from mini_claude.core.context import ExecutionContext
from mini_claude.core.events.bus import EventBus , EventHandler
from mini_claude.core.events.writer import EventWriter
from mini_claude.core.llm.base import LLMProvider
from mini_claude.core.llm.provider import AnthropicProvider
from mini_claude.core.tools.builtin.read_file import ReadFileTool
from mini_claude.core.tools.invocation import invoke_tool
from mini_claude.core.tools.registry import ToolRegistry
from mini_claude.core.trace.provider import TracingProvider
from mini_claude.core.trace.writer import TraceWriter
from mini_claude.core.utils.runs_dir import RUNS_DIR, new_run_id
from mini_claude.core.utils.time import now_iso


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
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._runs_dir = runs_dir or RUNS_DIR
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._trace = trace

    # 执行一次完整的 agent run：接线事件总线、驱动 AgentLoop
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        # 创建run_id对应的目录
        run_id = run_id or new_run_id()
        run_path = self._runs_dir / run_id
        run_path.mkdir(parents=True, exist_ok=True)

        # 建立事件总线
        bus = self._bus if self._bus is not None else EventBus()
        for handler in self._extra_handlers:
            bus.subscribe(handler)

        # 创建工作记忆
        ctx = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
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

            registry = ToolRegistry()
            registry.register(ReadFileTool())
            loop = AgentLoop(provider, registry, bus)

            cancelled = False
            try:
                await loop.run(ctx)
            except asyncio.CancelledError:
                cancelled = True
                if not ctx.is_done():
                    ctx.mark_failed("cancelled")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=ctx.status,
                    reason=ctx.reason,
                    steps=ctx.step,
                    ts=now_iso(),
                )
            )

        if cancelled:
            raise asyncio.CancelledError()

        

class AgentLoop:
    # 初始化循环所需的三个依赖：LLM provider、工具注册表、事件总线
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus

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
                )
            except asyncio.CancelledError:
                ctx.mark_failed("cancelled")
                raise
            except Exception:
                ctx.mark_failed("llm_error")
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
                        self._registry, tc, self._bus, ctx.run_id
                    )
                    ctx.add_tool_result(tc.id, result.content, is_error=result.is_error)

            if response.stop_reason == "end_turn":
                ctx.mark_success()
            elif ctx.step >= ctx.max_steps:
                ctx.mark_failed("exceeded_max_steps")

            await self._bus.publish(
                StepFinishedEvent(run_id=ctx.run_id, step=ctx.step, ts=now_iso())
            )