"""pyte 定制屏幕 + TermView 抽象。

_SafeHistoryScreen: 修复 pyte 上游 resize bug，简化 after_event。
TermView: 一个终端的视口，拥有独立 scroll_offset。
"""

from __future__ import annotations

from dataclasses import dataclass

import pyte
import pyte.modes as mo


class _SafeHistoryScreen(pyte.HistoryScreen):
    """定制 pyte HistoryScreen：

    保留 before_event 确保 feed 时 screen 在底部。
    简化 after_event（我们不使用 prev_page/next_page 做滚动，
    而是直接从 history.top 读取历史行渲染）。
    """

    def after_event(self, event: str) -> None:
        # 只保留光标可见性管理，去掉 prev_page/next_page 的行宽修剪
        self.cursor.hidden = not (
            self.history.position == self.history.size
            and mo.DECTCEM in self.mode
        )

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        # pyte 上游 bug：resize() 收缩行数时，restore_cursor 在 self.lines
        # 更新前执行，导致光标未被 clamp 到新范围。
        super().resize(lines, columns)
        if self.cursor.y >= self.lines:
            self.cursor.y = self.lines - 1
        if self.cursor.x >= self.columns:
            self.cursor.x = self.columns - 1


@dataclass
class TermView:
    """终端视口——独立的滚动位置。

    offset 模型：bottom-relative。
    offset=0 表示 live（看实时屏幕），offset=N 表示从底部往上 N 行。
    """
    id: str           # view_id
    term_id: str
    scroll_offset: int = 0   # 0 = live, >0 = scrolled
