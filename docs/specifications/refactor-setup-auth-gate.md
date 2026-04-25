# Setup 鉴权重构 — Setup Token 升级为登录方式 + 独立登录页

**状态**:✅ 已完成
**日期**:2026-04-25
**类型**:重构(安全模型重新分层 + 登录入口解耦)

## 背景

`refactor-auth-setup-mutgui` 已完成,setup 向导用 mutgui 重写。但鉴权和业务在同一个 mutgui View 里耦合:

- `step="token_input"` 与业务 step (`configure` / `select_provider` / ...) 平级
- 三处业务回调 (`_on_connect_relay`、`_on_start_oauth`、`_on_reconfigure`) 都重复同一段 `if (not self.is_local) and setup_token.is_active() and not self.setup_verified: ...` 防御
- 本地用户 `is_local=True` 完全免鉴权,隐含"凡是本地都有 root 权限"假设
- 远程未登录用户能直接打开 `/auth/setup` 看到 `already_configured`,Reconfigure 入口对"已登录管理员"和"未登录攻击者"路径相同
- WebSocket 连接本身没鉴权,middleware 把 `/auth/` 全量公开放行,未鉴权用户能直接建 WS、初始化 View

setup 等价于"重新定义管理员账户"的 root 级操作,但鉴权没有按 root 级操作来设计。

### 设计演进(讨论过的方案,最终未采用)

讨论过两套独立鉴权方案:

1. **专用 setup-session cookie + 独立 setup-login HTML 页**:setup 路径单独鉴权,签发 path=/auth/setup 的短期 cookie。问题:与现有"主登录 = 通用 session"机制并行存在两套鉴权,未来"已登录用户也能进 setup"是 middleware 加分支,长期分裂

2. **Setup-login 用 mutgui**:与 setup wizard 风格一致。问题:鉴权最后必须 HTTP 设 cookie + 跳转,WS 做不了,需要 OTT 桥接,复杂度不划算;同时未解决"两套 session 并存"

最终方案是把 **setup token 重新定位为一种"登录方式"**,与 OIDC/relay 并列,签发标准 session token。鉴权流程从此完全统一,不存在"setup 专用鉴权"这个概念。

## 设计方案

### 核心模型转变

**旧模型**:setup token 是"进入 setup 页的临时凭证"。用户输入 token → 进 setup → 配 auth → 完成。

**新模型**:setup token 是"未配置 auth 时可用的一种登录方式"。用户访问 mutbot → middleware 拦截到独立登录页 `/auth/login` → 看到 "Sign in with Setup Token" 按钮 → 点击 → 输入 token → **签发标准 session** → 已登录身份 → 进 setup 页配 auth → 完成。

| 维度 | 旧模型 | 新模型 |
|------|--------|--------|
| Setup token 验证产出 | 短期 setup cookie | 标准 session token |
| 验证后能访问的范围 | 只 `/auth/setup` 路径 | 所有需要登录的页面 |
| Setup 页本身的鉴权 | 专用 setup cookie | 标准 session(任何登录用户) |
| 登录流程的统一性 | 两条:OIDC、setup-token | 一条:`/auth/providers` 列出所有方式 |

### 业务和鉴权的解耦

业务侧(setup_view、未来其他需要鉴权的 View)只问一件事:**"当前请求有有效 session 吗?"** —— 由 middleware 统一回答。鉴权方式(OIDC、setup-token、未来的 FIDO 扫码等)对业务完全不可见。

业务和鉴权的关系变成:

```
鉴权层(middleware + /auth/* 路由)  →  签发标准 session
                                          ↓
业务层(任何受保护的 View)         →  从 scope["user"] 读身份,不关心来源
```

### `/auth/providers` 增加 setup-token 选项

`auth/views.py:ProvidersView` 当前在"无 auth 配置 + 无 providers"时返回 `{"providers": [], "auth_enabled": False}`。

新行为:在 `setup_token.is_active()` 时,**无论 auth 是否已配置**,都在 providers 列表前面追加一条:

```json
{
  "name": "setup-token",
  "label": "Setup Token",
  "type": "setup-token",
  "url": "/auth/setup-token-login"
}
```

`auth_enabled` 字段保持反映"实际 auth 是否配置",不被 setup-token 影响。`/auth/login` 独立登录页 fetch 这个接口后渲染按钮 → 点击 → 跳到 `/auth/setup-token-login`(带 next 参数透传)。

