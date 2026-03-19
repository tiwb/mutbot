# Auth 配置向导 设计规范

**状态**：✅ 已完成
**日期**：2026-03-19
**类型**：功能设计

## 背景

mutbot 已有完整的 OIDC 认证系统（`feature-openid-auth.md`），但配置认证需要用户手动编辑 `~/.mutbot/config.json`。对于中转认证（relay）这种零注册方案，配置非常简单，完全可以通过向导引导完成。

**目标**：当服务器未配置 `auth` 时，在 SessionList/Header 菜单中显示 Auth Setup 菜单项，点击后弹出 workspace 级 UI 向导，引导用户完成中转认证配置。

**范围**：仅覆盖 relay 认证配置。直连认证后续废弃（统一为 relay 模式，需要直连的场景通过自建 relay 实现）。

### 设计决策

- **Relay-only**：认证客户端只有 relay 一种模式。直连 = 自建 relay + relay 到自己。后续清除直连代码以降低复杂度。
- **先登录再写配置**：向导中用户必须先成功登录，验证整条链路可用后才保存配置，避免把自己锁在门外。
- **简化权限**：不需要手动输入 `allowed_users`。只有一个选项：是否允许所有人登录。不允许则第一个登录者自动成为唯一 allowed user。
- **当前页面跳转**：OAuth 不用弹窗（`window.open` 被拦截、移动端体验差），直接在当前页面跳转。成功后自动保存配置并回到首页，不需要确认步骤。

## 设计方案

### 新能力：Client 绑定的 Workspace 级 UI

当前 UIContext 绑定在 session 级别（通过 session channel 广播 `ui_view`/`ui_close`）。Auth 向导不属于任何 session，需要在 workspace 级别展示，且仅对触发操作的客户端可见。

#### 核心原则

1. **不广播**：UI 仅推送到触发操作的那个客户端，不是 workspace 全体
2. **生命周期绑定 client**：client 断开 → UIContext 收到断连信号 → 后端 async task 清理退出
3. **UIContext 零改动**：UIContext 只需要一个 `broadcast` callable，绑定到单 client send 即可

#### 后端架构

```
Menu.execute()
  → 获取触发 client 的 send 函数（RpcContext.sender_ws → Client）
  → 创建 UIContext(broadcast=client.send_json)
  → 注册 client 断连回调（断连时注入 disconnect 事件）
  → asyncio.ensure_future(wizard_task)
  → 返回 MenuResult(action="workspace_ui", data={context_id: "..."})
```

**RpcContext 扩展**：新增获取 client send 函数的能力。RpcContext 已有 `sender_ws`，通过 `_find_client_by_ws()` 获取 Client 对象，Client.send_json 可作为 UIContext 的 broadcast。

**Client 断连回调**：Client 对象新增断连回调机制。UIContext 注册时添加回调，client 断开时向事件队列注入 disconnect 事件，`show()` 收到 disconnect 返回 None（与 cancel 一致）。

```python
# Client 扩展
class Client:
    _disconnect_callbacks: list[Callable]

    def on_disconnect(self, callback):
        self._disconnect_callbacks.append(callback)

    async def _cleanup(self):
        for cb in self._disconnect_callbacks:
            cb()
```

**show() 扩展**：遇到 disconnect 事件时返回 None（通用改进，所有 UIContext 用户受益）。

#### 前端

App.tsx 监听 workspace WebSocket 的 `ui_view` / `ui_close`，渲染为全局 modal overlay（复用 ViewRenderer，与终端设置的 modal 模式相同）。

```typescript
// App.tsx
const [workspaceUI, setWorkspaceUI] = useState<{contextId: string; view: any} | null>(null);

// workspace WebSocket 事件处理
wsRpc.on("ui_view", (data) => {
  setWorkspaceUI({ contextId: data.context_id, view: data.view });
});
wsRpc.on("ui_close", (data) => {
  if (workspaceUI?.contextId === data.context_id) {
    setWorkspaceUI(null);
    // 支持 redirect 和 reload action
    if (data.redirect) window.location.href = data.redirect;
    else if (data.action === "reload") window.location.reload();
  }
});

// 渲染
{workspaceUI && (
  <div className="workspace-ui-overlay" onClick={e => {
    if (e.target === e.currentTarget) sendUIEvent({ type: "cancel", data: {} });
  }}>
    <div className="workspace-ui-modal">
      <ViewRenderer view={workspaceUI.view} mode="connected" onEvent={handleWorkspaceUIEvent} />
    </div>
  </div>
)}
```

