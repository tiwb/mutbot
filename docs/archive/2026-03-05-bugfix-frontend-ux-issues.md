# 前端 UX 问题修复 设计规范

**状态**：✅ 已完成
**日期**：2026-03-05
**类型**：Bug修复

## 背景

前端多处 UX 交互问题需要修复，涉及链接渲染、Session 列表交互、右键菜单、LLM 配置向导等。

## 问题清单与设计方案

### B1: config-update 说明中的 HTTP 链接无法点击

**根因**：`ToolCallCard.tsx:455-458` 的 `hint` 组件直接用 `String(schema.text)` 渲染纯文本，URL 不被解析。

**方案**：写一个 `linkify` 函数，正则匹配 `https?://...` 并拆分为 `(string | ReactElement)[]`，在 hint 组件中替代纯文本渲染。

---

### B2: Session 列表双击重命名容易误触

**根因**：`SessionListPanel.tsx:390` 的 `onDoubleClick={() => startRename(s.id)}`。

**方案**：移除该事件绑定。

---

### B3: Tab 标题栏双击重命名移除

**根因**：flexlayout 默认 `tabEnableRename: true`，双击 Tab 触发编辑。

**方案**：在 `lib/layout.ts` 的 global 配置中添加 `tabEnableRename: false`。保留右键菜单重命名。

---

### B4: 聊天区域非 Message 部分右键报错

**根因**：`MessageList.tsx:151` 的 `getMsgRole(msg!)` 对 null 使用 `!` 断言导致崩溃。

**方案**：安全处理 null：`(msg ? getMsgRole(msg) : null) ?? ""`。

---

### B5: 非 Message 区域右键应显示 Session 系统菜单

**根因**：B4 修复后非消息区域右键不再崩溃，但 `message_role` 为空时现有菜单项（如 Copy Markdown）通过 `check_visible()` 隐藏，导致菜单为空。

**方案**：复用 `MessageList/Context` category，现有 CopySelection/SelectAll 不限 role 已经可见。需要确认这两项在 role 为空时是否仍然有意义——Copy Selection 有意义，Select All 也可以。B4 修复后即可正常工作。

---

### B6: Edit Provider / Select Models 页面问题

**6a 根因**：`_select_provider_models()` 标题硬编码为 `"Select Models"`。
**6b 根因**：`ui.show()` 内部调用 `wait_event(type="submit")`，只接受 submit 事件，cancel 事件被静默丢弃，导致 Back 按钮无响应。（`context_impl.py:109`）
**6c**：`_edit_provider()` 仅支持修改模型列表。

**方案**：
- 6a：`_select_provider_models()` 增加 `provider_name` 参数，标题改为 `"Select Models — {name}"`
- 6b：修改 `ui.show()` 使其也接收 cancel 事件，收到 cancel 时返回 `None`（或抛出 `CancelledError`），调用方据此处理
- 6c：`_edit_provider()` 中增加编辑选项页（Edit Models / Change API Key）

---

### B7: 默认模型选择应使用下拉列表

**根因**：前端 `select` 组件只有 button card 渲染路径，无 dropdown 模式。后端使用 `layout: "vertical"` + `scrollable`。

**方案**：
- 后端：Default Model 的 select 组件使用 `layout: "dropdown"` 标记
- 前端：UIComponent 的 select 分支增加 `layout === "dropdown"` 渲染路径，使用原生 `<select>` 元素

---

### B9: 长参数撑满聊天框

**根因**：`.tool-arg-value` CSS 使用 `white-space: nowrap` + `overflow-x: auto`，导致水平滚动。

**方案**：改为 `overflow: hidden; text-overflow: ellipsis`。

---

### B10: 参数弹出框背景透明

**根因**：`.arg-modal` 使用 `background: var(--bg-card)`，但 `--bg-card` 在 CSS 中从未定义。

**方案**：改为 `background: var(--bg-tool)`（已定义为 `#252526`，适合卡片/弹框背景）。

---

### B11: Config-llm 选择区域布局问题

**根因**：`.ui-select-cards` 同时设置了 `flex-wrap: wrap`（基础样式）和 `flex-direction: column`（vertical 模式）。当 scrollable 限制了 `max-height` 后，column + wrap 导致内容分列横向溢出，表现为横向滚动而非竖向滚动。同时 `min-height: 200px` 在只有少量按钮时撑高容器，`flex: 1` 导致按钮拉伸填满。

