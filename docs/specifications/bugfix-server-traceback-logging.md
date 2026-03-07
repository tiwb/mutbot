# 服务器 Traceback 日志捕获缺失 设计规范

**状态**：✅ 已完成
**日期**：2026-03-07
**类型**：Bug修复

## 背景

mutbot 的设计意图是**当前进程下的所有日志都应被接管并保存**，提供统一的日志查询工具。但当前实现只配置了 `mutbot.*` 和 `mutagent.*` 两个 logger 的 FileHandler，导致进程中其他来源的日志不会写入 `~/.mutbot/logs/server-*.log`，也无法通过日志查询工具查询。

### 现状分析

**启动阶段**（`__main__.py:17-28`）：
- `logging.basicConfig(level=console_level)` 在 root logger 上创建 StreamHandler
- root logger level 被设为 `console_level`（默认 WARNING），StreamHandler 无独立 level
- `--debug` CLI 参数和 `logging.console_level` config 项都在此处读取
- config 在 `__main__.py` 和 `server.py` lifespan 中重复加载

**lifespan 阶段**（`server.py:196-215`）：
- FileHandler 和 LogStoreHandler 被加到**单独的** `mutbot`/`mutagent` logger 上
- 遗漏了 uvicorn、asyncio、第三方库等所有其他日志源

**uvicorn 默认 log_config**：
- uvicorn 默认给 `uvicorn`/`uvicorn.error`/`uvicorn.access` 创建独立的 StreamHandler
- 这些 logger 设置了 `propagate: False`，不会传播到 root logger
- 即使配了 root logger 的 FileHandler，uvicorn 日志也不会进入

### 偏差总结

| 偏差 | 影响 |
|------|------|
| 逐个列举 logger 而非配置 root logger | uvicorn、asyncio、第三方库日志不进文件/LogStore |
| 日志初始化在 lifespan 中，启动阶段日志不完整 | uvicorn 启动期间的日志不进文件/LogStore |
| uvicorn logger 设置 `propagate: False` | 即使用 root logger 也捕获不到 uvicorn 日志 |
| config 重复加载 | `__main__.py` 和 lifespan 各读一次 |
| 缺少 asyncio exception handler | `ensure_future` task 异常只到 stderr |
| 静默 `except: pass/continue` | traceback 完全丢失 |

## 设计方案

### 核心：初始化集中到 `server.main()`，`__main__.py` 保持薄入口

将 config 加载、日志初始化、`app.state` 赋值、argparse、uvicorn 启动全部放在 `server.main()` 中。`__main__.py` 只做 `from mutbot.web.server import main; main()`。保证从进程启动的第一条日志开始全量捕获。

#### `__main__.py` 改动

精简为薄入口：

```python
"""MutBot entry point: python -m mutbot"""

def main():
    from mutbot.web.server import main as _server_main
    _server_main()

main()
```

#### `server.py` 新增 `main()` 函数

执行顺序：argparse → config → 日志 → `app.state` 赋值 → uvicorn。

```python
def main():
    import argparse
    import mutbot
    import uvicorn

    parser = argparse.ArgumentParser(description="MutBot Web UI")
    parser.add_argument("-V", "--version", action="version",
                        version=f"mutbot {mutbot.__version__}")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8741, help="Bind port")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging to console")
    args = parser.parse_args()

    # 1. Config
    config = load_mutbot_config()

    # 2. 日志初始化（紧随 config）
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path.home() / ".mutbot" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 控制台 StreamHandler（--debug 覆盖 config）
    if args.debug:
        console_level = logging.DEBUG
    else:
        level_name = config.get("logging.console_level", default="WARNING")
        console_level = getattr(logging, level_name.upper(), logging.WARNING)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(
        "%(levelname)-8s %(name)s: %(message)s"
    ))
    root_logger.addHandler(console_handler)

    # FileHandler（全量 DEBUG）
    file_handler = logging.FileHandler(
        log_dir / f"server-{session_ts}.log", encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(SingleLineFormatter(
        "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    ))
    root_logger.addHandler(file_handler)

    # LogStoreHandler（全量 DEBUG，lifespan 中取出使用）
    _log_store = LogStore()
    mem_handler = LogStoreHandler(_log_store)
    mem_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(mem_handler)

    # 3. app.state 赋值（模块内部，无需跨模块设置）
    app.state.config = config
    app.state.log_store = _log_store

    # 4. uvicorn（log_config=None 禁用 uvicorn 自带日志配置）
    uvi_config = uvicorn.Config(app, host=args.host, port=args.port,
                                log_config=None)
    server = uvicorn.Server(uvi_config)

    # banner
    _original_startup = server.startup
    async def _startup_with_banner(sockets=None):
        await _original_startup(sockets=sockets)
        print(f"\n  Open https://mutbot.ai to get started\n")
    server.startup = _startup_with_banner

    try:
        server.run()
    except KeyboardInterrupt:
        pass
```