**为什么 token 有效就追加(即使 auth 已配置)**:支持 reconfigure 后管理员把自己锁出去的恢复 — 此时管理员重新生成 setup token(通过未来的 CLI 子命令,或重启服务),token 激活后能再次以 setup 身份登录修复配置。

### 新增 `/auth/setup-token-login` 路由

新文件 `auth/setup_login.py`,实现 `SetupTokenLoginView(View)`:

```python
class SetupTokenLoginView(View):
    path = "/auth/setup-token-login"

    async def get(self, request: Request) -> Response:
        # token 不再激活 → 跳回独立登录页
        if not setup_token.is_active():
            return Response(status=302, headers={"location": "/auth/login"})
        return html_response(_render_form(error=None))

    async def post(self, request: Request) -> Response:
        if not setup_token.is_active():
            return Response(status=302, headers={"location": "/auth/login"})

        form = await request.form()
        token = (form.get("token") or "").strip()
        if not setup_token.verify(token):
            return html_response(_render_form(error="Invalid token"), status=401)

        # 签发标准 session,sub 标记 setup 来源
        session_token = create_session_token(
            sub=SETUP_BOOTSTRAP_SUB,
            name="Setup Admin",
            avatar="",
            provider="setup-token",
            ttl=SETUP_SESSION_TTL,
        )
        # next 优先(透传 /auth/login?next=... 进来的目标),否则默认 /auth/setup
        target = _safe_next(form.get("next") or "") or "/auth/setup"
        headers = {"location": target}
        set_session_cookie(headers, session_token, secure=_is_secure(request))
        return Response(status=302, headers=headers)
```

HTML 表单内联(< 50 行,与现有 setup wizard 暗色主题一致),只有一个 token 输入框 + Verify 按钮 + 错误提示区。**不依赖外部 CSS / JS**。

常量:

- `SETUP_BOOTSTRAP_SUB = "setup:bootstrap"` — 标记 setup token 颁发的临时身份
- `SETUP_SESSION_TTL = 3600` — 1 小时,够走完 setup 流程

**为什么不用 mutgui 实现这个页面**:鉴权最后两步(设 cookie + 302)是 HTTP 层物理动作,WS 做不了。即使用 mutgui 渲染 UI,实现也是 form POST,没有用 mutgui 的实际收益。详见"设计演进"。

### 已配置场景下 setup-token 登录后的白名单检查

middleware 现有逻辑:`scope["user"]` 设置后,白名单检查 `allowed.contains(user.sub)`。

setup token 登录后 sub 是 `"setup:bootstrap"`,**不在 `allowed_users` 里**。这意味着:

- 已配置 + setup-token 登录 → middleware 检查白名单 → 拒绝
- 但是 `/auth/setup` 路径需要被这个 sub 访问到才有意义

**解决**:在 middleware 白名单检查里,**对 `setup:bootstrap` 这个特殊 sub 例外处理**:放行 setup 相关路径,根路径强制重定向到 setup 页,其他业务路径 403。

```python
# middleware._mutbot_before_route 内新增
SETUP_ALLOWED_PATHS = ("/auth/setup", "/auth/setup/ws", "/auth/relay-callback")

if user and user.get("sub") == SETUP_BOOTSTRAP_SUB:
    if path in SETUP_ALLOWED_PATHS:
        scope["user"] = user
        return None
    # 根路径 → 强制跳到 setup 页(避免加载主 React App 后所有操作都 403 的烂体验)
    if path == "/" or path == "":
        return Response(status=302, headers={"location": base_path + "/auth/setup"})
    # 其他业务路径直接 403
    return Response(status=403)
```

这样 setup admin **能且仅能**做 setup 相关操作:

- 访问 `/auth/setup` 配置 auth ✓
- 通过 `/auth/setup/ws` 与 mutgui View 通信 ✓
- 经 `/auth/relay-callback` 完成 OAuth(此时签发新 session 覆盖 setup-token session)✓
- 访问 `/`、`/api/*`、任何业务路径 → 跳转到 setup 或 403,无法借机访问 mutbot 业务功能

这是 setup token 的本意 — 它不是普通账户,只是"配置 auth"的一次性凭证。

### Middleware 简化与调整

