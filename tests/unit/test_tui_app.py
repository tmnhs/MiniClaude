from __future__ import annotations

from typing import Any

from textual.widgets import Static

from mini_claude.tui.app import LLMStreamBlock, MiniClaudeTuiApp


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
