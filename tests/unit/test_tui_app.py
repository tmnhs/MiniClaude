from __future__ import annotations

from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static

from mini_claude.tui.app import MiniClaudeTuiApp
from mini_claude.tui.widgets import (
    LLMStreamBlock,
    ToolCallBlock,
    _param_summary,
    _preview,
)


# 拦截 _append 收集挂载的 widget，绕开 Textual DOM 和渲染层
def _collect(app: MiniClaudeTuiApp) -> list[Any]:
    widgets: list[Any] = []
    app._append = lambda widget: widgets.append(widget)  # type: ignore[method-assign]
    return widgets


# 取 Static 渲染出的纯文本内容
def _text(widget: Static) -> str:
    renderable = widget.render()
    return str(getattr(renderable, "plain", renderable))


# 功能：验证 llm.token 事件累积进同一个流式块，下一个非 token 事件结束该块并保留纯文本
# 设计：用 _collect 代替真实 RichLog，绕开 Textual 渲染层，直接测试缓冲和收尾逻辑
def test_llm_tokens_buffered_and_flushed_together() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)

    app._handle_event({"type": "llm.token", "token": "Hello", "run_id": "r", "ts": "t"})
    app._handle_event({"type": "llm.token", "token": " world", "run_id": "r", "ts": "t"})
    assert len(widgets) == 1
    block = widgets[0]
    assert isinstance(block, LLMStreamBlock)
    assert block.text == "Hello world"

    app._handle_event({"type": "step.finished", "run_id": "r", "step": 1, "ts": "t"})
    assert app._current_llm is None  # type: ignore[attr-defined]
    assert app._plain_history == ["Hello world"]  # type: ignore[attr-defined]


# 功能：验证 run.started 事件将 run_id 和 goal 写入日志
# 设计：_collect 记录所有追加的 widget，断言 run_id 和 goal 出现在某条文本中，
#       不约束格式细节，避免日志字符串变化导致测试脆化
def test_run_started_writes_run_id_and_goal() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)

    app._handle_event(
        {"type": "run.started", "run_id": "20260515-abc", "goal": "test the thing", "ts": "t"}
    )
    assert len(widgets) == 1
    text = _text(widgets[0])
    assert "20260515-abc" in text
    assert "test the thing" in text


# 功能：验证 run.finished 按 status 选择成功/失败样式类
# 设计：分别发送 success 和 failed 事件，检查对应 CSS 类（run-ok=green，run-err=red）存在，
#       两次使用同一 app 实例以确认状态不被前次调用污染
def test_run_finished_color_depends_on_status() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)

    app._handle_event(
        {"type": "run.finished", "run_id": "r", "status": "success", "steps": 2, "ts": "t"}
    )
    app._handle_event(
        {"type": "run.finished", "run_id": "r", "status": "failed", "steps": 3, "ts": "t"}
    )

    assert "run-ok" in widgets[0].classes
    assert "run-err" in widgets[1].classes


# 功能：验证 unknown type 的事件不写入日志也不抛异常（静默忽略）
# 设计：发送未知 type，断言没有 widget 被追加，确认 _handle_event 的默认路径是静默忽略而非报错
def test_unknown_event_type_is_silently_ignored() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)

    app._handle_event({"type": "some.unknown.event", "run_id": "r", "ts": "t"})
    assert widgets == []


# 功能：验证 _preview 超出长度时截断并追加省略号
# 设计：不依赖任何 TUI 组件，纯函数测试
def test_preview_truncates() -> None:
    assert _preview("abcde", 3) == "abc…"
    assert _preview("ab", 5) == "ab"


# 功能：验证工具参数摘要优先展示工具最关键字段
# 设计：覆盖 read_file/bash/note_save 三类常见工具，避免工具块摘要退化成整段 JSON
def test_param_summary_prefers_key_fields() -> None:
    assert _param_summary("read_file", {"path": "README.md"}) == "path='README.md'"
    assert _param_summary("bash", {"command": "echo hi", "timeout": 1}) == "command='echo hi'"
    assert _param_summary("note_save", {"content": "Python 3.12"}) == "content='Python 3.12'"


# 功能：验证 LLMStreamBlock 结束时会把累积文本渲染为 Rich Markdown
# 设计：直接调用 finalize_markdown，断言 renderable 类型，覆盖 Markdown polish 的核心行为
def test_llm_block_finalize_renders_markdown() -> None:
    block = LLMStreamBlock()
    block.append_token("## Title\n\n- one\n\n```python\nprint('hi')\n```")
    block.finalize_markdown()
    assert isinstance(block.content, Markdown)


