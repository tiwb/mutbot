# 修复反向代理环境下 origin 推算错误 设计规范

**状态**：✅ 已完成
**日期**：2026-03-30
**类型**：Bug修复

## 需求

1. Auth Setup 向导在反向代理（nginx/k8s ingress）后面运行时，生成的 OAuth 回调 URL 指向 `http://127.0.0.1:8741` 而非外部域名（如 `https://xgui.netease.com`），导致浏览器跳转失败
2. Mobile Connect 二维码同样显示内部 IP 地址，而非外部可达的域名

## 关键参考

- `mutbot/src/mutbot/builtins/menus.py:482-510` — `_get_self_origin()`，从 config listen 推算 origin，`0.0.0.0` 硬编码为 `127.0.0.1`
- `mutbot/src/mutbot/builtins/menus.py:339-401` — `MobileConnectMenu`，枚举本机 IP 生成二维码地址
- `mutbot/src/mutbot/builtins/menus.py:463-479` — `AuthSetupMenu`，调用 `_get_self_origin` 获取 callback URL
- `mutbot/src/mutbot/auth/views.py:91-106` — `_is_secure()` 和 `_get_callback_url()`，HTTP 路径的正确实现（从 `Host` + `X-Forwarded-Proto` 推算）
- `mutbot/src/mutbot/auth/setup.py:98-106` — 向导中使用 `self_origin` 构造 callback URL
- `mutbot/src/mutbot/web/routes.py:234-286` — `WorkspaceWebSocket.connect()`，WebSocket 握手处理，创建 Client
- `mutbot/src/mutbot/web/transport.py:199-231` — `Client.__init__()`，当前不保存 headers
- `mutagent/src/mutagent/net/server.py:64-71` — `WebSocketConnection` Declaration，无 headers 属性
- `mutagent/src/mutagent/net/_server_impl.py:356-374` — `_make_ws_connection()`，ASGI scope 中有 headers 但未传递给 WebSocketConnection
- `mutbot/src/mutbot/web/rpc.py:40-57` — `RpcContext`，通过 `sender_ws` 可找到 Client

## 设计方案

### 根本原因

WebSocket RPC 通道（auth setup 向导、mobile connect）需要知道客户端访问的外部 origin，但当前链路中 ASGI scope 里的 HTTP headers 在 WebSocket 握手时被丢弃：

```
ASGI scope (含 headers) → _make_ws_connection() [丢弃 headers] → WebSocketConnection [无 headers]
                        → Client [无 headers] → RpcContext [无法推算 origin]
```

### 修复方案：三层 origin 获取

> 设计变更记录：原方案在 RpcMenu params 中传 origin，review 后认为 origin 是连接级属性而非 RPC 调用参数，改为以下分层方案。

**第一层：mutagent — WebSocketConnection 增加 headers（基础设施完善）**

`WebSocketConnection` Declaration 增加 `headers` 属性，`_make_ws_connection()` 从 ASGI scope 提取并传入（与 `_make_request()` 对 HTTP Request 的处理方式一致）。（✅ 已完成）

**第二层：mutbot — Client 从握手 headers 提取 origin（初始值）**

`Client` 创建时从 `WebSocketConnection.headers` 中的 `Host` + `X-Forwarded-Proto` 计算 `origin`，作为初始值。反向代理配置正确时即可用。

**第三层：mutbot — 前端 RPC `client.setInfo` 更新 origin（权威值）**

前端 WebSocket 连接建立后，发送 `client.setInfo` RPC 推送 `{origin: window.location.origin}`，覆盖 header 推算值。这是最准确的来源（浏览器地址栏），且模式可扩展——未来传 viewport、timezone 等都走此通道。

**消费方式**：Menu handler 通过 `context.get_sender_client().origin` 读取。

### MobileConnect 行为调整

- 统一只显示**当前访问地址**一个（从 `client.origin` 获取）
- 移除多地址枚举逻辑
- 二维码放大，URL 显示在下方

### base_path 处理

origin 只含 scheme+host，`base_path` 继续从 config 读取（与 `_get_callback_url` 一致）。

## 实施步骤清单

- [x] mutagent: `WebSocketConnection` 增加 `headers` 属性，`_make_ws_connection()` 从 ASGI scope 提取 headers 传入
- [x] mutbot 后端: `Client` 创建时从 WebSocket headers 提取 origin
- [x] mutbot 后端: 注册 `client.setInfo` RPC handler，前端可更新 client 元信息（origin 等）
- [x] mutbot 前端: WebSocket 连接建立后发送 `client.setInfo` 推送 origin
- [x] mutbot 后端: `_get_self_origin` 改为从 `client.origin` 读取
- [x] mutbot 后端: `MobileConnectMenu.execute()` 重写为单地址模式
- [x] mutbot 前端: MobileConnect 弹窗调整为单二维码大图 + URL 在下方的布局
- [x] 构建前端并本地验证
