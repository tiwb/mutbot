# Auth Setup 用 mutgui 重写

**状态**：✅ 已完成
**日期**：2026-04-24
**类型**：重构（同时引入 mutgui 框架）

## 需求

- mutbot 的 `/auth/setup` 当前是 970 行 Python 字符串拼 HTML（`auth/views.py` 的 `_render_setup_page`），多步骤向导（token_input → configure → select_provider → already_configured），全部通过 form POST 串联
- 用 mutgui 重写，**直接替换**，不保留旧 HTML 实现
- 这是 mutbot 引入 mutgui 框架的**第一阶段**，承担两个目标：
  1. 交付一个更易维护的 setup 页面（声明式 View 替代字符串模板）
  2. 验证 mutgui 在 mutbot 项目中作为独立 standalone 应用运行的完整链路（部署形态、Channel 适配、依赖管理、认证集成）
- 后续阶段（不在本规范范围内）：LLM 配置面板（嵌入主前端）→ 启动配置 + Terminal（自定义组件）→ Agent

## 在迁移路线中的定位

`setup auth` 是最适合作为第一阶段的目标：

- **完全独立路由**（`/auth/setup`），不与主 React 前端耦合 → 集成失败回滚成本极低
- **不需要嵌入到 mutbot 主前端**，纯 standalone mutgui 应用 → 可独立验证 mutgui 的部署、Channel 适配、antd 组件能力
- **多步骤向导 + 表单**正是 mutgui 的强项（antd Form/Input/Select/Button 全就绪）
- 旧实现 970 行 HTML/CSS 拼字符串，重写后预计 < 250 行声明式 Python，**净减代码**

后续阶段会基于本阶段验证的链路做扩展（Channel 适配复用、antd 主题复用、依赖管理已就位）。

## 前置工作 — mutbot import 升级到 mutio.net

**现状**：mutbot 全部 `from mutagent.net.*` import（13 处），而 `mutagent.net` 现在只是 re-export `mutio.net.*` 的兼容 shim（见 `mutagent/src/mutagent/net/server.py`、`__init__.py`）。实际 `View`、`WebSocketView`、`Server` 等类的实现已经搬到 `mutio.net`。

**决策**：作为本阶段的**前置步骤**，先把 mutbot 中所有 `mutagent.net.*` import 全量替换为 `mutio.net.*`，再开始 setup auth 重写。理由：
- 新写的 `AuthSetupView` / `AuthSetupWsView` 应该直接 import `mutio.net`，否则又往 shim 里加新债
- 全量替换是**纯机械性 sed**，风险极低，半小时完事
- 替换后可以删掉对 `mutagent.net` shim 的隐式依赖（只剩 mutagent 自身用）

**待替换文件**（13 处）：
- `mutbot/src/mutbot/auth/middleware.py`
- `mutbot/src/mutbot/auth/relay.py`
- `mutbot/src/mutbot/auth/views.py`
- `mutbot/src/mutbot/builtins/http_client.py`
- `mutbot/src/mutbot/cli/pysandbox.py`
- `mutbot/src/mutbot/proxy/routes.py`
- `mutbot/src/mutbot/ptyhost/__main__.py`
- `mutbot/src/mutbot/web/mcp.py`
- `mutbot/src/mutbot/web/routes.py`
- `mutbot/src/mutbot/web/server.py`（3 处）
- `mutbot/src/mutbot/web/transport.py`

**`mutbot/pyproject.toml`** 同步：声明 `mutio` 为直接依赖（当前应该只有 `mutagent` 间接依赖），版本约束按当前 mutio 版本。

**测试验证**：替换后跑现有测试套，确保无回归（import 等价替换，行为不变）。

## 关键参考

### mutbot 侧

