#!/usr/bin/env bash
# MiniClaude 一键安装脚本（macOS / Linux）
#
# 在仓库根目录或任意位置执行：
#   ./scripts/install.sh
#   bash scripts/install.sh
#
# 行为：
#   1. 检查操作系统（仅 macOS / Linux）
#   2. 缺失时自动安装 uv（https://docs.astral.sh/uv/）
#   3. 以隔离的工具环境安装当前仓库，生成 mc / mc-core / mc-tui
#   4. 首次运行时为 ~/.mc 生成 .env 与 config.toml（缺省模板）
#   5. 配置 PATH（写入 shell rc）并校验三个命令
#
# 选项：
#   -e, --editable      以可编辑模式安装（源码改动即时生效，默认）
#       --no-editable   以普通模式安装（固定当前代码快照）
#       --no-config     跳过 ~/.mc 配置模板生成
#   -h, --help          显示帮助

set -euo pipefail

# 记录安装前的 PATH，用于判断是否需要提示/写入 shell 配置（uv 自身的 warning 不算数）
INITIAL_PATH="${PATH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
EDITABLE=1
SETUP_CONFIG=1

usage() {
    awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} /^$/{print ""; next} {exit}' \
        "${BASH_SOURCE[0]:-$0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -e|--editable)   EDITABLE=1 ;;
        --no-editable)   EDITABLE=0 ;;
        --no-config)     SETUP_CONFIG=0 ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

log() { printf '%s\n' "$*"; }
die() { printf '错误: %s\n' "$*" >&2; exit 1; }

# ── 操作系统检查 ──────────────────────────────────────────────
case "$(uname -s)" in
    Darwin|Linux) ;;
    *) die "仅支持 macOS / Linux，当前系统: $(uname -s)" ;;
esac

[ -f "${REPO_DIR}/pyproject.toml" ] || die "未找到 pyproject.toml，请确认脚本位于仓库 scripts/ 目录"

# ── 安装 uv ──────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || die "未找到 curl，无法安装 uv"
    log "未检测到 uv，正在安装..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

command -v uv >/dev/null 2>&1 || die "uv 安装失败，请手动安装后重试：https://docs.astral.sh/uv/"
log "使用 uv $(uv --version 2>/dev/null | awk '{print $2}')"

# ── 安装 MiniClaude ─────────────────────────────────────────
if [ "${EDITABLE}" -eq 1 ]; then
    log "安装 mc / mc-core / mc-tui（editable）..."
    uv tool install --editable "${REPO_DIR}" --force --python 3.12
else
    log "安装 mc / mc-core / mc-tui..."
    uv tool install "${REPO_DIR}" --force --python 3.12
fi

BIN_DIR="$(uv tool dir --bin)"

# ── 生成全局配置模板（已存在则不覆盖）────────────────────────
if [ "${SETUP_CONFIG}" -eq 1 ]; then
    mkdir -p "${HOME}/.mc"
    if [ ! -f "${HOME}/.mc/.env" ] && [ -f "${REPO_DIR}/.env.example" ]; then
        cp "${REPO_DIR}/.env.example" "${HOME}/.mc/.env"
        log "已生成 ${HOME}/.mc/.env（请填写 ANTHROPIC_API_KEY 与 ANTHROPIC_BASE_URL）"
    fi
    if [ ! -f "${HOME}/.mc/config.toml" ] && [ -f "${REPO_DIR}/config.toml.example" ]; then
        cp "${REPO_DIR}/config.toml.example" "${HOME}/.mc/config.toml"
        log "已生成 ${HOME}/.mc/config.toml"
    fi
fi

# ── PATH 配置 ───────────────────────────────────────────────
# 子进程无法修改父 shell 的 PATH，因此：写入 shell rc（持久生效）+ 提示当前终端如何立即生效
path_has() {
    case ":$1:" in
        *":$2:"*) return 0 ;;
        *) return 1 ;;
    esac
}

if ! path_has "${INITIAL_PATH}" "${BIN_DIR}"; then
    case "$(basename "${SHELL:-}")" in
        zsh)  RC_FILE="${HOME}/.zshrc" ;;
        bash) RC_FILE="${HOME}/.bashrc" ;;
        *)    RC_FILE="" ;;
    esac

    MARKER="# >>> MiniClaude PATH >>>"
    if [ -n "${RC_FILE}" ]; then
        if ! grep -qF "${MARKER}" "${RC_FILE}" 2>/dev/null; then
            {
                printf '\n%s\n' "${MARKER}"
                printf 'export PATH="%s:$PATH"\n' "${BIN_DIR}"
                printf '# <<< MiniClaude PATH <<<\n'
            } >> "${RC_FILE}"
            log "已将 PATH 配置写入 ${RC_FILE}"
        else
            log "PATH 配置已存在于 ${RC_FILE}"
        fi
        source ${RC_FILE}
    else
        log "警告: ${BIN_DIR} 不在 PATH 中，请手动加入 shell 配置："
        log "  export PATH=\"${BIN_DIR}:\$PATH\""
    fi
fi

# ── 校验 ────────────────────────────────────────────────────
for cmd in mc mc-core mc-tui; do
    [ -x "${BIN_DIR}/${cmd}" ] || die "未找到可执行文件: ${BIN_DIR}/${cmd}"
done
"${BIN_DIR}/mc" --version

log ""
log "安装完成。下一步："
log "  1. 编辑 ~/.mc/.env 填写 ANTHROPIC_API_KEY 与 ANTHROPIC_BASE_URL（均为必填）"
log "  2. 在任意项目目录运行 mc-tui（daemon 会自动拉起）"
