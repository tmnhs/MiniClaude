from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.content import Content
from textual.selection import SELECT_ALL
from textual.widget import Widget
from textual.widgets import Static

from mini_claude.tui.app import MiniClaudeTuiApp
from mini_claude.tui.widgets import (
    LLMStreamBlock,
    PermissionBlock,
    PermissionSelect,
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


# 功能：验证 grep/glob/git 的摘要展示关键字段
# 设计：grep/glob 展示 pattern+path，git 展示 subcommand，避免整段 JSON 撑破摘要行
def test_param_summary_new_tools() -> None:
    assert _param_summary("grep", {"pattern": "TODO", "path": "src"}) == (
        "pattern='TODO', path='src'"
    )
    assert _param_summary("glob", {"pattern": "*.py"}) == "pattern='*.py'"
    assert _param_summary("git", {"subcommand": "status", "args": []}) == "subcommand='status'"


# 功能：验证 LLMStreamBlock 结束时渲染为 Content（Markdown 排版但可选中复制）
# 设计：RichVisual 不支持 Textual 的文本提取，必须以 Content 承载渲染结果
def test_llm_block_finalize_renders_markdown() -> None:
    block = LLMStreamBlock()
    block.append_token("## Title\n\n- one\n\n```python\nprint('hi')\n```")
    block.finalize_markdown()
    assert isinstance(block.content, Content)


# 功能：验证收尾后的 Markdown 块能被提取出完整文本，供 Ctrl+C 复制
# 设计：用 SELECT_ALL 模拟全选，断言 get_selection 返回正文而非 None
def test_llm_block_finalize_is_copyable() -> None:
    block = LLMStreamBlock()
    block.append_token("## Title\n\nHello **world**")
    block.finalize_markdown()

    selected = block.get_selection(SELECT_ALL)

    assert selected is not None
    text, _ = selected
    assert "Title" in text
    assert "Hello world" in text


# 功能：验证 Cmd+C（super+c）注册了复制绑定
# 设计：Screen 先复制选中文本；App 层的 super+c 作为无选中时的兜底
def test_super_c_binding_registered() -> None:
    bindings = {
        (binding.key, binding.action)
        for binding in MiniClaudeTuiApp.BINDINGS
        if isinstance(binding, Binding)
    }
    assert ("super+c", "copy_last") in bindings


# 功能：验证 action_copy_last 把最后一条 LLM 文本写入剪贴板
# 设计：替换 copy_to_clipboard/notify，避免依赖真实系统剪贴板与消息泵
def test_action_copy_last_writes_clipboard() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]
    app._plain_history.append("final answer")  # type: ignore[attr-defined]

    app.action_copy_last()

    assert copied == ["final answer"]


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


# 功能：验证 run.started 进入运行态并记录起始时间，供顶栏展示 step/耗时
# 设计：只断言运行态字段与 header 状态，时间轴不断言具体数值避免时钟依赖
def test_run_started_enters_running_state() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    _collect(app)

    app._handle_event({"type": "run.started", "run_id": "r", "goal": "g", "ts": "t"})

    assert app._run_started_at is not None  # type: ignore[attr-defined]
    assert app._current_step == 0  # type: ignore[attr-defined]
    assert app._header_state == "running"  # type: ignore[attr-defined]


# 功能：验证 run.finished(cancelled) 显示"已中断"，清除运行态并丢弃待审批
# 设计：预置 busy/step/pending 审批，发送 reason=cancelled 的终态，逐项断言已复位
def test_run_finished_cancelled_resets_state() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    widgets = _collect(app)
    app._busy = True
    app._run_started_at = 123.0  # type: ignore[attr-defined]
    app._current_step = 4  # type: ignore[attr-defined]
    app._pending_permission_blocks["u1"] = PermissionBlock("u1", "bash", "ls")  # type: ignore[assignment]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "failed",
        "reason": "cancelled", "steps": 4, "ts": "t",
    })

    assert app._busy is False
    assert app._run_started_at is None  # type: ignore[attr-defined]
    assert app._current_step == 0  # type: ignore[attr-defined]
    assert app._pending_permission_blocks == {}
    assert "run-err" in widgets[0].classes
    assert "已中断" in widgets[0].content


# 功能：验证左半顶栏含状态徽章与会话信息，右半含 step/耗时和上下文进度条
# 设计：直接设置状态字段后分别取左右 markup，覆盖运行态与高占用（bold red）分支；
#       同时断言模型名在无上下文占用时作为右半回退显示
def test_header_markup_includes_step_and_context() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999, model="m")
    app._session_id = "sess-A"
    app._header_state = "running"  # type: ignore[attr-defined]
    app._run_started_at = time.monotonic()  # type: ignore[attr-defined]
    app._current_step = 3  # type: ignore[attr-defined]
    app._last_context_pct = 0.9  # type: ignore[attr-defined]

    left = app._header_left_markup()
    right = app._header_right_markup()

    assert "运行中" in left
    assert "on yellow" in left  # 运行态徽章使用黄底
    assert "sess-A" in left
    assert "step 3" in right
    assert "90%" in right
    assert "bold red" in right
    assert "▓" * 7 + "░" in right  # 90% 对应 8 格中的 7 格

    # 无上下文占用时右半回退为模型名（运行中仍在前面保留 step/耗时）
    app._last_context_pct = 0.0  # type: ignore[attr-defined]
    assert app._header_right_markup().endswith("[dim]m[/dim]")


