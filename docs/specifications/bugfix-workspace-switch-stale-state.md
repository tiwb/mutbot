# 工作区切换时旧内容残留 设计规范

**状态**：✅ 已完成
**日期**：2026-03-23
**类型**：Bug修复

## 背景

在同一浏览器 tab 内切换工作区时（关闭 `ai` → 创建 `ai_skills` → 打开 `ai_skills`），页面虽然显示已进入 `ai_skills` 工作区，但内容区域仍然展示 `ai` 工作区的终端 tabs 和 session 列表。新开浏览器 tab 则不会复现。

## 设计方案

### 根因分析

`App.tsx` 第 311 行的 workspace useEffect 中，当 `workspace` 变化时存在两个状态清理遗漏：

1. **flexlayout model 未重建**：`hashchange` 和 `onCreated` 回调中调用 `setWorkspace(ws)` 时，只在 `target.layout` 存在时才调用 `createModel(target.layout)` 重建布局。新创建的工作区没有 `layout`，导致旧工作区的 model（包含已打开的终端 tabs）被原样保留。

2. **sessions 状态未清空**：useEffect 的 cleanup 函数（438-443 行）关闭旧 WebSocket 并清空 `rpc`，但没有清空 `sessions` 状态。虽然新连接建立后 `session.list` 返回空数组会最终覆盖，但在 model 未重建的情况下旧 tabs 仍然可见。

### 修复方案

在 workspace useEffect 入口处（`workspace` 变化触发时），立即重置前端状态：

- 调用 `setSessions([])` 清空旧 session 列表
- 若新 workspace 无 `layout`，调用 `createModel()` 重建为空默认布局，并同步更新 `hasOpenTabs` 状态

修改位置：`App.tsx:311-313`，在 `hadConnectionRef.current = false` 之前插入清理逻辑。

## 待定问题

（暂无）

## 关键参考

### 源码
- `frontend/src/App.tsx:311-444` — workspace useEffect（WebSocket 初始化 + 事件监听）
- `frontend/src/App.tsx:260-289` — hashchange 工作区切换
- `frontend/src/App.tsx:1161-1164` — 新工作区创建回调
- `frontend/src/lib/layout.ts:31-41` — `createModel()` 无参数时返回空默认布局

## 实施步骤清单

- [x] **Task 1**: 在 `App.tsx` workspace useEffect 入口处添加状态重置逻辑
  - [x] `setSessions([])` 清空旧 session 列表
  - [x] 若 `!workspace.layout`，调用 `createModel()` 重建空布局并同步 `hasOpenTabs`
  - 状态：✅ 已完成

## 测试验证

- [ ] 同一 tab 内：关闭工作区 → 创建新工作区 → 确认无旧内容残留
- [ ] 同一 tab 内：在有 session 的工作区和无 session 的工作区间切换
- [ ] 新开 tab 访问工作区，确认行为不受影响
