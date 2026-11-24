# 开发

## 环境

```bash
uv sync            # 安装运行与开发依赖（Python 3.12）
```

## 常用命令

```bash
make lint              # ruff + mypy
make test              # 单元测试
make integration-test  # 集成测试（需要真实 ANTHROPIC_API_KEY）
make docs              # 重新生成 WIRE_PROTOCOL.md
make verify-s0         # lint + 类型 + 单测 + ping 集成 + 协议文档一致性校验
```

单项执行：

```bash
uv run ruff check src tests scripts
uv run mypy src
uv run pytest tests/unit -v
uv run pytest tests/integration -v
```

## 线协议

客户端与 daemon 使用 TCP loopback + 每行一条 JSON（NDJSON）的 JSON-RPC 风格协议。说明见仓库根目录 [WIRE_PROTOCOL.md](../WIRE_PROTOCOL.md)，由 `scripts/gen_protocol_doc.py` 生成，请勿手改；修改协议后执行 `make docs` 并提交。

## 目录结构

```
src/mini_claude/
├── cli/          # mc 命令行入口与子命令（ping/chat/run/core/trace）
├── tui/          # mc-tui（Textual 界面）
└── core/         # mc-core daemon
    ├── app.py    # 服务装配与命令分发
    ├── runner.py # agent 主循环
    ├── bus/      # JSON-RPC 命令与事件定义
    ├── session/  # 会话管理、持久化（meta/thread/runs/notes）
    ├── tools/    # 工具注册、内置工具、沙箱、进程控制
    ├── permissions/
    ├── skills/   # Markdown 技能（builtin/ 为内置）
    ├── agents/   # TOML 角色配置（builtin/ 为内置）
    ├── subagent/ # 子 agent 派生与后台任务
    ├── compact/  # 上下文压缩
    ├── context.py# system prompt 组装
    ├── mcp/      # MCP 客户端
    ├── trace/    # 追踪
    ├── transport/# TCP NDJSON 服务端/客户端
    └── conf/     # 配置加载与模型
tests/
├── unit/         # 单元测试（默认运行）
└── integration/  # 集成测试（标记 integration）
scripts/
├── install.sh    # 一键安装
└── gen_protocol_doc.py
```

## 测试约定

- 单元测试不依赖真实 daemon 或外部 API。
- 集成测试标记为 `integration`，缺 `ANTHROPIC_API_KEY` 时自动跳过。
- 异步用例由 `pytest-asyncio`（`asyncio_mode = "auto"`）管理。
