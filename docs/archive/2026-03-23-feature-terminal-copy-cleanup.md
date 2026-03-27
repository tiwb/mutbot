# 终端拷贝格式整理 设计规范

**状态**：✅ 已完成
**日期**：2026-03-23
**类型**：功能设计

## 背景

终端拷贝（Ctrl+C 选区 / 右键菜单 Copy）直接使用 xterm.js 的 `getSelection()` 返回文本，存在两个格式问题：

1. **行末空格**：终端每行固定宽度（如 108 列），短内容后面填充空格，拷贝出来尾部有大量空格
2. **软折行变硬换行**：长文本因终端宽度折到下一行，`getSelection()` 在折行处插入 `\n`，拷贝结果多出不该有的换行

## 设计方案

### 核心思路

在前端拷贝时，利用 xterm.js Buffer API 的 `IBufferLine.isWrapped` 属性区分软折行和硬换行，对拷贝文本做后处理：

1. **去行末空格**：每行 `trimEnd()`
2. **合并软折行**：如果下一行 `isWrapped === true`，当前行与下一行之间不插入换行符

### 关于 pyte 后端

pyte 0.8.2 **不跟踪折行状态**（调研确认）：
- `Screen.buffer` 每行是纯字符字典，无 `isWrapped` 元数据
- `draw()` 中 DECAWM 触发折行时直接 `CR + LF`，不在新行上做标记

但这不影响实现——pyte 渲染的 ANSI 输出发到 xterm.js 后，xterm.js 自己会根据内容是否超出列宽正确标记 `isWrapped`。**实现完全在前端侧，不需要后端参与。**

### 实现位置

`TerminalPanel.tsx` 中有两个拷贝入口，都需要处理：

1. **Ctrl+C 快捷键**（第 429-434 行）：`copyToClipboard(sel)` — 这里 `sel` 来自 `term.getSelection()`
2. **右键菜单 Copy**（第 680-691 行）：`copyTermSelection()` — 同样用 `term.getSelection()`

思路是提取一个公共函数 `getCleanSelection(term: Terminal): string`，在两个入口中替换 `term.getSelection()`。

### getCleanSelection 算法

```
function getCleanSelection(term: Terminal): string {
  1. 获取选区范围：term.getSelectionPosition() → { start: {x, y}, end: {x, y} }
     - 如果无选区返回空字符串
  2. 获取 buffer：term.buffer.active
  3. 遍历选区内的每一行 (start.y → end.y)：
     a. 获取 bufferLine = buffer.getLine(y)
     b. 获取该行文本（translateToString，注意起止列）
     c. trimEnd() 去尾部空格
     d. 检查下一行的 isWrapped：
        - 如果 nextLine.isWrapped === true → 当前行末尾不加换行
        - 否则 → 加 \n
  4. 拼接返回
}
```

### 边界情况

| 场景 | 处理 |
|------|------|
| 选区起止不在行首/行尾 | 首行从 start.x 开始，末行到 end.x 结束，中间行取完整行 |
| 软折行中间部分被选中 | 同样合并，因为 isWrapped 是行属性，与选区无关 |
| 去掉行末空格后行为空 | 保留空行（它是真正的空行） |
| 合并后行尾有空格 | 软折行的前一行是满列的，trimEnd 后可能去掉了最后的空格。但终端满列折行时最后一列一定有字符，不会是空格，所以不影响 |
| scrollback 区域的选区 | xterm.js buffer 包含 scrollback，`getSelectionPosition()` 返回的 y 是 buffer 绝对行号，直接用即可 |

### trimEnd 策略

- **硬换行的行**（下一行 `isWrapped === false` 或无下一行）：`trimEnd()` 去尾部空格
- **软折行的前一行**（下一行 `isWrapped === true`）：**不做 trimEnd**，保留满列原始内容（避免误删终端应用故意写入的尾部空格）

## 关键参考

### 源码
- `mutbot/frontend/src/panels/TerminalPanel.tsx:406-412` — `copyToClipboard()` 函数
- `mutbot/frontend/src/panels/TerminalPanel.tsx:429-434` — Ctrl+C 拷贝入口
- `mutbot/frontend/src/panels/TerminalPanel.tsx:680-691` — 右键菜单 `copyTermSelection()`
- `mutbot/frontend/src/panels/TerminalPanel.tsx:31-41` — `execCopy()` fallback

### xterm.js API
- `Terminal.getSelection()` — 返回选中文本（纯文本，不含格式信息）
- `Terminal.getSelectionPosition()` — 返回选区起止坐标 `{start: {x, y}, end: {x, y}}`
- `Terminal.buffer.active` — 当前活动 buffer
- `IBuffer.getLine(y)` — 获取指定行
- `IBufferLine.isWrapped` — 该行是否是上一行的软折行延续
- `IBufferLine.translateToString(trimRight?, startCol?, endCol?)` — 行内容转文本

## 实施步骤清单

- [x] **Task 1**: 实现 `getCleanSelection(term)` 函数
  - [x] 在 `TerminalPanel.tsx` 顶部（`execCopy` 附近）添加函数
  - [x] 利用 `getSelectionPosition()` + buffer API + `isWrapped` 实现算法
  - [x] 处理边界情况（首行/末行部分选区、无选区）
  - 状态：✅ 已完成

- [x] **Task 2**: 替换两个拷贝入口
  - [x] Ctrl+C 路径：`term.getSelection()` → `getCleanSelection(term)`
  - [x] 右键菜单路径：`term.getSelection()` → `getCleanSelection(term)`
  - 状态：✅ 已完成

- [x] **Task 3**: 构建验证
  - [x] `npm --prefix mutbot/frontend run build` 确认编译通过
  - 状态：✅ 已完成
