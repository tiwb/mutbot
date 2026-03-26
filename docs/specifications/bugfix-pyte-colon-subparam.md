# pyte CSI 冒号子参数兼容性修复 设计规范

**状态**：✅ 已完成
**日期**：2026-03-26
**类型**：Bug修复

## 需求

1. 在 mutbot 终端中运行 Claude Code 等现代终端应用时，出现大量多余下划线
2. 退出应用后 `cls` 清屏无法消除下划线（cursor attrs 被卡住）
3. 部分 SGR 子参数内容（如 `3m`、`2:255:100:50m`）被当作纯文本输出到屏幕

## 关键参考

- `mutbot/src/mutbot/ptyhost/_manager.py:294-365` — `_flush_and_feed()`，pyte stream.feed 入口
- `mutbot/src/mutbot/ptyhost/_manager.py:403-442` — `_do_render_term()`，增量渲染
- `mutbot/src/mutbot/ptyhost/_screen.py` — `_SafeHistoryScreen`，已有的 pyte 定制
- `mutbot/src/mutbot/ptyhost/ansi_render.py` — `render_dirty/render_full/render_lines`
- [selectel/pyte#179](https://github.com/selectel/pyte/issues/179) — pyte 官方 issue：SGR subparameters 不支持
- [selectel/pyte#180](https://github.com/selectel/pyte/pull/180) — 官方 PR（未合并，挂了 1.5 年）

## 设计方案

### 根因分析

pyte 0.8.2 有两个 CSI 解析缺陷，均导致终端属性状态被破坏：

**缺陷 1（主因）：CSI `>` 前缀被静默忽略**

pyte 的 `_parser_fsm` 遇到 `>` 时执行 `pass`（本意跳过 Secondary DA），但后续参数仍被
正常解析并分派。Claude Code 启动时发送 `CSI > 4 ; 2 m`（启用 modifyOtherKeys 键盘协议），
pyte 忽略 `>` 后将 `4;2m` 当作 SGR 处理 → `select_graphic_rendition(4, 2)` → underline ON。
退出时发送 `CSI > 4 m`（重置），pyte 再次误读为 SGR 4 → underline ON。
cursor.attrs.underscore 永久为 True，`cls` 清屏也无法恢复。

**缺陷 2（潜在）：冒号子参数不支持**

CSI 解析器不识别 `:` 作为 ITU T.416 子参数分隔符，遇到 `:` 时直接中断 CSI 解析，导致：

1. **属性卡死**：`\x1b[4:0m`（underline OFF）被中断，无法关闭下划线
2. **文本污染**：冒号后内容被当作纯文本输出（`\x1b[4:3m` → `3m` 显示为可见文字）

涉及的现代 SGR 子参数格式：

| 序列 | 含义 | pyte 现状 |
|------|------|----------|
| `CSI > 4 ; 2 m` | modifyOtherKeys 启用 | `>` 忽略，误读为 SGR 4（underline ON） |
| `CSI > 4 m` | modifyOtherKeys 重置 | 同上 |
| `4:0` | underline off | CSI 中断，属性卡死 |
| `4:1`~`4:5` | underline 样式 | CSI 中断 |
| `58:2:R:G:B` | underline RGB 颜色 | CSI 中断 + 文本污染 |
| `58:5:N` | underline 256色 | CSI 中断 + 文本污染 |

### 长远考虑

pyte 0.8.2 是 PyPI 最新版，项目维护不活跃。我们已遇到 2 个 pyte bug（Variation Selectors、
本次 CSI 解析缺陷）。此外性能测试发现 pyte feed 93KB 数据耗时 285ms，存在性能瓶颈。
长远看需要完全重写终端状态机，同时解决兼容性和性能问题。
pyte 采用 LGPL v3 许可，vendor 进来有许可证约束，因此重写时需要全新实现。

本次采用最小侵入的绕过方案，不修改 pyte 代码。

### 修复策略：CSI 预处理

在 `_flush_and_feed()` 中 `term.stream.feed(text)` 之前，预处理终端输出，
绕过 pyte 的两个 CSI 解析缺陷。

**第一步：剥离 CSI > 私有序列**

正则 `\x1b\[>[0-9;]*[a-zA-Z]` 匹配所有 `CSI >` 开头的序列并整体删除。
pyte 不支持任何 `CSI >` 序列，剥离不影响终端功能。

**第二步：归一化 SGR 冒号子参数**

仅处理 SGR 序列（以 `m` 结尾的 CSI），对每个 `;` 分隔的参数组，如果含 `:`，按主参数分类处理：

| 输入模式 | 转换结果 | 理由 |
|---------|---------|------|
| `4:0` | `24` | underline off（标准 SGR 24） |
| `4:N`（N≥1） | `4` | underline on（pyte 不支持样式区分） |
| `38:2[:CS]:R:G:B` | `38;2;R;G;B` | 前景色 RGB，转为 pyte 支持的分号格式 |
| `38:5:N` | `38;5;N` | 前景色 256，转分号格式 |
| `48:2[:CS]:R:G:B` | `48;2;R;G;B` | 背景色 RGB，同上 |
| `48:5:N` | `48;5;N` | 背景色 256，同上 |
| `58:...` | 删除 | underline color，pyte 无对应属性 |
| `其他X:Y:...` | `X` | 保留主参数，丢弃子参数 |

注：`38:2` / `48:2` 的标准格式含可选 colorspace ID（`38:2:CS:R:G:B`），
部分终端省略 CS 直接写 `38:2:R:G:B`。转换时按子参数个数判断：
4 个子参数 = 有 CS（取后 3 个为 RGB），3 个子参数 = 无 CS（直接取 RGB）。

**实现方式**：正则匹配 `\x1b\[([0-9:;]*)m`，对匹配到的参数字符串做归一化。
放在独立函数 `_normalize_sgr_subparams(text)` 中，方便单元测试。

**分帧风险**：预处理在 `_flush_and_feed` 中执行，数据经过 16ms 静默期累积，
SGR 序列极短（通常 <30 字节），被截断的概率可忽略。

### 性能观察

测试中发现 pyte feed 93KB 数据耗时 285ms，存在性能瓶颈。
未来重写终端状态机时需同时解决兼容性和性能问题。

## 实施步骤清单

- [x] 在 `_manager.py` 中实现 `_normalize_sgr_subparams()` 函数，含详细的根因注释
- [x] 在 `_flush_and_feed()` 中调用预处理函数（`stream.feed` 之前）
- [x] 编写 `_normalize_sgr_subparams` 单元测试（32 项，含 CSI > 和冒号子参数）
- [x] 重启 ptyhost 验证修复效果
