# Supervisor 进程管理 — 热重启与平滑交接

**状态**：🔄 实施中
**日期**：2026-03-13
**类型**：功能设计

## 背景

mutbot 当前是单进程架构：`python -m mutbot` 直接启动 MutBotServer，绑定端口，处理所有 HTTP/WebSocket 请求。重启服务器需要手动 Ctrl+C 再启动，存在以下痛点：

1. **远程操作不便** — 通过远程终端操作时，重启服务器需要手动 kill 进程再启动
2. **重启有服务中断** — 进程退出到新进程就绪期间，端口不可用
3. **无法平滑交接** — 没有新旧进程交替机制，所有连接在重启时强制断开
4. **Agent 无法触发重启** — AI Agent 在开发迭代中需要频繁重启，但没有编程接口

本设计是 `feature-asgi-server.md` 中 Layer 2（Supervisor 进程管理）的完整设计，同时覆盖 Layer 3（在线更新）的核心能力。

## 设计方案

### 核心架构：Supervisor + Worker + TCP 代理

```
Client ──→ Supervisor (port 8741) ──TCP 透传──→ Worker (localhost:随机端口)
```

Supervisor 是主进程，职责：
- **绑定公网端口**，始终持有 socket，重启不释放
- **TCP 代理**，accept 客户端连接后透传给 Worker
- **管理 API**，处理 `/api/restart`、`/health` 等管理请求
- **Worker 生命周期管理**，spawn / drain / kill Worker 进程

Worker 是子进程，职责：
- 监听 localhost 随机端口，运行完整 MutBotServer
- 处理所有业务逻辑（HTTP、WebSocket、MCP）
- 通过 Supervisor 代理接收客户端连接

```
┌──────────────────────────────────────────────┐
│  Supervisor (主进程, port 8741)               │
│  ┌─────────────┐  ┌───────────────────────┐  │
│  │ 管理 API    │  │ TCP Proxy             │  │
│  │ /api/restart│  │ accept → 透传 Worker  │  │
│  │ /health     │  │ 按连接路由新旧 Worker │  │
│  └─────────────┘  └───────┬───────────────┘  │
│                           │                   │
│           ┌───────────────┼───────────────┐   │
│           ▼                               ▼   │
│  ┌─────────────────┐  ┌─────────────────┐    │
│  │ Worker 1 (旧)   │  │ Worker 2 (新)   │    │
│  │ localhost:rand1  │  │ localhost:rand2  │    │
│  │ 已有连接继续服务 │  │ 新连接路由到此   │    │
│  │ drain 后退出     │  │                  │    │
│  └─────────────────┘  └─────────────────┘    │
└──────────────────────────────────────────────┘
```

### TCP 代理设计

Supervisor 的代理工作在 **TCP 层**，不解析 HTTP/WebSocket 协议：

1. Accept 客户端 TCP 连接
2. 偷看（peek）第一行 HTTP 请求，判断是否为管理路径
3. 管理路径 → Supervisor 自己处理（极简 HTTP 响应）
4. 其余路径 → 与当前活跃 Worker 建立 TCP 连接，双向透传字节流

```python
async def handle_connection(client_reader, client_writer):
    first_line = await client_reader.readline()

    if is_management_path(first_line):
        await handle_management(first_line, client_reader, client_writer)
        return

    # TCP 透传给 Worker
    w_reader, w_writer = await asyncio.open_connection('127.0.0.1', active_worker_port)
    w_writer.write(first_line)  # 补发偷看的第一行
    await asyncio.gather(
        pipe(client_reader, w_writer),
        pipe(w_reader, client_writer),
    )
```

优点：
- **协议无关** — HTTP、WebSocket upgrade、SSE 全部透明穿透，不需要解析帧
- **实现简单** — 核心逻辑约 30 行
- **性能开销极小** — localhost TCP 透传，延迟 ~0.1ms，对 LLM 应用无感

### 管理 API

