# 手机端 Terminal 快捷键编辑增强 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：功能设计

## 背景

手机端 Terminal 的 ShortcutGrid 已有 4×4 默认快捷键网格和编辑对话框。当前编辑对话框分两层：预设列表选择 + 自定义手写转义序列。问题：

1. **自定义体验差**：用户需要手写转义序列（如 `\x1b`、`\x03`），大多数人不知道怎么写
2. **预设覆盖有限**：缺少 Ctrl+O、Shift+Tab 等常用快捷键，预设列表不够用时只能手写

## 设计方案

### 核心思路

用**组合选择器**替代预设列表，作为转义序列的辅助输入工具。输入框保持原始转义序列格式不变，选择器负责帮用户"打出"记不住的转义字符。

### 对话框布局

```
┌──────────────────────────────────┐
│  编辑快捷键                        │
│                                    │
│  显示名称                          │
│  ┌────────────────────────────┐   │
│  │ Ctrl+A cls                 │   │
│  └────────────────────────────┘   │
│                                    │
│  按键序列                     [⌫] │
│  ┌────────────────────────────┐   │
│  │ \x01cls\r                  │   │
│  └────────────────────────────┘   │
│                                    │
│  [Ctrl] [Alt] [Shift]              │
│                                    │
│  A B C D E F G H I J K L M        │
│  N O P Q R S T U V W X Y Z        │
│  0 1 2 3 4 5 6 7 8 9              │
│  ──────────────────────────────   │
│  Esc Tab Enter Space Back Del      │
│  ↑ ↓ ← → Home End PgUp PgDn      │
│  Ins F1 F2 F3 F4 F5 F6            │
│  F7 F8 F9 F10 F11 F12             │
│                                    │
│  [清空此格]              [确定]    │
└──────────────────────────────────┘
```

### 交互规则

**选择器操作**：
- **修饰键**（Ctrl/Alt/Shift）：toggle 开关，可多选
- **基础键**：点击立即追加对应转义序列到输入框末尾，修饰键自动清除
- **⌫ 按钮**：删除输入框末尾最后一个按键（按转义序列边界识别）

**输入框**：
- 显示原始转义序列，如 `\x01cls\r`
- 用户也可以直接手动编辑（兜底方案）

**操作示例**（目标：Ctrl+A → Backspace → cls → Enter）：

| 步骤 | 操作 | 输入框变化 | 说明 |
|------|------|-----------|------|
| 1 | 勾 [Ctrl] | （无变化） | 等待基础键 |
| 2 | 点 A | `\x01` | Ctrl+A，Ctrl 自动取消 |
| 3 | 点 Back | `\x01\x7f` | Backspace |
| 4 | 点 C | `\x01\x7fc` | 字面字符 c |
| 5 | 点 L | `\x01\x7fcl` | 字面字符 l |
| 6 | 点 S | `\x01\x7fcls` | 字面字符 s |
| 7 | 点 Enter | `\x01\x7fcls\r` | 回车 |
| 8 | 点 [确定] | 保存 | |

**纠错**（第4步点错成 D）：

| 步骤 | 操作 | 输入框变化 |
|------|------|-----------|
| 4' | 点 D | `\x01\x7fd` |
| 5' | 点 ⌫ | `\x01\x7f` |
| 6' | 点 C | `\x01\x7fc` |

### 转义序列映射

**Ctrl+letter**：`\x01`~`\x1a`（letter ASCII - 64）
- Ctrl+A = `\x01`, Ctrl+C = `\x03`, Ctrl+O = `\x0f`, ...
- Ctrl+[ = `\x1b`, Ctrl+\\ = `\x1c`, Ctrl+] = `\x1d`

**Alt+key**：`\x1b` + key
- Alt+B = `\x1bb`, Alt+F = `\x1bf`, Alt+. = `\x1b.`, ...

**Shift 组合**：
- Shift+Tab = `\x1b[Z`
- Shift+letter = 大写字母（字面字符）

**功能键**：
| 键 | 序列 | 键 | 序列 |
|----|------|----|------|
| Esc | `\x1b` | Tab | `\t` |
| Enter | `\r` | Space | （空格） |
| Back | `\x7f` | Del | `\x1b[3~` |
| ↑ | `\x1b[A` | ↓ | `\x1b[B` |
| ← | `\x1b[D` | → | `\x1b[C` |
| Home | `\x1b[H` | End | `\x1b[F` |
| PgUp | `\x1b[5~` | PgDn | `\x1b[6~` |
| Ins | `\x1b[2~` | | |
| F1 | `\x1bOP` | F2 | `\x1bOQ` |
| F3 | `\x1bOR` | F4 | `\x1bOS` |
| F5 | `\x1b[15~` | F6 | `\x1b[17~` |
| F7 | `\x1b[18~` | F8 | `\x1b[19~` |
| F9 | `\x1b[20~` | F10 | `\x1b[21~` |
| F11 | `\x1b[23~` | F12 | `\x1b[24~` |

**裸字母/数字**（无修饰键）：字面字符本身

### ⌫ 删除逻辑

从末尾向前识别一个完整按键并删除：

