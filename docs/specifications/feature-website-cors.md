# mutbot.ai 连接支持 — CORS 与 Health API

**状态**：🔄 进行中
**日期**：2026-02-26
**类型**：功能设计
**总体规划**：[mutbot.ai 总体规划](../../mutbot.ai/docs/specifications/feature-website-github-pages.md)

## 1. 背景

mutbot.ai 网站需要通过 `fetch` 和 `WebSocket` 连接本地 MutBot 后端。由于 mutbot.ai 是 HTTPS 页面，浏览器会执行跨域和混合内容检查。本文档为 mutbot 后端添加必要的 CORS 支持和 Health API。

**依赖关系**：mutbot.ai Phase 1（`feature-website-launch.md`）依赖本文档的实施。

## 2. 设计方案

### 2.1 Health API

新增 `/api/health` 端点，供 mutbot.ai 检测本地 MutBot 状态和版本：

**请求**：`GET /api/health`

**响应**：

```json
{
  "status": "ok",
  "api_version": "1.0.0"
}
```

**说明**：
- `api_version`：语义化版本号，用于 mutbot.ai 判断内置前端是否兼容
- 此端点不需要认证（即使启用了 auth，health 也应放行）
- 响应需包含 CORS 头（见 2.2）

### 2.2 CORS 响应头

当请求的 `Origin` 为 `https://mutbot.ai` 时，添加以下响应头：

```
Access-Control-Allow-Origin: https://mutbot.ai
Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization
Access-Control-Allow-Private-Network: true
```

**关键点**：

- `Access-Control-Allow-Private-Network: true` — Chrome 的 Private Network Access 规范要求。HTTPS 公网页面访问 localhost 时，Chrome 会发送 preflight 并检查此头。
- 仅对来自 `https://mutbot.ai` 的请求返回 CORS 头，其他 origin 不添加
- 需要处理 `OPTIONS` preflight 请求并返回 `204`

### 2.3 WebSocket Origin 校验

WebSocket 握手时，接受以下 Origin：

- `http://localhost:8741`（本地直接访问）
- `https://mutbot.ai`（从 mutbot.ai 连接）

### 2.4 实现位置

CORS 中间件添加到 FastAPI app（`mutbot/src/mutbot/web/server.py`）。

**方案**：使用 FastAPI 内置的 `CORSMiddleware`：

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://mutbot.ai"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

另外需要手动处理 `Access-Control-Allow-Private-Network` 头（FastAPI CORSMiddleware 不内置支持）：

```python
@app.middleware("http")
async def private_network_access(request, call_next):
    response = await call_next(request)
    if request.headers.get("Access-Control-Request-Private-Network"):
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response
```

### 2.5 Health 端点位置

添加到 `mutbot/src/mutbot/web/routes.py`：

```python
@router.get("/api/health")
async def health():
    return {"status": "ok", "api_version": "1.0.0"}
```

`api_version` 应从 `mutbot` 包元数据或常量中读取，而非硬编码。

## 3. 实施步骤清单

### 阶段一：实施 [待开始]
- [ ] **Task 1.1**: 添加 `/api/health` 端点
  - [ ] 在 routes.py 中添加端点
  - [ ] 定义 `API_VERSION` 常量
  - [ ] 跳过 auth 校验
  - 状态：⏸️ 待开始

- [ ] **Task 1.2**: 添加 CORS 支持
  - [ ] 添加 `CORSMiddleware`
  - [ ] 添加 `Access-Control-Allow-Private-Network` 中间件
  - [ ] WebSocket Origin 校验
  - 状态：⏸️ 待开始

- [ ] **Task 1.3**: 测试
  - [ ] 单元测试：health 端点响应
  - [ ] 单元测试：CORS 头和 preflight
  - [ ] 集成测试：从 HTTPS 页面 fetch localhost（手动验证）
  - 状态：⏸️ 待开始