要点：
- 不再使用 `logging.basicConfig()`，手动创建各 handler，root logger level=DEBUG + 各 handler 独立 level
- `--debug` 保留，覆盖 config 中的 `logging.console_level`
- `app.state` 在模块内部赋值，`__main__.py` 不接触 server 内部状态
- `log_config=None` 禁用 uvicorn 默认的日志配置，避免 uvicorn 创建独立 handler 并设 `propagate: False`
- `server.py` 已有 `from mutagent.runtime.log_store import LogStore, LogStoreHandler, SingleLineFormatter` 导入，无需新增

#### `server.py` lifespan 改动

lifespan 不再负责日志初始化和 config 加载。从 `app.state` 接收：

```python
config = app.state.config
log_store = app.state.log_store
```

删除原有的 `load_mutbot_config()` 调用和日志初始化代码（`server.py:159,190-217`）。同步更新模块全局变量 `log_store`（routes.py 等通过 `from mutbot.web.server import log_store` 引用）。

#### `routes.py` 改动

- `routes.py:101`（RPC handler）→ 通过 `ctx.managers["config"]` 获取
- `routes.py:1005`（WebSocket handler）→ 用 `websocket.app.state.config`

### asyncio 未捕获异常

在 lifespan 中设置 asyncio exception handler（需要 event loop，只能在 async 上下文中），兜底捕获所有 `ensure_future` 创建的 task 中未处理的异常：

```python
def _asyncio_exception_handler(loop, context):
    exception = context.get("exception")
    message = context.get("message", "Unhandled exception in async task")
    _logger = logging.getLogger("mutbot.asyncio")
    if exception:
        _logger.error("%s: %s", message, exception, exc_info=exception)
    else:
        _logger.error(message)

loop = asyncio.get_running_loop()
loop.set_exception_handler(_asyncio_exception_handler)
```

### 静默吞掉的异常

将静默的 `except: pass/continue` 改为至少记录日志：

| 文件 | 位置 | 当前行为 | 修复 |
|------|------|---------|------|
| `routes.py` | JSON 解析失败 | `except: continue` | `logger.warning("Invalid JSON", exc_info=True)` |
| `routes.py` | varint 解码失败 | `except ValueError: continue` | `logger.warning("Invalid varint", exc_info=True)` |
| `routes.py` | config change 事件 | `except: pass` | `logger.debug("config change notify failed", exc_info=True)` |
| `routes.py` | base64 scrollback 解码 | `except: pass` | `logger.warning("scrollback decode failed", exc_info=True)` |
| `transport.py` | `_ws_send` 失败 | `logger.debug(...)` 无 exc_info | 加 `exc_info=True` |
| `transport.py` | `_ws_send_control` 失败 | `logger.debug(...)` 无 exc_info | 加 `exc_info=True` |
| `connection.py` | pending event flush | `except: break` | `logger.debug("flush failed", exc_info=True)` |
| `connection.py` | broadcast send | `except: dead.append` | `logger.debug("broadcast failed", exc_info=True)` |

## 实施风险分析

### 低风险
| 改动 | 说明 |
|------|------|
| root logger 替代逐个 logger 配置 | 标准 Python 日志最佳实践，行为更正确 |
| `log_config=None` 禁用 uvicorn 日志配置 | uvicorn 官方支持的参数 |
| 静默异常加 `exc_info=True` | 纯增加日志输出，不改变控制流 |
| asyncio exception handler | 兜底性质，不影响正常流程 |

### 中等风险（需注意）
| 改动 | 风险 | 缓解 |
|------|------|------|
| app 传对象替代字符串 | 原 `"mutbot.web.server:app"` 支持 uvicorn `--reload`；传对象后 reload 失效 | 当前未用 reload，可接受 |

### 已确认的设计决策
1. **保留 `--debug`**：映射到 console_level=DEBUG，兼容现有用法
2. **`routes.py:101` config 获取**：通过 `ctx.managers["config"]` 传入
3. **初始化集中到 `server.main()`**：`__main__.py` 保持薄入口，初始化逻辑和 `app.state` 赋值都在 server 模块内部完成

## 关键参考

