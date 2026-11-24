from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import bashlex  # type: ignore[import-untyped]

RiskLevel = Literal["low", "medium", "high"]

_RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# bash 工具中识别风险等级和缓存粒度（pattern）的分析结果
@dataclass(frozen=True)
class Analysis:
    risk: RiskLevel = "low"
    pattern: str = "*"
    affected_paths: tuple[str, ...] = ()
    outside: bool = False
    force_ask: bool = False

# 需要按子命令区分 pattern 的程序（git status / npm test …）
_SUBCOMMAND_PROGRAMS: frozenset[str] = frozenset(
    {
        "git", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go",
        "docker", "kubectl", "helm", "brew", "poetry", "deno", "bun",
    }
)

# 这些旗标会占用下一个参数，用于跳过子命令探测中的参数值
_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-C", "-c", "-m", "-f", "-o", "-t", "-p", "-e", "--directory", "--file"}
)

# `--flag=value` 形式中，value 视为路径的常见旗标
_PATH_OPTION_FLAGS: frozenset[str] = frozenset(
    {
        "-C", "--directory", "--file", "-f", "--output", "-o", "--prefix",
        "--target-directory", "-t", "--root", "--path", "--config", "--cache",
    }
)

# 解释器内联代码：无法静态分析，直接视为高风险
_INTERPRETERS: frozenset[str] = frozenset(
    {"python", "python2", "python3", "perl", "ruby", "node", "nodejs", "php", "lua"}
)
_SHELLS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_INLINE_FLAGS: dict[str, str] = {
    "python": "-c", "python2": "-c", "python3": "-c",
    "perl": "-e", "ruby": "-e", "node": "-e", "nodejs": "-e",
    "php": "-r", "lua": "-e",
}

# 写操作命令：参数指向工作区外时视为高风险，工作区内视为中风险
_WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "rm", "mv", "cp", "touch", "mkdir", "rmdir", "ln", "truncate",
        "tee", "install", "patch", "dd", "chmod", "chown", "chgrp",
        "sed", "perl", "rsync", "unlink", "shred",
    }
)

# 无条件视为高风险的程序
_ALWAYS_HIGH: frozenset[str] = frozenset(
    {
        "sudo", "su", "doas", "xargs", "dd", "mkfs", "fdisk", "shutdown",
        "reboot", "halt", "poweroff", "kill", "pkill", "killall", "shred",
    }
)

# 敏感目录 / 文件片段：命中即视为高风险写入
_SENSITIVE_PARTS: frozenset[str] = frozenset(
    {".git", ".ssh", ".gnupg", ".aws", ".kube"}
)
_SENSITIVE_NAMES: frozenset[str] = frozenset(
    {"id_rsa", "id_ed25519", "authorized_keys", "credentials", ".env"}
)

# 无需展示、也不应触发越界判定的设备节点（如 2>/dev/null）
_IGNORED_PATHS: frozenset[str] = frozenset(
    {
        "/dev/null", "/dev/zero", "/dev/stdin", "/dev/stdout",
        "/dev/stderr", "/dev/tty", "/dev/random", "/dev/urandom",
    }
)

# 判断路径是否为可忽略的设备节点
def _is_ignored_path(raw: str) -> bool:
    text = raw.rstrip(".,")
    return text in _IGNORED_PATHS or text.startswith("/dev/fd/")

# 绝对路径：排除 URL、`--flag=/x` 与 `./x` / `../x` 中的斜杠
_ABS_PATH_RE = re.compile(r"(?<![\w.:=~/}])/[^\s'\";|&()<>]+")
# 家目录路径：~ 或 ~user
_TILDE_RE = re.compile(r"(?<![\w])~[^\s'\";|&()<>]*")
# 上级目录穿越
_PARENT_RE = re.compile(r"(?<![\w./])\.\.(?:/[^\s'\";|&()<>]*)?")
# $HOME / ${HOME} / $PWD
_ENV_HOME_RE = re.compile(r"\$\{?(?:HOME|PWD)\b")
# 命令替换
_SUBST_RE = re.compile(r"\$\(|`")

# 判断某个文本 token 是否像工作区外的路径（用于子命令探测跳过路径参数）
def _looks_like_path(text: str) -> bool:
    return text.startswith(("/", "~", "./", "../")) or "/" in text

# 取程序名；"." / ".." 的 Path.name 为空串，保留原始 token
def _program_name(arg: str) -> str:
    return Path(arg).name or arg

# 合并两个风险等级，取较高者
def _max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if _RISK_ORDER[a] >= _RISK_ORDER[b] else b

