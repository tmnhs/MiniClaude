# 配置

## 优先级

从高到低：

1. 系统环境变量
2. `.env`（项目根 `.env` → `~/.mc/.env`，不覆盖已存在的变量）
3. 配置文件（`~/.mc/config.toml` → `./.mc/config.toml`；设置 `MINICLAUDE_CONFIG` 时只读该文件）
4. 内置默认值

> 单 daemon 模式下，daemon 只读取全局 `~/.mc/config.toml` 与 `~/.mc/.env`，项目级 `config.toml` 不按客户端目录生效。

模板文件：[`.env.example`](../.env.example)、[`config.toml.example`](../config.toml.example)。

## 必填凭据

| 变量 | 说明 |
|------|------|
| `ANTHROPIC_API_KEY` | API Key |
| `ANTHROPIC_BASE_URL` | API 端点，支持兼容 Anthropic 协议的服务 |

缺失时 daemon 启动立即报错。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MINICLAUDE_HOST` / `MINICLAUDE_PORT` | `127.0.0.1` / `10721` | daemon 监听地址 |
| `MINICLAUDE_CONFIG` | — | 指定唯一配置文件路径 |
| `MINICLAUDE_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `MINICLAUDE_LOG_FILE` | `~/.mc/logs/core.log` | daemon 日志；空串表示仅 stderr |
| `MINICLAUDE_LOG_FORMAT` | `text` | `text` / `json` |
| `MINICLAUDE_TUI_LOG_FILE` | `~/.mc/logs/tui.log` | TUI 日志（只写文件，不干扰界面） |
| `MINICLAUDE_MAX_STEPS` | `20` | 单次 run 最大 agent 步数（正整数） |
| `MINICLAUDE_LLM_DEFAULT_MODEL` | `claude-sonnet-4-6` | 默认模型 |
| `MINICLAUDE_PERMISSION_TIMEOUT_S` | `60` | 审批超时秒数，`0` 表示不超时 |
| `MINICLAUDE_COMPACT_THRESHOLD` | `0.0` | 自动压缩阈值（0~1），`0` 禁用 |
| `MINICLAUDE_COMPACT_TOOL_LIMIT` | `8000` | 单个 tool_result 截断字符数 |
| `MINICLAUDE_COMPACT_TOOL_KEEP` | `4000` | 截断后保留前缀字符数 |
| `MINICLAUDE_TRACE_ENABLED` | `false` | 是否启用 trace |
| `MINICLAUDE_TRACE_FILE` | `~/.mc/logs/trace.jsonl` | trace 输出路径 |
| `MINICLAUDE_TRACE_INCLUDE_LLM_PAYLOAD` | `false` | 是否记录完整 LLM 请求/响应（可能含敏感内容） |
| `MINICLAUDE_SANDBOX_MODE` | `workspace` | `off` / `workspace` / `strict` |

## config.toml

小节：`[core]` `[logging]` `[agent]` `[llm]` `[trace]` `[permission]` `[compaction]` `[sandbox]`，以及可选的 `[[mcp.servers]]`。未知小节或未知键会直接报错退出。

要点（完整注释见 [`config.toml.example`](../config.toml.example)）：

- `[agent] max_steps`：单次 run 步数上限。
- `[llm] default_model` / `router`：默认模型；路由策略 `static` / `rule_based` / `cost_budget`。
- `[compaction] auto_threshold`：context 使用率超过阈值时自动压缩，`0` 禁用（推荐手动 `/compact`）。
- `[permission] timeout_s`：权限审批超时，`0` 表示不超时。

### 沙箱

```toml
[sandbox]
mode = "workspace"
allow_read = ["~/.gitconfig"]
allow_write = ["~/.cache/uv"]
```

| 模式 | 行为 |
|------|------|
| `off` | 不隔离，仅权限审批 |
| `workspace` | 写操作限定在工作区 |
| `strict` | 在 `workspace` 基础上禁止网络 |

`allow_read` / `allow_write` 用于放行工作区之外的例外路径（支持 `~` 展开）。Linux 需要安装 `bwrap`。

### MCP 服务器

```toml
[[mcp.servers]]
name = "filesystem"
transport = "stdio"          # 默认 stdio
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
env = { SOME_TOKEN = "xxx" }

[[mcp.servers]]
name = "remote"
transport = "tcp"
host = "localhost"
port = 3000
```

`stdio` 需要 `command`；`tcp` 需要 `host` / `port`。

## 日志说明

daemon 同时配置 stderr 与文件日志时：

- 前台交互（stderr 是终端）会同时输出 stderr 和 `core.log`；
- 后台拉起（stderr 被重定向）只写 `core.log`，`core.stderr.log` 仅保留日志初始化前的崩溃信息。

TUI 日志始终只写 `~/.mc/logs/tui.log`。
