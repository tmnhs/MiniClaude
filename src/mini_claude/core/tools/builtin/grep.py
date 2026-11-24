from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.limits import (
    DEFAULT_BASH_TIMEOUT,
    DEFAULT_GREP_MAX_LINE_LEN,
    DEFAULT_GREP_MAX_RESULTS,
    IGNORED_DIRS,
    MAX_BASH_OUTPUT_BYTES,
    MAX_GREP_FILE_BYTES,
    MAX_GREP_MAX_LINE_LEN,
    MAX_GREP_MAX_RESULTS,
    resolve_within_workspace,
)
from mini_claude.core.tools.process import decode_output, run_process


class GrepParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str
    path: str = "."
    glob: str | None = None
    case_sensitive: bool = False
    max_results: int = Field(default=DEFAULT_GREP_MAX_RESULTS, ge=1, le=MAX_GREP_MAX_RESULTS)
    max_line_len: int = Field(
        default=DEFAULT_GREP_MAX_LINE_LEN, ge=1, le=MAX_GREP_MAX_LINE_LEN
    )


# 按字符上限截断单行展示内容
def _clip(line: str, max_line_len: int) -> str:
    return line[:max_line_len] + "…" if len(line) > max_line_len else line


# 深度优先遍历目录，剪枝忽略目录，不跟随符号链接目录
def _walk_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


# Python 兜底实现：逐文件搜索，跳过越界、符号链接、超大与二进制文件
def _search_sync(
    regex: re.Pattern[str],
    root: Path,
    workspace: Path,
    glob_filter: str | None,
    max_results: int,
    max_line_len: int,
) -> tuple[list[str], bool]:
    hits: list[str] = []
    limit_hit = False

    files: Iterator[Path] = iter([root]) if root.is_file() else _walk_files(root)
    for path in files:
        if path.is_symlink():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(workspace) or not resolved.is_file():
            continue

        # 可选 glob 过滤：相对搜索根匹配
        if glob_filter is not None:
            base = root if root.is_dir() else root.parent
            if not Path(path.relative_to(base)).match(glob_filter):
                continue

        try:
            if resolved.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            data = resolved.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:1024]:
            continue

        try:
            rel = resolved.relative_to(workspace).as_posix()
        except ValueError:
            continue

        text = data.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if not regex.search(line):
                continue
            if len(hits) >= max_results:
                limit_hit = True
                break
            hits.append(f"{rel}:{lineno}:{_clip(line, max_line_len)}")
        if limit_hit:
            break

    return hits, limit_hit


# 可选 ripgrep 加速；后端缺失、执行失败或输出不可解析时返回 None 交给 Python 兜底
async def _rg_search(
    pattern: str,
    root: Path,
    workspace: Path,
    glob_filter: str | None,
    case_sensitive: bool,
    max_results: int,
    max_line_len: int,
) -> tuple[list[str], bool] | None:
    rg = shutil.which("rg")
    if rg is None:
        return None

    argv = [rg, "--json", "--hidden", "-e", pattern]
    if not case_sensitive:
        argv.append("-i")
    if glob_filter:
        argv += ["--glob", glob_filter]
    # 不传 --no-ignore，保留 .gitignore 语义；仅额外剪枝固定忽略目录（任意深度）
    for ignored in sorted(IGNORED_DIRS):
        argv += ["--glob", f"!**/{ignored}/**"]
    argv.append(os.path.relpath(root, workspace))

    try:
        result = await run_process(argv, cwd=workspace, timeout=DEFAULT_BASH_TIMEOUT)
    except Exception:
        return None
    # rg 退出码 1 表示无匹配，属正常
    if result.returncode not in (0, 1):
        return None

    hits: list[str] = []
    limit_hit = False
    for raw_line in result.output.splitlines():
        if not raw_line.startswith("{"):
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data", {})
        path_text = data.get("path", {}).get("text")
        line_no = data.get("line_number")
        line_text = data.get("lines", {}).get("text")
        if (
            not isinstance(path_text, str)
            or not isinstance(line_no, int)
            or not isinstance(line_text, str)
        ):
            continue
        if len(hits) >= max_results:
            limit_hit = True
            break
        rel = path_text[2:] if path_text.startswith("./") else path_text
        hits.append(f"{rel}:{line_no}:{_clip(line_text.rstrip('\n'), max_line_len)}")

    return hits, limit_hit


class GrepTool(BaseTool):
    params_model = GrepParams
    name = "grep"
    description = (
        "Search file contents with a regular expression (Python re syntax). "
        "Returns 'relpath:line:text' per match, up to max_results matches. "
        "Path must be relative to the current working directory and may not escape it. "
        f"Files larger than {MAX_GREP_FILE_BYTES // (1024 * 1024)} MB and binary files "
        "are skipped; common dependency/cache directories are ignored."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {
                "type": "string",
                "description": "Relative file or directory to search (default '.').",
            },
            "glob": {
                "type": "string",
                "description": "Optional glob filter applied to file paths, e.g. '*.py'.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive match (default false).",
            },
            "max_results": {
                "type": "integer",
                "description": (
                    f"Maximum matches to return (default {DEFAULT_GREP_MAX_RESULTS}, "
                    f"max {MAX_GREP_MAX_RESULTS})."
                ),
            },
            "max_line_len": {
                "type": "integer",
                "description": (
                    f"Truncate each displayed line at this many characters "
                    f"(default {DEFAULT_GREP_MAX_LINE_LEN}, max {MAX_GREP_MAX_LINE_LEN})."
                ),
            },
        },
        "required": ["pattern"],
    }

    # 在 workspace 内搜索正则命中，优先 rg，缺失时退回 Python 遍历
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = GrepParams.model_validate(params)
        if not p.pattern:
            return ToolResult(
                content="pattern must not be empty", is_error=True, error_type="schema_error"
            )
        try:
            regex = re.compile(p.pattern, 0 if p.case_sensitive else re.IGNORECASE)
        except re.error as exc:
            return ToolResult(
                content=f"invalid regex: {exc}", is_error=True, error_type="schema_error"
            )

        workspace = Path.cwd().resolve()
        root = resolve_within_workspace(p.path, workspace)
        if not root.exists():
            raise FileNotFoundError(f"no such path: {p.path}")

        result = await _rg_search(
            p.pattern, root, workspace, p.glob, p.case_sensitive, p.max_results, p.max_line_len
        )
        if result is not None:
            hits, limit_hit = result
        else:
            hits, limit_hit = await asyncio.to_thread(
                _search_sync, regex, root, workspace, p.glob, p.max_results, p.max_line_len
            )

        content = "\n".join(hits)
        if limit_hit:
            content += f"\n[truncated at {p.max_results} matches]"
        content, _ = decode_output(content.encode("utf-8"), MAX_BASH_OUTPUT_BYTES)
        return ToolResult(content=content)
