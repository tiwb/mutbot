"""pyte 定制屏幕 + TermView 抽象。

_SafeHistoryScreen: 修复 pyte 上游 resize bug，简化 after_event。
TermView: 一个终端的视口，拥有独立 scroll_offset。
"""

from __future__ import annotations

from dataclasses import dataclass

import pyte
import pyte.modes as mo
from wcwidth import wcwidth

# Variation Selectors (U+FE00-FE0F)。pyte 0.8.2 的 draw() 遇到这些字符时
# 会走 else:break 分支，中断整个 draw 循环丢弃后续字符。
# 上游已知问题（PR #201 已合并到 master 但未发版），我们在 draw() 中处理。
# 参考：https://github.com/selectel/pyte/pull/201
_VARIATION_SELECTORS = set(chr(c) for c in range(0xFE00, 0xFE10))


class _SafeHistoryScreen(pyte.HistoryScreen):
    """定制 pyte HistoryScreen：

    保留 before_event 确保 feed 时 screen 在底部。
    简化 after_event（我们不使用 prev_page/next_page 做滚动，
    而是直接从 history.top 读取历史行渲染）。
    支持 DEC Mode 2026（Synchronized Update）。
    修复 pyte 不识别 Variation Selectors 的问题。
    """

    # DEC Private Mode 2026 — Synchronized Update
    synchronized: bool = False

    def draw(self, data: str) -> None:
        """覆写 draw() 处理 Variation Selectors。

        pyte 0.8.2 的 draw() 对 wcwidth=0 且 combining=0 的字符执行
        ``else: break``，丢弃同批次所有后续字符。VS16 (U+FE0F) 等
        Variation Selectors 恰好满足此条件。

        处理策略：
        - 剥离 VS 防止 pyte break
        - VS16 跟在 wcwidth=1 的字符后面时，将该字符提升为 width-2
          （终端中 VS16 表示 emoji presentation，显示宽度为 2）
        """
        if not _VARIATION_SELECTORS.intersection(data):
            super().draw(data)
            return

        # 分段处理：在 VS 位置切开，逐段 feed 给 super().draw()
        i = 0
        batch_start = 0
        while i < len(data):
            if data[i] in _VARIATION_SELECTORS:
                # 先 flush 积累的普通字符
                if batch_start < i:
                    super().draw(data[batch_start:i])

                # VS16: 将前一个 width-1 字符提升为 width-2
                if data[i] == "\uFE0F":
                    self._promote_to_wide()

                # 跳过 VS 本身（不 feed 给 pyte）
                i += 1
                batch_start = i
            else:
                i += 1

        # flush 剩余
        if batch_start < len(data):
            super().draw(data[batch_start:])

    def _promote_to_wide(self) -> None:
        """将光标前一个 width-1 字符提升为 width-2（添加占位符）。"""
        x = self.cursor.x
        if x == 0:
            return
        line = self.buffer[self.cursor.y]
        prev = line[x - 1]
        if prev.data and wcwidth(prev.data) == 1 and x < self.columns:
            line[x] = self.cursor.attrs._replace(data="")
            self.cursor.x = min(x + 1, self.columns)

    def after_event(self, event: str) -> None:
        # 只保留光标可见性管理，去掉 prev_page/next_page 的行宽修剪
        self.cursor.hidden = not (
            self.history.position == self.history.size
            and mo.DECTCEM in self.mode
        )

    def set_mode(self, *modes: int, **kwargs: bool) -> None:
        if kwargs.get("private") and 2026 in modes:
            self.synchronized = True
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs: bool) -> None:
        if kwargs.get("private") and 2026 in modes:
            self.synchronized = False
        super().reset_mode(*modes, **kwargs)

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
    viewport_rows: int = 0   # 0 = 使用 screen.lines（全屏），>0 = 独立视口行数
    viewport_cols: int = 0   # 0 = 使用 screen.columns（全宽），>0 = 独立视口列数
