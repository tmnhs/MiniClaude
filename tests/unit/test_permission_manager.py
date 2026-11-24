from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mini_claude.core.bus.commands import PermissionRespondCommand
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from mini_claude.core.permissions.store import load_policy_file

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_manager(**policies: ToolPolicy) -> PermissionManager:
    # policy_file=None：测试中不使用持久化，不污染 ~/.mc/policy.toml
    return PermissionManager(policies or None)


async def _collect_emitted() -> tuple[list[dict[str, Any]], Any]:
    emitted: list[dict[str, Any]] = []

    async def emitter(event: dict[str, Any]) -> None:
        emitted.append(event)

    return emitted, emitter


# ── evaluate() delegation ─────────────────────────────────────────────────────

# 功能：验证 PermissionManager.evaluate 委托给 policy 层返回正确决策
# 设计：直接调用 evaluate()，不涉及 Future，验证策略加载与委托路径
def test_evaluate_delegates_to_policy() -> None:
    mgr = _make_manager()
    assert mgr.evaluate("read_file", {"path": "x"}) == PermissionDecision.ALLOW
    assert mgr.evaluate("bash", {"command": "echo hi"}) == PermissionDecision.ASK
    assert mgr.evaluate("write_file", {"path": "x", "content": ""}) == PermissionDecision.ASK


# ── check_and_wait: ALLOW path ───────────────────────────────────────────────