### 源码
- `src/mutbot/__main__.py:7-54` — main() 入口（需重构）
- `src/mutbot/web/server.py:18` — `from mutagent.runtime.log_store import LogStore, LogStoreHandler, SingleLineFormatter`
- `src/mutbot/web/server.py:130-151` — config watcher（`config.reload()` 就地更新，与 app.state.config 同一实例）
- `src/mutbot/web/server.py:154-167` — lifespan（改为从 app.state 接收 config/log_store）
- `src/mutbot/web/server.py:190-217` — 日志初始化（移到 __main__.py）
- `src/mutbot/web/server.py:312` — `app = FastAPI(...)` 模块级创建
- `src/mutbot/web/routes.py:101,1005` — `load_mutbot_config()` 调用（改为 app.state.config）
- `src/mutbot/web/transport.py:396` — `ensure_future()` 无异常处理（asyncio handler 兜底）
- `src/mutbot/web/agent_bridge.py:292,307,308` — `ensure_future()` 广播（asyncio handler 兜底）
- `src/mutbot/web/routes.py:1143,1175,1209,1430` — 静默吞异常的位置
- `src/mutbot/web/connection.py:31-34,56-59,84-87` — broadcast 静默吞异常

## 实施步骤清单

### Phase 1: 初始化重构 [✅ 已完成]

- [x] **Task 1.1**: `server.py` 新增 `main()` 函数 — config → 日志 → app.state → uvicorn
  - [x] argparse（保留 `--host`/`--port`/`--debug`/`-V`）
  - [x] 加载 config（`load_mutbot_config()`）
  - [x] root logger level=DEBUG + StreamHandler（`--debug` 覆盖 config 中 console_level）
  - [x] FileHandler（`server-{session_ts}.log`，level=DEBUG）
  - [x] LogStoreHandler（in-memory，level=DEBUG）
  - [x] `app.state.config` 和 `app.state.log_store` 赋值
  - [x] uvicorn 启动，`log_config=None`，传 app 对象
  - [x] 保留 banner 逻辑
  - 状态：✅ 已完成

- [x] **Task 1.2**: 精简 `__main__.py` — 薄入口，只调 `server.main()`
  - 状态：✅ 已完成

- [x] **Task 1.3**: 精简 `server.py` lifespan — 移除日志初始化和 config 加载
  - [x] 删除 `load_mutbot_config()` 调用，改为 `config = app.state.config`
  - [x] 删除日志初始化代码块（原 `server.py:190-217`），改为 `log_store = app.state.log_store`
  - [x] 同步更新模块全局变量 `log_store`（`_get_log_store()` 通过 import 引用）
  - 状态：✅ 已完成

- [x] **Task 1.4**: 更新 `routes.py` — 消除重复 config 加载
  - [x] WS handler 构建 `RpcContext.managers` 时加入 `"config": websocket.app.state.config`
  - [x] `routes.py:101` RPC handler 中改为 `ctx.managers["config"]`
  - [x] `routes.py:1005` WebSocket handler 中改为 `websocket.app.state.config`
  - 状态：✅ 已完成

### Phase 2: asyncio 异常兜底 [✅ 已完成]

- [x] **Task 2.1**: 在 lifespan 中注册 asyncio exception handler
  - [x] 添加 `_asyncio_exception_handler` 函数
  - [x] 在 lifespan startup 中 `loop.set_exception_handler(_asyncio_exception_handler)`
  - 状态：✅ 已完成

### Phase 3: 静默异常补日志 [✅ 已完成]

- [x] **Task 3.1**: `routes.py` 静默异常补日志
  - [x] JSON 解析 `except Exception: continue` → 加 `logger.warning`
  - [x] varint 解码 `except ValueError: continue` → 加 `logger.warning`
  - [x] config change 推送 `except Exception: pass` → 加 `logger.debug`
  - [x] base64 scrollback 解码 `except Exception: pass` → 加 `logger.warning`
  - 状态：✅ 已完成

- [x] **Task 3.2**: `transport.py` 日志补 exc_info
  - [x] `_ws_send` 失败日志加 `exc_info=True`
  - [x] `_ws_send_control` 失败日志加 `exc_info=True`
  - 状态：✅ 已完成

- [x] **Task 3.3**: `connection.py` 静默异常补日志
  - [x] pending event flush `except Exception: break` → 加 `logger.debug`
  - [x] broadcast send `except Exception: dead.append` → 加 `logger.debug`
  - [x] broadcast_all send `except Exception: dead_pairs.append` → 加 `logger.debug`
  - 状态：✅ 已完成

### Phase 4: 验证 [✅ 已完成]

- [x] **Task 4.1**: 启动验证
  - [x] `python -m mutbot` 启动正常，banner 正常显示
  - [x] `~/.mutbot/logs/server-*.log` 包含 uvicorn 启动日志（`uvicorn.error`）
  - [x] `--debug` 参数正常工作（控制台输出 DEBUG 级别）
  - [x] asyncio 日志也被捕获（`asyncio - Using proactor: IocpProactor`）
  - 状态：✅ 已完成