- `mutbot/src/mutbot/auth/views.py:518-680` — `AuthSetupView` 路由 + GET/POST 处理（含本地/远程判断、setup token 验证 cookie）
- `mutbot/src/mutbot/auth/views.py:760-969` — `_render_setup_page` HTML 模板（**待删除**）
- `mutbot/src/mutbot/auth/views.py:688-726` — `_validate_relay_url` SSRF 防护（**保留**）
- `mutbot/src/mutbot/auth/views.py:733-757` — setup verified cookie 工具（**重新评估**，见设计方案）
- `mutbot/src/mutbot/auth/setup.py` — `store_setup_nonce` / `save_auth_config` / nonce TTL（**保留**）
- `mutbot/src/mutbot/auth/setup_token.py` — setup token 生命周期（`is_active` / `verify` / `invalidate`，**保留**）
- `mutbot/src/mutbot/auth/network.py` — `is_loopback_ip`（**保留**）
- `mutbot/src/mutbot/auth/middleware.py` — `current_client_ip` ContextVar（**保留**）
- `mutbot/docs/archive/2026-03-19-feature-auth-setup-wizard.md` — 旧 setup 向导设计（背景参考）

### mutgui 侧

- `mutgui/src/mutgui/__init__.py` — 公开 API：`View`、`ViewBlock`、`ViewPort`、`Channel`、`Bind`、`Callback`
- `mutgui/src/mutgui/channel.py:21-24` — `Channel.send(message: dict)` 接口
- `mutgui/src/mutgui/_viewport_impl.py:24-52` — `ViewPort` 生命周期（`initialize` / `handle_event` / `detach`）
- `mutgui/demo/standalone/starlette.py` — **核心参考实现**，展示如何在非 mutgui 自带服务器上集成（Channel 适配 + WebSocket handler + HTML mount + 静态文件）
- `mutgui/demo/examples/antd.py` — antd Form/Input/Checkbox/Select 用法
- `mutgui/frontend/src/standalone.tsx:162-176` — `MutguiApp.mount(el, wsUrl, plugins)` 前端挂载 API
- `mutgui/src/mutgui/static/` — 前端构建产物：`mutgui.js`、`mutgui-antd.js`

### mutio 侧（独立后的 web 框架，原 mutagent.net 实现已迁入）

- `mutio/src/mutio/net/server.py:148` — `View` 基类（HTTP）
- `mutio/src/mutio/net/server.py:170` — `WebSocketView` 基类
- `mutio/src/mutio/net/server.py:64` — `WebSocketConnection.send`/`receive` 接口
- `mutio/src/mutio/net/server.py:204` — `html_response`
- `mutagent/src/mutagent/net/__init__.py` — 兼容 shim（仅 re-export，**待 mutbot 升级后逐步淘汰**）

## 设计方案

### 整体形态

旧实现是**纯 HTTP**（每次 form POST → 服务端渲染整页 HTML 返回）。新实现是 **HTTP + WebSocket 混合**：

- **HTTP**：保留 `GET /auth/setup` 返回挂载 mutgui 的 HTML 壳子；保留 OAuth 跳转的 302 重定向（不能用 WebSocket）
- **WebSocket**：新增 `/auth/setup/ws`，承载 mutgui View 的协议消息（按钮点击、输入回调、状态更新）

这样旧的"判断本地/远程 → 显示不同初始页面 / 验证 token"等逻辑全部下沉到 mutgui View 内部，不再需要服务端 HTML 分支渲染。

### View 结构

设计**单一 View 类** `AuthSetupView(mutgui.View)`，内部状态机替代旧的 `step` 参数。状态字段：

```python
class AuthSetupView(View):
    step: str  # "token_input" | "configure" | "select_provider" | "already_configured" | "redirecting"
    error: str
    relay_url: str
    providers: list[dict]  # [{name, label}, ...]
    setup_verified: bool   # 已通过 token 验证
    is_local: bool         # 当前连接是否本地（构造时确定）
```

`render()` 根据 `step` 返回不同的 antd 控件树。所有按钮 `onClick`、输入 `onChange` 通过 `Callback` / `Bind` 触发回到 Python 的方法。

不同 step 的内容（按钮组、输入框）就地写在 `render()` 里，用 `if step == "..."` 分支返回对应 children，不拆成多个子 View（旧实现也是单一函数 + step 分支，沿用）。

### Reconfigure 入口

`already_configured` 步骤显示当前配置摘要 + 两个按钮：
- "Back to MutBot"（同旧实现）
- "Reconfigure" — 触发重置流程

**安全考虑**：`/auth/setup` 路径在 middleware 中是公开白名单（`/auth/` 前缀全部放行），任何远程未鉴权用户都能访问。如果 Reconfigure 直接清空配置，会成为接管 mutbot 的攻击口子（远程攻击者点击 → 清空 auth → 用自己的 relay 接管）。