**方案**：
- `.ui-select-cards.vertical` 添加 `flex-wrap: nowrap`，确保竖向排列不分列
- `.ui-select-cards.vertical .ui-select-card` 设置 `flex: none`，按钮不拉伸
- `.ui-select-cards.scrollable` 移除 `min-height: 200px`，仅保留 `max-height`

---

### B12: Config-llm 无法被 Stop

**根因**：`session.run_setup` RPC 通过 `sm.start()` 创建 bridge 并调用 `request_tool("Config-llm")`，但 WS handler 中的本地 `bridge` 变量仍为 `None`（延迟启动场景下未赋值）。用户点击 Stop 时，cancel 处理分支检查 `if bridge:` 为 False，cancel 被静默忽略。

**方案**（跨三层修复）：
- **后端路由**：cancel 和 run_tool 的 WS 消息处理中，`bridge` 为 None 时 fallback 到 `sm.get_bridge(session_id)` 获取外部创建的 bridge
- **mutagent dispatch**：`tool_set_impl.py` 的 `dispatch()` 增加 `except asyncio.CancelledError` 分支，设置 `tool_call.status = "done"` 后 re-raise，确保工具状态正确
- **前端**：`agent_cancelled` 事件处理中同时清除 `uiView: null`，确保交互式 UI 立即消失

---

### B13: fetch_error 提示位置不当

**根因**：`_edit_provider()` 中 fetch_error 的 warning badge 追加在 Models 选择列表之后，用户需要滚动才能看到错误。

**方案**：将 fetch_error badge 移到 Models select 组件之前，优先展示错误信息。

---

### B14: Provider 列表交互优化

**根因**：Provider 列表页需先选中 provider 再点 Edit/Delete 按钮，操作步骤多。

**方案**：
- Provider 列表使用 `auto_submit`，点击直接进入编辑页面
- 主列表移除 Edit/Delete 按钮，仅保留 Add 和 Done
- Done 按钮改用 action type `"done"`（避免与 auto_submit 的 submit 事件冲突）
- Delete 按钮移入编辑页，删除确认取消后 `continue` 回到编辑循环
- `_delete_provider()` 返回 `bool` 表示是否实际删除
- 返回列表后不预选 provider（移除 `selected_provider` 参数和默认 `value`）

---

### B15: 编辑页残留调试日志

**根因**：`_edit_provider()` 的 refresh 流程中有 3 条 `logger.debug` 调试语句，开发完成后未清理。

**方案**：移除 3 条 debug 日志。

---

## 实施步骤清单

### 阶段一：简单修复（纯删除/CSS 修改）[✅ 已完成]

- [x] **Task 1.1**: B2 — 移除 Session 列表双击重命名
  - [x] `SessionListPanel.tsx:390` 删除 `onDoubleClick={() => startRename(s.id)}`
  - 状态：✅ 已完成

- [x] **Task 1.2**: B3 — 禁用 Tab 双击重命名
  - [x] `lib/layout.ts` global 配置中添加 `tabEnableRename: false`
  - 状态：✅ 已完成

- [x] **Task 1.3**: B4 — 修复非 Message 区域右键崩溃
  - [x] `MessageList.tsx:151` 修改 `getMsgRole(msg!)` 为安全访问
  - 状态：✅ 已完成

- [x] **Task 1.4**: B9 — 修复长参数显示过宽
  - [x] `index.css` 的 `.tool-arg-value` 改为 `overflow: hidden; text-overflow: ellipsis`
  - 状态：✅ 已完成

- [x] **Task 1.5**: B10 — 修复弹出框背景透明
  - [x] `index.css` 的 `.arg-modal` 改为 `background: var(--bg-tool)`
  - 状态：✅ 已完成

### 阶段二：小功能添加 [✅ 已完成]

- [x] **Task 2.1**: B7 — 前端 select 组件添加 dropdown 渲染模式
  - [x] `ToolCallCard.tsx` select case 中添加 `layout === "dropdown"` 渲染原生 `<select>`
  - [x] `index.css` 添加 `.ui-select-dropdown` 样式
  - 状态：✅ 已完成

- [x] **Task 2.2**: B7 — 后端 Default Model 改用 dropdown
  - [x] `config_toolkit.py` Default Model 组件改为 `layout: "dropdown"`，移除 `scrollable`
  - 状态：✅ 已完成

### 阶段三：B6 配置向导修复 [✅ 已完成]

