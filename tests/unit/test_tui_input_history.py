from __future__ import annotations

from mini_claude.tui.widgets import ChatTextArea


# 功能：↑ 依次回到更早的输入，↓ 越过最新一条后恢复进入历史前编辑的草稿
# 设计：直接调用历史导航方法（无 DOM 依赖），断言文本、边界停留与草稿恢复
def test_history_navigation_restores_draft() -> None:
    area = ChatTextArea()
    area._remember("first")
    area._remember("second")
    area.load_text("draft")

    assert area._history_back() is True
    assert area.text == "second"
    assert area._history_back() is True
    assert area.text == "first"

    # 已到最早一条：保持内容且事件仍被消费，避免光标跳动
    assert area._history_back() is True
    assert area.text == "first"

    assert area._history_forward() is True
    assert area.text == "second"
    assert area._history_forward() is True
    assert area.text == "draft"
    # 草稿状态不再可继续前进
    assert area._history_forward() is False


# 功能：连续重复输入只记录一次，提交后导航状态复位
# 设计：重复 remember 同一内容，再从历史中返回后提交新内容，断言列表与前进行为
def test_remember_skips_consecutive_duplicates_and_resets_navigation() -> None:
    area = ChatTextArea()
    area._remember("a")
    area._remember("a")
    area._remember("b")
    assert area._history == ["a", "b"]

    area.load_text("tmp")
    assert area._history_back() is True
    area._remember("c")
    assert area._history == ["a", "b", "c"]
    assert area._history_forward() is False


# 功能：载入多行历史时把光标放到末行末尾
# 设计：记录包含换行的输入，回溯后断言 text 与 cursor_location
def test_multiline_history_cursor_moves_to_end() -> None:
    area = ChatTextArea()
    area._remember("line1\nline2")

    assert area._history_back() is True
    assert area.text == "line1\nline2"
    assert area.cursor_location == (1, len("line2"))


# 功能：没有历史时导航不消费按键，交由 TextArea 处理
# 设计：空实例上调用 back/forward，断言返回 False
def test_navigation_without_history_is_not_handled() -> None:
    area = ChatTextArea()
    assert area._history_back() is False
    assert area._history_forward() is False