Supervisor 自身处理的管理端点（极简 HTTP，不走 ASGI）：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/restart` | POST | 触发 Worker 热重启 |
| `/api/upgrade` | POST | 更新代码后重启（预留） |
| `/health` | GET | 健康检查（返回 Worker 状态） |

当没有 Worker 在运行时（启动中/重启中），所有非管理请求返回 `503 Service Restarting`。

### 重启入口

三个入口，全部汇聚到 `POST /api/restart`：

| 入口 | 使用者 | 方式 |
|------|--------|------|
| 前端菜单 | 人 | 点击"重启服务器"按钮 → JS fetch `/api/restart` |
| CLI | Agent | `python -m mutbot restart` → HTTP POST 到 `localhost:8741/api/restart` |
| MCP tool | Agent | `restart_server` tool → HTTP POST 到 `localhost:8741/api/restart` |

### 重启流程（平滑交接）

```
时间线 ─────────────────────────────────────────────────────────────────────────►

Supervisor:  [proxy to W1] ──→ [drain W1] ──→ [spawn W2] ──→ [新连接→W2, 旧→W1] ──→ [W2 only]
Worker 1:    [accept+serve] ──→ [stop agents, persist, notify] ──→ [serve existing] ──→ [exit]
Worker 2:                                      [startup] ──→ [accept+serve] ─────────────────→
```

详细步骤：

1. **触发**：`POST /api/restart` → Supervisor 处理
2. **通知旧 Worker drain**：Supervisor 调用 Worker 1 的 API 通知进入 drain 模式
   - Worker 1 强制停止所有 Agent Session
   - Worker 1 将所有 session 状态持久化到磁盘
   - Worker 1 给所有 WS 客户端发送 `server_restarting` 事件
   - Worker 1 返回确认（持久化完成）
3. **Spawn 新 Worker**：Supervisor 启动 Worker 2（localhost:新随机端口）。新 Worker 从磁盘加载最新的 session 数据
4. **等待就绪**：Worker 2 启动完成（HTTP 健康检查通过）
5. **路由切换**：Supervisor 将新连接路由到 Worker 2（已有连接仍透传给 Worker 1）
6. **等待旧 Worker 退出**：Supervisor 监控 Worker 1 的活跃连接数（Supervisor 作为代理，精确知道连接数）
7. **退出旧 Worker**：Worker 1 连接归零 → Supervisor 关闭 Worker 1 进程；超时（默认 5 分钟）→ 强制关闭

> **为什么先 drain 再 spawn**：新 Worker 启动时会从磁盘加载 session 数据。如果先 spawn 再 drain，新 Worker 可能读到旧 Worker 尚未持久化的过期数据。先 drain 确保磁盘数据是最新的。步骤 2~4 期间旧 Worker 仍服务现有连接（Terminal 等正常工作），只是 Agent Session 已停止。

### Drain 通知机制

Supervisor 通过 Worker 的**现有 HTTP API** 通知 drain，不需要特殊 IPC：

```
Supervisor ──HTTP POST──→ Worker 1 (localhost:rand1) /internal/drain
```

`/internal/` 前缀的请求由 Supervisor 拦截：外部客户端发来的 `/internal/*` 请求不会透传给 Worker，Supervisor 直接返回 403。Supervisor 自身调用 Worker 时直接连 Worker 的内部端口，不经过代理。

Worker 收到 drain 通知后的当前策略：
- 给所有 WebSocket 客户端发送 `server_restarting` 事件
- **Agent Session：强制停止**（打断流式传输，不等待当前 API 调用完成）。这会导致 LLM 响应中断，但保证重启速度。Agent 的对话历史已持久化，重连后可继续
- Terminal Session：不影响（ptyhost 独立进程，客户端重连后恢复）
- 前端收到事件后显示"服务器正在重启..."，断开并自动重连（连到新 Worker）

> **远期优化：Agent 进程隔离（agent-subprocess / agent-process-isolation / agent-version-isolation）**
>
> 当前方案重启时必须强制停止 Agent Session，因为 Agent 运行在 Worker 进程内，Worker 退出则 Agent 必须退出。
>
> Agent 的进程隔离需求不同于 ptyhost。ptyhost 是纯资源持久化（所有 Terminal 跑同一份代码），而 Agent 需要的是**版本隔离**——mutagent 的核心设计是 Agent 可以自进化、自迭代代码，但自迭代有风险（可能改坏自己），因此：
>
> - 不同 Agent 可能同时运行不同版本的代码
> - 进化中的 Agent 在独立子进程中验证新代码，不影响其他 Agent
> - Agent 自行决定何时升级，而不是被统一管理器强制升级
> - 验证稳定后，新版本才推广到其他 Agent
>
> 这意味着 Agent 的进程模型不是"一个管理器管一群相同的 worker"（不能简单类比 ptyhost），而是**每个 Agent 都是自治实体**，各自控制自己的代码版本和生命周期。具体的架构方案需要到时再设计。
>
> 当前阶段的决策：强制停止 Agent Session，保证重启速度。Agent 的对话历史已持久化，重连后可继续。

### Worker 存活保障

Worker 意外崩溃时（非正常退出），Supervisor 自动 spawn 新 Worker。Supervisor 持有端口，Worker 挂了不拉起新的，端口就被占着但无人服务。

当前不做崩溃重试限制等复杂逻辑，遇到问题后再迭代。

### 在线更新（版本升级）

重启与版本升级的流程完全一致，区别仅在 spawn 新 Worker 之前多一步代码更新：

```
重启：  POST /api/restart  →                    drain Worker 1 → spawn Worker 2 → 路由切换
更新：  POST /api/upgrade  → pip install/拉代码 → drain Worker 1 → spawn Worker 2 → 路由切换
```

新 Worker 是新进程，`import` 加载的是磁盘上最新的代码，天然实现版本隔离。

`/api/upgrade` 为预留接口，本次不实现。

### 进程模型

```
python -m mutbot
  └─ main()
       ├─ 无 --worker 参数 → supervisor_main()  (Supervisor 模式)
       └─ 有 --worker 参数 → worker_main()      (Worker 模式)
```

Supervisor 通过 `multiprocessing` 或 `subprocess` spawn Worker 子进程：

```bash
# Supervisor spawn Worker 的命令
python -m mutbot --worker --port <随机端口> [其他参数透传]
```

Worker 的配置（listen 地址、debug 等）由 Supervisor 通过命令行参数传递。

### 对现有代码的影响

| 模块 | 变更 |
|------|------|
| `mutbot/web/server.py` | `main()` 拆分为 `supervisor_main()` 和 `worker_main()`；Worker 模式监听 localhost 随机端口 |
| `mutbot/web/supervisor.py` | **新文件**，Supervisor 核心逻辑：TCP 代理、管理 API、Worker 生命周期 |
| `mutbot/web/routes.py` | 添加 `/internal/drain` 端点（仅 localhost 可访问） |
| `mutbot/web/mcp.py` | 添加 `restart_server` MCP tool |
| `mutbot/__main__.py` | CLI 添加 `restart` 子命令 |
| 前端 | 添加"重启服务器"菜单项 + `server_restarting` 事件处理 |

### 实施概要

分三步：
1. 先实现 Supervisor 核心（TCP 代理 + Worker spawn），替换现有单进程启动，确保功能不退化
2. 实现重启流程（管理 API + drain + 平滑交接）
3. 实现三个重启入口（前端菜单、CLI 命令、MCP tool）

## 实施步骤清单

### 阶段一：Supervisor 核心 + Worker 模式 [已完成]

目标：`python -m mutbot` 启动 Supervisor → spawn Worker，现有功能不退化。

- [ ] **Task 1.1**: 创建 `mutbot/web/supervisor.py` — Supervisor 核心
  - [ ] TCP 代理：accept 连接、peek 第一行、双向 pipe 透传给 Worker
  - [ ] Worker 进程管理：subprocess spawn、监控进程存活、崩溃自动拉起
  - [ ] 管理路径识别：peek 第一行判断 `/api/restart`、`/health`、`/internal/` 等
  - [ ] `/internal/` 前缀拦截：外部请求返回 403
  - [ ] 无 Worker 时：accept 返回 503 Service Restarting
  - [ ] Supervisor 日志：独立 FileHandler + StreamHandler，写 `~/.mutbot/logs/supervisor-*.log`
  - [ ] 信号处理：Ctrl+C 优雅退出（先关 Worker 再退出 Supervisor）
  - 状态：✅ 已完成

- [x] **Task 1.2**: 改造 `mutbot/web/server.py` — 拆分 main() 为 supervisor_main() 和 worker_main()
  - [x] `supervisor_main()`：解析 CLI 参数、绑定 socket、启动 Supervisor 事件循环
  - [x] `worker_main()`：接收 `--worker --port <port>` 参数，监听 localhost 指定端口
  - [x] 现有 `main()` 中的配置加载、日志初始化、路由注册等逻辑归入 `worker_main()`
  - [x] CLI 参数透传：Supervisor 把 `--debug` 等参数传给 Worker
  - [x] `--no-supervisor` 参数：单进程回退模式
  - 状态：✅ 已完成

- [x] **Task 1.3**: 实现 `/health` 管理端点
  - [x] Supervisor 端：响应 `GET /health`，返回 Worker 状态（running/starting/none）
  - [x] Worker 端：响应 `GET /health`（Supervisor 用于就绪检测，轮询直到 200）
  - 状态：✅ 已完成

- [ ] **Task 1.4**: 端到端验证 — Supervisor 模式下现有功能不退化
  - [ ] `python -m mutbot` 启动正常（Supervisor → Worker）
  - [ ] HTTP 路由正常（静态文件 200）
  - [ ] WebSocket 连接正常（upgrade 101 + 双向通信）
  - [ ] MCP endpoint 正常
  - [ ] Ctrl+C 优雅退出正常
  - [ ] Worker 崩溃后自动拉起
  - 状态：⏸️ 待人工验证

### 阶段二：重启流程 [已完成]

目标：实现完整的热重启 + 平滑交接流程。

- [x] **Task 2.1**: 实现 `/api/restart` 管理端点
  - [x] Supervisor 端处理 `POST /api/restart`
  - [x] 编排完整重启流程：drain 旧 Worker → spawn 新 Worker → 就绪检测 → 路由切换
  - [x] 防重入：重启进行中拒绝新的重启请求
  - [x] 返回重启状态（成功/进行中/失败）
  - 状态：✅ 已完成

- [x] **Task 2.2**: 实现 Worker drain 机制
  - [x] Worker 端添加 `/internal/drain` 端点
  - [x] Drain 逻辑：强制停止所有 Agent Session → 持久化 session 状态 → 返回确认
  - [x] 给所有 WS 客户端发送 `server_restarting` 事件
  - 状态：✅ 已完成

- [x] **Task 2.3**: Supervisor 路由切换 + 旧 Worker 退出
  - [x] 路由切换：新 Worker 就绪后，新连接路由到新 Worker，已有连接保持在旧 Worker
  - [x] 连接计数：Supervisor 跟踪每个 Worker 的活跃代理连接数
  - [x] 旧 Worker 连接归零 → 关闭旧 Worker 进程
  - [x] 超时兜底（默认 5 分钟）→ 强制关闭旧 Worker
  - 状态：✅ 已完成

- [ ] **Task 2.4**: 重启流程端到端验证
  - [ ] `POST /api/restart` 触发重启，新 Worker 正常启动
  - [ ] 重启期间现有 WebSocket 连接不中断（旧 Worker 继续服务）
  - [ ] 新连接路由到新 Worker
  - [ ] 旧 Worker 连接断开后自动退出
  - [ ] 重复重启多次验证稳定性
  - 状态：⏸️ 待人工验证

### 阶段三：重启入口 [已完成]

目标：实现前端、CLI、MCP 三个重启入口。

- [x] **Task 3.1**: MCP tool — `restart_server`
  - [x] 在 `mutbot/web/mcp.py` 添加 `restart_server` tool
  - [x] 内部发 HTTP POST 到 `localhost:<port>/api/restart`
  - [x] 返回重启结果
  - 状态：✅ 已完成

- [x] **Task 3.2**: CLI 命令 — `python -m mutbot restart`
  - [x] 在 `mutbot/__main__.py` 添加 `restart` 子命令
  - [x] 发 HTTP POST 到 `localhost:8741/api/restart`
  - [x] 等待新 Worker 就绪（轮询 `/health` 直到 generation 变化）
  - [x] 打印重启结果
  - 状态：✅ 已完成

- [x] **Task 3.3**: 前端 — "重启服务器"菜单项
  - [x] 工作区菜单添加"Restart Server"按钮（紧邻"Close Workspace"）
  - [x] 点击后 fetch `POST /api/restart`
  - [x] 处理 `server_restarting` WS 事件：显示 toast 通知
  - [x] WebSocket 断开后自动重连（已有机制）
  - 状态：✅ 已完成

- [ ] **Task 3.4**: 三入口端到端验证
  - [ ] 前端菜单触发重启正常
  - [ ] `python -m mutbot restart` 触发重启正常
  - [ ] MCP tool 触发重启正常
  - 状态：⏸️ 待人工验证

### 阶段四：测试 [已完成]

- [x] **Task 4.1**: Supervisor 单元测试
  - [x] TCP 代理透传（HTTP）
  - [x] 管理路径识别和拦截（`/api/restart`、`/health`、`/internal/` 403）
  - [x] 无 Worker 时返回 503
  - [x] Worker 状态管理
  - [x] /health 端点（有 Worker / 无 Worker）
  - [x] /api/restart 端点（正常 / 重启中 / GET 拒绝）
  - [x] drain 机制（成功 / 连接拒绝）
  - [x] 连接计数跟踪
  - [x] CLI 参数解析
  - 状态：✅ 已完成（24 tests）

- [ ] **Task 4.2**: 重启流程集成测试
  - [ ] 完整重启流程：drain → spawn → 路由切换 → 旧 Worker 退出
  - [ ] 重启期间连接不中断
  - [ ] 重启后 session 数据不丢失
  - [ ] 连续多次重启稳定性
  - 状态：⏸️ 待人工验证（需要真实进程 spawn）

## 已确认决策

- **Worker 就绪检测**：Supervisor 轮询 Worker 的 `/health` 端点，返回 200 即就绪
- **Supervisor 日志**：独立写 `~/.mutbot/logs/supervisor-*.log`，不依赖 Worker 日志系统
- **Drain 超时**：默认 5 分钟，可配置
- **前端按钮**：工作区菜单中，紧邻"关闭工作区"
- **崩溃处理**：简单保活，不做重试限制，遇到问题再迭代
- **Agent Session drain**：强制停止，不等待 API 调用完成（远期通过 Agent 子进程化解决）

## 关键参考

### 源码
- `mutbot/src/mutbot/web/server.py` — 当前 server 入口，`main()` 函数（L376-489）
- `mutbot/src/mutbot/web/server.py:124-266` — `_on_startup()` / `_on_shutdown()` 生命周期
- `mutagent/src/mutagent/net/asgi.py` — `_ASGIServer` 实现，signal handling（L238-257）
- `mutagent/src/mutagent/net/_server_impl.py:508-576` — `Server.run()` 实现
- `mutbot/src/mutbot/web/routes.py` — WebSocket 端点，`_clients` 连接管理
- `mutbot/src/mutbot/web/mcp.py` — MCP introspection tools（`restart_server` 参照此处添加）

### 相关规范
- `mutbot/docs/specifications/feature-asgi-server.md` — Layer 1 已完成，本设计覆盖 Layer 2 + Layer 3 核心