# 功能：验证非 token 事件后 _current_llm 被重置，下一个 token 开启新块
# 设计：插入 step.started 中断流，验证前一块被 finalize，后续 llm.token 创建新 LLMStreamBlock
def test_llm_block_resets_after_non_token_event() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)

    app._handle_event({"type": "llm.token", "token": "A", "run_id": "r", "ts": "t"})
    app._handle_event({"type": "step.started", "run_id": "r", "step": 2, "ts": "t"})
    app._handle_event({"type": "llm.token", "token": "B", "run_id": "r", "ts": "t"})

    llm_blocks = [w for w in widgets if isinstance(w, LLMStreamBlock)]
    assert len(llm_blocks) == 2
    assert llm_blocks[0]._finalized


# 功能：验证 run.finished success 追加包含 "completed" 的 widget
# 设计：用 _collect 截获 widget，检查渲染内容包含 completed 和 green
def test_run_finished_success_shows_completed() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)

    app._handle_event(
        {"type": "run.finished", "run_id": "r", "status": "success", "steps": 3, "ts": "t"}
    )

    rendered = widgets[0].content
    assert "completed" in rendered
    assert "green" in rendered


# 功能：验证 run.finished failed 追加包含 "failed" 和 red 的 widget
# 设计：与 success 对称，检查颜色标记差异
def test_run_finished_failed_shows_red() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)

    app._handle_event(
        {
            "type": "run.finished",
            "run_id": "r",
            "status": "failed",
            "steps": 1,
            "reason": "llm_error",
            "ts": "t",
        }
    )

    rendered = widgets[0].content
    assert "failed" in rendered
    assert "red" in rendered


# 功能：验证 tool.call_started 追加 ToolCallBlock，call_finished 更新其结果
# 设计：直接调用 _handle_event 两次，通过 _pending_tool_blocks 验证状态流转
def test_tool_call_started_and_finished() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)

    app._handle_event(
        {
            "type": "tool.call_started",
            "tool_use_id": "uid-1",
            "tool_name": "bash",
            "params": {"command": "echo hi"},
            "run_id": "r",
            "ts": "t",
        }
    )
    assert "uid-1" in app._pending_tool_blocks

    app._handle_event(
        {
            "type": "tool.call_finished",
            "tool_use_id": "uid-1",
            "tool_name": "bash",
            "elapsed_ms": 42,
            "output": "hi",
            "run_id": "r",
            "ts": "t",
        }
    )
    assert "uid-1" not in app._pending_tool_blocks
    block = widgets[0]
    assert isinstance(block, ToolCallBlock)
    assert block._finished
    assert block._output == "hi"


# 功能：验证 note_save 成功完成时工具块摘要显示 remembered
# 设计：直接操作 ToolCallBlock，覆盖 note_save 的特殊低噪声展示策略
def test_note_save_tool_block_shows_remembered() -> None:
    block = ToolCallBlock("note_save", {"content": "Python 3.12"})
    block.set_result("saved", 3)
    assert "remembered" in block._summary()


# 功能：验证工具参数/输出中的 Rich markup 非法片段被转义，不会让摘要或展开详情崩溃
# 设计：用带 "[/not-a-tag]" 的 bash 命令构造 ToolCallBlock，分别解析摘要和点击后的详情文本；
#       未转义时 Text.from_markup 会抛 MarkupError，复现过真实崩溃
def test_tool_block_escapes_markup_in_params_and_output() -> None:
    block = ToolCallBlock("bash", {"command": "echo [/not-a-tag]"})
    block.set_result("stdout [/not-a-tag]", 5)

    captured: list[str] = []

    class _FakeDetail:
        def update(self, content: str) -> None:
            captured.append(content)

    block.query_one = lambda *a, **kw: _FakeDetail()  # type: ignore[method-assign]
    block.add_class = lambda *a, **kw: None  # type: ignore[method-assign]

    block.on_click()

    Text.from_markup(block._summary())
    assert captured
    Text.from_markup(captured[0])


