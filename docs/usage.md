# 使用指南

## 工作方式

- `mc-core` 是单实例常驻 daemon，多个 CLI/TUI 客户端共享同一进程。
- `mc-tui`、`mc chat`、`mc run` 在连接前会检查 daemon，未运行则自动后台拉起。
- 客户端启动时会把当前目录作为 workspace 传给会话；文件/命令工具、沙箱与权限审批均以该目录为根。
- 会话记录在 `~/.mc/sessions/<id>/`，可从任意目录恢复。

## TUI（mc-tui）

```bash
cd /path/to/project
mc-tui
```

| 操作 | 说明 |
|------|------|
| 输入内容 + `Enter` | 发送消息，agent 开始执行 |
| `↑` / `↓` | 首行按 `↑` 回溯历史输入，末行按 `↓` 前进；越过最新一条恢复未发送的草稿 |
| `Esc` | 中断正在执行的 run（等同发送 `run.cancel`） |
| `Ctrl+Q` | 退出（并关闭 session） |
| `⌘+C` / `super+c` | 复制最后一条完整回复（macOS 同时写入 `pbcopy`） |
| `/compact` | 手动压缩当前会话上下文 |

日志随输出自动滚到底部；若手动上翻查看历史，新输出不会强行抢滚动位置，底部提示 `有新输出 ↓`，滚回底部即恢复跟随。

退出时终端会打印可恢复的 session id。

### 恢复与回放

```bash
mc-tui --resume sess-xxxxxxxxxxxx   # 恢复会话：回放历史并继续对话
mc-tui --replay <run_id>            # 仅回放某次 run 的事件
```

## CLI

### 一次性任务

```bash
mc run --goal "在当前项目运行测试并总结失败原因"
```

### 多轮对话

```bash
mc chat
```

流式输出。工具执行前会请求权限，输入：

| 按键 | 决策 |
|------|------|
| `y` | 本次允许 |
| `a` | 永久允许（高风险命令不提供） |
| `n` | 本次拒绝 |
| `d` | 永久拒绝 |

审批超过 `permission.timeout_s` 秒（默认 60，`0` 表示不超时）后视为拒绝。

### daemon 管理

```bash
mc core start      # 后台启动（已在运行则提示）
mc core stop       # 停止
mc core restart    # 重启
mc core status     # 查看是否运行
mc ping            # 健康检查，返回 pong
```

### Trace

启用 `MINICLAUDE_TRACE_ENABLED=true` 或配置 `[trace] enabled = true` 后，事件写入 `~/.mc/logs/trace.jsonl`。

```bash
mc trace                       # 查看全部
mc trace <run_id>              # 按 run 过滤
mc trace --layer llm           # ipc | event | llm
mc trace --direction "CORE→LLM"  # 按方向过滤
mc trace --follow              # 实时跟随
mc trace --raw                 # 输出原始 NDJSON
```

## 日志与数据目录

| 路径 | 内容 |
|------|------|
| `~/.mc/sessions/<id>/` | `meta.json`、`thread.jsonl`、`runs/` |
| `~/.mc/logs/core.log` | daemon 日志（滚动） |
| `~/.mc/logs/tui.log` | TUI 日志 |
| `~/.mc/logs/trace.jsonl` | trace 记录 |
| `~/.mc/logs/core.stderr.log` | daemon 启动期的崩溃输出（正常运行时很小） |
| `~/.mc/mc-core.pid` | daemon PID 文件 |

日志重复或缺失时先检查 [configuration.md](configuration.md) 中的日志配置。