# 解析单个路径 token；相对路径按工作区解析
def _resolve_path(raw: str, workspace: Path) -> Path:
    try:
        path = Path(raw).expanduser()
    except RuntimeError:
        # HOME 缺失或 ~user 不存在时 expanduser 会抛错：按工作区外的路径保守处理
        path = Path("/") / raw.lstrip("~/")
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()

# 判断路径是否位于敏感目录或文件名
def _is_sensitive(path: Path) -> bool:
    if any(part in _SENSITIVE_PARTS for part in path.parts):
        return True
    return any(name in _SENSITIVE_NAMES for name in path.parts[-1:])

# 遍历 bashlex 节点的子节点（parts / list / redirects）
def _children(node: Any) -> list[Any]:
    out: list[Any] = []
    for attr in ("parts", "list", "redirects"):
        value = getattr(node, attr, None)
        if isinstance(value, list):
            out.extend(value)
    return out

# bash 命令 AST 分析器：收集路径、判定风险等级、计算缓存 pattern
class _Walker:
    def __init__(self, command: str, workspace: Path, depth: int = 0) -> None:
        self.command = command
        self.workspace = workspace
        self.depth = depth
        self.risk: RiskLevel = "low"
        self.outside = False
        self.write_outside = False
        self.sensitive_write = False
        self.opaque = False
        self.failed = False
        self.paths: list[str] = []
        self.commands: list[list[str]] = []

    # 提升风险等级
    def _escalate(self, risk: RiskLevel) -> None:
        self.risk = _max_risk(self.risk, risk)

    # 记录一个受影响的路径，并按需判定越界 / 敏感写入
    def _record_path(self, raw: str, *, writing: bool) -> None:
        if not raw:
            return
        resolved = _resolve_path(raw, self.workspace)
        text = str(resolved)
        if _is_ignored_path(raw) or _is_ignored_path(text):
            return

        if text not in self.paths:
            self.paths.append(text)

        if not resolved.is_relative_to(self.workspace):
            self.outside = True
            self._escalate("medium")
            if writing:
                self.write_outside = True
        if writing and _is_sensitive(resolved):
            self.sensitive_write = True

    # 从任意文本片段中提取路径 token 并记录
    def _scan_text(self, text: str, *, writing: bool) -> None:
        for pattern in (_ABS_PATH_RE, _TILDE_RE, _PARENT_RE):
            for match in pattern.finditer(text):
                self._record_path(match.group(0), writing=writing)

    # 递归遍历节点
    def walk(self, node: Any) -> None:
        kind = getattr(node, "kind", "")
        if kind == "command":
            self._handle_command(node)
        elif kind == "redirect":
            self._handle_redirect(node)
        elif kind == "assignment":
            self._handle_assignment(node)
        elif kind == "word":
            self._handle_word(node)
        elif kind in ("commandsubstitution", "processsubstitution"):
            self._handle_substitution(node, kind)
        else:
            for child in _children(node):
                self.walk(child)

    # command 节点：拆出 words / assignments / redirects
    def _handle_command(self, node: Any) -> None:
        words: list[Any] = []
        for part in getattr(node, "parts", []):
            kind = getattr(part, "kind", "")
            if kind == "word":
                words.append(part)
            elif kind == "assignment":
                self._handle_assignment(part)
            elif kind == "redirect":
                self._handle_redirect(part)
            else:
                self.walk(part)

        argv = [str(getattr(w, "word", "")) for w in words]
        if argv:
            self.commands.append(argv)
        for word in words:
            self._handle_word(word)
        if argv:
            self._classify(argv)

    # redirect 节点：< >> > 等输出目标视作路径；heredoc 分隔符忽略
    def _handle_redirect(self, node: Any) -> None:
        rtype = str(getattr(node, "type", ""))
        if rtype in ("<<", "<<-"):
            return
        target = getattr(node, "output", None)
        word = str(getattr(target, "word", "")) if target is not None else ""
        if not word or word.isdigit() or _is_ignored_path(word):
            return
        writing = rtype in (">", ">>", ">|", "&>", "&>>", "<>")
        self._scan_text(word, writing=writing)
        if writing:
            self._escalate("medium")

    # assignment（X=/path）：扫描右值中的路径
    def _handle_assignment(self, node: Any) -> None:
        text = str(getattr(node, "word", ""))
        if "=" in text:
            value = text.split("=", 1)[1]
            self._scan_text(value, writing=False)
            if _ENV_HOME_RE.search(value):
                self.outside = True
                self._escalate("medium")

    # word 节点：处理 ~、$HOME、$PWD、命令替换等特殊 part
    def _handle_word(self, node: Any) -> None:
        text = str(getattr(node, "word", ""))
        if _ENV_HOME_RE.search(text):
            self.outside = True
            self._escalate("medium")

        for part in getattr(node, "parts", []):
            kind = getattr(part, "kind", "")
            if kind == "tilde":
                # 记录实际使用的 ~ 路径，而不是笼统的家庭目录根
                self._scan_text(text, writing=False)
                self.outside = True
                self._escalate("medium")
            elif kind in ("commandsubstitution", "processsubstitution"):
                self._handle_substitution(part, kind)
            elif kind == "parameter":
                if _ENV_HOME_RE.search(text):
                    self.outside = True
                    self._escalate("medium")

        if _SUBST_RE.search(text) and not getattr(node, "parts", []):
            self._escalate("high")
            self.outside = True
            self._scan_text(text, writing=True)

    # 命令替换 / 进程替换：解析内层命令，风险由内层决定；无法解析时保守按高风险处理
    def _handle_substitution(self, node: Any, kind: str) -> None:
        if kind == "processsubstitution":
            inner_cmd = getattr(node, "command", None)
            if inner_cmd is not None:
                self.walk(inner_cmd)
            else:
                self.opaque = True
                self._escalate("high")
            self._escalate("medium")
            return

        if self.depth < 3:
            start, end = getattr(node, "pos", (0, 0))
            inner = self.command[start:end]
            if inner.startswith("$(") and inner.endswith(")"):
                inner = inner[2:-1]
            elif inner.startswith("`") and inner.endswith("`"):
                inner = inner[1:-1]
            self._escalate("medium")
            self._analyze_inline(inner)
        else:
            self.opaque = True
            self._escalate("high")

    # 递归分析内联脚本（sh -c '...'、$(...)）
    def _analyze_inline(self, script: str) -> None:
        if not script.strip():
            return
        try:
            nodes = bashlex.parse(script, strictmode=False)
        except Exception:
            self.opaque = True
            self._escalate("high")
            self._scan_text(script, writing=True)
            return
        child = _Walker(script, self.workspace, self.depth + 1)
        for child_node in nodes:
            child.walk(child_node)
        self._merge(child)

    # 合并子分析器的结果
    def _merge(self, child: _Walker) -> None:
        self.risk = _max_risk(self.risk, child.risk)
        self.outside = self.outside or child.outside
        self.write_outside = self.write_outside or child.write_outside
        self.sensitive_write = self.sensitive_write or child.sensitive_write
        self.opaque = self.opaque or child.opaque
        self.failed = self.failed or child.failed
        for path in child.paths:
            if path not in self.paths:
                self.paths.append(path)
        self.commands.extend(child.commands)

    # 对一条命令做风险与路径分类
    def _classify(self, argv: list[str]) -> None:
        prog = _program_name(argv[0])
        args = argv[1:]
        if _looks_like_path(argv[0]):
            self._record_path(argv[0], writing=False)

        if prog == "cd":
            self.outside = True
            self._escalate("medium")

        if prog in _SHELLS:
            inline = self._inline_script(args, "-c")
            if inline is not None:
                # 内层脚本可解析，风险由内层决定，至少按中风险处理
                self._escalate("medium")
                self._analyze_inline(inline)
                return
            if not args:
                self.opaque = True
                self._escalate("high")
                return

        if prog in _INTERPRETERS:
            flag = _INLINE_FLAGS.get(prog)
            inline = self._inline_script(args, flag) if flag else None
            if inline is not None:
                self.opaque = True
                self._escalate("high")
                self._scan_text(inline, writing=True)
                return

        if prog in ("eval", "source", "."):
            self._escalate("high")
            self._analyze_inline(" ".join(args))
            return

        if prog in _ALWAYS_HIGH:
            self._escalate("high")

        if prog == "find" and any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args):
            self._escalate("high")

        writing = prog in _WRITE_COMMANDS
        if writing:
            self._escalate("medium")

        if prog == "rm" and any(_has_short_flag(a, "r") or _has_short_flag(a, "f") for a in args):
            self._escalate("high")

        if prog == "chmod" and any(a == "777" or a == "0777" for a in args):
            self._escalate("high")

        if prog == "git":
            self._classify_git(args)

        for arg in args:
            self._scan_arg(arg, writing=writing)

    # git 子命令风险分级
    def _classify_git(self, args: list[str]) -> None:
        sub = _subcommand(args)
        if sub == "push":
            self._escalate("high")
        elif sub == "reset" and any(a == "--hard" for a in args):
            self._escalate("high")
        elif sub == "clean" and any(_has_short_flag(a, "f") for a in args):
            self._escalate("high")
        elif sub in (
            "commit", "add", "checkout", "switch", "merge",
            "rebase", "cherry-pick", "stash", "tag",
        ):
            self._escalate("medium")

    # 扫描单个参数；`--flag=value` 仅在已知路径旗标或明显越界时按路径处理
    def _scan_arg(self, arg: str, *, writing: bool) -> None:
        if arg.startswith("--") and "=" in arg:
            flag, _, value = arg.partition("=")
            if flag in _PATH_OPTION_FLAGS:
                self._scan_text(value, writing=writing)
            return
        if arg.startswith("-"):
            return
        self._scan_text(arg, writing=writing)

    # 提取解释器的内联脚本参数（如 sh -c 'cmd' / python -c 'code'）
    def _inline_script(self, args: list[str], flag: str | None) -> str | None:
        if flag is None:
            return None
        for i, arg in enumerate(args):
            if arg == flag and i + 1 < len(args):
                return args[i + 1]
            if flag in ("-c", "-e", "-r") and _has_short_flag(arg, flag[1:]) and i + 1 < len(args):
                return args[i + 1]
        return None

    # 计算 pattern：单条简单命令用程序名（或 git/npm 子命令），复合命令用归一化全文
    def pattern(self) -> str:
        if self.failed or len(self.commands) != 1:
            normalized = " ".join(self.command.split())
            return normalized or "*"
        argv = self.commands[0]
        prog = _program_name(argv[0])
        if prog in _SUBCOMMAND_PROGRAMS:
            sub = _subcommand(argv[1:])
            if sub is not None:
                return f"{prog} {sub}"
        return prog

    # 输出最终分析结果
    def result(self) -> Analysis:
        if self.opaque:
            self._escalate("high")
        if self.write_outside or self.sensitive_write:
            self._escalate("high")
        if self.outside:
            self._escalate("medium")
        return Analysis(
            risk=self.risk,
            pattern=self.pattern(),
            affected_paths=tuple(self.paths[:8]),
            outside=self.outside,
            force_ask=self.outside or self.risk == "high",
        )

