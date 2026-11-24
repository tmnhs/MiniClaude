from __future__ import annotations

from pathlib import Path

import pytest

from mini_claude.core.permissions.analysis import analyze_command, analyze_tool

# ── 基础风险等级与 pattern ────────────────────────────────────────────────────

# 功能：验证空命令返回默认低风险结果
# 设计：空白输入不应触发解析，pattern 使用通配符
def test_empty_command_returns_default() -> None:
    result = analyze_command("   ")
    assert result.risk == "low"
    assert result.pattern == "*"
    assert result.affected_paths == ()
    assert result.outside is False
    assert result.force_ask is False


# 功能：验证普通只读命令为低风险，pattern 取程序名
# 设计：echo 不修改文件、不越界，是缓存粒度最宽松的一类
def test_echo_is_low_risk() -> None:
    result = analyze_command("echo hi")
    assert result.risk == "low"
    assert result.pattern == "echo"
    assert result.outside is False
    assert result.force_ask is False


# 功能：验证带子命令的程序以「程序 + 子命令」作为 pattern
# 设计：同一程序的不同子命令应能分别授权，符合命令粒度审批
@pytest.mark.parametrize(
    ("command", "pattern"),
    [
        ("git status", "git status"),
        ("npm test", "npm test"),
        ("uv add ruff", "uv add"),
        ("pip install foo", "pip install"),
    ],
)
def test_subcommand_program_uses_prefix_pattern(command: str, pattern: str) -> None:
    assert analyze_command(command).pattern == pattern


# 功能：验证非子命令程序直接使用程序名作为 pattern
# 设计：cat 没有子命令语义，pattern 不应包含参数
def test_plain_program_pattern_is_program_name() -> None:
    assert analyze_command("cat README.md").pattern == "cat"


# 功能：验证相对路径形式的程序名会被正确提取
# 设计：bin/tool 的 Path.name 为 tool，pattern 不应带目录
def test_relative_program_uses_basename_pattern() -> None:
    result = analyze_command("bin/tool arg")
    assert result.pattern == "tool"
    assert result.outside is False


# 功能：验证复合命令退化为归一化全文作为 pattern
# 设计：多条命令无法用单一前缀授权，必须整体缓存
def test_compound_command_uses_full_text_pattern() -> None:
    assert analyze_command("echo a; echo b").pattern == "echo a; echo b"
    assert analyze_command("true && echo hi").pattern == "true && echo hi"


# 功能：验证 dot-source、source、eval 均为高风险
# 设计：都可在运行时执行任意代码，必须强制 ASK
@pytest.mark.parametrize(
    "command",
    [". ./x.sh", "source ./x.sh", "eval 'echo hi'"],
)
def test_dynamic_code_loading_is_high_risk(command: str) -> None:
    result = analyze_command(command)
    assert result.risk == "high"
    assert result.force_ask is True


# ── 重定向与路径收集 ──────────────────────────────────────────────────────────

# 功能：验证工作区内写重定向为中风险
# 设计：写文件需要审批但可缓存；中风险不强制 ASK
def test_write_redirect_inside_is_medium(tmp_path: Path) -> None:
    result = analyze_command("echo hi > out.txt", tmp_path)
    assert result.risk == "medium"
    assert result.outside is False
    assert result.force_ask is False


# 功能：验证工作区外写入为高风险并记录影响路径
# 设计：`echo > /tmp/x` 的 redirect 目标越界；写操作越界必须强制 ASK
def test_write_outside_workspace_is_high_risk(tmp_path: Path) -> None:
    result = analyze_command("echo hi > /tmp/x", tmp_path)
    assert result.risk == "high"
    assert result.outside is True
    assert result.force_ask is True
    assert any(p.endswith("/tmp/x") for p in result.affected_paths)


# 功能：验证追加重定向与合并重定向同样识别越界写入
# 设计：>>、&> 与 > 的风险语义一致
@pytest.mark.parametrize("redirect", [">>", "&>"])
def test_append_and_combined_redirect_detected(tmp_path: Path, redirect: str) -> None:
    result = analyze_command(f"echo hi {redirect} /tmp/x", tmp_path)
    assert result.risk == "high"
    assert result.outside is True
    assert any(p.endswith("/tmp/x") for p in result.affected_paths)