UI 事件通过 workspace WebSocket 发送，后端 workspace 消息处理新增 `ui_event` 分支，复用 `deliver_event()` 路由。

### Auth Setup 菜单

```python
class AuthSetupMenu(Menu):
    display_name = "Auth Setup"
    display_icon = "shield"
    display_category = "SessionList/Header"
    display_order = "0tools:1"

    @classmethod
    def check_visible(cls, context):
        """仅在未配置 auth 时显示"""
        from mutbot.web import server as _server_mod
        cfg = _server_mod.config
        if cfg is None:
            return True
        return not cfg.get("auth")
```

`execute()` 启动后台 async task 驱动向导 UI，返回 MenuResult 通知前端。

### 向导流程

**一步表单 + 当前页面跳转 OAuth**：

```
向导（一步）                     OAuth（当前页面跳转）         回来
┌───────────────┐    submit    ┌──────────────────┐    ┌───────────────────┐
│ relay URL     │ ──────────→  │ 跳转 relay OAuth │ →  │ relay-callback    │
│ 访问模式选择   │  后端存临时   │ → GitHub 授权    │    │ 验证断言          │
│               │  状态到 nonce │                  │    │ 保存 auth 配置    │
│ [Cancel]      │              └──────────────────┘    │ 签发 session JWT  │
│   [Sign in →] │                                      │ 重定向首页 ✅      │
└───────────────┘                                      └───────────────────┘
```

#### 步骤 1：表单

```
┌──────────────────────────────────────────┐
│  Auth Setup                              │
│                                          │
│  Enable login to control who can         │
│  access your MutBot server.              │
│                                          │
│  Relay Server:                           │
│  ┌──────────────────────────────────┐    │
│  │ https://mutbot.ai                │    │
│  └──────────────────────────────────┘    │
│                                          │
│  Access Mode:                            │
│  ○ Anyone can log in                     │
│  ● Only me (first login becomes admin)   │
│                                          │
│               [Cancel]  [Sign in →]      │
└──────────────────────────────────────────┘
```

用户点击 "Sign in" → 后端处理 → `ui.close({redirect: login_url})` → 前端跳转。

#### 步骤 2：OAuth（页面跳转）

当前页面跳转到 `{relay_url}/auth/start?callback=...&provider=github&nonce=xxx`。WebSocket 断开，async task 收到 disconnect 清理退出。

#### 步骤 3：回调处理

**成功**：relay-callback 后端一次性完成——验证断言 + 从 nonce 取出 relay URL 和访问模式 + 保存 auth 配置 + 签发 session JWT + 重定向首页。用户回到首页时已登录、认证已生效。

**失败**：relay-callback 页面显示明确的错误提示 + "返回 MutBot" 链接。用户回到首页后 auth 未配置，Auth Setup 菜单仍可见，可以重试。

### 向导后端实现

```python
# 临时状态：nonce → 向导收集的信息
_pending_setup: dict[str, dict] = {}

async def _auth_setup_wizard(client, context_id, rpc_context):
    ui = UIContext(context_id=context_id, broadcast=client.send_json)
    register_context(ui)

    def on_disconnect():
        queue = getattr(ui, '_event_queue', None)
        if queue:
            queue.put_nowait(UIEvent(type="disconnect", data={}))
    client.on_disconnect(on_disconnect)

    try:
        result = await ui.show(step1_view)
        if result is None:  # cancel 或 disconnect
            return

        relay_url = result["relay_url"]
        access_mode = result["access_mode"]  # "anyone" | "only_me"
        nonce = secrets.token_urlsafe(32)

        # 存临时状态（5 分钟 TTL），供 relay-callback 使用
        _pending_setup[nonce] = {
            "relay_url": relay_url,
            "access_mode": access_mode,
            "created": time.time(),
        }

        # 构造登录 URL
        self_origin = _get_self_origin(rpc_context)
        callback_url = f"{self_origin}/auth/relay-callback"
        login_url = (
            f"{relay_url}/auth/start"
            f"?callback={callback_url}&provider=github&nonce={nonce}"
        )

        # 关闭 UI 并跳转
        ui.close()
        client.send_json({"type": "ui_close", "context_id": context_id, "redirect": login_url})
    finally:
        ui.close()  # 幂等
        _cleanup_expired_nonces()
```

