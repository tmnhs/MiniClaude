from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_POLICY_PATH = Path("~/.mc/policy.toml")

# 一条持久化权限规则：工具 + 作用面 pattern + 决策
@dataclass(frozen=True)
class PermissionRule:
    tool: str
    pattern: str
    decision: str

# 解析 [[rules]] 数组，跳过非法条目
def _parse_rules(raw_rules: object) -> list[PermissionRule]:
    if not isinstance(raw_rules, list):
        return []
    rules: list[PermissionRule] = []
    for entry in raw_rules:
        if not isinstance(entry, dict):
            logger.warning("policy.toml: ignoring non-table [[rules]] entry %r", entry)
            continue
        tool = entry.get("tool")
        pattern = entry.get("pattern")
        decision = entry.get("decision")
        if (
            not isinstance(tool, str)
            or not isinstance(pattern, str)
            or decision not in ("allow", "deny")
        ):
            logger.warning("policy.toml: ignoring invalid [[rules]] entry %r", entry)
            continue
        rules.append(PermissionRule(tool=tool, pattern=pattern, decision=decision))
    return rules

# 加载 policy.toml：返回 [[rules]] 规则列表；文件不存在时返回空列表
def load_policy_file(path: Path | None = None) -> list[PermissionRule]:
    p = (path or _DEFAULT_POLICY_PATH).expanduser()
    if not p.exists():
        return []

    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("policy.toml: failed to load %s: %s", p, exc)
        return []

    return _parse_rules(data.get("rules"))

# 将规则写入 policy.toml（[[rules]] 格式），覆盖旧内容
def save_policy_file(
    rules: dict[tuple[str, str], str] | list[PermissionRule],
    path: Path | None = None,
) -> None:
    p = (path or _DEFAULT_POLICY_PATH).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)

    items = (
        [PermissionRule(tool=tool, pattern=pattern, decision=decision)
         for (tool, pattern), decision in rules.items()]
        if isinstance(rules, dict)
        else list(rules)
    )

    lines = [
        "# ~/.mc/policy.toml",
        "# 由 mc-core 自动管理，手动编辑生效但格式须正确",
        "",
    ]
    for rule in sorted(items, key=lambda r: (r.tool, r.pattern)):
        lines.extend(
            [
                "[[rules]]",
                f"tool = {json.dumps(rule.tool, ensure_ascii=False)}",
                f"pattern = {json.dumps(rule.pattern, ensure_ascii=False)}",
                f"decision = {json.dumps(rule.decision)}",
                "",
            ]
        )
    p.write_text("\n".join(lines), encoding="utf-8")

__all__ = ["PermissionRule", "load_policy_file", "save_policy_file"]
