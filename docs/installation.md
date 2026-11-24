# 安装

## 环境要求

| 依赖 | 版本 / 说明 |
|------|------|
| 操作系统 | macOS / Linux |
| Python | 3.12.x（由 uv 自动管理） |
| [uv](https://docs.astral.sh/uv/) | 任意较新版本 |
| 沙箱后端 | macOS 自带 `sandbox-exec`；Linux 需 `bwrap`（bubblewrap，`mode="off"` 可跳过） |

uv 缺失时安装脚本会自动安装；也可手动：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 一键安装（推荐）

```bash
git clone <repo> && cd MiniClaude
make install          # 等价于 ./scripts/install.sh
```

脚本会：

1. 检查操作系统（仅 macOS / Linux）；
2. 缺少时自动安装 uv；
3. 以隔离工具环境安装 `mc` / `mc-core` / `mc-tui`；
4. 首次运行时复制 `.env.example`、`config.toml.example` 到 `~/.mc`；
5. 把 bin 目录写入 shell rc（zsh → `~/.zshrc`，bash → `~/.bashrc`）。

常用选项：

```bash
./scripts/install.sh --editable     # 默认：源码改动即时生效
./scripts/install.sh --no-editable  # 固定当前代码快照
./scripts/install.sh --no-config    # 跳过 ~/.mc 配置模板生成
./scripts/install.sh -h             # 查看帮助
```

安装完成后若当前终端仍提示找不到命令，执行 `source ~/.zshrc`（或 `~/.bashrc`），或新开终端。

## 手动安装

```bash
uv tool install --editable .        # 去掉 --editable 则安装固定版本
export PATH="$(uv tool dir --bin):$PATH"
mc --version
```

## 首次配置

编辑 `~/.mc/.env`，`ANTHROPIC_API_KEY` 与 `ANTHROPIC_BASE_URL` 为必填：

```bash
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"   # 或 https://api.anthropic.com
```

其余可调项见 [configuration.md](configuration.md)。

## 升级与卸载

```bash
# 升级（editable 安装无需重装，源码拉取后即生效；普通安装需重新执行）
./scripts/install.sh

# 卸载
uv tool uninstall miniclaude
```

配置与会话数据位于 `~/.mc`，卸载不会删除。
