from __future__ import annotations

import json
import logging
import time
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.text import Text
from textual import events
from textual._context import NoActiveAppError
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TextArea

log = logging.getLogger(__name__)

# 流式 token 合并刷新的最小间隔（秒），避免每个 token 都触发布局与重绘
_TOKEN_FLUSH_INTERVAL = 0.05


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


class LogView(VerticalScroll):
    """事件日志滚动容器：滚动位置变化时通知 App，用于决定是否自动跟随底部。"""

    # 滚动位置更新后发布，at_end 表示当前是否已滚到最底部
    class Scrolled(Message):
        def __init__(self, at_end: bool) -> None:
            self.at_end = at_end
            super().__init__()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_attached:
            self.post_message(self.Scrolled(self.is_vertical_scroll_end))


def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "bash": ("command",),
        "note_save": ("content",),
        "grep": ("pattern", "path"),
        "glob": ("pattern", "path"),
        "git": ("subcommand",),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class LLMStreamBlock(Static):
    """在同一个 Static widget 中累积 LLM 流式 token。"""

    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; color: $text; }"

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__("")
        self._text = ""
        self._finalized = False
        self._last_flush = 0.0
        self._flush_scheduled = False

    # 追加一个 token；按固定间隔合并刷新，流结束时由 finalize 统一收尾
    def append_token(self, token: str) -> None:
        if self._finalized:
            return

        self._text += token
        if time.monotonic() - self._last_flush >= _TOKEN_FLUSH_INTERVAL:
            self._flush()
        elif not self._flush_scheduled and self.is_attached:
            self._flush_scheduled = True
            self.set_timer(_TOKEN_FLUSH_INTERVAL, self._flush)

    # 将当前累积文本写入显示区；定时器与即时刷新共用
    def _flush(self) -> None:
        self._flush_scheduled = False
        if self._finalized:
            return
        self._last_flush = time.monotonic()
        self.update(self._text)

    # 当前已累积的纯文本
    @property
    def text(self) -> str:
        return self._text

    # 将累积文本渲染为 Markdown，供流式块结束后显示
    def finalize_markdown(self) -> None:
        if self._finalized:
            return

        self._finalized = True
        if self._text.strip():
            self.update(self._markdown_content())

    # 把 Markdown 渲染为 Content：Rich 渲染结果是 RichVisual，
    # Textual 无法从中提取选中文本，导致 Ctrl+C 复制不到内容
    def _markdown_content(self) -> Content:
        try:
            console = self.app.console
        except NoActiveAppError:
            console = Console()
        width = self.content_size.width or console.width
        options = console.options.update_width(width).update(highlight=False)
        text = Text()
        for segment in console.render(Markdown(self._text, code_theme="monokai"), options):
            text.append(segment.text, style=segment.style)
        return Content.from_rich_text(text, console)


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 2; color: $text-muted; }
    ToolCallBlock > .summary { color: $text-muted; }
    ToolCallBlock > .detail { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        yield Static("", classes="detail")

    # 生成摘要行文本
    def _summary(self) -> str:
        if self._tool_name == "note_save" and self._finished and not self._is_error:
            return f"  [green]remembered[/green]  [dim]{self._elapsed_ms}ms[/dim]"

        params_pre = _param_summary(self._tool_name, self._params)
        line = f"  [dim]tool[/dim] [bold]{self._tool_name}[/bold]"
        if params_pre:
            line += f"  [dim]{escape(params_pre)}[/dim]"

        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click to expand)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            self.query_one(".summary", Static).update(self._summary())

    # 点击时切换展开/折叠状态
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"[dim]params:[/dim]\n    {escape(self._params_full)}\n"
                f"[dim]output:[/dim]\n    {escape(self._output)}\n"
                f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
            )
            self.add_class("expanded")


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：输入 / 时显示可用 skill 列表并支持键盘筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 (name, description) 列表
    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[tuple[str, str]] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [(n, d) for n, d in self._all_items if not q or q in n.lower()]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表，高亮当前光标项
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._filtered):
            desc_part = f"  [dim]{escape(desc)}[/dim]" if desc else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{escape(name)}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{escape(name)}[/cyan]{desc_part}")
        lines.append("[dim]  ↑↓ navigate   tab/enter select   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    # 初始化输入历史：已提交内容、当前浏览位置（None 表示正在编辑草稿）、进入历史前的草稿
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""

    # 记录一次已提交的输入，并重置历史浏览位置（连续重复输入只保留一条）
    def _remember(self, content: str) -> None:
        if content and (not self._history or self._history[-1] != content):
            self._history.append(content)
        self._history_index = None
        self._history_draft = ""

    # 将历史条目载入输入框，并把光标移到末尾
    def _load_history(self, value: str) -> None:
        self.load_text(value)
        last_row = value.count("\n")
        self.move_cursor((last_row, len(value) - value.rfind("\n") - 1))

    # 切换到更早的输入；没有历史时返回 False 交由 TextArea 正常处理
    def _history_back(self) -> bool:
        if not self._history:
            return False
        if self._history_index is None:
            self._history_draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return True  # 已到最早一条，保持当前内容
        self._load_history(self._history[self._history_index])
        return True

    # 切换到更晚的输入；越过最新一条时恢复进入历史前编辑的草稿
    def _history_forward(self) -> bool:
        if self._history_index is None:
            return False
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._load_history(self._history[self._history_index])
        else:
            self._history_index = None
            self._load_history(self._history_draft)
            self._history_draft = ""
        return True

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            if popup is not None and popup.has_selection():
                popup.select_current()
                return
            content = self.text.strip()
            if content:
                self._remember(content)
                self.post_message(self.Submitted(self))
            return

        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return

        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return

        # 光标位于首行时 ↑ 翻历史，位于末行时 ↓ 翻历史；其余情况保留多行文本内的正常移动
        if key == "up" and self.cursor_location[0] == 0 and self._history_back():
            event.stop()
            event.prevent_default()
            return
        on_last_line = self.cursor_location[0] == self.text.count("\n")
        if key == "down" and on_last_line and self._history_forward():
            event.stop()
            event.prevent_default()
            return

        await super()._on_key(event)


class PermissionSelect(Static):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once",   "Allow once",   "y / 1"),
        ("always_allow", "Always allow", "a / 2"),
        ("deny_once",    "Deny",         "n / 3"),
        ("always_deny",  "Always deny",  "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",  "1": "allow_once",
        "a": "always_allow","2": "always_allow",
        "n": "deny_once",   "3": "deny_once",
        "d": "always_deny", "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(self, widget: PermissionSelect, tool_use_id: str, decision: str) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储工具 ID（用于 IPC 回复）与风险等级（高风险不提供 always allow）
    def __init__(self, tool_use_id: str, risk_level: str = "low") -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self._risk_level = risk_level
        self._choices = self._choices_for_risk(risk_level)
        allowed = {choice[0] for choice in self._choices}
        self._key_map = {key: dec for key, dec in self._KEY_MAP.items() if dec in allowed}
        self._cursor = 0

    # 高风险命令不提供 always allow 选项
    def _choices_for_risk(self, risk_level: str) -> tuple[tuple[str, str, str], ...]:
        if risk_level == "high":
            return tuple(c for c in self._CHOICES if c[0] != "always_allow")
        return self._CHOICES

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()
        log.debug(
            "PermissionSelect.on_mount  can_focus=%s  focused_after=%r",
            self.can_focus,
            self.app.focused,
        )
        self.app.call_after_refresh(self._log_deferred_focus)

    # 在下一帧记录焦点是否真正转移到本控件
    def _log_deferred_focus(self) -> None:
        log.debug(
            "PermissionSelect.deferred_focus  app.focused=%r  has_focus=%s  focusable=%s",
            self.app.focused,
            self.has_focus,
            self.focusable,
        )

    # 焦点到达时记录，用于确认 focus() 是否真正生效
    def on_focus(self, event: events.Focus) -> None:
        log.debug(
            "PermissionSelect.on_focus  has_focus=%s  app.focused=%r",
            self.has_focus,
            self.app.focused,
        )

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._choices):
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ {label}[/bold cyan]  [dim]{key_hint}[/dim]")
            else:
                lines.append(f"    {label}  [dim]{key_hint}[/dim]")
        lines.append("[dim]  ↑↓ navigate   enter confirm[/dim]")
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key

        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._choices)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._choices)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._choices[self._cursor][0])
        else:
            decision = self._key_map.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        log.debug("PermissionSelect._pick  decision=%s", decision)
        self.post_message(self.Decided(self, self._tool_use_id, decision))


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once":   "allowed (once)",
        "always_allow": "always allowed",
        "deny_once":    "denied",
        "always_deny":  "always denied",
        "timeout":      "⏱ timed out",
        "cancelled":    "cancelled",
    }
    LABEL_MAP = _LABEL_MAP

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、名称、参数预览与影响路径
    def __init__(
        self,
        tool_use_id: str,
        tool_name: str,
        param_preview: str,
        affected_paths: tuple[str, ...] = (),
    ) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._affected_paths = affected_paths
        self._resolved = False
        super().__init__(self._pending_text(), classes="log-line")

    # 影响路径，供审批块单行展示
    def _paths_suffix(self) -> str:
        if not self._affected_paths:
            return ""
        joined = ", ".join(self._affected_paths)
        return f"  [dim]paths: {escape(joined)}[/dim]"

    def _pending_text(self) -> str:
        preview = f"  [dim]{escape(self._param_preview)}[/dim]" if self._param_preview else ""
        return (
            f"[bold red]? permission[/bold red]  [bold]{self._tool_name}[/bold]"
            f"{self._paths_suffix()}{preview}"
        )

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return

        self._resolved = True
        allowed = decision in ("allow_once", "always_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        preview = f"  [dim]{escape(self._param_preview)}[/dim]" if self._param_preview else ""

        self.update(
            f"{icon} permission  [bold]{self._tool_name}[/bold]"
            f"{self._paths_suffix()}{preview}  [dim]{label}[/dim]"
        )
        self.post_message(self.Resolved(self, decision))
