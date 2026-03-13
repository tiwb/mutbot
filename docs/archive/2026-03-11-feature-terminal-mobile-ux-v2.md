# Terminal 移动端 UX 迭代（V2） 设计规范

**状态**：✅ 已完成
**日期**：2026-03-11
**类型**：功能设计

## 背景

在上一轮移动端终端 UX 增强（输入栏 + 快捷键面板 + 触摸滚动）基础上，继续优化交互体验：

1. Terminal session 图标风格不佳，需更换
2. 输入栏按钮使用文本符号（↵、▲/▼），风格不够统一，需改为图标
3. 发送按钮缺少单行/多行输入模式切换
4. 快捷键编辑模式缺少拖动排序功能
5. 快捷键设置按钮（⚙）使用带颜色的 emoji，与 UI 风格不一致
6. 设置按钮功能单一，需扩展为菜单
7. 默认快捷键布局不够直观，需优化为更贴近电脑键盘的排列

## 设计方案

### 一、Terminal Session 图标更换

将 terminal session 的默认图标从 `terminal` 改为 `square-terminal`（Lucide 图标）。

**修改位置**：`SessionIcons.tsx` 中 `KIND_FALLBACK` 映射表，将 `terminal: "terminal"` 改为 `terminal: "square-terminal"`。

### 二、输入栏按钮图标化

将输入栏的三个按钮从文本符号改为 Lucide 图标：

| 按钮 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 发送 | `↵` | `corner-down-left` | 回车/发送图标 |
| 展开快捷键 | `▲` | `chevron-up` | 向上展开 |
| 收起快捷键 | `▼` | `chevron-down` | 向下收起 |

图标尺寸：18px，颜色跟随按钮文本色。

**长按触发选择**：发送按钮支持长按触发模式切换菜单（见方案三）。长按阈值 500ms，触发时给触觉反馈（`navigator.vibrate`）。

### 三、单行/多行输入模式

**发送按钮长按**弹出选择菜单，可切换「单行模式」和「多行模式」：

#### 单行模式（默认，当前行为）
- 输入框为 `<input type="text">`
- 软键盘 Return 键 = 发送消息
- 空内容时点击发送 = 发送纯回车（`\r`）
- 发送按钮图标：`corner-down-left`

#### 多行模式
- 输入框切换为 `<textarea>`
- **自动增高**：初始 1 行高度，随内容输入自动增高，最大 4 行；超过 4 行后出现滚动条
  - 实现方式：每次 onChange 时重置 `scrollHeight` 再设 `style.height`，配合 `max-height` 限制
- 软键盘 Return 键 = 换行（输入框内换行）
- 只有**点击发送按钮**才会发送内容
- 空内容时点击发送 = 发送纯回车（`\r`）
- 发送按钮图标切换为 `send`（区分模式）

**输入栏布局**：输入栏使用 `align-items: flex-end`，确保发送按钮和 ▲/▼ 按钮始终与输入框**底部对齐**。输入框增高时，按钮保持在底部不动。

**模式持久化**：存入 localStorage。

**长按菜单样式**：小型弹出菜单（popover），出现在按钮上方，包含两个选项：
- 「单行输入」— 带 `corner-down-left` 图标
- 「多行输入」— 带 `send` 图标
- 当前选中项高亮

### 四、快捷键拖动交换

编辑模式下，支持**拖动快捷键交换位置**：

- 长按某个快捷键格位开始拖动（区分于点击编辑）
- 拖动到另一个格位上松手，两个格位的内容互换
- 一次拖动只影响 2 个快捷键的位置
- ⚙ 按钮始终占据最后一行左下角，不参与拖动。其位置为动态计算：`(rows - 1) * cols`
- 拖动过程中，目标格位高亮提示
- 使用 touch 事件实现（移动端为主），同时兼容 mouse 事件

**实现方式**：不使用拖拽库，用原生 touch/mouse 事件：
- `touchstart`/`mousedown`（长按 300ms 后激活拖动）
- `touchmove`/`mousemove`（跟踪手指/鼠标位置，高亮目标格位）
- `touchend`/`mouseup`（在目标格位释放，执行交换）