# 功能：验证上下文进度条按占用率分档着色与填充格数
# 设计：抽查 0%/50%/75%/100% 四档，断言格数、百分比与颜色标记
def test_ctx_gauge_thresholds() -> None:
    assert "░░░░░░░░" in MiniClaudeTuiApp._ctx_gauge(0.0)
    assert "dim" in MiniClaudeTuiApp._ctx_gauge(0.5)

    mid = MiniClaudeTuiApp._ctx_gauge(0.75)
    assert "▓" * 6 + "░" * 2 in mid
    assert "yellow" in mid

    full = MiniClaudeTuiApp._ctx_gauge(1.0)
    assert "▓" * 8 in full
    assert "bold red" in full


# 功能：验证方块字 logo 每行等宽、行数与渐变色数一致
# 设计：字形是手工对齐的 ASCII art，行宽不一致会在面板中错位，故用断言锁定
def test_logo_rows_are_uniform_width() -> None:
    rows = MiniClaudeTuiApp._LOGO_ROWS
    styles = MiniClaudeTuiApp._LOGO_STYLES

    assert len(rows) == len(styles) == 5
    assert len({len(row) for row in rows}) == 1


# 功能：验证运行中每秒推进 spinner 帧，空闲时不推进
# 设计：设置运行起始时间后连续调用两次 _refresh_header_clock，断言帧索引递增；
#       再清空运行态，断言索引保持不变
def test_refresh_header_clock_advances_spinner_only_when_running() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    app._busy = True
    app._run_started_at = time.monotonic()  # type: ignore[attr-defined]

    app._refresh_header_clock()
    first = app._spinner_index  # type: ignore[attr-defined]
    app._refresh_header_clock()
    assert app._spinner_index == first + 1  # type: ignore[attr-defined]

    app._busy = False
    app._run_started_at = None  # type: ignore[attr-defined]
    app._refresh_header_clock()
    assert app._spinner_index == first + 1  # type: ignore[attr-defined]


# 功能：验证空闲时 Esc 不触发中断，避免无谓的 RPC
# 设计：_busy=False 时替换 run_worker 捕获调用，断言未被调用
def test_action_cancel_run_ignored_when_idle() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    app._client = object()  # type: ignore[assignment]
    app._session_id = "sess-A"
    scheduled: list[Any] = []
    app.run_worker = lambda *a, **kw: scheduled.append(a)  # type: ignore[method-assign]

    app.action_cancel_run()

    assert scheduled == []


# 功能：验证中断请求发送 run.cancel 并携带当前 session_id
# 设计：fake client 记录命令，直接 await _do_cancel_run 避免调度 worker
async def test_do_cancel_run_sends_command() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def send_command(self, method: str, params: dict) -> dict:
            self.calls.append((method, params))
            return {"cancelled": True}

    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    app._append = lambda widget: None  # type: ignore[method-assign]
    client = _FakeClient()
    app._client = client  # type: ignore[assignment]
    app._session_id = "sess-A"
    app._busy = True
    app._cancel_requested = True  # type: ignore[attr-defined]

    await app._do_cancel_run()

    assert client.calls == [("run.cancel", {"session_id": "sess-A"})]


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


# 功能：验证回放中历史 permission.requested 只渲染只读记录，不弹出交互选择器
# 设计：置 _replaying=True 后发送 permission.requested，断言不查询/挂载 PermissionSelect，
#       再发送 permission.granted，断言记录被回填且从 pending 中移除
def test_replay_permission_requested_is_read_only_and_resolves() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999, resume_session_id="sess-A")
    widgets = _collect(app)
    app._replaying = True
    mounted: list[Any] = []
    app._mount_permission_select = lambda sel: mounted.append(sel)  # type: ignore[method-assign]

    app._handle_event(
        {
            "type": "permission.requested",
            "tool_use_id": "uid-1",
            "tool_name": "bash",
            "param_preview": "command='ls'",
            "session_id": "sess-A",
            "run_id": "r",
            "ts": "t",
        }
    )

    assert len(widgets) == 1
    assert isinstance(widgets[0], PermissionBlock)
    assert "uid-1" in app._pending_permission_blocks
    assert mounted == []  # 回放不挂载选择器

    app._handle_event(
        {
            "type": "permission.granted",
            "tool_use_id": "uid-1",
            "decision": "allow_once",
            "run_id": "r",
            "ts": "t",
        }
    )

    assert "uid-1" not in app._pending_permission_blocks
    assert widgets[0]._resolved
    assert "allowed (once)" in _text(widgets[0])


