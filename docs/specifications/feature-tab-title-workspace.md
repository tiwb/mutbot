# 浏览器页签标题显示工作区名称

**状态**：✅ 已完成
**日期**：2026-03-23
**类型**：功能设计

## 背景

当用户同时打开多个 mutbot 工作区时，所有浏览器页签都显示静态标题 "MutBot"，无法区分。需要在页签标题中显示当前工作区名称。

## 设计方案

### 核心设计

在 `App.tsx` 中添加 `useEffect`，当 `workspace` 变化时更新 `document.title`：

- 有工作区时：`{workspace.name} - MutBot`
- 无工作区时（未加载/首页）：保持默认 `MutBot`

不引入额外依赖，直接操作 `document.title`。

## 实施步骤清单

- [x] **Task 1**: 在 App.tsx 中添加 useEffect 监听 workspace.name 变化，设置 document.title
  - 状态：✅ 已完成

## 关键参考

### 源码
- `frontend/src/App.tsx:109-112` — 新增的 useEffect
