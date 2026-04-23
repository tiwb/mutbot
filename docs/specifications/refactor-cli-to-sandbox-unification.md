# CLI 统一到 Sandbox 设计规范

**状态**：✅ 已完成
**日期**：2026-04-23
**类型**：重构

## 需求

1. `refactor: MCP 内省工具迁移至 sandbox namespace` 完成后,调试能力有两条入口:
   - **MCP 入口** — Claude Code agent 通过 `mcp__mutbot__pysandbox` 调用 `mutbot.*`
   - **CLI 入口** — 用户手动运行 `python -m mutbot log ...` / `python -m mutbot.cli.cdp_debug ...`
2. 两条入口逻辑重复,语法不一致:
   - CLI 自成一套 argparse 参数体系(`log query -l ERROR -s xxx -p xxx`),与 `mutbot.logs(level="ERROR", ...)` 不对称
   - 某些能力只在一处存在(如 `cdp_debug` 有 CLI 无 sandbox 对应函数)
3. 希望 CLI 变成 sandbox 的另一个前端,而非平行实现:
   - **只有一套能力**(sandbox namespace 函数),**两个调用入口**(MCP / CLI),**统一的调用语法**
   - 无论 agent 还是人类用户,看到的都是相同函数签名
4. **本次只支持活 server 场景**。脱机场景(server 崩溃后查磁盘日志)不在本次范围 —— 频率低,tail/grep 原始日志文件可替代。未来真有需求再加

## 关键参考

- `mutbot/src/mutbot/__main__.py` — CLI 入口分发,当前支持 `restart` / `log` 子命令
- `mutbot/src/mutbot/cli/log_query.py` (475 行) — 日志查询 CLI,将删除
- `mutbot/src/mutbot/cli/cdp_debug.py` (707 行) — Chrome CDP 调试 CLI,将删除
- `mutbot/src/mutbot/cli/proxy_log.py` (134 行) — LLM proxy 日志,待定(非本次范围但要记录)
- `mutbot/src/mutbot/builtins/debug_tools.py` — 已迁好的 `MutbotTools` namespace
- `mutagent/src/mutagent/sandbox/app.py` — SandboxApp Declaration
- `mutagent/src/mutagent/sandbox/_app_impl.py` — SandboxApp 实现,`exec_code(code, state)` 已有
- `D:/ai/CLAUDE.md` — "Sandbox 运行时内省" + "mutbot 日志" + "前端调试" 三节需要重写

## 设计方案

### 总体架构

```
                          ┌──────────────────────┐
                          │   SandboxApp(单例)    │
                          │   mutbot.* / web.*   │
                          └──────────┬───────────┘
                                     │ exec_code(code, state)
                        ┌────────────┴────────────┐
                        │                         │
            ┌───────────▼──────────────┐  ┌───────▼────────┐
            │ MCP endpoint             │  │  CLI 入口      │
            │ pysandbox tool           │  │ mutbot         │
            │ (mutbot 内置)            │  │ pysandbox      │
            │ 同时供 Claude Code agent │  │                │
            │ 与 build_default_agent   │  │                │
            └──────────────────────────┘  └────────────────┘
```

两个入口共享同一个 SandboxApp 实例(活 server 进程内)。CLI 本身不持有 SandboxApp —— 脱机场景不支持(见下方"不做脱机"段),直接报错。Agent 侧(Claude Code / build_default_agent)通过 MCP pysandbox tool 调用,无需单独入口。

### CLI 命令形态

`mutbot pysandbox` 子命令,**对齐 python CLI 约定**:

```bash
# 单条执行(最常用)
mutbot pysandbox -c "mutbot.logs(level='ERROR', last_n=10)"
mutbot pysandbox -c "mutbot.status()"

# 执行脚本文件(位置参数)
mutbot pysandbox script.py

# 从 stdin 读代码
echo "mutbot.logs()" | mutbot pysandbox
mutbot pysandbox -   # 显式从 stdin 读(python 约定)

# 交互式 REPL(可选,第二阶段)
mutbot pysandbox
```

形态对齐 `python`:`-c` 代码字符串、位置参数脚本文件、`-` 或管道 stdin、无参 REPL。Python 本身没有 `--code-file`,因此不引入该 flag(脚本文件用位置参数即可)。

