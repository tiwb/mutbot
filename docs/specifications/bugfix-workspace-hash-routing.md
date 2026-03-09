# Workspace Hash 路由后退问题

**状态**：✅ 已完成（接受现状）
**日期**：2026-03-09
**类型**：Bug修复

## 背景

用户直接访问 `http://127.0.0.1:8741/#workspace_name` 进入 workspace 后，点击浏览器后退按钮期望回到 landing page（`/`），但实际会退到空白页面（浏览器上一个页面或 `about:blank`）。

原因：直接访问带 hash 的 URL 时，浏览器历史中没有 `/`（landing page）这个条目，后退自然不会回到 landing page。

## 尝试过的方案

### 方案 1: `main.tsx` 初始化时注入历史条目

在 React 挂载前，用 `replaceState` + `pushState` 手动注入 landing page 历史条目：

```typescript
// main.tsx
if (location.hash) {
  const hash = location.hash;
  history.replaceState(null, "", location.pathname);  // 当前条目改为 /
  history.pushState(null, "", hash);                  // 新增 #hash 条目
}
```

**结果**：Chrome 后退时跳过 `pushState` 注入的条目，直接退到更早的页面。Chrome 的反劫持机制会忽略脚本注入的历史条目，防止恶意网站阻止用户离开。

### 方案 2: 其他常见前端路由方案

- `popstate` 事件拦截 → Chrome 同样限制，无法阻止用户后退
- `beforeunload` 弹窗提示 → 体验差，且现代浏览器会忽略非用户交互触发的 `beforeunload`
- `location.replace` → 会丢失当前页面状态

## 结论

这是**浏览器安全策略的限制**，不是应用 bug。Chrome（及其他现代浏览器）为防止恶意网站劫持后退按钮，会对脚本注入的历史条目采取反劫持措施。SPA 无法可靠地在首次直接访问时往历史中插入"回退目标页"。

### 当前行为（已接受）

| 场景 | 后退行为 |
|------|---------|
| 从 landing page 点击进入 workspace | 正常回到 landing page |
| 直接访问 `/#workspace` URL | 退到浏览器之前的页面（可能是空白页） |
| workspace 内点击"关闭 workspace"按钮 | 正常回到 landing page（`exitWorkspace()` → `history.back()`） |

### 保留的代码

`main.tsx` 中的 `ensureLandingInHistory` 逻辑保留不删除——它在"从 landing page 刷新后仍带 hash"的场景下仍有价值，且不会造成副作用。

## 关键参考

### 源码
- `frontend/src/main.tsx:6-13` — `ensureLandingInHistory` 逻辑
- `frontend/src/App.tsx:36-38` — `exitWorkspace()` 实现（`history.back()`）
- `frontend/src/App.tsx:41-44` — `parseWorkspaceHash()` 解析 hash

### 相关提交
- `1e6fc71` — workspace hash 路由统一重构
