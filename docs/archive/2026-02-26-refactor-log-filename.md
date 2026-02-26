# 日志文件命名规范化 设计规范

**状态**：✅ 已完成
**日期**：2026-02-26
**类型**：重构

## 1. 背景

日志系统迭代后，文件命名存在以下问题：

1. **Server 日志**冗余后缀：`server-20260226_155702-log.log` 中 `-log` 多余
2. **Session 日志**未写入：session 级别的 `.log` 文件没有被实际写入
3. **Session 文件名缺少时间戳**：当前 session 的 API 文件以 `{session_id}-api.jsonl` 命名（如 `7502cfa0f599-api.jsonl`），纯 hex ID 不直观
4. **Sessions 持久化文件缺少时间戳**：`~/.mutbot/sessions/` 下的 `.json` 和 `.events.jsonl` 仅用 hex ID 命名，不便查找

### 当前命名 → 目标命名

| 文件类型 | 当前命名 | 目标命名 |
|----------|----------|----------|
| Server 日志 | `server-20260226_155702-log.log` | `server-20260226_155702.log` |
| Session 日志 | 不存在（未写入） | `session-20260226_155702-7502cfa0f599.log` |
| Session API | `7502cfa0f599-api.jsonl` | `session-20260226_155702-7502cfa0f599-api.jsonl` |
| Session 元数据 | `7502cfa0f599.json` | `20260226_155702-7502cfa0f599.json` |
| Session 事件 | `7502cfa0f599.events.jsonl` | `20260226_155702-7502cfa0f599.events.jsonl` |

**命名规则**：`session-{创建时间戳}-{session_id}[-api].{ext}`，时间戳来自 session 的 `created_at` 字段。

## 2. 设计方案

### 2.1 统一文件命名格式

新命名模式：
- **Server 日志**：`server-{server_ts}.log`
- **Session 日志**：`session-{session_ts}-{session_id}.log`
- **Session API**：`session-{session_ts}-{session_id}-api.jsonl`

其中 `session_ts` 从 `session.created_at`（ISO 格式 UTC）转为 `YYYYMMDD_HHMMSS`（本地时间）。

### 2.2 Server 日志改动

**文件**：`mutbot/src/mutbot/web/server.py`

改动最小——仅修改文件名模板：
- `f"server-{session_ts}-log.log"` → `f"server-{session_ts}.log"`

### 2.3 Session 日志写入（ContextVar 隔离方案）

Session 日志捕获 **该 session 的 agent task 内产生的所有日志**，用于诊断 agent 运行问题，与 API 录制一一对应。

**核心机制**：Agent 以 asyncio Task 运行（`AgentBridge._start_agent_task`），利用 `contextvars.ContextVar` 标记当前 session，通过自定义 `logging.Filter` 实现精确隔离。

**Logger 范围**：Handler 挂到 **root logger**（`logging.getLogger()`）。无论代码用什么 logger 名（mutagent、mutbot、扩展模块、第三方库），只要在该 session 的 asyncio Task 内执行就会被捕获。Task 外的日志因没有 ContextVar 而被 Filter 过滤掉。

#### 实现步骤

**1) 定义 ContextVar 和 Filter**

新建 `mutbot/src/mutbot/runtime/session_logging.py`：

```python
import contextvars
import logging

# 标记当前 asyncio task 所属的 session_id
current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    'current_session_id', default=''
)

class SessionFilter(logging.Filter):
    """仅放行来自指定 session context 的日志记录。"""
    def __init__(self, session_id: str):
        super().__init__()
        self._session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:
        return current_session_id.get('') == self._session_id
```

**2) Agent Task 中设置 ContextVar**

在 `AgentBridge._start_agent_task()` 的 `_run()` 开头设置：

```python
async def _run():
    current_session_id.set(self.session_id)
    # ... 后续 agent.run() 及其所有子调用都继承此 context
```

asyncio Task 创建时会 copy 当前 context，Task 内部 `set()` 不影响外部。agent.run() 中所有代码（mutagent、mutbot、扩展模块、第三方库）的日志调用都在此 Task 内，ContextVar 自动可见。