**MSYS2 路径转换注意事项**:
- `-c` 值中若含 `/`(URL、路径、正则,如 `mutbot.logs(pattern='/api/')`),Git Bash 下会被 MSYS2 转成 Windows 路径,破坏语义。**此时必须用 stdin 或脚本文件传代码**。
- 脚本文件位置参数传 `script.py` / `./script.py` / `D:/path/script.py` 这类不以 `/` 开头的形式不会被转换,安全。
- 仅以 `/` 开头的裸路径(如 `/tmp/script.py`)会被转换,不推荐这样写。

实际规则:
- `-c "<code>"` 代码不含 `/` 时直接用
- 代码含 `/`(URL、路径、正则)时用 stdin 或脚本文件

### 不做脱机(设计决策)

CLI 仅作为活 server 的 RPC 客户端,server 未运行时直接报错。

**不做脱机的理由**:
- 真实使用场景 90%+ 发生在活 server 下,脱机是低频事件
- 脱机成本大头是 `MutbotTools` 的磁盘 fallback 分支(`logs` / `errors` / `session_messages` / `config_get` 都要加),估计 150–250 行,相当于把 `log_query.py` 的日志文件解析搬进 `debug_tools.py`
- server 挂了的替代方案足够:
  - 人类用户:`tail -100 ~/.mutbot/logs/server-*.log` + `grep ERROR` 足够
  - 重启 server 后内存日志仍有磁盘持久化,可继续追
- 符合 YAGNI,未来真需要脱机再加 fallback 分支

**脱机行为**:CLI 探测到 server 未运行时,输出一条提示并退出非 0:

```
Error: mutbot server not running at http://127.0.0.1:8741

To start: python -m mutbot
To read logs offline: tail -100 ~/.mutbot/logs/server-*.log
```

### 连接活 server 的 RPC 路径

CLI 向 `127.0.0.1:8741/mcp` 发 HTTP POST,调 `pysandbox` tool,代码作为参数传入。

```python
mutbot pysandbox -c "mutbot.logs()"
  → POST http://127.0.0.1:8741/mcp  (tools/call, pysandbox)
      成功 → stdout 输出 server 返回,exit 0
      连接拒绝/超时 → 打印"server not running"提示,exit 1
```

实现参考 `__main__.py` 现有 `_restart_command` 的 urllib 代码风格(同样是简单 HTTP 客户端)。

### 删除清单

**删除的 CLI 入口**:
- `python -m mutbot log ...`(`__main__.py` 的 `_log_command` 和 `mutbot.cli.log_query`)
- `python -m mutbot.cli.cdp_debug ...`(`mutbot/cli/cdp_debug.py`)

**保留**:
- `python -m mutbot`(启动 server)
- `python -m mutbot restart`(触发重启)
- `python -m mutbot --worker` / `--no-supervisor`(服务端选项)
- `mutbot.cli.proxy_log`(独立于本次重构,LLM proxy 专用,未使用 `mutbot.*` 能力体系;纯文件查询,与 server 是否运行无关,保留独立 CLI)

**新增**:
- `python -m mutbot pysandbox -c "..."` 或 `mutbot pysandbox -c "..."`

### cdp_debug 能力的替代

`cdp_debug` 做的事情:连外部 Chrome(9222 端口)跑 JS、监听 console、检查 marker 等。这**不是** `mutbot.exec_frontend` 的替代 —— 后者走 mutbot 前端 WebSocket,前端必须连上才行。

两者用途不同:

| 场景 | 工具 |
|---|---|
| 调试 mutbot 前端(已登录、已进入应用) | `mutbot.exec_frontend` |
| 调试任意外部 Chrome 页面、首次加载验证 | Chrome CDP 相关能力 |

`cdp_debug` 的能力在本次重构**不保留也不替换**。理由:
- 它是浏览器调试的通用工具,和 mutbot 运行时内省无直接关系
- 未来若需要,应通过 `chrome-cdp` skill(已存在)提供,或在 sandbox 中暴露 CDP namespace
- 保留它会打乱"mutbot CLI = sandbox 前端"的一致性

删除后用户需要这类能力,文档引导至 `chrome-cdp` skill。

### 破坏性变更

- `python -m mutbot log` 删除,所有使用者改用 `mutbot pysandbox -c "mutbot.logs(...)"`
- `python -m mutbot.cli.cdp_debug` 删除,无直接替代(用 chrome-cdp skill 或浏览器 devtools)
- CLAUDE.md 中所有 `python -m mutbot log ...` 示例全部重写