### relay-callback 扩展

现有 RelayCallbackView 处理断言验证时，增加对临时 nonce 的支持：

```python
# auth/views.py — relay-callback POST 处理
async def _handle_relay_assertion(self, request):
    assertion = request.data.get("assertion", "")
    claims = decode_jwt_unverified(assertion)
    nonce = claims.get("nonce", "")

    # 优先从 config 获取 relay URL
    relay_url = config.get("auth", {}).get("relay")

    # 无 config → 查临时状态（向导流程）
    setup_info = None
    if not relay_url:
        setup_info = _pending_setup.pop(nonce, None)
        if setup_info:
            relay_url = setup_info["relay_url"]

    if not relay_url:
        return error_page("No relay configured")

    # 验证断言（现有逻辑）
    public_key = await fetch_relay_public_key(relay_url)
    user_info = verify_relay_assertion(assertion, public_key, nonce)

    # 向导流程：保存 auth 配置
    if setup_info:
        auth_config = {"relay": setup_info["relay_url"]}
        if setup_info["access_mode"] == "only_me":
            auth_config["allowed_users"] = [user_info["sub"]]
        _save_auth_config(auth_config)

    # 签发 session JWT + 重定向（现有逻辑）
    token = sign_session_jwt(user_info)
    response = redirect("/")
    response.set_cookie("mutbot_token", token, ...)
    return response
```

### 失败处理

relay-callback 出错时，返回一个友好的错误页面：

```html
<!-- 错误页面模板 -->
<div style="text-align: center; padding: 50px;">
  <h2>Login failed</h2>
  <p>{error_message}</p>
  <a href="/">← Back to MutBot</a>
</div>
```

回到 mutbot 后，auth 未配置 → `check_visible` 返回 True → Auth Setup 菜单仍显示，用户可重试。

### 临时 nonce 清理

- 每个 nonce 5 分钟 TTL
- 向导 task 的 finally 块清理过期 nonce
- relay-callback 消费后立即 pop

```python
def _cleanup_expired_nonces():
    now = time.time()
    expired = [k for k, v in _pending_setup.items() if now - v["created"] > 300]
    for k in expired:
        del _pending_setup[k]
```

### 配置保存

**允许所有人**（不设 `allowed_users`，任何通过 relay 认证的用户都可访问）：
```json
{
  "auth": {
    "relay": "https://mutbot.ai"
  }
}
```

**仅限自己**（登录者身份自动写入）：
```json
{
  "auth": {
    "relay": "https://mutbot.ai",
    "allowed_users": ["github:tiwb"]
  }
}
```

## 实施步骤清单

### 阶段一：后端基础设施 [✅ 已完成]

- [x] **Task 1.1**: Client 断连回调机制
  - Client 类新增 `_on_disconnect` 列表和 `on_disconnect()` 方法
  - `enter_buffering()` 时遍历回调
  - 状态：✅ 已完成

- [x] **Task 1.2**: UIContext show() 支持 disconnect 事件
  - `show()` 遇到 `type` 为 `"cancel"` 或 `"disconnect"` 时返回 None
  - 通用改进，所有 UIContext 用户受益
  - 状态：✅ 已完成

### 阶段二：Workspace 级 UI 事件路由 [✅ 已完成]

- [x] **Task 2.1**: workspace WebSocket 新增 ui_event 处理
  - workspace 消息处理中增加 `ui_event` 分支（ch==0 时优先拦截）
  - 复用 `deliver_event()` 路由
  - 状态：✅ 已完成

- [x] **Task 2.2**: RpcContext 获取 Client 引用
  - 新增 `get_sender_client()` 方法，通过 `_find_client_by_ws` 获取 Client
  - 状态：✅ 已完成

