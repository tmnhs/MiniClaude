from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, cast
from pydantic import ValidationError

from mini_claude.core.bus.events import PermissionDeniedEvent, PermissionGrantedEvent, PermissionRequestedEvent, ToolCallFailedEvent, ToolCallFinishedEvent, ToolCallStartedEvent
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import ToolCallBlock
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.tools.base import ToolResult
from mini_claude.core.tools.errors import RateLimitedError
from mini_claude.core.tools.registry import ToolRegistry
from mini_claude.core.utils.time import now_iso

_DEFAULT_TIMEOUT: float = 120.0
_MAX_RETRIES: int = 2
_RETRY_BASE_S: float = 2.0  # backoff base; tests can monkeypatch to 0
_RETRYABLE: frozenset[str] = frozenset({"runtime_error", "rate_limited"})

# 发布 ToolCallFailedEvent 并返回对应 ToolResult
async def _fail(
    bus: EventBus,
    run_id: str,
    tool_call: ToolCallBlock,
    error_class: str,
    error_message: str,
    elapsed_ms: int,
    *,
    attempt: int = 1,
    session_id: str = "",
) -> ToolResult:
    await bus.publish(
        ToolCallFailedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            error_class=error_class,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
            attempt=attempt,
            session_id=session_id or None,
            ts=now_iso(),
        )
    )
    return ToolResult(content=error_message, is_error=True, error_type=error_class)


# 校验参数、限时调用工具、发布进度事件，返回 ToolResult（不抛异常）
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
    workspace: Path | None = None,
) -> ToolResult:
    t0 = time.monotonic()

    await bus.publish(
        ToolCallStartedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            session_id=session_id or None,
            ts=now_iso(),
        )
    )

    def elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(
            bus, run_id, tool_call,
            "runtime_error", f"unknown tool: {tool_call.name}", elapsed(),
            session_id=session_id,
        )

    # 参数校验
    if tool.params_model is not None:
        try:
            tool.params_model.model_validate(dict(tool_call.input))
        except ValidationError as exc:
            return await _fail(
                bus, run_id, tool_call,
                "schema_error", str(exc), elapsed(),
                session_id=session_id,
            )

    # 权限审批
    if permission_manager is not None:
        async def _emit_permission(raw: dict[str, Any]) -> None:
            await bus.publish(PermissionRequestedEvent(**raw, run_id=run_id))

        allowed, decision = await permission_manager.check_and_wait(
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            session_id=session_id,
            event_emitter=_emit_permission,
            workspace=workspace,
        )

        if allowed:
            if decision not in ("auto_allow",):
                await bus.publish(
                    PermissionGrantedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        session_id=session_id or None,
                        ts=now_iso(),
                    )
                )
        else:
            if decision != "auto_deny":
                await bus.publish(
                    PermissionDeniedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        session_id=session_id or None,
                        ts=now_iso(),
                    )
                )

            return await _fail(
                bus, run_id, tool_call,
                "permission_denied",
                "Permission denied by user. You may not execute this command. "
                "Try an alternative approach or ask the user what to do.",
                elapsed(),
                session_id=session_id,
            )

    # 执行
    for attempt in range(1, _MAX_RETRIES + 2):
        error_class: str | None = None
        error_message: str | None = None

        try:
            result = await asyncio.wait_for(
                tool.invoke(dict(tool_call.input)), timeout=timeout
            )
            ms = elapsed()

            if result.is_error:
                error_class = result.error_type or "runtime_error"
                error_message = result.content
            else:
                await bus.publish(
                    ToolCallFinishedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        tool_name=tool_call.name,
                        elapsed_ms=ms,
                        output=result.content,
                        session_id=session_id or None,
                        ts=now_iso(),
                    )
                )
                return result

        except RateLimitedError as exc:
            error_class = "rate_limited"
            error_message = str(exc)
        except TimeoutError:
            return await _fail(
                bus, run_id, tool_call,
                "timeout", f"tool timed out after {timeout}s", elapsed(),
                attempt=attempt,
                session_id=session_id,
            )
        except Exception as exc:
            error_class = "runtime_error"
            error_message = str(exc)

        assert error_class is not None and error_message is not None
        ms = elapsed()

        if error_class in _RETRYABLE and attempt <= _MAX_RETRIES:
            await bus.publish(
                ToolCallFailedEvent(
                    run_id=run_id,
                    tool_use_id=tool_call.id,
                    tool_name=tool_call.name,
                    error_class=error_class,
                    error_message=error_message,
                    elapsed_ms=ms,
                    attempt=attempt,
                    session_id=session_id or None,
                    ts=now_iso(),
                )
            )

            await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            continue

        return await _fail(
            bus, run_id, tool_call,
            error_class, error_message, ms,
            attempt=attempt,
            session_id=session_id,
        )

    return ToolResult(content="internal error", is_error=True, error_type="runtime_error")
    