"""
S1 agent 流水线的端到端集成测试。

需要真实的 ANTHROPIC_API_KEY，缺失时会自动跳过。
可显式运行：
    uv run pytest tests/integration/test_run_e2e.py -v
或使用 marker：
    uv run pytest -m integration -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from mini_claude.core.conf.config import MiniClaudeConfig
from mini_claude.core.runner import AgentRunner

# 加载项目 .env，使 ANTHROPIC_API_KEY 无需经过 get_config() 即可使用
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)

pytestmark = pytest.mark.integration


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    f = tmp_path / "sample.txt"
    f.write_text(
        "# Test Document\n\nThe magic number mentioned in this file is 7391.\n",
        encoding="utf-8",
    )
    return f


# 功能：验证完整的端到端 agent 链路：调用真实 LLM → 执行 read_file → 成功完成并写入 events.jsonl
# 设计：使用真实 ANTHROPIC_API_KEY 和真实文件，goal 中指定一个具体的数字（7391）以便断言 LLM 确实读了文件；
#       通过 events.jsonl 的事件序列断言每个关键阶段都被记录，而非只检查 stdout，因为 events.jsonl 是 S1 的核心验收产物
async def test_run_e2e_reads_file_and_succeeds(
    sample_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    # ReadFileTool 按 CWD 解析路径，因此切换到 tmp_path
    monkeypatch.chdir(tmp_path)

    goal = (
        "Use the read_file tool to read the file 'sample.txt' "
        "and report the magic number it mentions."
    )
    runs_dir = tmp_path / "runs"

    config = MiniClaudeConfig()
    config.agent.max_steps = 5

    runner = AgentRunner(config, runs_dir=runs_dir)
    await runner.run(goal)

    # ── events.jsonl must exist ──────────────────────────────────────────────
    jsonl_files = list(runs_dir.rglob("events.jsonl"))
    assert len(jsonl_files) == 1, "expected exactly one events.jsonl"

    events = [
        json.loads(line)
        for line in jsonl_files[0].read_text(encoding="utf-8").splitlines()
        if line
    ]
    types = [e["type"] for e in events]

    # ── event sequence assertions (from §6.4) ────────────────────────────────
    assert types[0] == "run.started"
    assert types[-1] == "run.finished"
    assert "step.started" in types
    assert "tool.call_started" in types
    assert "tool.call_finished" in types
    assert "llm.usage" in types

    # ── run completed successfully ────────────────────────────────────────────
    finished = events[-1]
    assert finished["status"] == "success", (
        f"run finished with status={finished['status']!r}, reason={finished.get('reason')!r}"
    )

    # ── read_file was actually invoked ────────────────────────────────────────
    tool_starts = [e for e in events if e["type"] == "tool.call_started"]
    assert any(e["tool_name"] == "read_file" for e in tool_starts), (
        "expected at least one read_file tool call"
    )

    # ── run_id is consistent across the event stream ─────────────────────────
    run_id = events[0]["run_id"]
    assert all(e["run_id"] == run_id for e in events), "run_id must be the same in every event"

    # ── LLM cache stats are present ──────────────────────────────────────────
    usage_events = [e for e in events if e["type"] == "llm.usage"]
    assert len(usage_events) >= 1
    for ue in usage_events:
        assert "input_tokens" in ue
        assert "output_tokens" in ue
        assert "cache_read_input_tokens" in ue