**准入控制**：复用 setup token 机制（与 token_input 路径同款）。流程：

1. 用户点击 "Reconfigure"
2. View 检查准入：本地请求直接放行；远程请求 → 触发 setup token 重新激活（调用 `setup_token` 模块的激活逻辑，控制台打印新 token）→ 切换到 `token_input` 步骤
3. 远程用户输入正确 token → `setup_verified = True` → 自动进入清空配置 + `step = "configure"`
4. 本地用户直接执行清空配置 + `step = "configure"`

**Token 重新激活**：调用 `setup_token.generate()`（已存在），并复用 `web/server.py:_print_security_warning` 风格的控制台打印（直接 `print` 即可，或抽出一个小函数 `print_setup_token(token)` 复用）。远程用户在 mutbot 控制台读取新 token。

**新状态字段**：
```python
pending_reconfigure: bool  # 标记 token 验证后应执行清空，而非进入 configure
```

token_input → verify_token 成功后，根据 `pending_reconfigure` 决定下一步：True → 清空配置 + `step = "configure"`；False → `step = "configure"`（首次配置流程）。

### 多客户端 / 多次访问处理

mutgui 默认所有 ViewPort 共享同一个 View 实例 → 多个浏览器 tab 同时打开 setup 页会互相干扰。

**决策**：每个 WebSocket 连接独立创建一个 `AuthSetupView` 实例。`AuthSetupView` 是临时向导状态，无需多端同步。参考 `mutgui/demo/standalone/starlette.py` 但改为 per-connection 而非共享。

### Setup token 验证 — 简化为 WebSocket 内验证

旧实现用短期 httponly cookie（`mutbot_setup_verified`，TTL 5 分钟）跨 form POST 维持"已验证"状态。

**新实现取消 cookie**：`AuthSetupView` 实例内存中保持 `setup_verified` 字段。同一 WebSocket 连接全程是同一个 View 实例，不需要持久化跨请求验证状态。

**断线重连场景**：刷新页面或 WS 断开 → 新建 View 实例 → `setup_verified` 重置为 False → 远程用户需重新输入 token。这是合理行为（5 分钟内重连本就是边缘场景，旧 cookie TTL 也是 5 分钟）。

→ 删除 `_set_setup_verified_cookie` / `_check_setup_verified_cookie` / `_SETUP_COOKIE_NAME` 相关代码。

### 本地 vs 远程判断时机

旧实现在 `GET /auth/setup` 时通过 `current_client_ip` ContextVar 判断。

**新实现**：在 WebSocket 连接建立时获取 client IP，传给 `AuthSetupView` 构造函数（`is_local` 字段）。后续整个会话期间 `is_local` 不变。

WebSocket 同样支持 `current_client_ip` ContextVar（mutbot 已有的 middleware 在 ASGI 层设置），可以直接复用。需在 `AuthSetupServeView`（WebSocket 处理类）的 `connect()` 里读取并传入。

### Relay URL 校验与 provider 加载

**复用** `_validate_relay_url`（SSRF 防护）和 `_fetch_relay_providers`（HTTP 请求 relay）函数。新 View 在用户点击"Connect"按钮时调用：

```python
async def _on_connect_relay(self) -> None:
    err = _validate_relay_url(self.relay_url)
    if err:
        self.error = err
        self.invalidate()
        return
    providers = await _fetch_relay_providers(self.relay_url)
    if not providers:
        self.error = f"Cannot connect to relay server: {self.relay_url}"
        self.invalidate()
        return
    self.providers = [{"name": n, "label": _humanize(n)} for n in providers]
    self.step = "select_provider"
    self.error = ""
    self.invalidate()
```

mutgui Callback 支持 async 方法，可直接 `await`。

### OAuth 跳转

旧实现 `_handle_start_oauth` 在 form POST 后返回 `Response(status=302, location=login_url)`。WebSocket 不能直接做 302。

**新实现**：用户点击 provider 按钮 → 后端在 `_on_start_oauth` 中：
1. 创建 nonce 并调用 `store_setup_nonce`（与旧实现一致）
2. 构造 OAuth URL
3. 通过 mutgui 的能力让前端 `window.location.href = oauth_url`