`middleware.py` 改动:

1. **`/auth/setup` 从 `_PUBLIC_PREFIXES` 中移除**(实现上是把 `_PUBLIC_PREFIXES` 改细 — 仍然放行 `/auth/callback`、`/auth/relay-callback`、`/auth/providers`、`/auth/userinfo`、`/auth/logout`、`/auth/setup-token-login`,但 `/auth/setup` 和 `/auth/setup/ws` 走标准鉴权流程)

2. **未登录访问 `/auth/setup`**(HTTP 或 WS)→ 重定向到 `/auth/login?next=/auth/setup`,与"未登录访问任何业务路径"行为一致

3. **新增 `setup:bootstrap` sub 的限定放行**(见上一节)

4. **"无 auth + 非本地"分支**:旧逻辑重定向到 `/auth/setup`。新逻辑改为重定向到 `/auth/login` — 独立登录页 fetch `/auth/providers`,setup_token 激活时会看到 setup-token 选项

5. **根路径 `/` 也走重定向**:不再放行让 React App 加载,未登录访问 `/` 直接 302 到 `/auth/login`(React App 已无登录引导,放行也无意义)

6. **`is_loopback_ip` 在 setup 流程中不再被引用** — 本地访问 `/auth/setup` 也走标准登录(不再特殊放行)。这是"本地不再免鉴权"决策的落地

### setup_view 大幅精简

`setup_view.py` 删除:

- `is_local` 字段及构造参数
- `setup_verified` 字段
- `pending_reconfigure` 字段
- `step="token_input"` 分支(`_render_token_input`)
- `_on_verify_token` 回调
- `_on_reconfigure` 中的本地/远程分支、`setup_token.generate()` 调用、控制台打印代码
- `_on_connect_relay` 和 `_on_start_oauth` 中的 `setup_token.is_active() and not self.setup_verified` 防御
- `AuthSetupWsView.connect` 中读 `current_client_ip` 的逻辑

`AuthSetupView.__init__` 简化:

```python
def __init__(self) -> None:
    super().__init__()
    if _is_already_configured():
        self.step = "already_configured"
    else:
        self.step = "configure"
    self.error = ""
    self.relay_url = "https://mutbot.ai"
    self.providers = []
    self.redirect_url = ""
```

`_on_reconfigure` 简化:

```python
def _on_reconfigure(self) -> None:
    self.step = "configure"
    self.error = ""
    self.relay_url = _read_current_relay() or "https://mutbot.ai"
    self.invalidate()
```

`AuthSetupWsView.connect` 简化:不再读 IP,直接 `AuthSetupView()`。`_ws_host` / `_ws_secure` 保留(构造 OAuth callback URL 仍需要)。

### 独立登录页 `/auth/login` 取代 React LoginPage

新增 `auth/login_view.py`,实现 `LoginPageView(View)`:

- `/auth/login` → 纯 HTML 登录页(暗色主题、内联 CSS+JS,不依赖 React/mutgui),fetch `/auth/providers` 后用原生 DOM 渲染按钮
- 支持 `?next=<path>`,登录成功后跳回原路径
- 已登录用户也能打开(显示当前身份 + Sign out 链接,提供"切换身份"能力)
- 钥匙图标、虚线边框、setup-token hint 等"setup-token 区分"UI 全部内置

**`/auth` 和 `/auth/` 的 302 由 middleware 直接处理**(纯 URL 规范化,不需要 View 兜底):
- 早于鉴权检查,无视登录态(已登录用户访问 `/auth` 也会被规范化到 `/auth/login`)
- 同时处理 `/auth` 和 `/auth/`,避免 trailing slash 漏到下方业务分支返回空页
- 透传 `?next=` 参数,通过同样的 `_safe_next` 校验防 open redirect

**为什么不用 React 渲染登录页**:登录页是鉴权大门,不应该依赖一坨主应用 bundle 才能加载;React App 加载完了再判断"未登录"再渲染登录界面,等于给未登录用户也下发了主应用代码,既慢又松散。独立 HTML 页一来即用,鉴权边界清晰。

**为什么不用 mutgui 实现**:与 setup-token-login 同理 — 鉴权终点必须设 cookie + 302,WS 做不了。

### 删除 React 登录路径(死代码清理)

