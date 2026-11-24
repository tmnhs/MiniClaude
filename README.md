# MiniClaude

本地 AI Agent 系统。`mc-core` 作为常驻守护进程处理所有任务，`mc`（CLI）和 `mc-tui`（TUI）通过 TCP loopback 与之通信。

## 环境要求

| 依赖 | 版本 |
|------|------|
| 操作系统 | macOS / Linux |
| Python | 3.12.x |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.4 |

安装 uv（若尚未安装）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Python 3.12 由 uv 自动管理，无需手动安装。

## 快速开始

```bash
git clone <repo> && cd MiniClaude
uv sync
cp .env.example .env        # 按需修改

uv run mc-core &          # 启动守护进程（后台）
uv run mc ping            # 验证连通：应返回 pong
uv run mc --version       # 输出当前版本号，如 MiniClaude CLI 0.1.4
```