# 功能：验证输入重定向的越界路径会被收集
# 设计：sort < /etc/passwd 属于越界读，应记录受影响路径
def test_input_redirect_outside_path(tmp_path: Path) -> None:
    result = analyze_command("sort < /etc/passwd", tmp_path)
    assert result.outside is True
    assert any(p.endswith("/etc/passwd") for p in result.affected_paths)


# 功能：验证重定向到 /dev/null 不产生影响路径，也不触发越界
# 设计：2>/dev/null 是常见写法，不应出现在审批路径中
def test_dev_null_is_ignored(tmp_path: Path) -> None:
    result = analyze_command("echo hi > /dev/null", tmp_path)
    assert result.affected_paths == ()
    assert result.outside is False
    assert result.risk == "low"


# 功能：验证 /dev/fd 等设备节点被忽略
# 设计：cat /dev/fd/3 属于进程内文件描述符，不应判定越界
def test_dev_fd_is_ignored(tmp_path: Path) -> None:
    result = analyze_command("cat /dev/fd/3", tmp_path)
    assert result.affected_paths == ()
    assert result.outside is False


# 功能：验证 heredoc 内容不会被当成路径扫描
# 设计：heredoc 只是 stdin 数据，正文中的 /etc/passwd 不应触发越界
@pytest.mark.parametrize("operator", ["<<", "<<-"])
def test_heredoc_body_is_ignored(tmp_path: Path, operator: str) -> None:
    result = analyze_command(f"cat {operator}EOF\n/etc/passwd\nEOF", tmp_path)
    assert result.risk == "low"
    assert result.affected_paths == ()
    assert result.outside is False


# 功能：验证文件描述符复制重定向被忽略
# 设计：2>&1 的目标是 fd 编号，不是文件路径
def test_fd_dup_redirect_is_ignored(tmp_path: Path) -> None:
    result = analyze_command("echo hi 2>&1", tmp_path)
    assert result.affected_paths == ()
    assert result.outside is False


# 功能：验证重复出现的路径只记录一次
# 设计：受影响路径用于审批展示，重复项会造成噪音
def test_duplicate_paths_are_deduped(tmp_path: Path) -> None:
    result = analyze_command("cat /etc/passwd /etc/passwd", tmp_path)
    matches = [p for p in result.affected_paths if p.endswith("/etc/passwd")]
    assert len(matches) == 1


# ── 变量、家目录与相对路径 ────────────────────────────────────────────────────

# 功能：验证 $HOME / ${HOME} / $PWD 被视为越界引用
# 设计：这些变量无法静态确认落点，保守按越界只读处理
@pytest.mark.parametrize("command", ["echo $HOME", "echo ${HOME}/x", "echo $PWD"])
def test_home_variables_are_outside(command: str) -> None:
    result = analyze_command(command)
    assert result.risk == "medium"
    assert result.outside is True
    assert result.force_ask is True


# 功能：验证赋值语句中的 $HOME 也会触发越界判定
# 设计：FOO=$HOME/x 的环境变量赋值同样指向工作区外
def test_assignment_with_home_is_outside() -> None:
    result = analyze_command("FOO=$HOME/x echo hi")
    assert result.risk == "medium"
    assert result.outside is True


# 功能：验证赋值语句中的绝对路径会被收集
# 设计：FOO=/etc/passwd 会影响后续命令读取的内容
def test_assignment_path_is_recorded(tmp_path: Path) -> None:
    result = analyze_command("FOO=/etc/passwd echo hi", tmp_path)
    assert result.outside is True
    assert any(p.endswith("/etc/passwd") for p in result.affected_paths)


# 功能：验证 ~ 路径展开到家目录并判定越界
# 设计：HOME 指向工作区外时，~/secret.txt 必须触发 ASK
def test_tilde_path_expands_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = analyze_command("cat ~/secret.txt", workspace)

    assert result.outside is True
    assert result.force_ask is True
    assert any(p.endswith("/secret.txt") for p in result.affected_paths)


# 功能：验证无法展开的 ~user 路径不会让分析崩溃，并按工作区外保守处理
# 设计：Path.expanduser 对不存在的用户抛 RuntimeError，分析层必须降级而不是炸掉审批流程
def test_unresolvable_tilde_user_is_conservative(tmp_path: Path) -> None:
    result = analyze_command("cat ~mc-no-such-user/secret.txt", tmp_path)
    assert result.outside is True
    assert result.force_ask is True