由于这些 CLI 只在本工作区被 Claude 和用户手动使用,没有外部脚本依赖,破坏可接受。

### 测试验证

手工验证清单:

1. **活 server 下 CLI 走 RPC**:启动 mutbot,另开终端 `mutbot pysandbox -c "mutbot.status()"`,返回与 MCP 调用一致
2. **活 server 下 CLI 走 `mutbot.logs`**:`mutbot pysandbox -c "mutbot.logs(level='ERROR', last_n=5)"` 返回内存日志
3. **server 未运行时报错清楚**:停掉 mutbot,`mutbot pysandbox -c "mutbot.status()"` 打印 "server not running" 提示(含 `tail` 替代方案),exit 非 0
4. **stdin 传代码**:`echo 'print(mutbot.status())' | mutbot pysandbox`
5. **显式 `-` 从 stdin 读**:`echo 'mutbot.status()' | mutbot pysandbox -`
6. **`-c` 代码含 `/` 场景**:文档示例引导用 stdin,实际用 `echo "mutbot.logs(pattern='\\\\bapi\\\\b')" | mutbot pysandbox` 验证
7. **位置参数脚本文件**:`mutbot pysandbox ./probe.py` 执行脚本,返回打印输出
8. **旧 CLI 已删除**:`python -m mutbot log sessions` 报错 "unknown command"

## 消费者场景

| 消费者 | 场景 | 依赖的输出 | 验收标准 |
|---|---|---|---|
| 用户(终端手动) | 服务运行时查内存日志 | `mutbot pysandbox -c "mutbot.logs(...)"` | 活 server 场景返回内存日志(最新) |
| 用户(终端手动) | 服务挂了时查磁盘日志 | `tail -100 ~/.mutbot/logs/server-*.log` | 走 shell 原生工具,CLI 仅给提示 |
| Claude Code agent | 通过 MCP 调用 | `mcp__mutbot__pysandbox` → `mutbot.logs(...)` | 与 CLI 语法一致,返回一致 |
| CLAUDE.md 文档读者 | 学会调试 mutbot | CLAUDE.md 中重写的日志/调试章节 | 不再出现 `python -m mutbot log ...` / `cdp_debug`,统一走 `mutbot pysandbox -c "..."` |

## 待定问题

(无)

## 实施步骤清单

- [x] 新增 `mutbot/cli/pysandbox.py` — CLI 子命令实现
  - [x] argparse 支持 `-c CODE` / 位置参数脚本 / stdin(`-` 或管道)
  - [x] HTTP POST 到 `127.0.0.1:8741/mcp` 调 `pysandbox` tool(实际复用 `mutagent.net.client.MCPClient` 封装,比手写 urllib 更简洁)
  - [x] server 未运行时打印提示(含 `tail ~/.mutbot/logs/server-*.log` 替代方案)+ exit 非 0
- [x] 在 `__main__.py` 注册 `pysandbox` 子命令分发,同时删除 `_log_command` 与对应 `log` 分支
- [x] 删除 `mutbot/cli/log_query.py`(475 行)
- [x] 删除 `mutbot/cli/cdp_debug.py`(707 行)
- [x] 跑完测试验证清单 8 条(全部通过)
- [x] 重写 `D:/ai/CLAUDE.md` 三节
  - [x] 第 25 行 `restart_server` 改为"通过 `mutbot pysandbox -c "mutbot.restart()"` 或 MCP pysandbox"
  - [x] 第 88–131 行 mutbot 日志 CLI 段落改写为 `mutbot pysandbox -c "mutbot.logs(...)"` 示例,补一行"server 未运行时用 `tail`/`grep` 读 `~/.mutbot/logs/server-*.log`"
  - [x] 第 133–150 行 CDP 段落删除,引导至 `chrome-cdp` skill
  - [x] Sandbox namespace 清单保留,合并到"MCP / CLI 两种调用方式"表格下
  - [x] mutagent 日志 CLI(第 92–103 行)保持不变(不在本 spec 范围)
- [x] 更新 `C:/Users/lijia01/.claude/projects/D--ai/memory/feedback_log_errors_expand.md`(旧 `-e` 迁至 `mutbot.logs()` 默认展开;补一条 CLI 返回 repr 包装的注意事项)
