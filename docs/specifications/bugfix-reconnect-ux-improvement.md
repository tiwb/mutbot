# 重连体验优化 — 状态可见性与 UI 机制改进

**状态**：🔄 实施中
**日期**：2026-03-17
**类型**：Bug修复 / 体验优化

## 背景

手机锁屏恢复或网络切换后，WebSocket 重连体验不佳：

1. **状态不透明** — 重连界面只显示"Connection lost. Reconnecting..."，用户不知道当前是第几次 retry、还剩多少次、下次 retry 倒计时多久
2. **Toast 易误触消失** — 重连状态用 toast 通知展示，用户点一下就消失了，之后完全不知道重连进度
3. **重连困难** — 手机恢复后可能长时间卡在重连中（10 次 retry、最长 30s 间隔，全流程可能耗时 ~3 分钟）

## 设计方案

### 核心设计

#### 1. 连接状态栏（替代 toast）

重连状态从 toast 改为**顶部固定状态栏**（顶部横幅是系统级状态的常见 UI 模式，底部容易被移动端虚拟键盘遮挡）：
- 不可点击关闭 — 只在连接恢复后自动消失
- 显示详细状态信息 — retry 次数、实时倒计时、当前动作（实时倒计时让用户感知"系统在工作"）
- 视觉层级更高 — 顶部横幅比底部 toast 更醒目

状态信息示例：
```
🔴 连接已断开 — 正在重连 (3/10)，下次尝试 4s...
🟡 正在连接服务器...
🟢 连接已恢复                    ← 显示 2 秒后自动消失
🔴 无法连接服务器 — 已尝试 10 次   [重试]
```

#### 2. WorkspaceRpc 状态回调增强

WorkspaceRpc 新增 `onRetry` 回调，暴露重连进度信息（attempt、maxRetries、delay、phase）。App.tsx 通过此回调驱动状态栏。

#### 3. 重试耗尽后的手动重试

10 次 retry 全部失败后，状态栏显示"无法连接服务器"并提供 **[重试]** 按钮，调用 WorkspaceRpc 新增的 `retry()` 方法重置 retry 计数重新开始。

#### 4. toast 保留用于瞬态消息

Toast 仍保留，但只用于瞬态成功/信息消息（如"配置已更新"），不再用于需要持续展示的连接状态。

### 设计决策

- **状态栏位置：顶部**（底部容易被移动端虚拟键盘遮挡）
- **实时倒计时**（1 秒 interval，用户感知系统在工作）
- **visibility/online 事件已足够**（当前代码已同时监听两者，暂不额外增强）
- **服务端 30s buffering 超时暂不调整**（手机恢复时 visibilitychange 立即重连，30s 够用）

## 关键参考

### 源码
- `frontend/src/lib/websocket.ts` — ReconnectingWebSocket（AppRpc 用），指数退避逻辑
- `frontend/src/lib/workspace-rpc.ts` — WorkspaceRpc，可靠传输 + visibility/online 监听，重连在 `ws.onclose`（:228）和 `connect()`（:208）
- `frontend/src/App.tsx:129-138` — showToast 实现
- `frontend/src/App.tsx:281-299` — WorkspaceRpc onOpen/onClose toast 逻辑
- `frontend/src/App.tsx:1138-1142,1219-1223` — toast 渲染（两处：移动端/桌面端布局）
- `frontend/src/index.css:3462-3482` — toast 样式

## 实施步骤清单

### Phase 1: WorkspaceRpc 状态回调增强 [✅ 已完成]

- [x] **Task 1.1**: WorkspaceRpc 新增 `onRetry` 回调和 `retry()` 方法
  - 新增 `RetryInfo` 导出类型和 `onRetryCb` 回调
  - `ws.onclose` 中触发 waiting/connecting/exhausted 三种 phase
  - 新增 `retry()` 公开方法
  - 状态：✅ 已完成

### Phase 2: ConnectionStatusBar 组件 [✅ 已完成]

- [x] **Task 2.1**: 创建 `ConnectionStatusBar` React 组件
  - `frontend/src/components/ConnectionStatusBar.tsx`
  - 四种状态：connected / waiting / connecting / exhausted
  - waiting 实时倒计时，exhausted 显示重试按钮，connected 2 秒后自动隐藏
  - 状态：✅ 已完成

- [x] **Task 2.2**: ConnectionStatusBar 样式
  - `frontend/src/index.css` 新增 Connection Status Bar 样式段
  - 顶部固定，z-index 10001，三色区分（红/黄/绿）
  - 状态：✅ 已完成

### Phase 3: App.tsx 集成 [✅ 已完成]

- [x] **Task 3.1**: App.tsx 中集成 ConnectionStatusBar 替代重连 toast
  - 新增 `connPhase` / `connRetry` / `hadConnectionRef` state
  - onOpen 中改用 `setConnPhase("connected")`，移除 showToast
  - onRetry 回调驱动状态栏
  - 两处布局（移动端/桌面端）均添加 ConnectionStatusBar
  - 保留 showToast 用于配置更新、服务器重启等瞬态消息
  - 状态：✅ 已完成

### Phase 4: 构建与验证 [✅ 已完成]

- [x] **Task 4.1**: 前端构建并验证
  - `tsc --noEmit` 零错误，`vite build` 成功
  - 状态：✅ 已完成

## 测试验证

- TypeScript 类型检查通过（零错误）
- Vite 生产构建成功
- 待手动验证：断开网络 → 状态栏显示 → 倒计时 → 恢复后消失