# 功能：验证回放中 permission.denied 同样回填为只读结果
# 设计：requested 后发送 denied，断言记录 resolved 且显示 denied 标签
def test_replay_permission_denied_resolves_block() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999, resume_session_id="sess-A")
    widgets = _collect(app)
    app._replaying = True
    app._mount_permission_select = lambda sel: None  # type: ignore[method-assign]

    app._handle_event(
        {
            "type": "permission.requested",
            "tool_use_id": "uid-2",
            "tool_name": "bash",
            "param_preview": "",
            "session_id": "sess-A",
            "run_id": "r",
            "ts": "t",
        }
    )
    app._handle_event(
        {
            "type": "permission.denied",
            "tool_use_id": "uid-2",
            "decision": "deny_once",
            "run_id": "r",
            "ts": "t",
        }
    )

    assert "uid-2" not in app._pending_permission_blocks
    assert widgets[0]._resolved
    assert "denied" in _text(widgets[0])


# 功能：验证审批块展示影响路径但不展示风险等级
# 设计：TUI 不再呈现风险徽标，路径仍保留供用户判断
def test_permission_block_shows_paths_without_risk_badge() -> None:
    block = PermissionBlock("u1", "bash", "command='rm -rf x'", ("/tmp/x",))
    text = _text(block)
    assert "/tmp/x" in text
    assert "HIGH RISK" not in text
    assert "MEDIUM" not in text
    assert "LOW" not in text


# 功能：验证高风险选择器不提供 always allow 选项
# 设计：高风险直接移除该选项与快捷键，而不是给出提示
def test_permission_select_hides_always_allow_on_high_risk() -> None:
    select = PermissionSelect("u1", "high")
    rendered = select._render_ui()
    assert "Always allow" not in rendered
    assert "Allow once" in rendered
    assert "a" not in select._key_map
    assert "2" not in select._key_map


# 功能：验证实时模式 permission.requested 仍会挂载交互选择器并锁住输入框
# 设计：_replaying=False，替换挂载方法记录调用，断言选择器被挂载（不回归审批交互）
def test_live_permission_requested_mounts_select() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    _collect(app)
    mounted: list[Any] = []
    app._prompt = lambda: None  # type: ignore[method-assign]  # 未挂载 DOM，跳过输入框操作
    app._mount_permission_select = lambda sel: mounted.append(sel)  # type: ignore[method-assign]

    app._handle_event(
        {
            "type": "permission.requested",
            "tool_use_id": "uid-3",
            "tool_name": "bash",
            "param_preview": "command='ls'",
            "run_id": "r",
            "ts": "t",
        }
    )

    assert len(mounted) == 1
    assert getattr(mounted[0], "_tool_use_id", None) == "uid-3"
    assert "uid-3" in app._pending_permission_blocks


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
    assert "agent" in area.border_title.lower()
    assert "esc" in area.border_title.lower()
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


# 功能：验证 resume_hint 使用最近一次 session id 生成可复制命令
# 设计：直接设置 _last_session_id，断言提示文案含 --resume 与完整 sid
def test_resume_hint_contains_session_id() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    app._last_session_id = "sess-abc123"

    hint = app.resume_hint()

    assert hint is not None
    assert "mc-tui --resume sess-abc123" in hint


# 功能：验证 replay 模式或尚未建立会话时不输出恢复提示
# 设计：覆盖两种空场景，避免打印无意义的 --resume 命令
def test_resume_hint_none_for_replay_or_no_session() -> None:
    replay = MiniClaudeTuiApp("127.0.0.1", 9999, replay_run_id="run-old")
    replay._last_session_id = "sess-abc"
    assert replay.resume_hint() is None

    fresh = MiniClaudeTuiApp("127.0.0.1", 9999)
    assert fresh.resume_hint() is None


# 功能：验证 print_resume_hint 在终端打印可复制的恢复命令
# 设计：用 capsys 捕获 stdout，断言命令出现在输出中；replay 模式则保持静默
def test_print_resume_hint_outputs_command(capsys: Any) -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    app._last_session_id = "sess-xyz789"

    app.print_resume_hint()

    out = capsys.readouterr().out
    assert "mc-tui --resume sess-xyz789" in out

    replay = MiniClaudeTuiApp("127.0.0.1", 9999, replay_run_id="run-old")
    replay._last_session_id = "sess-xyz789"
    replay.print_resume_hint()
    assert capsys.readouterr().out == ""


# 功能：验证退出时会先把恢复提示写入日志再调用 exit
# 设计：未连接状态下调用 action_quit，截获 _append 内容与 exit 调用，
#       断言提示含 --resume 且应用确实退出
async def test_action_quit_appends_resume_hint() -> None:
    app = MiniClaudeTuiApp("127.0.0.1", 9999)
    app._last_session_id = "sess-quit1"
    captured: list[Any] = []
    app._append = lambda widget: captured.append(widget)  # type: ignore[method-assign]
    exited: list[bool] = []
    app.exit = lambda *a, **k: exited.append(True)  # type: ignore[method-assign]

    await app.action_quit()

    assert exited == [True]
    assert any("mc-tui --resume sess-quit1" in _text(w) for w in captured)