mutgui 没有内置 "前端跳转 URL" 的标准组件。**方案**：通过自定义组件 `mutbot.Redirect` 实现（详见下文「mutbot 自定义组件」段落）。后端 render 时根据 `step == "redirecting"` 输出 `{"$component": "mutbot.Redirect", "url": login_url}`，前端 `useEffect` 触发 `location.href`。

### "Already configured" 分支

旧实现在 `GET /auth/setup` 时检查配置存在，直接渲染 `already_configured` 页面。

**新实现**：在 WebSocket 连接建立后，View 的 `__init__` 或 `initialize` 钩子里检查配置，命中则直接设置 `step = "already_configured"`。

### HTTP 入口

`GET /auth/setup` 简化为返回固定 HTML 壳（< 30 行）：

```html
<!DOCTYPE html>
<html><head>
  <title>MutBot Setup</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
  <div id="app"></div>
  <link rel="stylesheet" href="/static/setup.css">
  <script type="module" src="/static/setup.js"></script>
</body></html>
```

`setup.js` 由 mutbot frontend vite 构建产出（详见下文「集成方式」与「目录结构」），内部完成：import mutgui → 注册 `Redirect` 自定义组件 → 调用 `MutguiView` 挂载到 `#app` → 建立 WebSocket 到 `/auth/setup/ws`。

`POST /auth/setup` **完全删除**（不再有 form 提交）。

### 集成方式 — npm 包 (file: dep)

mutgui 提供两种前端集成模式：

| 模式 | 形态 | 适用场景 |
|------|------|----------|
| Standalone（script tag） | `mutgui.js` + `mutgui-antd.js`，自带 React/antd，IIFE 全局对象 | 宿主无 React 应用（纯 HTML、Python demo、其他框架） |
| npm 包（`@mutgui/core`） | ESM 模块，react/antd 作为 external，宿主提供 | 宿主已有 React 应用（如 mutbot 主前端） |

**本阶段走 npm 包**。决定理由：

- mutbot 主前端已是完整 React 19 应用，使用 standalone 会导致**两份 React** → hooks 报错、context 隔离 bug
- 重复 800KB antd 资源浪费
- mutbot 后续 LLM 配置面板要把 mutgui 嵌入主前端 dock，必须共享 React 实例 — 那时只能走 npm 包，本阶段先打通这条路
- 类型检查端到端、tree shaking 生效、组件可直接 `import` 复用

**依赖方式**：`mutbot/frontend/package.json` 用 `"@mutgui/core": "file:../../mutgui/frontend"` 引用本地 mutgui 已构建的 npm 包产物（`mutgui/frontend/dist/`）。

**工作流**：
- 改 mutgui 源码 → `npm --prefix mutgui/frontend run build` 重新生成 `dist/`
- 改 mutbot 前端 → `npm --prefix mutbot/frontend run build` 把 mutgui dist + mutbot 自己的代码打成一个 bundle

不上 npm workspaces，本阶段 file: dep 简单够用。

### 目录结构

mutbot 现有 `mutbot/frontend/src/` 主前端**完全不动**，新增 `mutbot/frontend/setup/` 与 `src/` 平级：

```
mutbot/frontend/
├── src/                       # 现有主前端，零改动
│   ├── App.tsx
│   ├── components/
│   ├── panels/
│   ├── lib/
│   └── main.tsx               # 现有 entry
├── setup/                     # 新增
│   ├── index.tsx              # entry: 注册 Redirect + mount mutgui
│   └── Redirect.tsx           # 自定义组件
├── package.json               # 新增 "@mutgui/core": "file:../../mutgui/frontend"
└── vite.config.ts             # 改为多 entry: main + setup
```

**为什么不大范围挪文件**：保持 src/ 不动避免 git history 噪音和大批 import 路径调整。setup/ 与 src/ 平级，物理隔离构建/调试互不干扰。

**vite 多 entry 配置示意**（最终以实施为准）：

```ts
build: {
  rollupOptions: {
    input: {
      main: 'src/main.tsx',
      setup: 'setup/index.tsx',
    },
    output: { entryFileNames: '[name].js', assetFileNames: '[name][extname]' },
  },
}
```