**3) Session 级 FileHandler 的生命周期**

在 `SessionManager.start()` 中：
- 创建 `FileHandler`（文件名 `session-{ts}-{session_id}.log`）
- 添加 `SessionFilter(session_id)` 到此 handler
- 挂载到 **root logger**（`logging.getLogger()`）

在 `SessionManager.stop()` 中：
- 从 root logger 移除该 handler 并 close

Handler 引用存储在 `AgentSessionRuntime` dataclass 中。

### 2.4 Session API 文件命名

**文件**：`mutagent/src/mutagent/runtime/api_recorder.py`

`ApiRecorder` 当前用 `f"{self._session_ts}-api.jsonl"` 命名。需要传入完整前缀。

改动：将 `session_ts` 参数改为传完整前缀（`session-{ts}-{session_id}`），ApiRecorder 无需知道命名规则。

### 2.5 LogQueryEngine 适配

**文件**：`mutagent/src/mutagent/runtime/log_query.py`

当前 `_LOG_SUFFIXES = ("-log.log", "-api.jsonl")`，解析逻辑依赖这些后缀提取 session 前缀。

改动：
1. 更新后缀列表：`_LOG_SUFFIXES = (".log", "-api.jsonl")`
2. `_resolve_log_file` / `_resolve_api_file` 需要适配新的文件名格式
3. `list_sessions` 需要正确分组 server 日志和 session 日志（两者都以 `.log` 结尾）
4. `_extract_session_prefix` 需要识别 `server-` 和 `session-` 前缀

### 2.6 Sessions 持久化文件命名

**目录**：`~/.mutbot/sessions/`
**文件**：`mutbot/src/mutbot/runtime/storage.py`

当前 sessions 目录下的文件以 `{session_id}.json` / `{session_id}.events.jsonl` 命名。改为 `{ts}-{session_id}.json` / `{ts}-{session_id}.events.jsonl`，无 `session-` 前缀（目录本身已表明语义）。

时间戳来自 session 的 `created_at` 字段，格式 `YYYYMMDD_HHMMSS`（本地时间）。

**影响范围**（storage.py 中的函数）：
- `save_session_metadata(session_data)` — 需要从 data 中提取 `created_at` 构建文件名
- `load_session_metadata(session_id)` — 需要按 session_id 查找文件（文件名含时间戳前缀）
- `append_session_event(session_id, event_data)` — 同上
- `load_session_events(session_id)` — 同上
- `load_all_sessions()` — glob 模式不变（`*.json`），解析方式不变

**查找策略**：`load_session_metadata` 等按 session_id 查找的函数，使用 glob `*{session_id}.json` / `*{session_id}.events.jsonl`。此模式天然兼容新旧两种格式：
- 新格式：`20260226_155702-7502cfa0f599.json` — 匹配 `*7502cfa0f599.json` ✓
- 旧格式：`7502cfa0f599.json` — 匹配 `*7502cfa0f599.json` ✓

### 2.7 mutagent 独立模式兼容

**文件**：`mutagent/src/mutagent/builtins/main_impl.py`

mutagent 独立运行时也生成 `{session_ts}-log.log` 和 `{session_ts}-api.jsonl`。

## 3. 待定问题

### Q1: mutagent 独立模式的命名是否也要改？
**问题**：mutagent 独立运行时（`python -m mutagent`）的日志文件目前命名为 `20260226_155702-log.log` 和 `20260226_155702-api.jsonl`。是否也统一去掉 `-log` 后缀？
**建议**：是，改为 `20260226_155702.log` 和 `20260226_155702-api.jsonl`。保持一致。

### Q2: Session 日志的过滤范围
**问题**：Session 日志应该捕获哪些日志？
**决定**：Handler 挂到 root logger，通过 ContextVar + SessionFilter 隔离。捕获该 session 的 agent asyncio task 内产生的所有日志（mutagent、mutbot、扩展模块、第三方库），详见 2.3 节。