- [x] **Task 3.1**: B6b — 修复 `ui.show()` 不响应 cancel 事件
  - [x] `context.py` 返回类型改为 `dict | None`
  - [x] `context_impl.py` `show()` 不限 type，cancel 返回 `None`
  - [x] `config_toolkit.py` 所有 `ui.show()` 调用方检查 None
  - [x] `toolkit.py` 的 `UIShowToolkit.show()` 处理 None
  - 状态：✅ 已完成

- [x] **Task 3.2**: B6a — Select Models 标题显示 provider 名称
  - [x] `_select_provider_models()` 增加 `provider_name` 参数
  - [x] 标题改为 `f"Select Models — {provider_name}"`
  - [x] 所有调用处传入 provider_name
  - 状态：✅ 已完成

- [x] **Task 3.3**: B6c — Edit provider 支持修改 API Key
  - [x] `_edit_provider()` 增加编辑选项页（Edit Models / Change API Key）
  - [x] Change API Key 流程：输入新 Key → 保存到 pconf
  - 状态：✅ 已完成

### 阶段四：Config-llm 布局与交互修复 [✅ 已完成]

- [x] **Task 4.1**: B11 — 修复 vertical scrollable select 布局
  - [x] `.ui-select-cards.vertical` 添加 `flex-wrap: nowrap`
  - [x] `.ui-select-cards.vertical .ui-select-card` 设置 `flex: none`
  - [x] `.ui-select-cards.scrollable` 移除 `min-height: 200px`
  - 状态：✅ 已完成

- [x] **Task 4.2**: B12 — 修复 Config-llm 无法被 Stop
  - [x] `routes.py` cancel/run_tool 消息处理 fallback `sm.get_bridge(session_id)`
  - [x] `tool_set_impl.py` dispatch 增加 `CancelledError` 处理
  - [x] `AgentPanel.tsx` agent_cancelled 清除 `uiView: null`
  - 状态：✅ 已完成

- [x] **Task 4.3**: B13 — fetch_error badge 移到 Models 列表上方
  - [x] `config_toolkit.py` `_edit_provider()` 调整 fetch_error badge 位置
  - 状态：✅ 已完成

- [x] **Task 4.4**: B14 — Provider 列表交互优化
  - [x] Provider 列表改用 `auto_submit` 点击直接编辑
  - [x] 主列表移除 Edit/Delete 按钮，Done 改为 action type `"done"`
  - [x] Delete 移入 `_edit_provider()` 操作栏
  - [x] `_delete_provider()` 返回 `bool`，取消时 `continue` 回编辑页
  - [x] 移除 `selected_provider` 参数，返回列表后不预选
  - 状态：✅ 已完成

- [x] **Task 4.5**: B15 — 清理 `_edit_provider()` 中 3 条 debug 日志
  - 状态：✅ 已完成

## 测试验证

- TypeScript 编译通过（`npx tsc --noEmit` 无错误）
- Python 语法验证通过（`ast.parse` 全部 OK）
- Pyright 无新增错误（已有的 `reportAttributeAccessIssue` 为既有问题）

## 关键参考

### 源码
- `mutbot/frontend/src/components/ToolCallCard.tsx:455-458` — hint 组件纯文本渲染（B1）
- `mutbot/frontend/src/components/MessageList.tsx:136-155` — 右键菜单和 menuContext（B4/B5）
- `mutbot/frontend/src/panels/SessionListPanel.tsx:390` — 双击重命名（B2）
- `mutbot/frontend/src/lib/layout.ts:10-13` — flexlayout global 配置（B3）
- `mutbot/frontend/src/index.css` — `.tool-arg-value`（B9）、`.arg-modal`（B10）、`.ui-select-cards`（B11）
- `mutbot/src/mutbot/ui/context_impl.py:106-110` — `ui.show()` 只等 submit 事件（B6b 根因）
- `mutbot/src/mutbot/builtins/config_toolkit.py` — Provider 列表页（B7/B14）、Edit provider（B6/B13/B14/B15）、Select models（B6a）
- `mutbot/src/mutbot/builtins/menus.py:234-290` — MessageList/Context 菜单项（B5）
- `mutbot/src/mutbot/web/routes.py:926-934` — WS cancel/run_tool 消息处理（B12）
- `mutbot/frontend/src/panels/AgentPanel.tsx:297-311` — agent_cancelled 处理（B12）
- `mutagent/src/mutagent/builtins/tool_set_impl.py:361-394` — ToolSet.dispatch CancelledError 处理（B12）

### 相关规范
- `docs/specifications/feature-session-list-management.md`
- `docs/specifications/feature-session-ux-improvements.md`
