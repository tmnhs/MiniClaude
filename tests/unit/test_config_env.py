from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.conf.config import get_config


def _write_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# 功能：验证 .env 文件中的值被正确加载并覆盖内建默认值
# 设计：写 .env 到临时目录并 chdir 进去，清除同名系统环境变量排除干扰，确认 .env 加载路径有效
def test_dotenv_base_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, "MINICLAUDE_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINICLAUDE_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 9999


# 功能：验证系统环境变量的优先级高于 .env 文件中的值
# 设计：.env 写 9999，系统环境变量写 8888，确认最终值为 8888，对应四级优先链的顶层约束
def test_system_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, "MINICLAUDE_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINICLAUDE_PORT", "8888")

    cfg = get_config()

    assert cfg.port == 8888


# 功能：验证 .env 文件不存在时静默跳过，使用内建默认值（不抛异常）
# 设计：chdir 到空目录，清除系统环境变量，确认 get_config() 不因 .env 缺失而崩溃，默认端口为 10721
def test_missing_env_file_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINICLAUDE_PORT", raising=False)
    monkeypatch.setenv("MINICLAUDE_CONFIG", str(tmp_path / "absent.toml"))

    cfg = get_config()

    assert cfg.port == 10721


# 功能：验证 .env 中设置的 MINICLAUDE_CONFIG 能正确影响 TOML 配置文件的加载路径
# 设计：.env 指向自定义 TOML 文件，TOML 中写入不同端口，确认 .env 在 TOML 加载前被读取（优先级链的正确顺序）
def test_dotenv_before_toml_miniclaude_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml_path = tmp_path / "custom.toml"
    toml_path.write_bytes(b'[core]\nport = 5555\n')

    env_file = tmp_path / ".env"
    _write_env(env_file, f"MINICLAUDE_CONFIG={toml_path}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINICLAUDE_CONFIG", raising=False)
    monkeypatch.delenv("MINICLAUDE_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 5555


# 功能：验证同一变量经过完整四级优先链后，最终值为最高优先级来源（系统环境变量）
# 设计：同时设置默认值(10721)/TOML(6000)/.env(7000)/系统环境变量(8000)，确认最终值为 8000，是优先级链的综合正确性验证
def test_priority_chain_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 默认值：10721
    # TOML：6000
    # .env：7000
    # 系统环境变量：8000（最高）
    toml_path = tmp_path / "miniclaude.toml"
    toml_path.write_bytes(b'[core]\nport = 6000\n')

    env_file = tmp_path / ".env"
    _write_env(env_file, "MINICLAUDE_PORT=7000\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINICLAUDE_CONFIG", str(toml_path))
    monkeypatch.setenv("MINICLAUDE_PORT", "8000")

    cfg = get_config()

    assert cfg.port == 8000


# 功能：验证沙箱模式默认为 workspace（安全的默认值）
# 设计：隔离到临时目录与空配置，清除环境变量，确认默认配置不会退化为 off
def test_sandbox_default_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINICLAUDE_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("MINICLAUDE_SANDBOX_MODE", raising=False)

    cfg = get_config()

    assert cfg.sandbox.mode == "workspace"
    assert cfg.sandbox.allow_write == []
    assert cfg.sandbox.allow_read == []


# 功能：验证 [sandbox] 的 mode/allow_write/allow_read 被正确解析
# 设计：写一份最小 TOML，确认三个字段都落到 SandboxConfig 上
def test_sandbox_toml_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_path = tmp_path / "mc.toml"
    toml_path.write_bytes(
        b'[sandbox]\nmode = "strict"\n'
        b'allow_write = ["/tmp/x"]\nallow_read = ["/tmp/y"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINICLAUDE_CONFIG", str(toml_path))
    monkeypatch.delenv("MINICLAUDE_SANDBOX_MODE", raising=False)

    cfg = get_config()

    assert cfg.sandbox.mode == "strict"
    assert cfg.sandbox.allow_write == ["/tmp/x"]
    assert cfg.sandbox.allow_read == ["/tmp/y"]


# 功能：验证非法 sandbox.mode 在配置加载阶段 fail-fast
def test_sandbox_invalid_toml_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_path = tmp_path / "mc.toml"
    toml_path.write_bytes(b'[sandbox]\nmode = "jail"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINICLAUDE_CONFIG", str(toml_path))
    monkeypatch.delenv("MINICLAUDE_SANDBOX_MODE", raising=False)

    with pytest.raises(SystemExit):
        get_config()


# 功能：验证 MINICLAUDE_SANDBOX_MODE 覆盖 TOML/默认值
def test_sandbox_env_overrides_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINICLAUDE_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("MINICLAUDE_SANDBOX_MODE", "off")

    cfg = get_config()

    assert cfg.sandbox.mode == "off"


# 功能：验证非法环境变量值 fail-fast
def test_sandbox_invalid_env_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINICLAUDE_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("MINICLAUDE_SANDBOX_MODE", "nope")

    with pytest.raises(SystemExit):
        get_config()