1. `\x1b[<数字>~` — CSI 功能键序列（如 `\x1b[3~`）
2. `\x1bO<letter>` — SS3 功能键序列（如 `\x1bOP`）
3. `\x1b[<letter>` — CSI 方向键序列（如 `\x1b[A`）
4. `\x1b<char>` — Alt 组合（如 `\x1bb`）
5. `\x??` — 单字节转义（如 `\x01`、`\x7f`）
6. `\t`、`\r`、`\n` — 特殊转义
7. 普通字符 — 单个字符

### 与现有代码的变更

| 文件 | 变更 |
|------|------|
| `ShortcutEditDialog.tsx` | 删除 PRESETS 列表和自定义双 input，替换为组合选择器 |
| `ShortcutGrid.tsx` | 无变更（ShortcutSlot 接口不变：`{label, sequence}`）|
| `index.css` | 新增选择器网格样式 |

数据结构 `ShortcutSlot { label: string; sequence: string }` 保持不变，兼容已有 localStorage 数据。

### 默认布局调整

针对 Claude Code 使用场景，调整默认 4×4 布局。替换不常用的 Ctrl+E、Ctrl+L、Ctrl+A、Ctrl+Z，加入 Shift+Tab、Ctrl+O、Ctrl+T、Ctrl+B。

**新默认布局**：

```
┌───────┬───────┬───────┬───────┐
│  Esc  │  Tab  │ S+Tab │ Back  │
├───────┼───────┼───────┼───────┤
│ Ct+T  │ Ct+D  │ Ct+O  │  Del  │
├───────┼───────┼───────┼───────┤
│ Ct+B  │ Ct+C  │   ↑   │ Enter │
├───────┼───────┼───────┼───────┤
│   ⚙   │   ←   │   ↓   │   →   │
└───────┴───────┴───────┴───────┘
```

**变更对比**：

| 位置 | 旧 | 新 | 原因 |
|------|-----|-----|------|
| (0,2) | Ctrl+E | Shift+Tab | Claude Code 切换权限模式 |
| (1,0) | Ctrl+A | Ctrl+T | Claude Code 切换任务列表（行首在手机端用输入框替代） |
| (1,2) | Ctrl+L | Ctrl+O | Claude Code 切换详细输出（清屏不常用） |
| (2,0) | Ctrl+Z | Ctrl+B | Claude Code 后台运行（SIGTSTP 对 Claude Code 无意义） |

**代码变更**：修改 `ShortcutGrid.tsx` 的 `DEFAULT_SLOTS` 数组。

**兼容性**：已自定义过布局的用户不受影响（localStorage 有数据时不读默认值）。

## 关键参考

### 源码
- `mutbot/frontend/src/mobile/ShortcutGrid.tsx` — 快捷键网格组件，DEFAULT_SLOTS 定义默认布局
- `mutbot/frontend/src/mobile/ShortcutEditDialog.tsx` — 编辑对话框，当前 PRESETS + 自定义模式
- `mutbot/frontend/src/mobile/MobileLayout.tsx` — 手机端布局，集成 ShortcutGrid

### 相关规范
- `docs/specifications/bugfix-mobile-terminal-switch-and-pin-resize.md` — 移动端 Terminal 相关修复

## 实施步骤清单

### Phase 1: 默认布局更新 [待开始]

- [ ] **Task 1.1**: 更新 `ShortcutGrid.tsx` 的 `DEFAULT_SLOTS`
  - [ ] Ctrl+E → Shift+Tab (`\x1b[Z`)
  - [ ] Ctrl+A → Ctrl+T (`\x14`)
  - [ ] Ctrl+L → Ctrl+O (`\x0f`)
  - [ ] Ctrl+Z → Ctrl+B (`\x02`)
  - 状态：⏸️ 待开始

### Phase 2: 组合选择器替代预设列表 [待开始]

- [ ] **Task 2.1**: 重写 `ShortcutEditDialog.tsx`
  - [ ] 删除 PRESETS 列表和自定义双 input 模式
  - [ ] 实现修饰键 toggle（Ctrl/Alt/Shift）
  - [ ] 实现基础键网格（字母、数字、功能键）
  - [ ] 点击基础键追加转义序列到输入框末尾，修饰键自动清除
  - [ ] 保留显示名称和按键序列两个输入框（可手动编辑）
  - 状态：⏸️ 待开始

- [ ] **Task 2.2**: 实现 ⌫ 删除逻辑
  - [ ] 按转义序列边界从末尾删除一个按键
  - [ ] 覆盖 CSI 序列、SS3 序列、Alt 组合、单字节转义、特殊转义、普通字符
  - 状态：⏸️ 待开始

### Phase 3: 样式 [待开始]

- [ ] **Task 3.1**: 新增组合选择器 CSS 样式
  - [ ] 修饰键 toggle 按钮样式（选中/未选中）
  - [ ] 基础键网格布局和按钮样式
  - [ ] ⌫ 按钮样式
  - [ ] 适配手机端屏幕宽度
  - 状态：⏸️ 待开始

### Phase 4: 验证 [待开始]

- [ ] **Task 4.1**: 构建前端并手机端验证
  - [ ] `npm --prefix mutbot/frontend run build`
  - [ ] 验证默认布局显示正确
  - [ ] 验证编辑对话框组合选择器功能
  - [ ] 验证 ⌫ 删除逻辑
  - [ ] 验证已有 localStorage 自定义布局不受影响
  - 状态：⏸️ 待开始
