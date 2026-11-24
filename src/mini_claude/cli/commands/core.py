from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from mini_claude.core.conf.config import MiniClaudeConfig

_PID_FILE = Path.home() / ".mc" / "mc-core.pid"
_STDERR_LOG = Path.home() / ".mc" / "logs" / "core.stderr.log"

# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: MiniClaudeConfig) -> None:
    _r, w = await asyncio.open_connection(config.host, config.port)
    w.close()
    await w.wait_closed()

# 轮询等待 daemon 端口就绪，超时返回 False
async def _wait_until_ready(config: MiniClaudeConfig, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            await _ping_check(config)
            return True
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.05)
    return False

# 校验 pid 对应的进程确实是 mc-core daemon，避免重启后 PID 复用导致误杀无关进程
def _is_daemon_process(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-ww", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "mini_claude.core" in out.stdout

# 读取 PID 文件并确认进程存活且是 daemon，进程已消失或 PID 被复用则删除文件并返回 None
def _running_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError):
        _PID_FILE.unlink(missing_ok=True)
        return None

    if not _is_daemon_process(pid):
        _PID_FILE.unlink(missing_ok=True)
        return None

    return pid

# 读取文件末尾若干行，用于回显 daemon 启动错误
def _tail(path: Path, lines: int = 12) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:]).strip()

# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: MiniClaudeConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")

# 确保 daemon 在运行：已连通则直接返回，否则后台拉起并等待端口就绪。
# 供 TUI / CLI 在做任何请求前调用，实现“没有就自动启动”。
def ensure_daemon(config: MiniClaudeConfig, timeout: float = 5.0) -> None:
    try:
        asyncio.run(_ping_check(config))
        return
    except (ConnectionRefusedError, OSError):
        pass

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STDERR_LOG.parent.mkdir(parents=True, exist_ok=True)
    # 以 home 为 cwd 启动：daemon 配置只依赖全局 ~/.mc，不受首个客户端目录的 .env/config 影响
    with open(_STDERR_LOG, "w") as stderr_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mini_claude.core"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            cwd=Path.home(),
        )
    _PID_FILE.write_text(str(proc.pid))

    if not asyncio.run(_wait_until_ready(config, timeout)):
        _PID_FILE.unlink(missing_ok=True)
        detail = _tail(_STDERR_LOG)
        message = f"failed to start core daemon ({config.host}:{config.port})"
        if detail:
            message += f"\ndaemon stderr:\n{detail}"
        else:
            message += "; check ~/.mc/logs or run `mc core status`"
        raise SystemExit(message)


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: MiniClaudeConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"already running  ({config.host}:{config.port})")
        return
    except (ConnectionRefusedError, OSError):
        pass

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STDERR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_STDERR_LOG, "w") as stderr_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mini_claude.core"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            cwd=Path.home(),
        )
    _PID_FILE.write_text(str(proc.pid))

    # 等待端口就绪，避免“启动成功”假象；失败时回显 daemon stderr
    if not asyncio.run(_wait_until_ready(config)):
        _PID_FILE.unlink(missing_ok=True)
        print(f"failed to start  pid={proc.pid}", file=sys.stderr)
        detail = _tail(_STDERR_LOG)
        if detail:
            print(f"{detail}", file=sys.stderr)
        sys.exit(1)

    print(f"started  pid={proc.pid}  ({config.host}:{config.port})")


# 向 daemon 发送 SIGTERM 停止进程，若未运行则提示
def cmd_core_stop(config: MiniClaudeConfig) -> None:
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    os.kill(pid, signal.SIGTERM)
    _PID_FILE.unlink(missing_ok=True)
    print(f"stopped  pid={pid}")


# 先停止运行中的 daemon 并等待其退出，再重新启动；未运行时直接启动
def cmd_core_restart(config: MiniClaudeConfig) -> None:
    pid = _running_pid()
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
        _PID_FILE.unlink(missing_ok=True)

        # 等待旧进程真正退出，避免新 daemon 与旧进程争抢端口
        deadline = time.monotonic() + 5.0
        while _is_daemon_process(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _is_daemon_process(pid):
            print(f"failed to stop  pid={pid}", file=sys.stderr)
            sys.exit(1)
        print(f"stopped  pid={pid}")

    cmd_core_start(config)