### 五、设置按钮图标替换

将快捷键面板左下角的 ⚙（emoji）替换为 Lucide 图标 `settings`（单色 SVG，与 UI 风格一致）。

编辑模式下的「保存」文字保持不变（功能明确，无需图标化）。

图标尺寸：16px，颜色：`var(--text-dim)`。

### 六、设置按钮菜单扩展

将设置按钮的行为从「直接进入编辑模式」改为「弹出菜单」：

点击 `settings` 图标后弹出菜单（popover），选项：
1. **编辑快捷键** — 进入当前的编辑模式（现有功能）
2. **网格大小** — 弹窗中分别设置行数和列数（各一个 stepper，最小 1，无上限）。修改后自动调整布局数组长度：扩大时新增空槽位；缩小时保留前 `rows*cols - 1` 个有效快捷键（排除 ⚙ 槽位后按顺序截断）。⚙ 按钮位置动态计算为 `(rows-1)*cols`（最后一行左下角）。网格尺寸持久化到 localStorage
3. **恢复默认** — 重置快捷键布局和网格大小为默认值（4x4 + DEFAULT_LAYOUT），需二次确认

**菜单样式**：与发送按钮的长按菜单风格统一，出现在按钮上方。

**编辑模式下**：设置按钮仍然显示「保存」，点击直接保存退出编辑模式（不弹菜单）。

### 七、默认快捷键布局优化

优化默认 4x4 布局，使排列更贴近电脑键盘习惯：

**当前布局**：
```
┌─────┬─────┬─────┬─────┐
│ Esc │ Tab │Back │ Del │
├─────┼─────┼─────┼─────┤
│Ct+C │Ct+D │Ct+Z │Ct+L │
├─────┼─────┼─────┼─────┤
│Ct+A │Ct+E │  ↑  │Enter│
├─────┼─────┼─────┼─────┤
│ ⚙  │  ←  │  ↓  │  →  │
└─────┴─────┴─────┴─────┘
```

**新布局**：
```
┌─────┬─────┬─────┬─────┐
│ Esc │ Tab │Ct+E │Back │
├─────┼─────┼─────┼─────┤
│Ct+A │Ct+D │Ct+L │ Del │
├─────┼─────┼─────┼─────┤
│Ct+Z │Ct+C │  ↑  │Enter│
├─────┼─────┼─────┼─────┤
│ ⚙  │  ←  │  ↓  │  →  │
└─────┴─────┴─────┴─────┘
```

**变更点**：
- 第一行：Esc、Tab 不变；Ctrl+E 放在第三列（行尾跳转，高频）；Back 保持右上角
- 第二行：Ctrl+A、Ctrl+D、Ctrl+L 为常用编辑快捷键；Del 放在 Back 正下方（删除键纵向对齐）
- 第三行：Ctrl+Z、Ctrl+C 放在左侧（undo/interrupt 常用组合），↑ 和 Enter 保持不变
- 第四行：不变（⚙ + 方向键 ← ↓ →）

**注意**：此修改只影响 `DEFAULT_LAYOUT` 常量。已自定义布局的用户不受影响（localStorage 中已有保存）。「恢复默认」功能（设计方案六）将使用此新布局。

## 关键参考

### 源码
- `frontend/src/mobile/TerminalInput.tsx` — 输入栏（统一 textarea + 单行/多行模式 + 长按菜单）
- `frontend/src/mobile/ShortcutGrid.tsx` — 动态网格（可变行列 + Settings 图标 + 拖动交换预览）
- `frontend/src/mobile/ShortcutEditDialog.tsx` — 快捷键编辑弹窗
- `frontend/src/mobile/MobileLayout.tsx` — 移动端布局（设置菜单 + 网格大小弹窗 + GridSizeDialog）
- `frontend/src/components/SessionIcons.tsx` — 图标系统（terminal → square-terminal）
- `frontend/src/index.css:3788+` — 移动端终端相关样式
- `src/mutbot/session.py:146` — 后端 TerminalSession.display_icon

### 图标库
- lucide-react ^0.575.0 — 已确认可用图标：`SquareTerminal`、`Send`、`SendHorizontal`、`CornerDownLeft`、`ChevronUp`、`ChevronDown`、`Settings`、`GripVertical`