# 功能：验证 cd 一律视为越界中风险
# 设计：目录切换会影响后续相对路径解析，无法静态确认落点
def test_cd_is_outside_medium(tmp_path: Path) -> None:
    result = analyze_command("cd sub", tmp_path)
    assert result.risk == "medium"
    assert result.outside is True


# 功能：验证相对路径只读命令不越界
# 设计：cat README.md 在工作区内，风险低
def test_relative_read_is_low_risk() -> None:
    result = analyze_command("cat README.md")
    assert result.outside is False
    assert result.risk == "low"


# 功能：验证中间的 .. 片段不会被误判为越界
# 设计：a/../b 最终仍在工作区内，正则只匹配词首的 ..
def test_parent_segment_inside_not_false_positive(tmp_path: Path) -> None:
    result = analyze_command("cat a/../b", tmp_path)
    assert result.outside is False
    assert result.affected_paths == ()


# 功能：验证 URL 中的斜杠不会被识别为绝对路径
# 设计：http://host/path 的 / 前面是 : 或 /，不属于文件路径
def test_url_is_not_path(tmp_path: Path) -> None:
    result = analyze_command("echo 'http://host/path'", tmp_path)
    assert result.outside is False
    assert result.affected_paths == ()


# ── 命令替换 / 进程替换 / 内联脚本 ────────────────────────────────────────────

# 功能：验证安全的命令替换按中风险处理，且不误判为越界
# 设计：内层 whoami 无副作用；风险由内层决定，未知输出仅提升到 medium
def test_safe_command_substitution_is_medium_risk() -> None:
    result = analyze_command("echo $(whoami)")
    assert result.risk == "medium"
    assert result.outside is False
    assert result.force_ask is False


# 功能：验证反引号命令替换同样按中风险处理
# 设计：`cmd` 与 $(cmd) 语义相同
def test_backtick_substitution_is_medium_risk() -> None:
    result = analyze_command("echo `whoami`")
    assert result.risk == "medium"
    assert result.outside is False


# 功能：验证命令替换会继承内层的越界读取
# 设计：$(echo /etc/passwd) 的路径必须出现在最终分析结果中
def test_command_substitution_propagates_outside(tmp_path: Path) -> None:
    result = analyze_command("cat $(echo /etc/passwd)", tmp_path)
    assert result.risk == "medium"
    assert result.outside is True
    assert any(p.endswith("/etc/passwd") for p in result.affected_paths)


# 功能：验证命令替换会继承内层的高风险写操作
# 设计：$(rm -rf /tmp/z) 应把整体提升为高风险越界
def test_command_substitution_propagates_write_risk(tmp_path: Path) -> None:
    result = analyze_command("echo hi $(rm -rf /tmp/z)", tmp_path)
    assert result.risk == "high"
    assert result.outside is True
    assert any(p.endswith("/tmp/z") for p in result.affected_paths)


# 功能：验证进程替换按中风险处理
# 设计：<(echo hi) 会产生临时输入，但内层安全
def test_process_substitution_is_medium_risk() -> None:
    result = analyze_command("cat <(echo hi)")
    assert result.risk == "medium"
    assert result.outside is False


# 功能：验证进程替换会继承内层路径
# 设计：<(/bin/cat /etc/passwd) 的越界路径必须被记录
def test_process_substitution_propagates_paths(tmp_path: Path) -> None:
    result = analyze_command("cat <(/bin/cat /etc/passwd)", tmp_path)
    assert result.outside is True
    assert any(p.endswith("/etc/passwd") for p in result.affected_paths)


# 功能：验证过深的嵌套替换降级为高风险
# 设计：递归超过 3 层无法可靠展开，保守按高风险处理
def test_deep_nested_substitution_is_high() -> None:
    result = analyze_command("echo $(echo $(echo $(echo $(ls))))")
    assert result.risk == "high"
    assert result.force_ask is True


# 功能：验证只读的 sh -c 内联命令按中风险处理
# 设计：内层可解析且安全，不应因使用 sh -c 就判为高风险
def test_safe_shell_inline_is_medium_risk() -> None:
    result = analyze_command("sh -c 'echo hi'")
    assert result.risk == "medium"
    assert result.outside is False