# 功能：验证策略为 ALLOW 时 check_and_wait 立即返回 (True, "auto_allow")，不发任何事件
# 设计：read_file 默认 ALLOW，断言不产生 permission.requested 事件，覆盖"无噪声放行"路径
async def test_check_and_wait_allow_no_event() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t1", tool_name="read_file",
        params={"path": "README.md"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert emitted == []


# ── check_and_wait: ASK path + respond ───────────────────────────────────────

# 功能：验证 ASK 策略时发出 permission.requested 事件并等待 respond() 解决 Future
# 设计：在后台协程中调用 respond("allow_once")，主协程 await 结束后断言结果；
#       这是权限系统的核心反向请求通路
async def test_check_and_wait_ask_emits_event_and_waits() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_respond() -> None:
        await asyncio.sleep(0)  # yield once so check_and_wait can emit the event
        mgr.respond("t2", "allow_once", session_id="s1")

    task = asyncio.create_task(_auto_respond())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t2", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is True
    assert decision == "allow_once"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"
    assert emitted[0]["tool_use_id"] == "t2"
    assert emitted[0]["tool_name"] == "bash"


# 功能：验证 respond("deny_once") 使 check_and_wait 返回 (False, "deny_once")
# 设计：用户拒绝时工具不应执行，确认 False 返回值而不是异常
async def test_check_and_wait_deny_once_returns_false() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _auto_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("t3", "deny_once", session_id="s1")

    task = asyncio.create_task(_auto_deny())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t3", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is False
    assert decision == "deny_once"


# ── always_allow cache ────────────────────────────────────────────────────────

# 功能：验证 respond("always_allow") 后同 session 同一 pattern 下次不再发事件
# 设计：第二次调用使用同程序名（echo）的命令，命中 pattern 级缓存，返回 (True, "auto_allow")，
#       emitted 仍为 1 条；证明缓存按 tool + pattern 而非 tool 生效
async def test_always_allow_skips_future_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # First call: user says "always allow"
    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t4", "always_allow", session_id="s1")

    task = asyncio.create_task(_auto_always())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="t4", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1 is True

    # Second call: same pattern ("echo") → should hit cache, no new event
    r2, d2 = await mgr.check_and_wait(
        tool_use_id="t5", tool_name="bash",
        params={"command": "echo again"}, session_id="s1",
        event_emitter=emitter,
    )

    assert r2 is True
    assert d2 == "auto_allow"
    assert len(emitted) == 1  # only the first call emitted an event


# 功能：验证 always_allow 在同一 manager 实例内对所有 session 生效（persistent_always 共享）
# 设计：s1 设置 always_allow → 写入 _persistent_always；s2 命中 persistent 缓存，直接放行；
#       emitted 只有 1 条（s2 不需要再 ASK）。这是 persistent always 的核心跨 session 语义。
async def test_always_allow_not_shared_across_sessions() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # session s1 sets always allow for bash
    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t6", "always_allow", session_id="s1")

    task = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="t6", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    # session s2 — persistent_always["bash"] = "allow" → 直接放行，不再 ASK
    r, d = await mgr.check_and_wait(
        tool_use_id="t7", tool_name="bash",
        params={"command": "echo"}, session_id="s2",
        event_emitter=emitter,
    )

    assert r is True
    assert d == "auto_allow"
    assert len(emitted) == 1  # s2 命中 persistent 缓存，不再发出事件


# ── always_deny cache ─────────────────────────────────────────────────────────

# 功能：验证 respond("always_deny") 后同 session 同工具下次直接返回 (False, "auto_deny")
# 设计：用户选择 always deny 后不应继续骚扰，下次调用静默拒绝
async def test_always_deny_skips_future_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("t8", "always_deny", session_id="s1")

    task = asyncio.create_task(_auto_always_deny())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="t8", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1 is False

    # Second call: same pattern ("echo") → cache hit, no event
    r2, d2 = await mgr.check_and_wait(
        tool_use_id="t9", tool_name="bash",
        params={"command": "echo again"}, session_id="s1",
        event_emitter=emitter,
    )
    assert r2 is False
    assert d2 == "auto_deny"
    assert len(emitted) == 1


# ── cancel_session ────────────────────────────────────────────────────────────

# 功能：验证 cancel_session 将 pending Future 设为 deny_once，check_and_wait 返回 False
# 设计：模拟客户端断连场景——check_and_wait 挂起后调用 cancel_session，
#       确认 Future 被解决而非永久挂起（防止僵尸 run）
async def test_cancel_session_resolves_pending_future() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _cancel_after_emit() -> None:
        await asyncio.sleep(0)  # wait for event to be emitted
        mgr.cancel_session("s1", reason="client_disconnected")

    task = asyncio.create_task(_cancel_after_emit())
    allowed, _ = await mgr.check_and_wait(
        tool_use_id="t10", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is False


# 功能：验证 cancel_session 只取消属于该 session 的 pending Future
# 设计：s1 和 s2 各有一个 pending，cancel_session(s2) 不影响 s1 的 Future
async def test_cancel_session_only_affects_target_session() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    # Launch two concurrent check_and_wait for different sessions
    s1_done = asyncio.Event()
    s2_done = asyncio.Event()
    s1_result: list[bool] = []
    s2_result: list[bool] = []

    async def _s1() -> None:
        r, _ = await mgr.check_and_wait(
            tool_use_id="ta", tool_name="bash",
            params={"command": "echo"}, session_id="s1",
            event_emitter=emitter,
        )
        s1_result.append(r)
        s1_done.set()

    async def _s2() -> None:
        r, _ = await mgr.check_and_wait(
            tool_use_id="tb", tool_name="bash",
            params={"command": "echo"}, session_id="s2",
            event_emitter=emitter,
        )
        s2_result.append(r)
        s2_done.set()

    t1 = asyncio.create_task(_s1())
    t2 = asyncio.create_task(_s2())

    await asyncio.sleep(0)  # let both emit events and hang

    # cancel only s2
    mgr.cancel_session("s2")
    await s2_done.wait()

    # s1 should still be pending; resolve it manually
    mgr.respond("ta", "allow_once", session_id="s1")
    await s1_done.wait()

    await t1
    await t2

    assert s1_result == [True]   # s1 was allowed
    assert s2_result == [False]  # s2 was cancelled → denied


# ── respond: unknown tool_use_id ──────────────────────────────────────────────

# 功能：验证 respond 传入不存在的 tool_use_id 时静默忽略，不抛异常
# 设计：竞态场景（客户端重复发送响应）不应导致 daemon crash
def test_respond_unknown_tool_use_id_is_noop() -> None:
    mgr = _make_manager()
    mgr.respond("nonexistent", "allow_once", session_id="s1")  # should not raise


# ── OUTSIDE_CWD 不被 always 缓存绕过 ─────────────────────────────────────────

# 功能：验证 always_allow bash 之后，含绝对路径的命令仍触发 ASK，不被缓存绕过
# 设计：先让 session s1 对 bash 设置 always_allow，再请求含绝对路径命令；
#       OUTSIDE_CWD 检查在 always 缓存之前，应发出 permission.requested 事件
async def test_always_allow_does_not_bypass_outside_cwd() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # 首次 allow → 写入 session always 缓存
    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t_always", "always_allow", session_id="s1")

    t = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="t_always", tool_name="bash",
        params={"command": "echo ok"}, session_id="s1",
        event_emitter=emitter,
    )
    await t
    assert len(emitted) == 1  # 首次 ASK 触发事件

    # 第二次：bash + 绝对路径 → OUTSIDE_CWD 强制 ASK，不命中 session always 缓存
    async def _auto_respond_abs() -> None:
        await asyncio.sleep(0)
        mgr.respond("t_abs", "allow_once", session_id="s1")

    t2 = asyncio.create_task(_auto_respond_abs())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t_abs", tool_name="bash",
        params={"command": "cat /etc/hosts"}, session_id="s1",
        event_emitter=emitter,
    )
    await t2

    assert allowed is True
    assert len(emitted) == 2  # 绝对路径命令再次触发 ASK，共 2 个事件


