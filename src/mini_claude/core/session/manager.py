from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.bus.events import SessionClosedEvent, SessionCreatedEvent, SessionMessageReceivedEvent, SessionResumedEvent, SessionWaitingForInputEvent, SkillInvokedEvent
from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.base import LLMProvider
from mini_claude.core.session.model import SESSION_ID_PATTERN, Session, SessionMode
from mini_claude.core.session.store import SessionStore
from mini_claude.core.skills.loader import SkillLoader
from mini_claude.core.utils.runs_dir import new_run_id
from mini_claude.core.utils.time import now_iso

if TYPE_CHECKING:
    from mini_claude.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_ID_CONFLICT = -32013
SESSION_ID_INVALID = -32014

class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂和事件总线
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._skill_loader = SkillLoader()

    # 创建新 session 并写入 meta.json；session_id 可由客户端预生成（用于先订阅再创建）
    async def create(
        self,
        mode: SessionMode,
        title: str = "",
        session_id: str | None = None,
    ) -> Session:
        if session_id is not None and not re.fullmatch(SESSION_ID_PATTERN, session_id):
            raise HandlerError(SESSION_ID_INVALID, "invalid session id format")

        sid = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        if sid in self._sessions:
            raise HandlerError(SESSION_ID_CONFLICT, f"session id already exists: {sid}")
        ts = now_iso()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)

        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=now_iso()))

            self._store.append_message(sid, "user", content)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=now_iso())
            )

            if not session.title:
                session.title = content[:40]

            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)
            session.updated_at = now_iso()
            self._store.write_meta(session)

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None

            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(skill_name)

                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt_override = skill.system_prompt_template
                    tool_whitelist = skill.allowed_tools or None

                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            session_id=sid,
                            ts=now_iso(),
                        )
                    )

            runner = self._runner_factory()
            await runner.run_and_capture(
                goal=goal,
                run_id=run_id,
                session=session,
                store=self._store,
                system_prompt_override=system_prompt_override,
                tool_whitelist=tool_whitelist,
            )

            session.updated_at = now_iso()
            if session.mode == "one_shot":
                session.status = "closed"
                await self._bus.publish(
                    SessionClosedEvent(
                        session_id=sid, 
                        ts=session.updated_at
                        )
                )
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )

            self._store.write_meta(session)
            return run_id

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")

        async with lock:
            from mini_claude.core.bus.commands import SessionCompactResult
            from mini_claude.core.compact.compactor import Compactor

            messages = self._store.read_messages(sid)
            session_dir = self._store.session_dir(sid)
            compactor = Compactor(self._bus, session_dir, sid)

            result = await compactor.compact_messages(messages, self._provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")

            self._store.write_compacted(sid, [
                {"role": "user", "content": result.summary_text},
                {"role": "assistant", "content": "Understood, I'll continue from this summary."},
            ])
    
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            session.updated_at = now_iso()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_messages(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session