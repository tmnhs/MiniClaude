from __future__ import annotations

import asyncio
import datetime
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from mini_claude.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    matches_outside_cwd,
    param_preview,
)
from mini_claude.core.permissions.store import load_policy_file, save_policy_file
from mini_claude.core.utils.time import now_iso

logger = logging.getLogger(__name__)

@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    session_id: str
    tool_name: str

# 管理工具调用权限：策略评估、用户审批挂起、session 级和持久化 always 缓存、超时
class PermissionManager:
    def __init__(
        self,
        policies: dict[str, ToolPolicy] | None = None,
        *,
        policy_file: Path | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._policies: dict[str, ToolPolicy] = policies or dict(DEFAULT_POLICIES)
        # tool_use_id → 待处理的 Future + 元数据
        self._pending: dict[str, _PendingRequest] = {}
        # (session_id, tool_name) → "allow" | "deny"（session 内存，重启丢失）
        self._session_always: dict[tuple[str, str], str] = {}
        # tool_name → "allow" | "deny"（持久化，从 policy_file 加载）
        self._policy_file = policy_file
        self._persistent_always: dict[str, str] = (
            load_policy_file(policy_file) if policy_file is not None else {}
        )
        # 0 表示不超时
        self._timeout_s = timeout_s

    # 对工具名 + 参数执行 4 层静态评估，不挂起
    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionDecision:
        from mini_claude.core.permissions.policy import evaluate
        policy = self._policies.get(tool_name)
        return evaluate(tool_name, params, policy)

    # 检查权限；如需 ask 则向客户端发事件并等待响应；返回 (allowed, decision_str)
    async def check_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> tuple[bool, str]:
        command = str(params.get("command", "")) if tool_name == "bash" else ""
        policy = self._policies.get(tool_name)

        # 第 1 层：deny_patterns（仅 bash，不可被缓存绕过）
        if command and policy:
            for pat in policy.deny_patterns:
                if re.search(pat, command):
                    logger.debug("permission: deny_pattern hit tool=%s", tool_name)
                    return False, "auto_deny"

        # 第 2 层：OUTSIDE_CWD_HEURISTICS（仅 bash，强制 ASK，不可被任何缓存绕过）
        outside_cwd = bool(command and matches_outside_cwd(command))   
        
        if not outside_cwd:
            # 第 3 层：session 级 always 缓存
            session_key = (session_id, tool_name)
            if session_key in self._session_always:
                cached = self._session_always[session_key]
                logger.debug("permission: session cache hit tool=%s decision=%s", tool_name, cached)
                return cached == "allow", f"auto_{cached}"

            # 第 4 层：persistent always（跨 session）
            if tool_name in self._persistent_always:
                cached = self._persistent_always[tool_name]
                logger.debug("permission: persistent cache hit tool=%s decision=%s", tool_name, cached)
                return cached == "allow", f"auto_{cached}"

            # 第 5 层：allow_patterns（仅 bash）
            if command and policy:
                for pat in policy.allow_patterns:
                    if re.search(pat, command):
                        return True, "auto_allow"

            # 第 6 层：工具默认策略
            if policy is not None:
                if policy.default == PermissionDecision.ALLOW:
                    return True, "auto_allow"
                if policy.default == PermissionDecision.DENY:
                    return False, "auto_deny"
            # default == ASK（bash、未知工具）→ 落入 Future 等待路径

        # ASK 路径（来自 OUTSIDE_CWD 强制 ASK，或 default=ASK）
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[tool_use_id] = _PendingRequest(
            future=future,
            session_id=session_id,
            tool_name=tool_name,
        )

        await event_emitter(
            {
                "type": "permission.requested",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "params": params,
                "param_preview": param_preview(tool_name, params),
                "session_id": session_id,
                "ts": now_iso(),
            }
        )

        try:
            if self._timeout_s > 0:
                raw = await asyncio.wait_for(future, timeout=self._timeout_s)
            else:
                raw = await future
        except asyncio.TimeoutError:
            self._pending.pop(tool_use_id, None)
            logger.info("permission: timeout tool_use_id=%s tool=%s", tool_use_id, tool_name)
            return False, "timeout"

        allowed = self._apply_response(raw, session_id, tool_name)
        return allowed, raw

    # 处理客户端返回的审批决策，校验会话归属后 resolve 对应 Future
    # session_id 必填；缺失直接报错，会话不匹配返回 False（未处理）
    def respond(
        self,
        tool_use_id: str,
        decision: str,
        session_id: str | None,
    ) -> bool:
        if session_id is None:
            raise ValueError("permission.respond requires session_id")

        req = self._pending.get(tool_use_id)
        if req is None:
            logger.warning("permission.respond: unknown tool_use_id=%s", tool_use_id)
            return False

        if req.session_id != session_id:
            logger.warning(
                "permission.respond: session mismatch tool_use_id=%s expected=%s got=%s",
                tool_use_id,
                req.session_id,
                session_id,
            )
            return False

        self._pending.pop(tool_use_id, None)
        if not req.future.done():
            req.future.set_result(decision)
        return True

    # 应用审批决策，更新 session + persistent 缓存，返回是否放行
    def _apply_response(self, decision: str, session_id: str, tool_name: str) -> bool:
        allow = decision in ("allow_once", "always_allow")

        if decision == "always_allow":
            self._session_always[(session_id, tool_name)] = "allow"
            self._persistent_always[tool_name] = "allow"

            logger.info(
                "permission: always allow tool=%s policy_file=%s persistent=%s",
                tool_name, self._policy_file, self._persistent_always,
            )

            if self._policy_file is not None:
                try:
                    save_policy_file(self._persistent_always, self._policy_file)
                    logger.info("permission: policy.toml written path=%s", self._policy_file)
                except Exception:
                    logger.exception("permission: failed to write policy.toml path=%s", self._policy_file)
            else:
                logger.warning("permission: policy_file is None, skipping persistence") 

        elif decision == "always_deny":
            self._session_always[(session_id, tool_name)] = "deny"
            self._persistent_always[tool_name] = "deny"

            logger.info(
                "permission: always deny tool=%s policy_file=%s persistent=%s",
                tool_name, self._policy_file, self._persistent_always,
            )

            if self._policy_file is not None:
                try:
                    save_policy_file(self._persistent_always, self._policy_file)
                    logger.info("permission: policy.toml written path=%s", self._policy_file)
                except Exception:
                    logger.exception("permission: failed to write policy.toml path=%s", self._policy_file)
            else:
                logger.warning("permission: policy_file is None, skipping persistence")

        return allow

    # 客户端断连时拒绝该 session 所有待审批请求，防止 Future 永久挂起
    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        to_cancel = [
            uid for uid, req in self._pending.items()
            if req.session_id == session_id
        ]
        for uid in to_cancel:
            req = self._pending.pop(uid)
            if not req.future.done():
                logger.debug(
                    "permission: cancel pending tool_use_id=%s reason=%s", uid, reason
                )
                req.future.set_result("deny_once")