# ── 持久化 always 写文件 ──────────────────────────────────────────────────────

# 功能：验证 always_allow 决策写入 policy_file，新 PermissionManager 加载后自动放行
# 设计：用 tmp_path 作为 policy_file，断言文件存在且内容正确；
#       再新建 manager 加载文件，同工具无需 ASK 直接返回 auto_allow
async def test_persistent_always_written_and_reloaded(tmp_path: pytest.TempPathFixture) -> None:
    policy_file = tmp_path / "policy.toml"
    mgr = PermissionManager(policy_file=policy_file)
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("tp1", "always_allow", session_id="s1")

    t = asyncio.create_task(_auto_always())
    allowed, _ = await mgr.check_and_wait(
        tool_use_id="tp1", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await t
    assert allowed is True
    assert policy_file.exists()

    loaded = {(r.tool, r.pattern): r.decision for r in load_policy_file(policy_file)}
    assert loaded.get(("bash", "echo")) == "allow"

    # 新 manager 加载同一文件，bash 应直接 auto_allow（无 OUTSIDE_CWD）
    mgr2 = PermissionManager(policy_file=policy_file)
    emitted2, emitter2 = await _collect_emitted()
    allowed2, decision2 = await mgr2.check_and_wait(
        tool_use_id="tp2", tool_name="bash",
        params={"command": "echo new"}, session_id="s2",
        event_emitter=emitter2,
    )
    assert allowed2 is True
    assert decision2 == "auto_allow"
    assert emitted2 == []  # 无需 ASK


# ── 审批超时 ──────────────────────────────────────────────────────────────────

# 功能：验证 check_and_wait 超时后返回 (False, "timeout")，不永久挂起
# 设计：timeout_s=0.05 极短超时，不主动 respond；断言在合理时间内返回 False
async def test_permission_timeout_returns_false() -> None:
    mgr = PermissionManager(timeout_s=0.05)
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t_timeout", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is False
    assert decision == "timeout"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"


# 功能：验证超时后 pending 被清理，迟到的 respond 不影响后续调用
# 设计：超时后调用 respond，不抛异常（unknown tool_use_id 静默忽略）；
#       再次 check_and_wait 同 tool_use_id 仍正常发出新的 permission.requested
async def test_permission_timeout_cleans_up_pending() -> None:
    mgr = PermissionManager(timeout_s=0.05)
    _, emitter = await _collect_emitted()

    await mgr.check_and_wait(
        tool_use_id="t_late", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    # 超时后迟到的 respond 不应 crash
    mgr.respond("t_late", "allow_once", session_id="s1")  # should be noop
    assert "t_late" not in mgr._pending


# ── respond: session 归属校验 ────────────────────────────────────────────────

# 功能：验证错误 session 的 respond 被拒绝且不 resolve Future，随后正确 session 可审批
# 设计：check_and_wait 挂起后先用 s2 越权 respond，断言返回 False 且 Future 仍挂起；
#       再用 s1 正常审批，确认请求仍可完成——防止其他客户端串会话审批
async def test_respond_rejects_wrong_session_then_allows_correct() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _later() -> None:
        await asyncio.sleep(0.01)
        # 越权：s2 审批 s1 的请求 → False，不 resolve
        assert mgr.respond("tow1", "allow_once", session_id="s2") is False
        assert "tow1" in mgr._pending
        # 正确 session → True
        assert mgr.respond("tow1", "allow_once", session_id="s1") is True

    task = asyncio.create_task(_later())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="tow1", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is True
    assert decision == "allow_once"


# 功能：验证不传 session_id 的 respond 直接报错，不再向后兼容
# 设计：省略 session_id 触发 TypeError；显式传 None 触发 ValueError，
#       确保旧式越权审批无法静默生效
def test_respond_without_session_id_raises() -> None:
    mgr = _make_manager()

    with pytest.raises(TypeError):
        mgr.respond("tcompat", "allow_once")  # type: ignore[call-arg]

    with pytest.raises(ValueError):
        mgr.respond("tcompat", "allow_once", session_id=None)


# 功能：验证 permission.respond 命令缺少 session_id 时在协议层校验失败
# 设计：直接构造 PermissionRespondCommand，断言 Pydantic 抛 ValidationError，
#       阻止旧客户端把不带会话归属的审批发到 daemon
def test_permission_respond_command_requires_session_id() -> None:
    with pytest.raises(ValidationError):
        PermissionRespondCommand.model_validate(
            {"type": "permission.respond", "tool_use_id": "t1", "decision": "allow_once"}
        )


# ── pattern 级缓存与高风险策略 ───────────────────────────────────────────────

# 功能：验证 always_allow 仅覆盖同一 pattern，git status 的授权不会放行 git log
# 设计：先对 bash 的 "git status" 设 always_allow，再请求 "git log"；后者 pattern 不同，
#       仍应发出 permission.requested，证明审批从「工具级」升级为「命令前缀级」
async def test_always_allow_is_pattern_scoped() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("p1", "always_allow", session_id="s1")

    t1 = asyncio.create_task(_auto_always())
    allowed1, _ = await mgr.check_and_wait(
        tool_use_id="p1", tool_name="bash",
        params={"command": "git status"}, session_id="s1",
        event_emitter=emitter,
    )
    await t1
    assert allowed1 is True

    async def _auto_allow_once() -> None:
        await asyncio.sleep(0)
        mgr.respond("p2", "allow_once", session_id="s1")

    t2 = asyncio.create_task(_auto_allow_once())
    allowed2, decision2 = await mgr.check_and_wait(
        tool_use_id="p2", tool_name="bash",
        params={"command": "git log"}, session_id="s1",
        event_emitter=emitter,
    )
    await t2

    assert allowed2 is True
    assert decision2 == "allow_once"
    assert len(emitted) == 2


# 功能：验证高风险命令的 always_allow 只放行一次，不会写入缓存
# 设计：rm -rf 命中高风险规则；第一次选择 always_allow 后第二次仍触发 ASK，
#       证明高风险命令「永不缓存」的用户决策
async def test_high_risk_always_allow_not_cached() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("h1", "always_allow", session_id="s1")

    t1 = asyncio.create_task(_auto_always())
    allowed1, _ = await mgr.check_and_wait(
        tool_use_id="h1", tool_name="bash",
        params={"command": "rm -rf build"}, session_id="s1",
        event_emitter=emitter,
    )
    await t1
    assert allowed1 is True

    async def _auto_allow_once() -> None:
        await asyncio.sleep(0)
        mgr.respond("h2", "allow_once", session_id="s1")

    t2 = asyncio.create_task(_auto_allow_once())
    allowed2, decision2 = await mgr.check_and_wait(
        tool_use_id="h2", tool_name="bash",
        params={"command": "rm -rf build"}, session_id="s1",
        event_emitter=emitter,
    )
    await t2

    assert allowed2 is True
    assert decision2 == "allow_once"
    assert len(emitted) == 2


# 功能：验证高风险命令的 always_deny 仍然生效（deny 缓存不被 force_ask 跳过）
# 设计：第一次 always_deny 后第二次同 pattern 直接 auto_deny，不再骚扰用户；
#       与高风险 allow 永不缓存形成对称
async def test_high_risk_always_deny_is_cached() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("hd1", "always_deny", session_id="s1")

    t1 = asyncio.create_task(_auto_always_deny())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="hd1", tool_name="bash",
        params={"command": "rm -rf build"}, session_id="s1",
        event_emitter=emitter,
    )
    await t1
    assert r1 is False

    r2, d2 = await mgr.check_and_wait(
        tool_use_id="hd2", tool_name="bash",
        params={"command": "rm -rf build"}, session_id="s1",
        event_emitter=emitter,
    )
    assert r2 is False
    assert d2 == "auto_deny"
    assert len(emitted) == 1


# 功能：验证文件工具的 always_allow 以精确路径为粒度，a.txt 的授权不影响 b.txt
# 设计：write_file 两个不同路径，第一次 always_allow；第二次应再次 ASK，
#       落实「精确文件路径」的缓存粒度
async def test_file_tool_always_allow_scoped_to_exact_path(tmp_path: Path) -> None:
    mgr = PermissionManager(workspace=tmp_path)
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("f1", "always_allow", session_id="s1")

    t1 = asyncio.create_task(_auto_always())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="f1", tool_name="write_file",
        params={"path": "a.txt", "content": "x"}, session_id="s1",
        event_emitter=emitter,
    )
    await t1
    assert r1 is True

    async def _auto_allow_once() -> None:
        await asyncio.sleep(0)
        mgr.respond("f2", "allow_once", session_id="s1")

    t2 = asyncio.create_task(_auto_allow_once())
    r2, d2 = await mgr.check_and_wait(
        tool_use_id="f2", tool_name="write_file",
        params={"path": "b.txt", "content": "y"}, session_id="s1",
        event_emitter=emitter,
    )
    await t2

    assert r2 is True
    assert d2 == "allow_once"
    assert len(emitted) == 2