# 功能：验证提交用户输入时会追加 user turn，并进入 busy 状态
# 设计：用 fake client 替代 SocketClient，直接调用 on_chat_text_area_submitted，
#       覆盖 TextArea 清空内容 + 设置 busy 占位符的核心状态迁移
async def test_input_submit_appends_user_turn_and_disables_prompt() -> None:
    class _FakeArea:
        def __init__(self) -> None:
            self.disabled = False
            self.border_title = ""
            self.text = "hello"

    class _FakeEvent:
        def __init__(self, area: _FakeArea) -> None:
            self.value = area.text
            self.text_area = area

    class _FakeClient:
        async def send_command(self, method: str, params: dict) -> dict:
            return {"run_id": "run-1"}

    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets: list[Widget] = []
    app._append = lambda w: widgets.append(w)  # type: ignore[method-assign]
    app._update_header = lambda state: None  # type: ignore[method-assign]
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-1"

    area = _FakeArea()
    event = _FakeEvent(area)
    await app.on_chat_text_area_submitted(event)  # type: ignore[arg-type]

    assert app._busy
    assert area.disabled
    assert area.text == ""
    assert "agent is working" in area.border_title.lower()
    assert widgets[0].content == "[bold]>[/bold] hello"


# 功能：验证来自其他 session 的事件被防御性丢弃
# 设计：设置 app._session_id=sess-A，发送 session_id=sess-B 的 run.started，断言无 widget 追加
def test_event_from_other_session_is_dropped() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)
    app._session_id = "sess-A"

    app._handle_event(
        {
            "type": "run.started",
            "run_id": "r",
            "goal": "g",
            "session_id": "sess-B",
            "ts": "t",
        }
    )

    assert widgets == []


# 功能：验证匹配当前 session 的事件正常处理
# 设计：session_id 与 app._session_id 一致，断言 run.started 被渲染
def test_event_from_same_session_is_handled() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)
    app._session_id = "sess-A"

    app._handle_event(
        {
            "type": "run.started",
            "run_id": "r",
            "goal": "g",
            "session_id": "sess-A",
            "ts": "t",
        }
    )

    assert len(widgets) == 1


# 功能：验证缺少 session_id 的旧事件被放行（向后兼容）
# 设计：不设置 session_id 字段，app._session_id=sess-A，断言事件仍被渲染
def test_event_without_session_id_is_handled() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)
    app._session_id = "sess-A"

    app._handle_event(
        {"type": "run.started", "run_id": "r", "goal": "g", "ts": "t"}
    )

    assert len(widgets) == 1


# 功能：验证 replay 模式下不丢弃其他 session 的历史事件
# 设计：构造 replay_run_id 的 app，发送不匹配 session_id 的事件，断言仍被渲染
def test_replay_mode_allows_other_session_events() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999, replay_run_id="run-old")
    widgets = _collect(app)
    app._session_id = "sess-A"

    app._handle_event(
        {
            "type": "run.started",
            "run_id": "run-old",
            "goal": "g",
            "session_id": "sess-B",
            "ts": "t",
        }
    )

    assert len(widgets) == 1


# 功能：验证审批决策发送的 permission.respond 带当前 session_id
# 设计：fake client 捕获命令参数，断言 tool_use_id/decision/session_id 三者正确
async def test_permission_decided_sends_session_id() -> None:
    class _FakeWidget:
        def remove(self) -> None:
            pass

    class _Msg:
        tool_use_id = "uid-1"
        decision = "allow_once"
        widget = _FakeWidget()

    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def send_command(self, method: str, params: dict) -> dict:
            self.calls.append((method, params))
            return {}

    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    app._session_id = "sess-A"
    client = _FakeClient()
    app._client = client  # type: ignore[assignment]
    # 保留一个 pending，避免处理函数尝试操作未挂载的输入框
    app._pending_permission_blocks["other"] = object()  # type: ignore[assignment]

    await app.on_permission_select_decided(_Msg())  # type: ignore[arg-type]

    assert client.calls == [
        (
            "permission.respond",
            {"tool_use_id": "uid-1", "decision": "allow_once", "session_id": "sess-A"},
        )
    ]


# 功能：验证 _reset_session_state 清空所有会话瞬态状态
# 设计：手动置脏各状态字段后调用 reset，逐个断言已恢复初始值
def test_reset_session_state_clears_transient_fields() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    app._busy = True
    app._pending_tool_blocks["t"] = object()  # type: ignore[assignment]
    app._pending_permission_blocks["p"] = object()  # type: ignore[assignment]
    app._last_context_pct = 55.0
    app._subagent_run_ids["r"] = "desc"
    app._subagent_start_times["r"] = 1.0

    app._reset_session_state()

    assert app._busy is False
    assert app._pending_tool_blocks == {}
    assert app._pending_permission_blocks == {}
    assert app._last_context_pct == 0.0
    assert app._subagent_run_ids == {}
    assert app._subagent_start_times == {}
    assert app._current_llm is None