### 阶段三：Auth Setup 菜单 + 向导 [✅ 已完成]

- [x] **Task 3.1**: AuthSetupMenu
  - `check_visible`：仅在无 auth 配置时显示
  - `execute()`：获取 client → 启动向导 async task → 返回 MenuResult
  - 状态：✅ 已完成

- [x] **Task 3.2**: 向导 async task
  - 新建 `src/mutbot/auth/setup.py`
  - 创建 client-bound UIContext + 注册断连回调
  - 表单：relay URL（默认 mutbot.ai）+ 访问模式（anyone / only_me）
  - 提交后：生成 nonce → 存临时状态 → 构造 login URL → ui.close + redirect
  - finally 块：清理 UIContext
  - 状态：✅ 已完成

- [x] **Task 3.3**: 临时 nonce 状态管理
  - `_pending_setup` 字典（nonce → relay_url + access_mode + created）
  - 5 分钟 TTL + 清理函数
  - 状态：✅ 已完成

### 阶段四：relay-callback 扩展 [✅ 已完成]

- [x] **Task 4.1**: RelayCallbackView 支持临时 nonce
  - 无 auth config 时先 decode JWT unverified 提取 nonce
  - 从 `_pending_setup` 查找 nonce 对应的 relay URL
  - 向导流程：验证断言成功后调用 `save_auth_config()` 保存到 config.json
  - 消费后立即 pop nonce
  - 状态：✅ 已完成

- [x] **Task 4.2**: 失败错误页面
  - relay-callback HTML 错误时显示 "← Back to MutBot" 链接
  - 状态：✅ 已完成

### 阶段五：前端 [✅ 已完成]

- [x] **Task 5.1**: App.tsx workspace 级 UI 渲染
  - 监听 workspace WebSocket 的 `ui_view` / `ui_close`
  - `workspaceUI` state + modal overlay + ViewRenderer
  - `ui_close` 支持 `redirect` 和 `reload` action
  - 状态：✅ 已完成

- [x] **Task 5.2**: workspace 级 UI 事件发送
  - `sendToChannel(0, {type: "ui_event", ...})` 通过 workspace WebSocket
  - 格式与 session 级一致（context_id + event_type + data + source）
  - 状态：✅ 已完成

- [x] **Task 5.3**: modal overlay 样式
  - `workspace-ui-overlay` / `workspace-ui-modal` CSS（fixed 定位、z-index 200）
  - 点击遮罩关闭
  - 状态：✅ 已完成

### 阶段六：验证 [✅ 已完成]

- [x] **Task 6.1**: 构建验证
  - 前端构建成功
  - 后端模块导入验证通过
  - 状态：✅ 已完成

## 关键参考

### 源码
- `src/mutbot/ui/context.py` — UIContext Declaration（set_view / wait_event / show / close）
- `src/mutbot/ui/context_impl.py` — UIContext @impl + deliver_event + 全局注册表
- `src/mutbot/runtime/terminal.py:629-686` — 终端设置 UI 模式（参考：background task + UIContext）
- `src/mutbot/builtins/menus.py` — Menu 体系 + SessionList/Header 菜单
- `src/mutbot/web/routes.py` — workspace WebSocket 消息处理 + Client + 事件广播
- `src/mutbot/web/rpc.py:40-67` — RpcContext（sender_ws）
- `src/mutbot/auth/views.py` — RelayCallbackView（需扩展支持临时 nonce）
- `src/mutbot/auth/middleware.py` — before_route 认证拦截
- `frontend/src/panels/TerminalPanel.tsx:939-950` — 终端设置 modal overlay（参考）
- `frontend/src/components/ToolCallCard.tsx` — ViewRenderer 组件

### 相关规范
- `docs/specifications/feature-openid-auth.md` — 认证系统设计（已完成）
- `docs/design/auth.md` — 认证设计概览
- `docs/archive/2026-03-02-feature-interactive-ui-tools.md` — 后端驱动 UI 框架设计
- `docs/archive/2026-03-03-refactor-setup-wizard.md` — LLM 配置向导重构（UIToolkit 模式参考）
