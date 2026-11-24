from __future__ import annotations

from pathlib import Path

from mini_claude.core.permissions.store import (
    PermissionRule,
    load_policy_file,
    save_policy_file,
)


# 功能：验证新格式 [[rules]] 写入后可被原样读回
# 设计：save → load 往返，断言规则集合一致；这是持久化 always 决策的基础契约
def test_rules_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    rules = {
        ("bash", "git status"): "allow",
        ("write_file", str(tmp_path / "a.txt")): "deny",
    }

    save_policy_file(rules, path)
    loaded = {(r.tool, r.pattern): r.decision for r in load_policy_file(path)}

    assert loaded == rules


# 功能：验证写入内容使用 [[rules]] 而非旧 [always]
# 设计：直接检查文件文本，确保客户端升级后不会再生成旧格式
def test_save_writes_rules_format(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    save_policy_file([PermissionRule("bash", "echo", "allow")], path)
    text = path.read_text(encoding="utf-8")
    assert "[[rules]]" in text
    assert "[always]" not in text


# 功能：验证文件不存在时返回空列表
# 设计：首次运行场景，不应抛异常
def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_policy_file(tmp_path / "nope.toml") == []


# 功能：验证非法 TOML 被忽略而不是让守护进程崩溃
# 设计：policy.toml 是用户可手改文件，解析失败应降级为空规则
def test_invalid_toml_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text("not = [valid", encoding="utf-8")
    assert load_policy_file(path) == []