# 功能：验证 shell 内联脚本会继承内层越界路径
# 设计：sh -c 'cat /etc/passwd' 的路径必须被识别
def test_shell_inline_propagates_outside(tmp_path: Path) -> None:
    result = analyze_command("sh -c 'cat /etc/passwd'", tmp_path)
    assert result.outside is True
    assert any(p.endswith("/etc/passwd") for p in result.affected_paths)


# 功能：验证合并短旗标的内联写法也能被识别
# 设计：sh -ec 'cmd' / python -xc 'code' 的脚本参数仍需分析
@pytest.mark.parametrize(
    ("command", "risk"),
    [("sh -ec 'cat /etc/passwd'", "medium"), ("python -xc 'print(1)'", "high")],
)
def test_combined_inline_flags_are_detected(command: str, risk: str) -> None:
    assert analyze_command(command).risk == risk


# 功能：验证 shell 内联的危险命令提升为高风险
# 设计：bash -c 'rm -rf /tmp/z' 的风险由内层决定
def test_shell_inline_propagates_write_risk(tmp_path: Path) -> None:
    result = analyze_command("bash -c 'rm -rf /tmp/z'", tmp_path)
    assert result.risk == "high"
    assert result.outside is True


# 功能：验证无参数 shell 被视为高风险
# 设计：交互式 shell 可执行任意后续命令，无法静态分析
def test_shell_without_args_is_high() -> None:
    assert analyze_command("bash").risk == "high"


# 功能：验证解释器内联代码被判为高风险
# 设计：-c/-e/-r 内容无法静态展开，保守按高风险处理
@pytest.mark.parametrize(
    "command",
    ["python -c 'print(1)'", "perl -e 'print 1'", "node -e 'x'"],
)
def test_inline_interpreter_is_high_risk(command: str) -> None:
    assert analyze_command(command).risk == "high"


# 功能：验证解释器执行脚本文件不按内联处理
# 设计：python script.py 是常规用法，不应直接判为高风险
def test_interpreter_script_file_is_not_inline() -> None:
    assert analyze_command("python script.py").risk == "low"


# ── 高危程序与写操作 ──────────────────────────────────────────────────────────

# 功能：验证 rm 的递归 / 强制变体为高风险
# 设计：-r、-f 及合并写法 -rf 都满足 T3 的高风险规则
@pytest.mark.parametrize("command", ["rm -rf build", "rm -r build", "rm -f build"])
def test_rm_destructive_flags_are_high(command: str) -> None:
    result = analyze_command(command)
    assert result.risk == "high"
    assert result.force_ask is True


# 功能：验证普通 rm 为可缓存的中风险
# 设计：在工作区内删除普通文件不需要每次 ASK
def test_rm_without_flags_is_medium() -> None:
    result = analyze_command("rm build")
    assert result.risk == "medium"
    assert result.force_ask is False


# 功能：验证 chmod 777 为高风险
# 设计：开放全部权限属于高危操作
@pytest.mark.parametrize("mode", ["777", "0777"])
def test_chmod_777_is_high(mode: str) -> None:
    assert analyze_command(f"chmod {mode} x").risk == "high"


# 功能：验证普通 chmod 为可缓存的中风险
# 设计：非 777 的权限变更不需要强制 ASK
def test_chmod_normal_mode_is_medium() -> None:
    assert analyze_command("chmod 644 x").risk == "medium"


# 功能：验证无条件高危程序被识别
# 设计：sudo/kill/xargs/dd 等即使参数安全也按高风险处理
@pytest.mark.parametrize(
    "command",
    ["sudo ls", "kill 1", "xargs rm", "dd if=/dev/zero of=/tmp/x"],
)
def test_always_high_programs(command: str) -> None:
    result = analyze_command(command)
    assert result.risk == "high"
    assert result.force_ask is True


# 功能：验证 find 的 -exec/-ok 变体为高风险
# 设计：find 可对任意文件执行任意命令
@pytest.mark.parametrize("flag", ["-exec", "-execdir", "-ok", "-okdir"])
def test_find_exec_is_high(flag: str) -> None:
    assert analyze_command(f"find . {flag} rm {{}} ;").risk == "high"


