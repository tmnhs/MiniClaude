from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
import uuid
from typing import Any

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Label, Static

from mini_claude.core.conf.config import MiniClaudeConfig
from mini_claude.core.skills.loader import SkillLoader
from mini_claude.core.transport.socket_client import IpcError, SocketClient
from mini_claude.tui.widgets import (
    ChatTextArea,
    LLMStreamBlock,
    PermissionBlock,
    PermissionSelect,
    SlashCompleteWidget,
    ToolCallBlock,
    _preview,
)

logger = logging.getLogger(__name__)

class MiniClaudeTuiApp(App[None]):
    """MiniClaude 终端 UI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "MiniClaude TUI"
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
    ]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    #banner { padding: 1 2 0 2; }
    """

    _BANNER = (
        "[bold cyan]MiniClaude[/bold cyan]  [dim]·[/dim]  [dim]terminal agent[/dim]\n"
        "[dim]  输入消息开始对话  ·  键入 / 触发 skill  ·  Ctrl+Q 退出[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._plain_history: list[str] = []
        self._session_id: str | None = None
        self._busy = False
        self._last_context_pct: float = 0.0
        self._slash_items: list[tuple[str, str]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time

     # 构建 UI：顶部状态栏 + 可滚动事件日志
    def compose(self) -> ComposeResult:
        yield Label("[bold]MiniClaude[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    # 挂载后启动 socket 连接 worker，并展示 banner / 预构建斜杠候选
    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(Static(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [("compact", "compress context window")]
        try:
            for skill in SkillLoader().list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc))
        except Exception:
            pass
        return items

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return

        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        logger.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里

            key = event.key
            decision = PermissionSelect._KEY_MAP.get(key)

            if decision:
                event.stop()
                select._pick(decision)

            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())

            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())

            elif key == "enter":
                event.stop()
                select._pick(PermissionSelect._CHOICES[select._cursor][0])
        except Exception:
            pass

    # 退出前尽力关闭当前 session，失败也不阻塞 TUI 退出
    async def action_quit(self) -> None:
        if self._client is not None and self._session_id is not None:
            try:
                await self._client.send_command("session.close", {"session_id": self._session_id})
            except (IpcError, RuntimeError, OSError):
                self._append(Static("[yellow]warning: failed to close session[/yellow]"))
        self.exit()

    # 将输入框提交内容发送给当前 chat session
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return

        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return

        if self._client is None or self._session_id is None or self._busy:
            self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
            return

        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.border_title = "agent is working..."
        self._append(Static(f"[bold]>[/bold] {escape(content)}", classes="user-turn"))

        self._update_header("running")
        self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return

        self._append(Static("[dim]⚡ compacting context...[/dim]", classes="log-line"))
        try:
            result = await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )

            summary_tokens = result.get("summary_tokens", 0)
            saved_tokens = result.get("saved_tokens", 0)
            self._last_context_pct = 0.0

            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]summary={summary_tokens} tokens  saved≈{saved_tokens} tokens[/dim]",
                classes="log-line",
            ))
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]compact error: {e}[/red]", classes="log-line"))

    # 写入剪贴板：OSC 52 为主，macOS 上额外用 pbcopy 兼容 Terminal.app
    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        if sys.platform == "darwin":
            try:
                subprocess.run(["pbcopy"], input=text.encode(), check=False)
            except OSError:
                pass

    # 复制最后一次完整的 LLM 文本
    def action_copy_last(self) -> None:
        if not self._plain_history:
            self.notify("No message to copy", severity="warning")
            return
        self.copy_to_clipboard(self._plain_history[-1])
        self.notify("Copied last message")

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str) -> None:
        if self._client is None:
            return

        try:
            await self._client.send_command(
                "session.send_message",
                {"session_id": self._session_id, "content": content},
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"

            self._update_header("ready")
            self._append(Static(f"[red]send error: {e}[/red]", classes="log-line"))

     # 处理内联审批控件的用户决策：发送 IPC 响应并恢复输入框
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        tool_use_id = msg.tool_use_id
        decision = msg.decision
        logger.info("permission decided tool_use_id=%s decision=%s", tool_use_id, decision)

        try:
            msg.widget.remove()
            perm_block = self._pending_permission_blocks.pop(tool_use_id, None)
            if perm_block is not None:
                perm_block._resolve(decision)

            if self._client is not None:
                try:
                    await self._client.send_command(
                        "permission.respond",
                        {
                            "tool_use_id": tool_use_id,
                            "decision": decision,
                            "session_id": self._session_id,
                        },
                    )
                except (IpcError, RuntimeError, OSError):
                    pass

            if not self._pending_permission_blocks:
                p = self._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    p.focus()
        except Exception:
            logger.exception("on_permission_select_decided failed tool_use_id=%s", tool_use_id)

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块，并保留其纯文本供复制
    def _break_llm(self) -> None:
        if self._current_llm is not None and self._current_llm.text:
            self._plain_history.append(self._current_llm.text)
            self._current_llm.finalize_markdown()
        self._current_llm = None

    # 断连/换会话时清空瞬态状态，避免上一个会话的残留影响下一个
    def _reset_session_state(self) -> None:
        self._busy = False
        self._break_llm()
        self._pending_tool_blocks.clear()
        self._pending_permission_blocks.clear()
        self._last_context_pct = 0.0
        self._subagent_run_ids.clear()
        self._subagent_start_times.clear()

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except NoMatches:
            return None

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return

        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        color = {
            "ready": "green",
            "running": "yellow",
            "disconnected": "red",
            "connecting": "dim",
        }.get(state, "dim")

        header.update(
            f"[bold]MiniClaude[/bold]  [dim]{self._host}:{self._port}[/dim]"
            f"{session}  [{color}]{state}[/{color}]"
        )

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                header.update("[bold]MiniClaude[/bold]  [red]not connected — retrying...[/red]")
                await asyncio.sleep(2)
                continue

            logger.info("connected to %s:%s", self._host, self._port)
            self._client = client
            self._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                # 客户端预生成 session_id，先订阅再创建：create 期间（含 session.created）
                # 的事件不会因订阅尚未就绪而丢失；服务端收到 id 时直接使用
                self._session_id = f"sess-{uuid.uuid4().hex[:12]}"

                params: dict[str, Any] = {
                    "topics": [
                        "session.*",
                        "run.*",
                        "step.*",
                        "tool.*",
                        "llm.token",
                        "llm.usage",
                        "logger.*",
                        "permission.*",
                        "context.*",
                        "subagent.*",
                        "skill.*",
                    ],
                    "scope": f"session:{self._session_id}",
                }

                if self._replay_run_id is not None:
                    params["replay_from_run"] = self._replay_run_id

                await client.send_command("event.subscribe", params)
                await client.send_command(
                    "session.create",
                    {"mode": "chat", "session_id": self._session_id},
                )
                logger.info("session created session_id=%s", self._session_id)

                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = False
                    prompt.read_only = False
                    prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    prompt.focus()
                self._update_header("ready")

                await loop_task
            except IpcError as e:
                header.update(f"[bold]MiniClaude[/bold]  [red]subscribe error: {e}[/red]")
            except asyncio.CancelledError:
                # pending RPC future 被取消 = 连接在等待响应时断开：按断连处理并继续重连；
                # 若是 worker 被主动取消（应用退出），继续向上传播
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                logger.debug("connection dropped while awaiting rpc response")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._client = None
                self._session_id = None

                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.read_only = False
                    prompt.border_title = "disconnected, retrying..."

                self._reset_session_state()
                await client.close()

            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event_inner(event)
        except Exception:
            logger.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 生成 context 占用率的彩色进度条字符串
    def _render_ctx_bar(self, pct: float) -> str:
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        label = f"ctx:{pct * 100:.1f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        # 防御性过滤：服务端已按 session scope 隔离，这里兜底旧 core / 旧日志的事件
        # 回放模式下放行，允许查看历史会话
        sid = event.get("session_id")
        if sid and self._session_id and sid != self._session_id and self._replay_run_id is None:
            return

        t = event.get("type", "")

        if t == "llm.token":
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return

        self._break_llm()

        if t == "session.waiting_for_input":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_header("ready")

        elif t == "session.closed":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("disconnected")

        elif t == "run.started":
            run_id = event.get("run_id", "")
            goal = event.get("goal", "")
            self._append(Static(
                f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{escape(_preview(goal, 96))}[/dim]",
                classes="run-header",
            ))

        elif t == "skill.invoked":
            skill_name = str(event.get("skill_name", ""))
            arguments = str(event.get("arguments", ""))
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{escape(args_preview)}[/dim]" if args_preview else ""
            self._append(Static(
                f"[bold cyan]/{escape(skill_name)}[/bold cyan]{args_part}",
                classes="log-line",
            ))

        elif t == "subagent.started":
            run_id = str(event.get("run_id", ""))
            description = str(event.get("description", ""))
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            desc_part = escape(_preview(description, 72))
            self._append(Static(
                f"[dim]┌─[/dim] [cyan]{desc_part}[/cyan]  [dim]{short_id}[/dim]",
                classes="log-line",
            ))

        elif t == "subagent.finished":
            run_id = str(event.get("run_id", ""))
            status = event.get("status", "")
            description = self._subagent_run_ids.pop(run_id, str(event.get("description", "")))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{escape(_preview(description, 72))}[/cyan]{elapsed}"
            if status == "success":
                self._append(Static(
                    f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                    classes="log-line",
                ))
            else:
                self._append(Static(
                    f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                    classes="log-line",
                ))

        elif t == "step.started":
            run_id = str(event.get("run_id", ""))
            if run_id in self._subagent_run_ids:
                return
            step = event.get("step", "")
            self._append(Static(
                f"[dim]step {step}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = str(event.get("run_id", ""))
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_msg = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._append(Static(
                    f"[bold green]✓ completed[/bold green]  [dim]{steps} steps[/dim]",
                    classes="run-ok",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))

        elif t == "llm.usage":
            run_id = str(event.get("run_id", ""))
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            ctx_bar = self._render_ctx_bar(pct)

            self._append(Static(
                f"[dim]  tokens  "
                f"in={event.get('input_tokens')} "
                f"out={event.get('output_tokens')} "
                f"cache={event.get('cache_read_input_tokens')}[/dim]"
                f"  {ctx_bar}",
                classes="usage",
            ))

        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            self._last_context_pct = 0.0

            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]original≈{orig} tokens → summary={summary} tokens[/dim]",
                classes="log-line",
            ))

        elif t == "permission.requested":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"

            logger.info(
                "permission.requested tool=%s id=%s  app.focused=%s",
                tool_name, tool_use_id, _focused_repr,
            )
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview)
            self._pending_permission_blocks[tool_use_id] = perm_block
            prompt = self._prompt()

            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"

            self._append(perm_block)
            select = PermissionSelect(tool_use_id)
            self._mount_permission_select(select)
            logger.debug("PermissionSelect mounted before #prompt  pending=%d", len(self._pending_permission_blocks))

        elif t == "permission.denied":
            # 处理超时或断连等非用户交互触发的 deny（用户主动 deny 已由 on_permission_select_decided 处理）
            tool_use_id = str(event.get("tool_use_id", ""))
            decision = str(event.get("decision", "denied"))
            if tool_use_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(tool_use_id, None)
                if perm_block is not None:
                    perm_block._resolve(decision)
    
                try:
                    select = self.query_one(PermissionSelect)
                    select.remove()
                except Exception:
                    pass

                if not self._pending_permission_blocks:
                    p = self._prompt()
                    if p is not None:
                        p.disabled = False
                        p.read_only = False
                        p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                        p.focus()

        elif t == "logger.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(Static(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{escape(str(event.get('source', '')))}[/dim]  "
                f"{escape(str(event.get('message', '')))}",
                classes="log-line",
            ))


# TUI 入口：读取配置并启动 MiniClaudeTuiApp
def run(config: MiniClaudeConfig, replay_run_id: str | None = None) -> None:
    app = MiniClaudeTuiApp(config.host, config.port, replay_run_id=replay_run_id)
    app.run()