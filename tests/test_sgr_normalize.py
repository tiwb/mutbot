"""_normalize_sgr_subparams 单元测试。

验证 pyte SGR 冒号子参数预处理函数的正确性。
"""

from mutbot.ptyhost._manager import _normalize_sgr_subparams


class TestUnderlineSubparams:
    """SGR 4:N — underline 样式子参数。"""

    def test_underline_off(self):
        assert _normalize_sgr_subparams("\x1b[4:0m") == "\x1b[24m"

    def test_underline_single(self):
        assert _normalize_sgr_subparams("\x1b[4:1m") == "\x1b[4m"

    def test_underline_double(self):
        assert _normalize_sgr_subparams("\x1b[4:2m") == "\x1b[4m"

    def test_underline_curly(self):
        assert _normalize_sgr_subparams("\x1b[4:3m") == "\x1b[4m"

    def test_underline_dotted(self):
        assert _normalize_sgr_subparams("\x1b[4:4m") == "\x1b[4m"

    def test_underline_dashed(self):
        assert _normalize_sgr_subparams("\x1b[4:5m") == "\x1b[4m"


class TestColorSubparams:
    """SGR 38/48 — 前景/背景色冒号格式。"""

    def test_fg_rgb_no_cs(self):
        # 38:2:R:G:B（无 colorspace）→ 38;2;R;G;B
        assert _normalize_sgr_subparams("\x1b[38:2:255:100:50m") == "\x1b[38;2;255;100;50m"

    def test_fg_rgb_with_cs(self):
        # 38:2:CS:R:G:B（含 colorspace ID）→ 38;2;R;G;B
        assert _normalize_sgr_subparams("\x1b[38:2:0:255:100:50m") == "\x1b[38;2;255;100;50m"

    def test_bg_rgb_no_cs(self):
        assert _normalize_sgr_subparams("\x1b[48:2:10:20:30m") == "\x1b[48;2;10;20;30m"

    def test_bg_rgb_with_cs(self):
        assert _normalize_sgr_subparams("\x1b[48:2:1:10:20:30m") == "\x1b[48;2;10;20;30m"

    def test_fg_256(self):
        # 38:5:N → 38;5;N
        assert _normalize_sgr_subparams("\x1b[38:5:196m") == "\x1b[38;5;196m"

    def test_bg_256(self):
        assert _normalize_sgr_subparams("\x1b[48:5:42m") == "\x1b[48;5;42m"


class TestUnderlineColor:
    """SGR 58 — underline color，应被删除。"""

    def test_underline_color_rgb(self):
        # 58:2:R:G:B → 整组删除，只剩空 SGR
        assert _normalize_sgr_subparams("\x1b[58:2:255:0:0m") == "\x1b[m"

    def test_underline_color_256(self):
        assert _normalize_sgr_subparams("\x1b[58:5:196m") == "\x1b[m"

    def test_underline_color_with_other_params(self):
        # 4;58:2:255:0:0m → 58 组删除，保留 4
        assert _normalize_sgr_subparams("\x1b[4;58:2:255:0:0m") == "\x1b[4m"


class TestMixedParams:
    """含冒号和不含冒号参数的混合序列。"""

    def test_mixed_underline_and_color(self):
        # bold + curly underline + fg rgb
        assert _normalize_sgr_subparams("\x1b[1;4:3;38:2:255:0:0m") == "\x1b[1;4;38;2;255;0;0m"

    def test_mixed_with_underline_off(self):
        # underline off + red fg
        assert _normalize_sgr_subparams("\x1b[4:0;31m") == "\x1b[24;31m"

    def test_reset_not_affected(self):
        assert _normalize_sgr_subparams("\x1b[0m") == "\x1b[0m"

    def test_standard_sgr_passthrough(self):
        # 不含冒号的标准 SGR 不受影响
        assert _normalize_sgr_subparams("\x1b[1;31;42m") == "\x1b[1;31;42m"

    def test_semicolon_color_passthrough(self):
        # 分号格式的 RGB 不受影响
        assert _normalize_sgr_subparams("\x1b[38;2;255;100;50m") == "\x1b[38;2;255;100;50m"


class TestGenericSubparams:
    """其他含冒号的参数 — 保留主参数。"""

    def test_unknown_param_with_subparam(self):
        assert _normalize_sgr_subparams("\x1b[9:1m") == "\x1b[9m"


class TestNonSGR:
    """非 SGR 序列不受影响。"""

    def test_cursor_position(self):
        assert _normalize_sgr_subparams("\x1b[10;20H") == "\x1b[10;20H"

    def test_private_mode(self):
        assert _normalize_sgr_subparams("\x1b[?25h") == "\x1b[?25h"

    def test_text_around_sgr(self):
        text = "Hello\x1b[4:3mWorld\x1b[4:0mEnd"
        assert _normalize_sgr_subparams(text) == "Hello\x1b[4mWorld\x1b[24mEnd"

    def test_no_escape(self):
        assert _normalize_sgr_subparams("plain text") == "plain text"

    def test_empty(self):
        assert _normalize_sgr_subparams("") == ""


class TestCSIGtStripping:
    """CSI > 私有序列 — 应被完整剥离。

    pyte 的 _parser_fsm 遇到 > 时执行 pass（静默忽略），但后续参数仍被
    正常解析并分派。导致 CSI > 4 ; 2 m（modifyOtherKeys）被误读为
    SGR 4（underline ON），cursor.attrs.underscore 被卡死。
    """

    def test_modify_other_keys_enable(self):
        # CSI > 4 ; 2 m — 启用 modifyOtherKeys mode 2
        assert _normalize_sgr_subparams("\x1b[>4;2m") == ""

    def test_modify_other_keys_reset(self):
        # CSI > 4 m — 重置 modifyOtherKeys
        assert _normalize_sgr_subparams("\x1b[>4m") == ""

    def test_csi_gt_with_surrounding_text(self):
        text = "before\x1b[>4;2mafter"
        assert _normalize_sgr_subparams(text) == "beforeafter"

    def test_csi_gt_other_final_char(self):
        # CSI > Ps c — Secondary DA，也应被剥离
        assert _normalize_sgr_subparams("\x1b[>1c") == ""

    def test_csi_gt_does_not_affect_normal_sgr(self):
        # CSI > 被剥离但正常 SGR 保留
        text = "\x1b[>4;2m\x1b[1;31mHello\x1b[0m"
        assert _normalize_sgr_subparams(text) == "\x1b[1;31mHello\x1b[0m"

    def test_csi_gt_mixed_with_colon_subparams(self):
        # 两个缺陷同时出现
        text = "\x1b[>4;2m\x1b[4:3mHello\x1b[4:0m"
        assert _normalize_sgr_subparams(text) == "\x1b[4mHello\x1b[24m"