# 功能：验证普通 find 查询为低风险
# 设计：只读遍历不影响文件内容
def test_find_without_exec_is_low() -> None:
    assert analyze_command("find . -name x").risk == "low"


# 功能：验证写操作命令在工作区内为可缓存的中风险
# 设计：mv/touch/mkdir/sed 等写命令需要审批，但不必每次 ASK
@pytest.mark.parametrize(
    "command",
    ["mv a b", "touch new.txt", "mkdir -p x", "sed -i 's/a/b/' x"],
)
def test_write_commands_inside_are_medium(command: str, tmp_path: Path) -> None:
    result = analyze_command(command, tmp_path)
    assert result.risk == "medium"
    assert result.outside is False
    assert result.force_ask is False


# 功能：验证写操作命令指向工作区外时为高风险
# 设计：touch /tmp/x 越界写入必须强制 ASK
def test_write_command_outside_is_high(tmp_path: Path) -> None:
    result = analyze_command("touch /tmp/x", tmp_path)
    assert result.risk == "high"
    assert result.outside is True
    assert any(p.endswith("/tmp/x") for p in result.affected_paths)


# 功能：验证已知路径旗标的 =value 会被扫描
# 设计：--output=/tmp/x 明确指向文件，必须纳入越界判定
def test_known_path_flag_value_is_scanned(tmp_path: Path) -> None:
    result = analyze_command("cat --output=/tmp/x", tmp_path)
    assert result.outside is True
    assert any(p.endswith("/tmp/x") for p in result.affected_paths)


# 功能：验证未知旗标的 =value 不会被误判为路径
# 设计：--grep=/fix 是模式参数，不是文件路径
def test_unknown_flag_value_is_not_path(tmp_path: Path) -> None:
    result = analyze_command("grep --grep=/fix x", tmp_path)
    assert result.outside is False
    assert result.affected_paths == ()


# ── git 子命令风险 ────────────────────────────────────────────────────────────

# 功能：验证 git 的高风险子命令
# 设计：push、reset --hard、clean -f 会改写远端或不可恢复地丢弃改动
@pytest.mark.parametrize(
    "command",
    ["git push", "git reset --hard", "git clean -f"],
)
def test_git_high_risk(command: str) -> None:
    result = analyze_command(command)
    assert result.risk == "high"
    assert result.force_ask is True


# 功能：验证 git 的中风险子命令
# 设计：会修改仓库状态但可撤销/可恢复
@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'x'",
        "git add a.txt",
        "git checkout main",
        "git switch -c x",
        "git merge a",
        "git rebase main",
        "git cherry-pick a",
        "git stash",
        "git tag v1",
    ],
)
def test_git_medium_risk(command: str) -> None:
    result = analyze_command(command)
    assert result.risk == "medium"
    assert result.force_ask is False


# 功能：验证 git 只读子命令为低风险
# 设计：status/log/reset --soft/clean -n 不破坏工作区
@pytest.mark.parametrize(
    "command",
    ["git status", "git log", "git reset --soft", "git clean -n"],
)
def test_git_readonly_is_low(command: str) -> None:
    assert analyze_command(command).risk == "low"


# 功能：验证 git 的 -C 参数值不会被子命令探测误取
# 设计：git -C /tmp status 的 pattern 仍应是 git status
def test_git_flag_value_skipped_in_pattern() -> None:
    assert analyze_command("git -C /tmp status").pattern == "git status"


# 功能：验证没有子命令时 pattern 退化为程序名
# 设计：git --version 只有旗标，没有可用的子命令
def test_git_without_subcommand_uses_program() -> None:
    assert analyze_command("git --version").pattern == "git"


# ── 解析失败 ──────────────────────────────────────────────────────────────────

# 功能：验证无法解析的命令按高风险兜底
# 设计：引号未闭合等语法错误无法静态分析，必须强制 ASK
def test_unparseable_command_is_high() -> None:
    result = analyze_command("echo 'unterminated")
    assert result.risk == "high"
    assert result.outside is True
    assert result.force_ask is True
    assert result.pattern == "echo 'unterminated"