# 功能：验证 permission.requested 事件携带 risk_level 与 affected_paths
# 设计：请求 cat /etc/hosts（越界只读）→ 风险 medium 且路径被收集；用于 TUI/CLI 风险展示
async def test_permission_event_includes_risk_and_paths() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_allow() -> None:
        await asyncio.sleep(0)
        mgr.respond("r1", "allow_once", session_id="s1")

    task = asyncio.create_task(_auto_allow())
    await mgr.check_and_wait(
        tool_use_id="r1", tool_name="bash",
        params={"command": "cat /etc/hosts"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert len(emitted) == 1
    assert emitted[0]["risk_level"] == "medium"
    assert any("hosts" in p for p in emitted[0]["affected_paths"])


# 功能：验证高风险命令在事件中被标记为 high
# 设计：rm -rf 命中高风险规则；断言事件 risk_level == "high"，供 UI 提示不可缓存
async def test_high_risk_event_marks_high() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("hr", "deny_once", session_id="s1")

    task = asyncio.create_task(_auto_deny())
    await mgr.check_and_wait(
        tool_use_id="hr", tool_name="bash",
        params={"command": "rm -rf /tmp/x"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert emitted[0]["risk_level"] == "high"


# ── git / grep 权限集成 ──────────────────────────────────────────────────────

# 功能：git 只读子命令自动放行且不发审批事件
# 设计：default_decision 把 status 提升为 ALLOW，走无噪声放行路径
async def test_git_readonly_auto_allow_no_event() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="g1", tool_name="git",
        params={"subcommand": "status"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert emitted == []


# 功能：git commit 发出中风险审批事件并展示 subcommand 摘要
# 设计：变更子命令默认 ASK；risk_level=medium，param_preview 展示 subcommand
async def test_git_commit_asks_with_medium_risk() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_allow() -> None:
        await asyncio.sleep(0)
        mgr.respond("g2", "allow_once", session_id="s1")

    task = asyncio.create_task(_auto_allow())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="g2", tool_name="git",
        params={"subcommand": "commit", "message": "x"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is True
    assert decision == "allow_once"
    assert len(emitted) == 1
    assert emitted[0]["risk_level"] == "medium"
    assert emitted[0]["param_preview"] == "subcommand='commit'"


# 功能：git commit 的 always_allow 可缓存，同子命令二次调用不再 ASK
# 设计：medium 风险允许缓存，第二次命中 (git, "git commit") 缓存
async def test_git_commit_always_allow_is_cached() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("g3", "always_allow", session_id="s1")

    task = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="g3", tool_name="git",
        params={"subcommand": "commit", "message": "x"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="g4", tool_name="git",
        params={"subcommand": "commit", "message": "y"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert len(emitted) == 1


# 功能：git restore 为高风险，always_allow 不缓存，二次调用仍 ASK
# 设计：丢弃未提交改动不可恢复，高风险 always_allow 只放行一次
async def test_git_restore_always_allow_not_cached() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("g5", "always_allow", session_id="s1")

    task = asyncio.create_task(_auto_always())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="g5", tool_name="git",
        params={"subcommand": "restore"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1 is True

    async def _auto_allow_once() -> None:
        await asyncio.sleep(0)
        mgr.respond("g6", "allow_once", session_id="s1")

    task2 = asyncio.create_task(_auto_allow_once())
    r2, d2 = await mgr.check_and_wait(
        tool_use_id="g6", tool_name="git",
        params={"subcommand": "restore"}, session_id="s1",
        event_emitter=emitter,
    )
    await task2

    assert r2 is True
    assert d2 == "allow_once"
    assert len(emitted) == 2
    assert emitted[0]["risk_level"] == "high"


# 功能：grep 普通路径自动放行且无事件
# 设计：只读搜索默认 ALLOW，降低权限疲劳
async def test_grep_normal_path_auto_allow(tmp_path: Path) -> None:
    mgr = PermissionManager(workspace=tmp_path)
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="gr1", tool_name="grep",
        params={"pattern": "x"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert emitted == []


# 功能：grep 敏感目标路径强制 ASK 并标记 high
# 设计：搜索 .git 等高敏目录时，即使策略默认 ALLOW 也必须审批
async def test_grep_sensitive_path_asks(tmp_path: Path) -> None:
    mgr = PermissionManager(workspace=tmp_path)
    emitted, emitter = await _collect_emitted()

    async def _auto_allow() -> None:
        await asyncio.sleep(0)
        mgr.respond("gr2", "allow_once", session_id="s1")

    task = asyncio.create_task(_auto_allow())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="gr2", tool_name="grep",
        params={"pattern": "x", "path": ".git/config"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is True
    assert decision == "allow_once"
    assert len(emitted) == 1
    assert emitted[0]["risk_level"] == "high"