**产物**（mutbot vite 输出到 `mutbot/src/mutbot/static/`）：
- `main.js` + `main.css` — 主前端（不变）
- `setup.js` + `setup.css` — setup 页（新增）

### mutbot 自定义组件 — `mutbot.Redirect`

需要一个组件让前端跳转 URL（OAuth 流程触发）。这是 mutbot 第一个 mutgui 自定义组件。

**位置**：`mutbot/frontend/setup/Redirect.tsx`，作为 setup entry 的局部组件。

**注册方式**：在 `setup/index.tsx` 启动时通过 mutgui 的 `registerComponents` 注册到 `mutbot` 命名空间：

```tsx
// setup/index.tsx 示意（最终以实施为准）
import { MutguiView, registerComponents } from '@mutgui/core';
import { Redirect } from './Redirect';

registerComponents({ __name__: 'mutbot', Redirect });

// ... mount MutguiView 到 #app，建立 WebSocket
```

**Redirect 组件本体**：

```tsx
// setup/Redirect.tsx
import { useEffect } from 'react';

export function Redirect({ url }: { url: string }) {
  useEffect(() => { window.location.href = url; }, [url]);
  return <div>Redirecting...</div>;
}
```

后端 render 时根据 `step == "redirecting"` 输出 `{"$component": "mutbot.Redirect", "url": login_url}`，前端 `useEffect` 触发跳转。

**未来复用**：阶段 2 的 LLM 配置面板若也需要 Redirect / 其他 mutbot 自定义组件，到时再把组件抽到 `mutbot/frontend/uicomponents/` 平级目录。本阶段不预先抽（YAGNI）。



### 依赖声明

**Python 侧**：`mutbot/pyproject.toml` 新增 `mutgui ~=0.1.0`（按当前 mutgui 实际版本，需查确认）。

按 `CLAUDE.md` 的本地开发规范，`uv tool install --editable D:/ai/mutbot --with-editable D:/ai/mutgui ...` 重跑一次即可。

**前端侧**：`mutbot/frontend/package.json` 新增 `"@mutgui/core": "file:../../mutgui/frontend"`。首次安装前需在 mutgui 侧跑一次 `npm --prefix mutgui/frontend run build` 生成 `dist/`。

### 路由注册

mutbot 通过 mutobj `discover_subclasses` 自动发现 `View` / `WebSocketView` 子类。新增的 `AuthSetupView`（HTTP 壳）和 `AuthSetupWsView`（WebSocket 处理）放在 `auth/views.py` 同文件，自动注册。

### 不变 / 保留的接口

- `/auth/relay-callback` 路由（OAuth 回调）— **完全不动**
- `/auth/providers`、`/auth/userinfo` — **完全不动**
- `mutbot.auth.setup.store_setup_nonce` / `pop_setup_nonce` / `save_auth_config` — **完全不动**
- `mutbot.auth.setup_token` 模块 — **完全不动**
- `_validate_relay_url`、`_fetch_relay_providers` — **保留**，从 `views.py` 中保留为模块函数被新 View 调用

### 删除清单

- `_render_setup_page` 函数及其 970 行 HTML/CSS 字符串
- `AuthSetupView.post` 方法及 `_handle_verify_token` / `_handle_connect_relay` / `_handle_start_oauth`
- `_set_setup_verified_cookie` / `_check_setup_verified_cookie` / `_SETUP_COOKIE_NAME` / `_SETUP_COOKIE_MAX_AGE`

### 测试策略

- **单元测试**：`AuthSetupView` 的状态转换可纯 Python 测试（mock `_fetch_relay_providers` 等异步调用）
- **手动浏览器验证**：开发者本人覆盖三个核心路径：
  1. 本地访问 → 直接到 configure 步骤 → 输入 relay URL → 选择 provider → 触发跳转
  2. 远程访问 + 有 setup token → token_input → 输入正确 token → configure → ...
  3. 已配置场景 → 直接显示 already_configured
- **集成测试自动化推迟到阶段 2**（LLM 配置面板）一起搭框架
- 旧 form POST 的测试（如果存在）需要重写或删除

## 待定问题

（无）

## 已确认决策