# 判断 flag 是否为短旗标且包含指定字母（支持 -rf、-la 合并写法）
def _has_short_flag(arg: str, letter: str) -> bool:
    return len(arg) > 1 and arg[0] == "-" and not arg.startswith("--") and letter in arg[1:]

# 取首个非旗标、非路径参数作为子命令
def _subcommand(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            i += 2 if arg in _VALUE_FLAGS else 1
            continue
        if _looks_like_path(arg):
            i += 1
            continue
        return arg
    return None

# 分析 bash 命令，返回风险、pattern、影响路径与是否强制 ASK
def analyze_command(command: str, workspace: Path | None = None) -> Analysis:
    command = command.strip()
    if not command:
        return Analysis()
    root = (workspace or Path.cwd()).resolve()
    try:
        nodes = bashlex.parse(command, strictmode=False)
    except Exception:
        # 解析失败时无法判断命令语义，按最高风险处理，并尽力提取路径用于展示
        scanner = _Walker(command, root)
        scanner._scan_text(command, writing=True)
        return Analysis(
            risk="high",
            pattern=" ".join(command.split()) or "*",
            affected_paths=tuple(scanner.paths[:8]),
            outside=True,
            force_ask=True,
        )

    walker = _Walker(command, root)
    for node in nodes:
        walker.walk(node)
    return walker.result()

# 分析任意工具调用；文件工具以目标绝对路径作为 pattern 与影响路径
def analyze_tool(tool_name: str, params: dict[str, Any], workspace: Path | None = None) -> Analysis:
    root = (workspace or Path.cwd()).resolve()
    if tool_name == "bash":
        return analyze_command(str(params.get("command", "")), root)

    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return Analysis()

    resolved = _resolve_path(raw_path, root)
    outside = not resolved.is_relative_to(root)
    writing = tool_name in ("write_file", "edit_file")
    sensitive = writing and _is_sensitive(resolved)
    risk: RiskLevel = "low"
    if writing:
        risk = "medium"
    if outside:
        risk = "high" if writing else "medium"
    if sensitive:
        risk = "high"
    return Analysis(
        risk=risk,
        pattern=str(resolved),
        affected_paths=(str(resolved),),
        outside=outside,
        force_ask=writing and (outside or sensitive),
    )

__all__ = [
    "Analysis",
    "RiskLevel",
    "analyze_command",
    "analyze_tool",
]