### Q3: Session 日志级别
**问题**：Session 的 `.log` 文件应该从哪个级别开始记录？
**建议**：与 server 日志一致，`DEBUG` 级别。

## 4. 实施步骤清单

### 阶段一：文件命名修改 [✅ 已完成]

- [x] **Task 1.1**: 修改 server 日志文件名
  - [x] `server.py`: `f"server-{session_ts}-log.log"` → `f"server-{session_ts}.log"`
  - [x] 更新注释
  - 状态：✅ 已完成

- [x] **Task 1.2**: 修改 session API 文件命名
  - [x] `session_impl.py`: 构建 `session-{ts}-{session_id}` 前缀传给 ApiRecorder
  - [x] 从 `session.created_at`（ISO UTC）转为本地时间 `YYYYMMDD_HHMMSS` 格式
  - [x] 新增 `_build_session_prefix()` 辅助函数
  - 状态：✅ 已完成

- [x] **Task 1.3**: 添加 session 日志文件写入（ContextVar 隔离）
  - [x] 新建 `mutbot/src/mutbot/runtime/session_logging.py`（ContextVar + SessionFilter）
  - [x] `agent_bridge.py` 的 `_run()` 开头设置 `current_session_id.set(session_id)`
  - [x] `session_impl.py` 的 `start()` 中创建带 SessionFilter 的 FileHandler
  - [x] FileHandler 挂载到 root logger
  - [x] Handler 引用存入 `AgentSessionRuntime`
  - [x] `stop()` 中从 root logger 移除并关闭 handler
  - [x] 新增 `_create_session_log_handler()` / `_remove_session_log_handler()` 辅助函数
  - 状态：✅ 已完成

### 阶段二：LogQueryEngine 适配 [✅ 已完成]

- [x] **Task 2.1**: 更新 `_LOG_SUFFIXES` 和 `_extract_session_prefix`
  - [x] 新增 `_SESSION_FILE_RE` 正则匹配新格式 + 旧格式兼容
  - [x] `_extract_session_prefix` 优先使用正则，回退到旧后缀剥离
  - [x] 正确提取 session 前缀用于分组
  - 状态：✅ 已完成

- [x] **Task 2.2**: 更新 `_resolve_log_file` / `_resolve_api_file`
  - [x] `_resolve_log_file` 新格式 `.log` 优先，回退到旧格式 `-log.log`
  - [x] `_find_latest_file` 增加 `_extract_session_prefix` 校验
  - 状态：✅ 已完成

### 阶段三：mutagent 独立模式 [✅ 已完成]

- [x] **Task 3.1**: 更新 `main_impl.py` 文件名
  - [x] `f"{session_ts}-log.log"` → `f"{session_ts}.log"`
  - 状态：✅ 已完成

### 阶段四：Sessions 持久化文件命名 [✅ 已完成]

- [x] **Task 4.1**: 修改 `storage.py` 文件命名
  - [x] 新增 `_find_session_file(session_id, suffix)` — glob `*{session_id}{suffix}` 查找
  - [x] 新增 `_session_ts_prefix(created_at)` — ISO UTC → 本地 `YYYYMMDD_HHMMSS-`
  - [x] `save_session_metadata`: 用 `{ts}-{id}.json` 文件名
  - [x] `load_session_metadata`: 用 glob 查找（兼容新旧格式）
  - [x] `append_session_event`: 用 glob 查找，首次创建时从 `.json` 推导前缀
  - [x] `load_session_events`: 用 glob 查找
  - 状态：✅ 已完成

## 5. 测试验证

### 单元测试
- [x] mutagent 721 passed, 4 skipped
- [x] mutbot 250 passed
- [x] LogQueryEngine 旧格式兼容（通过现有测试）

### 集成测试
- [ ] 启动 mutbot server，确认 server 日志文件名正确
- [ ] 创建 session 并发送消息，确认 session `.log` 和 `-api.jsonl` 文件均创建
- [ ] 使用 `log_query` CLI 工具查询，确认 `sessions` 和 `logs` 命令正常工作