- **Q1 静态文件路径** → 不再单独挂 mutgui 静态资源；mutbot vite 把 mutgui 打进 `setup.js`，统一从 mutbot `/static/` 提供
- **Q2 自定义组件目录** → `mutbot/frontend/setup/Redirect.tsx`（与 setup entry 同目录），未来复用时再抽到 `frontend/uicomponents/`
- **Q3 Reconfigure 按钮** → 加，但需准入控制（`/auth/setup` 是公开路径，直接放开会成为接管 mutbot 的攻击口子）。本地请求直接放行，远程请求复用 setup token 验证流程
- **Q4 集成测试** → 本阶段只做单元测试 + 手动浏览器验证，不做自动化集成测试
- **集成方式** → npm 包（file: dep），不走 standalone（避免两份 React）

## 消费者场景

无下游消费者（这是终端用户使用的页面，行为通过浏览器手动验证）。

## 实施步骤清单

### 前置 — import 升级

- [x] mutbot 13 处 `from mutagent.net.*` 替换为 `from mutio.net.*`
- [x] `mutbot/pyproject.toml` 把 `mutio` 升为直接依赖，确认版本约束
- [x] 跑现有测试套确认无回归

### 前端构建链改造

- [x] mutgui 跑一次 `npm --prefix mutgui/frontend run build` 生成最新 `dist/`，确认产物完整
- [x] mutbot `frontend/package.json` 加 `"@mutgui/core": "file:../../mutgui/frontend"`
- [x] mutbot `frontend/vite.config.ts` 改为多 entry（main + setup），确认产物输出名稳定（`main.js` / `setup.js`）
- [x] 跑一次 mutbot 前端 build，确认 `main.js` 行为不变（不破坏现有主前端）

### 自定义组件

- [x] 新建 `mutbot/frontend/setup/Redirect.tsx`
- [x] 新建 `mutbot/frontend/setup/index.tsx`：注册 Redirect → mount mutgui → 连 `/auth/setup/ws`

### 后端 — 新 View 和 WebSocket

- [x] 在 `mutbot/src/mutbot/auth/` 新建文件存放 mutgui View 实现（建议 `setup_view.py`，避免污染 `views.py`）
- [x] 实现 `AuthSetupView(mutgui.View)`：状态字段、`render()` 按 step 分支、所有 `_on_*` 异步回调
- [x] 实现 `step in {token_input, configure, select_provider, already_configured, redirecting}` 的 antd 控件树
- [x] 实现 `pending_reconfigure` 标志和 reconfigure 流程（调用 `setup_token.generate` + 控制台打印）
- [x] 在 `auth/views.py` 把旧 `AuthSetupView`（HTTP form 处理）改为只返回 HTML 壳（< 30 行）
- [x] 新建 `AuthSetupWsView(WebSocketView)`，路径 `/auth/setup/ws`，per-connection 创建 `AuthSetupView` 实例 + `ViewPort`，复用 `mutgui/demo/standalone/starlette.py` 风格的 Channel 适配
- [x] WebSocket 连接建立时通过 `current_client_ip` 读取 IP，传入 View 的 `is_local`

### 后端 — 删除清单

- [x] 删除 `_render_setup_page` 函数（970 行）
- [x] 删除旧 `AuthSetupView.post` / `_handle_verify_token` / `_handle_connect_relay` / `_handle_start_oauth`
- [x] 删除 `_set_setup_verified_cookie` / `_check_setup_verified_cookie` / `_SETUP_COOKIE_NAME` / `_SETUP_COOKIE_MAX_AGE`
- [x] 保留并复用：`_validate_relay_url`、`_fetch_relay_providers`、`_create_nonce`、`_get_callback_url`

### 测试

- [x] 新增 `tests/test_auth_setup_view.py`：覆盖 View 状态转换（mock `_fetch_relay_providers` 等）
- [x] 删除/重写旧 form POST 测试（如有）
- [x] 手动浏览器验证三条路径：本地直接配置 / 远程 token_input / already_configured + Reconfigure（本地 + 远程两种）

### 收尾

- [x] 跑 `pytest`，全绿
- [x] 跑 `pyright`（mutbot 项目使用 pyright，不是 mypy），无新增类型错误
- [x] 检查相关设计文档（`docs/archive/2026-03-19-feature-auth-setup-wizard.md` 等）是否需要补一句"已被 mutgui 重写覆盖"

