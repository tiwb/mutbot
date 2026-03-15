"""pyte Screen → ANSI 转义序列渲染。

提供两个核心函数：
- render_dirty(screen)  — 只渲染 dirty lines（增量更新）
- render_full(screen)   — 全屏渲染（连接快照）
"""

from __future__ import annotations

import pyte


# pyte 命名色 → SGR 前景色编号
_FG_NAMED: dict[str, int] = {
    "black": 30, "red": 31, "green": 32, "brown": 33,
    "blue": 34, "magenta": 35, "cyan": 36, "white": 37,
    "light_black": 90, "light_red": 91, "light_green": 92,
    "light_brown": 93, "light_blue": 94, "light_magenta": 95,
    "light_cyan": 96, "light_white": 97,
}

# pyte 命名色 → SGR 背景色编号
_BG_NAMED: dict[str, int] = {
    "black": 40, "red": 41, "green": 42, "brown": 43,
    "blue": 44, "magenta": 45, "cyan": 46, "white": 47,
    "light_black": 100, "light_red": 101, "light_green": 102,
    "light_brown": 103, "light_blue": 104, "light_magenta": 105,
    "light_cyan": 106, "light_white": 107,
}


def _sgr_params_for_char(char: pyte.screens.Char) -> list[int]:
    """为单个字符生成 SGR 参数列表。"""
    params: list[int] = []

    # 属性
    if char.bold:
        params.append(1)
    if char.italics:
        params.append(3)
    if char.underscore:
        params.append(4)
    if char.strikethrough:
        params.append(9)
    if char.reverse:
        params.append(7)

    # 前景色
    fg = char.fg
    if fg and fg != "default":
        if fg in _FG_NAMED:
            params.append(_FG_NAMED[fg])
        elif len(fg) == 6:
            # 6 位 hex → RGB
            r, g, b = int(fg[0:2], 16), int(fg[2:4], 16), int(fg[4:6], 16)
            params.extend([38, 2, r, g, b])

    # 背景色
    bg = char.bg
    if bg and bg != "default":
        if bg in _BG_NAMED:
            params.append(_BG_NAMED[bg])
        elif len(bg) == 6:
            r, g, b = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)
            params.extend([48, 2, r, g, b])

    return params


def _char_sgr_key(char: pyte.screens.Char) -> tuple:
    """返回字符的属性签名，用于检测属性变化。"""
    return (char.fg, char.bg, char.bold, char.italics,
            char.underscore, char.strikethrough, char.reverse)


def _render_line(screen: pyte.Screen, row: int) -> str:
    """渲染一行为 ANSI 序列（光标定位 + 内容 + 行尾清除）。"""
    line = screen.buffer[row]
    cols = screen.columns
    parts: list[str] = []
    # 光标定位到行首（1-based）
    parts.append(f"\x1b[{row + 1};1H")

    # HistoryScreen 滚动后 buffer 行可能是普通 dict（非 StaticDefaultDict），
    # 缺失的 key 需要用默认空白字符填充
    default_char = screen.default_char

    prev_key: tuple = ()
    for col in range(cols):
        char = line.get(col, default_char) if isinstance(line, dict) else line[col]
        # 跳过宽字符占位列（pyte 在宽字符后紧跟一个 data="" 的占位 Char）
        if char.data == "":
            continue
        key = _char_sgr_key(char)
        if key != prev_key:
            sgr = _sgr_params_for_char(char)
            if sgr:
                parts.append(f"\x1b[0;{';'.join(str(p) for p in sgr)}m")
            else:
                parts.append("\x1b[0m")
            prev_key = key
        parts.append(char.data or " ")

    # 重置属性 + 清除行尾（处理行内容短于终端宽度的情况）
    parts.append("\x1b[0m\x1b[K")
    return "".join(parts)


def render_dirty(screen: pyte.Screen) -> bytes:
    """渲染 dirty lines 为 ANSI 帧，清空 dirty set。

    返回空 bytes 表示无变化。
    """
    if not screen.dirty:
        return b""

    parts: list[str] = []
    # 隐藏光标，避免渲染过程中光标闪烁
    parts.append("\x1b[?25l")

    for row in sorted(screen.dirty):
        if 0 <= row < screen.lines:
            parts.append(_render_line(screen, row))

    # 恢复光标位置并显示
    cx, cy = screen.cursor.x, screen.cursor.y
    parts.append(f"\x1b[{cy + 1};{cx + 1}H")
    parts.append("\x1b[?25h")

    screen.dirty.clear()
    return "".join(parts).encode("utf-8")


def render_lines(lines: list, cols: int, default_char: "pyte.screens.Char") -> bytes:
    """渲染任意行列表为全屏 ANSI 帧（用于滚动视图）。

    lines: list of line dicts (pyte buffer row or history row)
    cols: 终端列数
    default_char: 默认空白字符
    """
    parts: list[str] = []
    parts.append("\x1b[?25l")  # 隐藏光标

    for row_idx, line in enumerate(lines):
        # 光标定位到行首
        parts.append(f"\x1b[{row_idx + 1};1H")
        prev_key: tuple = ()
        for col in range(cols):
            char = line.get(col, default_char) if isinstance(line, dict) else line[col]
            if char.data == "":
                continue
            key = _char_sgr_key(char)
            if key != prev_key:
                sgr = _sgr_params_for_char(char)
                if sgr:
                    parts.append(f"\x1b[0;{';'.join(str(p) for p in sgr)}m")
                else:
                    parts.append("\x1b[0m")
                prev_key = key
            parts.append(char.data or " ")
        parts.append("\x1b[0m\x1b[K")

    # 隐藏光标（滚动浏览时不显示光标）
    parts.append(f"\x1b[{len(lines)};1H")

    return "".join(parts).encode("utf-8")


def render_full(screen: pyte.Screen) -> bytes:
    """全屏渲染为 ANSI（清屏 + 所有行），用于连接快照。"""
    parts: list[str] = []
    # 重置 + 清屏 + 光标归位
    parts.append("\x1b[0m\x1b[2J\x1b[H")
    # 隐藏光标
    parts.append("\x1b[?25l")

    for row in range(screen.lines):
        parts.append(_render_line(screen, row))

    # 恢复光标位置并显示
    cx, cy = screen.cursor.x, screen.cursor.y
    parts.append(f"\x1b[{cy + 1};{cx + 1}H")
    parts.append("\x1b[?25h")

    return "".join(parts).encode("utf-8")
