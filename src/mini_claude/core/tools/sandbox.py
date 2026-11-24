from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from mini_claude.core.conf.model import SandboxConfig

_DARWIN_BACKEND = "sandbox-exec"
_LINUX_BACKEND = "bwrap"


class SandboxUnavailableError(RuntimeError):
    """当前平台没有可用的沙箱后端，或后端未安装。"""


# 将配置中的额外路径（支持 ~）解析为绝对路径；相对路径按工作区解析
def _resolve_extra(raw: str, workspace: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


# 转义 SBPL 字符串，避免路径中的引号或反斜杠破坏 profile
def _sbpl_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# 生成 SBPL 的 (subpath "...") 过滤器
def _subpath(path: str | Path) -> str:
    return f'(subpath "{_sbpl_escape(str(path))}")'


# 需要默认禁止读取的家目录路径：当前用户家目录 + macOS 的 /Users 根
def _denied_read_roots() -> list[Path]:
    roots = {Path.home().resolve()}
    users = Path("/Users")
    if users.is_dir():
        roots.add(users.resolve())
    return sorted(roots)


# 生成允许路径的祖先目录元数据读规则。
def _ancestor_read_rules(paths: list[str]) -> list[str]:
    rules: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        for parent in Path(raw).parents:
            key = str(parent)
            if key in seen:
                continue
            seen.add(key)
            rules.append(f'(allow file-read-metadata (literal "{_sbpl_escape(key)}"))')
    return rules


# 允许写入的系统临时目录：TMPDIR、/tmp、/var/tmp。
def _temp_write_roots() -> list[Path]:
    candidates = {Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")}
    return sorted({p.resolve() for p in candidates if p.exists()})


# 构造 macOS sandbox-exec 的 SBPL profile。
# 采用「allow default + 定向 deny」：SBPL 中后匹配规则优先，
# 因此先禁止家目录读取/全局写入，再放行工作区、配置的额外路径与系统临时目录。
# 放行路径必须同时放行其祖先目录的元数据读权限，否则 realpath/canonicalize
# 在祖先被 deny 时会返回 EPERM（见 _ancestor_read_rules）。
# 注意：路径必须是 resolve() 后的真实路径（如 /tmp → /private/tmp），否则规则不生效。
def _darwin_profile(workspace: Path, cfg: SandboxConfig) -> str:
    readable = [str(workspace)] + [
        str(_resolve_extra(p, workspace)) for p in cfg.allow_read
    ]
    writable = [str(workspace)] + [
        str(_resolve_extra(p, workspace)) for p in cfg.allow_write
    ]

    rules = ["(version 1)", "(allow default)"]
    for root in _denied_read_roots():
        rules.append(f"(deny file-read* {_subpath(root)})")
    rules.extend(f"(allow file-read* {_subpath(p)})" for p in readable)
    rules.extend(_ancestor_read_rules(readable + writable))

    rules.append('(deny file-write* (subpath "/"))')
    rules.extend(f"(allow file-write* {_subpath(p)})" for p in writable)
    # 仅配置 allow_write 的路径也需要能 stat 自身，否则工具的存在性检查
    # 会因 EPERM 误判为不存在，随后 mkdir 得到 EEXIST（uv 缓存即如此）
    rules.extend(
        f'(allow file-read-metadata (literal "{_sbpl_escape(p)}"))' for p in writable
    )
    rules.extend(
        f"(allow file-write* {_subpath(p)})" for p in _temp_write_roots()
    )
    rules.append(
        '(allow file-write* (literal "/dev/null") (literal "/dev/stdout") '
        '(literal "/dev/stderr") (literal "/dev/tty"))'
    )

    if cfg.mode == "strict":
        rules.append("(deny network*)")

    return "\n".join(rules)


# 构造 Linux bubblewrap 的 argv。
# 以只读根目录 + 可写工作区实现写隔离；系统临时目录额外可写（uv 等依赖 TMPDIR）；
# strict 追加 --unshare-net 禁用网络。
# 注意：Linux 端读取范围仍为全盘（ro-bind /），与 macOS 的家目录读隔离存在平台差异。
def _linux_argv(command: str, workspace: Path, cfg: SandboxConfig) -> list[str]:
    argv = [
        _LINUX_BACKEND,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--ro-bind", "/", "/",
        "--bind", str(workspace), str(workspace),
    ]
    for raw in cfg.allow_write:
        extra = str(_resolve_extra(raw, workspace))
        argv.extend(["--bind", extra, extra])
    for tmp in _temp_write_roots():
        argv.extend(["--bind", str(tmp), str(tmp)])
    argv.extend(["--dev", "/dev", "--proc", "/proc", "--chdir", str(workspace)])
    if cfg.mode == "strict":
        argv.append("--unshare-net")
    argv.extend(["--", "/bin/sh", "-c", command])
    return argv


# 返回当前平台可用的后端名；不可用时返回 None
def backend_name() -> str | None:
    if sys.platform == "darwin":
        return _DARWIN_BACKEND if shutil.which(_DARWIN_BACKEND) else None
    if sys.platform.startswith("linux"):
        return _LINUX_BACKEND if shutil.which(_LINUX_BACKEND) else None
    return None


# 要求当前平台存在可用后端，否则 fail-closed 抛错并给出安装提示
def _require_backend() -> str:
    name = backend_name()
    if name is not None:
        return name
    if sys.platform == "darwin":
        raise SandboxUnavailableError("macOS sandbox backend 'sandbox-exec' not found")
    if sys.platform.startswith("linux"):
        raise SandboxUnavailableError(
            "Linux sandbox backend 'bwrap' (bubblewrap) not found; "
            "install bubblewrap or set sandbox.mode = \"off\""
        )
    raise SandboxUnavailableError(
        f"no sandbox backend available on platform {sys.platform!r}"
    )


# 根据模式与平台构造最终执行 argv。
# off 直接沿用 /bin/sh -c；workspace/strict 在 macOS 走 sandbox-exec，在 Linux 走 bwrap。
def build_argv(command: str, workspace: Path, cfg: SandboxConfig) -> list[str]:
    if cfg.mode == "off":
        return ["/bin/sh", "-c", command]
    if cfg.mode not in ("workspace", "strict"):
        raise ValueError(f"unknown sandbox mode: {cfg.mode!r}")

    workspace = workspace.resolve()
    backend = _require_backend()
    if backend == _DARWIN_BACKEND:
        return [
            _DARWIN_BACKEND,
            "-p",
            _darwin_profile(workspace, cfg),
            "/bin/sh",
            "-c",
            command,
        ]
    return _linux_argv(command, workspace, cfg)