### 上一轮规范
- `docs/specifications/feature-terminal-mobile-ux.md` — V1 已完成

## 实施步骤清单

### Phase 1: 简单替换 [✅ 已完成]

- [x] **Task 1.1**: Session 图标更换
  - `SessionIcons.tsx` — `KIND_FALLBACK` 中 `terminal` 值改为 `"square-terminal"`
  - 状态：✅ 已完成

- [x] **Task 1.2**: 默认快捷键布局更新
  - `ShortcutGrid.tsx` — 更新 `DEFAULT_SLOTS` 数组顺序为新布局
  - 状态：✅ 已完成

### Phase 2: 输入栏重构 — 图标化 + 单行/多行模式 [✅ 已完成]

- [x] **Task 2.1**: 输入栏按钮图标化
  - 状态：✅ 已完成

- [x] **Task 2.2**: 单行/多行输入模式切换
  - 状态：✅ 已完成

- [x] **Task 2.3**: 发送按钮长按菜单
  - 状态：✅ 已完成

- [x] **Task 2.4**: 输入栏 CSS 调整
  - 状态：✅ 已完成

### Phase 3: 快捷键网格动态化 — 可变行列 + ⚙ 动态位置 [✅ 已完成]

- [x] **Task 3.1**: ShortcutGrid 动态行列支持
  - 状态：✅ 已完成

- [x] **Task 3.2**: MobileLayout 适配动态网格
  - 状态：✅ 已完成

### Phase 4: 设置按钮改造 — 图标 + 菜单 [✅ 已完成]

- [x] **Task 4.1**: 设置按钮图标替换
  - 状态：✅ 已完成

- [x] **Task 4.2**: 设置按钮菜单（编辑/网格大小/恢复默认）
  - 状态：✅ 已完成

### Phase 5: 拖动交换 [✅ 已完成]

- [x] **Task 5.1**: 编辑模式下拖动交换快捷键
  - 状态：✅ 已完成

- [x] **Task 5.2**: 拖动相关 CSS
  - 状态：✅ 已完成

### Phase 6: 构建验证 [✅ 已完成]

- [x] **Task 6.1**: 前端构建 + 基本验证
  - `npm --prefix frontend run build` 编译通过
  - 状态：✅ 已完成

### Phase 7: 测试修复 [✅ 已完成]

- [x] **Task 7.1**: 后端 terminal 图标未生效
  - `session.py` 中 `TerminalSession.display_icon` 也需改为 `"square-terminal"`（后端声明优先级高于前端 KIND_FALLBACK）
  - 状态：✅ 已完成

- [x] **Task 7.2**: 设置菜单点击无效
  - `.mobile-terminal-input-panel` 缺少 `position: relative`，绝对定位菜单飘出屏幕
  - 状态：✅ 已完成

- [x] **Task 7.3**: 多行 textarea 自适应不生效
  - 加 `box-sizing: border-box` + `min-height`，高度重置用 `"0"` 替代 `"auto"`
  - 状态：✅ 已完成

- [x] **Task 7.4**: 单行/多行输入框统一为 textarea
  - 两种模式外观一致（都自动增高），仅 Enter 行为不同（单行=发送，多行=换行）
  - 状态：✅ 已完成

- [x] **Task 7.5**: 按钮高度与输入框不对齐
  - 发送和展开按钮加 `min-height` + `box-sizing: border-box` + `font-size: 16px` 匹配 textarea 单行高度
  - 状态：✅ 已完成

- [x] **Task 7.6**: 拖动快捷键时触发页面滚动 + 松手不执行交换 + 无拖动预览
  - `touch-action: none` 移到所有 `.shortcut-grid-btn`（始终阻止滚动）
  - 拖动目标改用 ref 跟踪（避免 stale closure），松手正确执行交换
  - 拖动中实时预览交换效果（`displayLayout` 渲染交换后的布局）
  - 状态：✅ 已完成

## 测试验证

- 前端构建通过（tsc + vite build）
- 手机端测试发现 6 个问题，全部修复