# 功能：验证解析失败时仍尽量提取路径用于展示
# 设计：命令语法错误但绝对路径信息可独立识别
def test_unparseable_command_collects_paths(tmp_path: Path) -> None:
    result = analyze_command("cat /etc/passwd && echo '", tmp_path)
    assert result.risk == "high"
    assert any(p.endswith("/etc/passwd") for p in result.affected_paths)


# ── 文件工具分析 ──────────────────────────────────────────────────────────────

# 功能：验证 bash 工具委托给命令分析
# 设计：analyze_tool("bash") 与 analyze_command 结果一致
def test_bash_tool_delegates(tmp_path: Path) -> None:
    result = analyze_tool("bash", {"command": "rm -rf build"}, tmp_path)
    assert result.risk == "high"
    assert result.pattern == "rm"


# 功能：验证缺失或非字符串 path 返回默认结果
# 设计：参数非法时不猜测目标，交由工具自身校验
@pytest.mark.parametrize("params", [{}, {"path": 1}])
def test_tool_without_valid_path_returns_default(params: dict) -> None:
    result = analyze_tool("read_file", params)
    assert result == analyze_tool("read_file", {})
    assert result.pattern == "*"


# 功能：验证 write_file 工作区内写入为中风险，pattern 为精确绝对路径
# 设计：普通写文件需要审批但可缓存；精确路径限制授权范围
def test_write_file_inside_is_medium(tmp_path: Path) -> None:
    result = analyze_tool("write_file", {"path": "a.txt"}, tmp_path)
    assert result.risk == "medium"
    assert result.pattern == str((tmp_path / "a.txt").resolve())
    assert result.force_ask is False


# 功能：验证 write_file 越界为目标高风险
# 设计：`../escape.txt` 落在工作区外，必须强制 ASK 且不可缓存
def test_write_file_outside_is_high(tmp_path: Path) -> None:
    result = analyze_tool("write_file", {"path": "../escape.txt"}, tmp_path)
    assert result.risk == "high"
    assert result.outside is True
    assert result.force_ask is True


# 功能：验证写入 .git / .ssh 等敏感目录为高风险
# 设计：即使位于工作区内，改写仓库元数据或密钥目录也需要强确认
@pytest.mark.parametrize("path", [".git/config", ".ssh/id_ed25519", ".aws/credentials"])
def test_write_file_sensitive_dir_is_high(path: str, tmp_path: Path) -> None:
    result = analyze_tool("edit_file", {"path": path}, tmp_path)
    assert result.risk == "high"
    assert result.force_ask is True


# 功能：验证写入敏感文件名为高风险
# 设计：.env / id_rsa 等命名约定直接暴露凭据
@pytest.mark.parametrize("path", [".env", "id_rsa", "authorized_keys"])
def test_write_file_sensitive_name_is_high(path: str, tmp_path: Path) -> None:
    result = analyze_tool("write_file", {"path": path}, tmp_path)
    assert result.risk == "high"
    assert result.force_ask is True


# 功能：验证工作区内只读文件工具为低风险
# 设计：read_file / list_dir 在工作区内无需 ASK
@pytest.mark.parametrize(
    ("tool", "path"),
    [("read_file", "README.md"), ("list_dir", ".")],
)
def test_read_tool_inside_is_low(tool: str, path: str, tmp_path: Path) -> None:
    result = analyze_tool(tool, {"path": path}, tmp_path)
    assert result.risk == "low"
    assert result.outside is False


# 功能：验证越界只读为中等风险但不额外强制 ASK
# 设计：read_file 自身会拒绝越界路径，权限层只需展示风险，不重复打扰
@pytest.mark.parametrize("tool", ["read_file", "list_dir"])
def test_read_tool_outside_is_medium_without_force(tool: str, tmp_path: Path) -> None:
    result = analyze_tool(tool, {"path": "../secret.txt"}, tmp_path)
    assert result.risk == "medium"
    assert result.outside is True
    assert result.force_ask is False


# 功能：验证文件工具的影响路径为解析后的精确绝对路径
# 设计：审批展示需要真实目标路径，而非相对写法
def test_file_tool_records_resolved_path(tmp_path: Path) -> None:
    result = analyze_tool("write_file", {"path": "sub/a.txt"}, tmp_path)
    assert result.affected_paths == (str((tmp_path / "sub/a.txt").resolve()),)
