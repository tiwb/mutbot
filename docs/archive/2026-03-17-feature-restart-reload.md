# 服务器重启前端体验优化 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：功能设计

## 背景

服务器重启后前端体验不理想。v1 方案尝试用 badge 叠加在导航按钮上显示连接状态，但实践中发现：
- 绿色常驻 badge 是视觉噪音——正常状态不需要提示
- badge 太小，断线时不够醒目
- `restartPending` + `location.reload()` 的时序依赖 WS 重连成功，retry 耗尽时 toast 永不消失

## 设计方案（v2）

**核心思路**：去掉所有状态小圆点，改用 toast 显示连接状态变化。Toast 足够醒目，且只在异常时出现。

### 一、连接状态用 Toast 通知

| 事件 | Toast 内容 | 持续时间 |
|------|-----------|----------|
| disconnected | "连接已断开，正在重连..." | 持续显示，直到 connected |
| connecting（重连中） | 同上（不重复弹） | 同上 |
| connected（恢复） | "连接已恢复" | 3 秒自动消失 |
| server_restarting | "服务器正在重启..." | 持续显示，直到页面刷新 |

**关键**：正常连接时不显示任何指示。只在断开和恢复时用 toast 通知。

### 二、服务器重启 → 版本检查 → 强制刷新

不依赖 `restartPending` 内存标记（页面刷新后丢失、retry 耗尽时无效）。改为**服务端驱动**：

1. **后端**：welcome 消息中携带 `build_hash`（前端构建产物的 hash）
2. **前端**：构建时注入 `BUILD_HASH` 常量（vite define）
3. **重连时**：前端比对 welcome 中的 `build_hash` 与自身 `BUILD_HASH`，不一致则 `location.reload()`

**流程**：

```
服务器重启（代码更新）→ 新 Worker 启动 → 前端构建产物 hash 变化
    → 前端重连 → 收到 welcome{build_hash: "new"}
    → 比对本地 BUILD_HASH → 不一致 → location.reload()
```

**优势**：
- 不依赖 `server_restarting` 事件的时序
- 即使手动刷新后也能检测版本不一致
- 仅在前端代码真正变化时才刷新（纯后端改动不触发无意义刷新）

### 三、清理状态圆点

- 删除 v1 实施的 `status-badge`（PC toggle / 移动端 hamburger 上的 badge）
- 删除移动端原有的 `mobile-status-dot`（v1 已删除）
- 保留 session 状态文字无圆点（v1 已完成，保留）

## 待定问题

### QUEST Q1: build_hash 生成方式
**问题**：用什么作为 hash？
**建议**：vite 构建时用 `Date.now()` 或构建产物的 content hash。最简单的方式是 `define: { __BUILD_HASH__: JSON.stringify(Date.now().toString(36)) }`，每次构建自动变化。

### QUEST Q2: 没有前端变更的重启是否刷新
**问题**：如果只修改了后端代码（前端构建产物不变），build_hash 一致，不会触发刷新。这是期望行为吗？
**建议**：是。纯后端变更不需要刷新前端，WS 重连恢复即可。

## 关键参考

### 源码
- `mutbot/src/mutbot/web/routes.py:294-316` — welcome 消息发送
- `mutbot/frontend/src/lib/workspace-rpc.ts:610-625` — handleWelcome 处理
- `mutbot/frontend/src/App.tsx:280-304` — WorkspaceRpc 回调（onOpen/onClose/onConnecting）
- `mutbot/frontend/vite.config.ts` — vite 构建配置（需添加 define）
- `mutbot/src/mutbot/web/server.py` — Worker 启动（需计算前端 build_hash）