`/auth/login` 上线后,React `LoginPage.tsx` 立即变成死代码,顺手删掉:

- 删除 `frontend/src/components/LoginPage.tsx` 整个文件
- `App.tsx` 删 `LoginPage` import、`authReady` state、auth 探测 useEffect、登录分支
- `index.css` 删 `.login-page / .login-card / .login-providers / .login-provider-btn / .login-provider-btn-setup / .login-setup-hint` 等(原 React 登录页样式)
- `index.css` 删 `.login-screen / .login-form / .login-error`(更早期遗留死代码,无任何引用)

清理后未登录用户访问任意路径(含 `/`)都由 middleware 直接 302 到 `/auth/login`,登录入口唯一。

### setup-token session 的安全边界

| 场景 | 行为 | 说明 |
|------|------|------|
| token 激活 + 未登录用户访问任何路径 | 重定向到 /auth/login | 独立登录页显示 setup-token 选项 |
| 用 setup-token 登录后访问 /auth/setup | 放行 | scope["user"].sub == SETUP_BOOTSTRAP_SUB,middleware 例外放行 |
| 用 setup-token 登录后访问 /(根路径) | 302 到 /auth/setup | 避免 React App 加载后所有 API 都 403 的烂体验 |
| 用 setup-token 登录后访问 /api/* 或其他业务路径 | 403 | 这个身份不能用于业务,仅用于 setup |
| OAuth 完成 → /auth/relay-callback 签发新 session | 自动覆盖 setup-token cookie | 用户自然过渡到真实身份,无感 |
| 配置完成后 save_auth_config 调 setup_token.invalidate() | token 失效 | 但已签发的 setup-token session 仍然有效到 1 小时过期 |
| token 失效后 setup-token session 持有者再访问 | 不变 | sub 仍然是 SETUP_BOOTSTRAP_SUB,仍能且仅能访问 setup 路径,可以再次 reconfigure |
| 真实管理员通过 OIDC 登录后访问 /auth/setup | 走 allowed_users 白名单检查 | 已配置场景下,真实管理员才是常规进 setup 的路径 |

**关于 setup-token session 在 token 失效后仍然有效**:理论上可以做服务端 revoke,但 mutbot session 是 JWT 自验证模式,无服务端状态。引入 revoke list 不值得。1 小时 TTL 是可接受的曝光窗口,因为该 session 的能力被限定在 setup 路径。

### 不变 / 保留的接口

- `/auth/callback`、`/auth/relay-callback` — 完全不变
- `/auth/userinfo`、`/auth/logout` — 完全不变
- `setup_token.generate / verify / invalidate / is_active` — 完全不变(新登录路由复用)
- `auth/setup.py` 的 `store_setup_nonce` / `pop_setup_nonce` / `save_auth_config` — 完全不变
- React `LoginPage` 已删除,登录入口由 `/auth/login` 独立 HTML 页承担

### 测试策略

- **单元测试**(`tests/test_setup_token_login.py` 新增):
  - GET 在 token_active 时返回表单
  - GET 在 token_invalidated 时 302 到 /auth/login
  - POST 错误 token → 401 + 错误页
  - POST 正确 token → 302 到 /auth/setup + Set-Cookie(标准 session)
  - 验证签发的 session token 含 `sub=SETUP_BOOTSTRAP_SUB`、`provider=setup-token`、`ttl=3600`

- **`test_auth_setup_view.py` 更新**:
  - 删除 `is_local` 参数和相关分支
  - 删除 `token_input` step 的渲染/转换测试
  - 删除 `setup_verified` / `pending_reconfigure` 测试
  - `_on_reconfigure` 测试简化为单分支(只验证切到 configure step)

- **`test_public_access_hardening.py` 扩展**:
  - `/auth/setup` 未登录(HTTP)→ 302 到 /auth/login?next=/auth/setup
  - `/auth/setup/ws` 未登录 → 4401
  - setup-token session 访问 `/auth/setup` → 放行
  - setup-token session 访问 `/`(根路径)→ 302 到 /auth/setup
  - setup-token session 访问 `/api/...` → 403
  - 本地 + 未登录访问 /auth/setup → 也跳 /auth/login(行为变化)

- **`test_providers_view.py` 或扩展现有 test**:
  - token 激活 + 无 auth 配置 → providers 含 setup-token 项,auth_enabled=False
  - token 激活 + 已配置 auth → providers 含 setup-token 项 + 配置的 OIDC 项
  - token 失效 → providers 不含 setup-token 项

- **手动浏览器验证**:
  1. 远程 + 未配置:访问 `/` → 302 到 `/auth/login` → 显示 Setup Token 按钮 → 点击 → 输 token → 进 setup → 完成
  2. 本地 + 未配置:同上(行为变化:本地也走 `/auth/login`)
  3. 已配置 + 远程:访问 `/` → 302 到 `/auth/login` → OIDC 登录 → 回 `/` → 设置页进 setup → Reconfigure → 完成
  4. 已配置 + 未登录:`/auth/setup` 直接跳 `/auth/login?next=/auth/setup`(无法绕过)
  5. setup-token 登录后试图访问 /(根路径)→ 302 到 /auth/setup;访问 /api/* → 403

## 待定问题

(无,所有点已在设计方案中收敛)

## 关键参考

### mutbot 侧
- `mutbot/src/mutbot/auth/views.py:463-509` — `ProvidersView`,新增 setup-token 项的位置
- `mutbot/src/mutbot/auth/views.py:113-138` — `_create_nonce`/`_verify_nonce`,setup-login HTML 模板可借鉴风格
- `mutbot/src/mutbot/auth/middleware.py:24-28` — `_PUBLIC_PREFIXES`,`/auth/setup` 拆出
- `mutbot/src/mutbot/auth/middleware.py:99-193` — `_mutbot_before_route`,新增 SETUP_BOOTSTRAP_SUB 例外放行 + 调整无 auth 分支重定向目标
- `mutbot/src/mutbot/auth/setup_view.py:90-112` — `AuthSetupView.__init__`,删 `is_local`
- `mutbot/src/mutbot/auth/setup_view.py:179-198` — `_render_token_input`,整段删除
- `mutbot/src/mutbot/auth/setup_view.py:273-316` — `_on_reconfigure` 简化、`_on_verify_token` 删除
- `mutbot/src/mutbot/auth/setup_view.py:325-331, 374-378` — token 防御代码,删除
- `mutbot/src/mutbot/auth/setup_view.py:440-456` — `AuthSetupWsView.connect`,删除 client IP 读取
- `mutbot/src/mutbot/auth/setup_token.py` — 不变(`is_active`/`verify`/`invalidate` 仍被新登录路由用)
- `mutbot/src/mutbot/auth/token.py` — `create_session_token` / `set_session_cookie`,setup-login POST 复用
- `mutbot/src/mutbot/auth/login_view.py` — 新增,独立登录页 `/auth/login` + `/auth` 重定向(取代 React LoginPage)

### 历史规范
- `mutbot/docs/archive/2026-03-24-feature-public-access-hardening.md` — setup token 机制起源
- `mutbot/docs/archive/2026-03-19-feature-auth-setup-wizard.md` — auth setup wizard 旧设计

### 关联未完成 spec
- `mutgui/docs/specifications/feature-builtin-redirect.md`(新建)— Redirect 内置到 mutgui core(后置,不阻塞本次)

## 消费者场景

无下游消费者(终端用户使用的页面 + 内部 middleware/View 实现)。

## 实施步骤清单

### 后端 — 新登录路由

- [x] 新建 `mutbot/src/mutbot/auth/setup_login.py`,实现 `SetupTokenLoginView(View)`
  - GET:token 失效 → 302 到 /;否则返回 token 输入表单 HTML
  - POST:验证 token → 签发标准 session(`sub=SETUP_BOOTSTRAP_SUB`,TTL=3600)→ 设 cookie → 302 到 /auth/setup
  - 暗色主题 HTML 内联,与现有 setup wizard 风格一致
- [x] `SETUP_BOOTSTRAP_SUB = "setup:bootstrap"` 和 `SETUP_SESSION_TTL = 3600` 作为模块常量(供 middleware import)

### 后端 — `/auth/providers` 增加 setup-token 选项

- [x] `auth/views.py:ProvidersView.get` 中,`setup_token.is_active()` 时在返回的 `providers` 列表前面追加 `{"name": "setup-token", "label": "Setup Token", "type": "setup-token", "url": "/auth/setup-token-login"}`
- [x] `auth_enabled` 字段保持反映"实际 auth 配置存在",不被 setup-token 影响

### 后端 — middleware 调整

- [x] `_PUBLIC_PREFIXES` 改细:从 `("/auth/", ...)` 拆为具体白名单路径(`/auth/callback`、`/auth/relay-callback`、`/auth/providers`、`/auth/userinfo`、`/auth/logout`、`/auth/setup-token-login`、`/auth/login`),`/auth/setup` 和 `/auth/setup/ws` 不在白名单内
- [x] `/auth` 和 `/auth/` 早期拦截 — middleware 在 client IP 解析后立即 302 到 `/auth/login`(URL 规范化,无视登录态)
- [x] 新增 `setup:bootstrap` sub 的限定放行:在标准认证检查通过后,该 sub 仅能访问 `SETUP_ALLOWED_PATHS = ("/auth/setup", "/auth/setup/ws", "/auth/relay-callback")`,根路径 302 到 /auth/setup,其他业务路径 403
- [x] "无 auth + 非本地"分支重定向目标改为 `/auth/login?next=<原路径>`
- [x] 已配置 auth 场景下根路径 `/` 不再放行,未登录访问也直接 302 到 `/auth/login`
- [x] 删除 setup 路径的"本地直接放行"特殊逻辑(本地访问 setup 也走标准登录)
- [x] 新增 `_login_redirect_target()` 辅助函数,统一构造 `/auth/login?next=<原路径>`

### 后端 — setup_view 精简

- [x] 删除 `AuthSetupView.is_local` 字段及构造参数
- [x] 删除 `setup_verified` 字段
- [x] 删除 `pending_reconfigure` 字段
- [x] 删除 `_render_token_input` 方法和 `step="token_input"` 的 render 分支
- [x] 删除 `_on_verify_token` 回调
- [x] 简化 `_on_reconfigure`:只切到 configure step + 重置 relay_url + invalidate
- [x] 删除 `_on_connect_relay` 中 setup_token 防御代码
- [x] 删除 `_on_start_oauth` 中 setup_token 防御代码
- [x] `AuthSetupView.__init__` 移除 `is_local` 参数,初始 step 只看 `_is_already_configured()`
- [x] `AuthSetupWsView.connect` 删除 `current_client_ip` 读取和 `is_loopback_ip` 判断,直接 `AuthSetupView()`
- [x] `_ws_host` / `_ws_secure` 仍然保留(OAuth callback URL 构造需要)
- [x] 删除 `_print_setup_token_console` 函数(reconfigure 不再触发 token 重新激活)
- [x] 删除模块顶部不再使用的 `_clear_auth_config` 静态方法(若确认无引用)

### 测试

- [x] 新增 `tests/test_setup_token_login.py`:GET token_active/inactive 分支、POST 错误 token 401、POST 正确 token 302+Set-Cookie、签发 session 的 sub/provider/ttl 字段、`next` 参数透传
- [x] 新增 `tests/test_login_view.py`:`/auth` → `/auth/login` 重定向、`/auth/login` 渲染、`?next=` 安全校验、`?msg=` 白名单
- [x] 更新 `tests/test_auth_setup_view.py`:删除 is_local / token_input / setup_verified / pending_reconfigure 相关用例,_on_reconfigure 简化为单分支测试
- [x] 扩展 `tests/test_public_access_hardening.py`:
  - `/auth/setup` 未登录 HTTP → 302 到 /auth/login?next=/auth/setup
  - `/auth/setup/ws` 未登录 → 4401
  - `setup:bootstrap` session 访问 /auth/setup → 放行
  - `setup:bootstrap` session 访问 / → 302 到 /auth/setup
  - `setup:bootstrap` session 访问 /api/* → 403
  - 本地未登录访问 /auth/setup → 也 302 到 /auth/login(行为变化验证)
  - 远程 + 已配置 + 未登录访问 / → 302 到 /auth/login(根路径行为变化)
- [x] 更新或新增 `tests/test_providers_view.py`:
  - token 激活 + 无 auth → providers 含 setup-token,auth_enabled=false
  - token 激活 + 已配置 → providers 含 setup-token + 配置项
  - token 失效 → providers 不含 setup-token

### 前端

- [x] 删除 `frontend/src/components/LoginPage.tsx` 整个文件
- [x] `App.tsx`:删除 `LoginPage` import、`authReady` state、auth 探测 useEffect、登录分支
- [x] `index.css`:删除 `.login-page` / `.login-card` 等 React 登录页样式块,以及更早期遗留的 `.login-screen` / `.login-form` / `.login-error` 死代码
- [x] 重新构建 `npm --prefix frontend run build`(让 Python 包内静态产物同步)

### 独立登录页 `/auth/login`

- [x] 新建 `mutbot/src/mutbot/auth/login_view.py`,实现 `LoginPageView(View)`
  - `/auth/login` 渲染纯 HTML 暗色主题登录页,fetch `/auth/providers` 后用原生 DOM 渲染按钮
  - `?next=<path>` 透传给 provider URL,登录回流后中继到原路径
  - 钥匙图标、虚线边框、setup-token hint 内置;`?msg=logged_out|session_expired` 渲染提示
- [x] middleware 直接处理 `/auth` 和 `/auth/` 两种形式 → 302 到 `/auth/login`(URL 规范化,无视登录态;早于鉴权检查执行)
- [x] `_safe_next()` 校验 next 参数,只允许同源相对路径(防 open redirect)
- [x] middleware 把 `/auth/login` 加入 `_PUBLIC_PREFIXES`
- [x] middleware import `mutbot.auth.login_view` 触发 View 注册
- [x] `setup_login.py` 的 GET/POST 都支持 `?next=` / form `next` 透传,登录成功后优先跳 next

### 验证

- [x] `pytest` 全绿
- [x] `pyright` 无新增类型错误
- [x] 手动浏览器验证(5 条路径):
  1. 远程 + 未配置:/ → 主登录页 → Setup Token → 输 token → 进 setup → 完成
  2. 本地 + 未配置:同上(行为变化)
  3. 已配置 + 远程 OIDC 登录:/ → 登录 → 设置页进 setup → Reconfigure → 完成
  4. 已配置 + 未登录:`/auth/setup` 直接 302 到 /(无法绕过)
  5. setup-token 登录后访问 / → 302 到 /auth/setup;访问 /api/health → 403

### 收尾

- [x] git 提交,中文 commit message

## 实施中发现的问题与修复

### Trailing-slash 漏到空响应(已修复)

**现象**:已登录用户访问 `http://host/auth/`(带 trailing slash)拿到空响应,而 `/auth`(不带斜杠)正常 302 到 `/auth/login`。

**根因**:原方案用 `LoginRedirectView(path="/auth")` 处理 `/auth → /auth/login` 的重定向。但 mutio View 的 `path` 是字符串精确匹配,`/auth` ≠ `/auth/`。已登录用户访问 `/auth/` 时:
- middleware 因为已登录走"放行"分支
- `/auth/` 没有任何 View 注册(LoginRedirectView 只匹配 `/auth`)
- 又因为以 `/auth/` 开头但不在白名单具体路径里,也不被静态文件 fallback 命中
- 服务器返回空响应

**修复**:在 middleware 早期(client IP 解析后立即)直接拦截 `/auth` 和 `/auth/` 两种形式,302 到 `/auth/login`,**无视登录态**(URL 规范化是纯前置规则)。同时删除 `LoginRedirectView`(成为死代码)和 `_PUBLIC_EXACT` 配置。

**取舍说明**:把"URL 规范化"硬塞进鉴权 middleware 不是漂亮架构——鉴权层本不该关心 trailing slash。但 mutio 当前缺少 trailing-slash 规范化能力和"一个 View 多 path"语法,middleware 早期拦截是当下成本最低的修复。

**后续清理路径**:mutio 已新建 spec `mutio/docs/specifications/feature-url-normalization-and-route-helpers.md`,提案 trailing-slash 规范化 + `View.path` 列表 + `RedirectView` 基类。mutio 实施完成后,mutbot 应:
1. 删除 middleware 里 `/auth` / `/auth/` 早期拦截块及 4 个对应测试
2. 注册 `class AuthRedirect(RedirectView): path="/auth"; target="/auth/login"`(或 `path=["/auth","/auth/"]`)
3. 重新跑测试验证行为一致

预计 1 个 commit、~10 分钟工作量。

**测试**:`tests/test_public_access_hardening.py` 新增 4 个用例覆盖 `/auth`、`/auth/`、安全 next 透传、不安全 next 丢弃。
