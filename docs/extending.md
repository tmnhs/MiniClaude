# 扩展：Skills、Agents 与工具

## Skills

在对话中以 `/` 前缀调用，如 `/init`、`/review src/`。技能会展开为系统提示词覆盖 + 工具白名单。

内置技能：

| 技能 | 作用 |
|------|------|
| `/init` | 分析项目并生成 `.mc/context.md` |
| `/orchestrate` | planner → executor → reviewer 三阶段多 agent 工作流 |
| `/review` | 对指定路径做分级代码审查 |
| `/summarize` | 将当前会话压缩为人类可读摘要 |

### 自定义技能

放置位置（优先级从高到低）：

1. `<workspace>/.mc/skills/`
2. `~/.mc/skills/`
3. 内置

支持两种写法：`<name>.md` 或 `<name>/SKILL.md`。

```markdown
---
name: my-skill
description: 技能说明
allowed_tools:
  - read_file
  - bash
---
系统提示词正文，可用 $ARGUMENTS 引用调用参数。
```

`description` 支持 YAML 块标量（`>` 折叠 / `|` 保留换行）。

## Agents（角色配置）

TOML 文件，放在 `<workspace>/.mc/agents/<name>.toml` 或 `~/.mc/agents/<name>.toml`，内置 `planner`、`executor`、`reviewer`。

```toml
[agent]
description = "执行者"
system_prompt = "你负责按计划逐步执行任务并汇报结果。"
allowed_tools = ["read_file", "write_file", "bash", "grep", "glob"]
model = ""   # 留空使用默认模型
```

优先级：项目本地 > 用户全局 > 内置。

## 子 agent

| 工具 | 说明 |
|------|------|
| `spawn_agent` | 派生冷启动子 agent（只含给定 prompt，不继承当前对话）；`run_in_background=true` 可并行 |
| `agent_result` | 取回后台子 agent 的结果 |

## 内置工具

| 工具 | 说明 |
|------|------|
| `read_file` / `write_file` / `edit_file` | 读取、写入、精确替换文件内容 |
| `list_dir` / `glob` / `grep` | 目录列表、文件名匹配、内容搜索 |
| `bash` | 执行 shell 命令（受沙箱与权限约束） |
| `git` | 常用 git 操作 |
| `note_save` | 记录重要事实到会话笔记 |
| `task_create` / `task_update` / `task_list` / `task_get` | 会话任务清单 |
| `spawn_agent` / `agent_result` | 派生与收集子 agent |

## 项目上下文

每次 run 会把以下内容注入 system prompt：

| 来源 | 路径 |
|------|------|
| 全局上下文 | `~/.mc/context.md` |
| 项目上下文 | `<workspace>/.mc/context.md`（`/init` 可自动生成） |
| 会话笔记 | 由 `note_save` 工具写入 `~/.mc/sessions/<id>/notes.md` |
