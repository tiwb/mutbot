# mutbot log CLI 工具 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：功能设计

## 背景

当前查询 mutbot 日志需要通过 mutagent 的 CLI 工具并手动指定日志目录：

```bash
python -m mutagent.cli.log_query --dir ~/.mutbot/logs sessions
```

痛点：
1. 每次必须写 `--dir ~/.mutbot/logs`，啰嗦
2. session 列表只有时间戳，无法辨识（没有标题、类型等上下文）
3. 无法按时间范围过滤，session 多时找目标困难
4. 没有 `--last N` 只看最近几个的快捷方式
5. server session 碎片化严重，大量短生命周期 session 淹没有用信息

## 设计方案

### 核心设计

在 mutbot 层新建独立的 `python -m mutbot log` CLI 子命令，不复用 mutagent 的 `LogQueryEngine`，直接针对 mutbot 的日志结构和 session 元数据实现。

**入口**：`python -m mutbot log <subcommand>`

**子命令**：

| 子命令 | 说明 |
|--------|------|
| `sessions` | 列出日志 session，带元数据增强 |
| `query` | 查询日志内容（替代 `logs` 子命令） |
| `errors` | 快捷查看最近的 ERROR/WARNING |
| `tail` | 实时跟踪日志输出 |

### sessions 子命令

```bash
python -m mutbot log sessions [options]
```

**增强点**：
- 关联 mutbot session 元数据：从 `~/.mutbot/sessions/` 读取持久化的 session JSON，匹配日志文件中的 session_id，显示 title 和 type
- server 日志显示启动/关闭原因摘要（从首尾几行提取）
- 默认只显示最近 10 个（`--last N` 控制）
- 支持 `--since <duration>` 时间范围过滤（如 `2h`、`1d`、`30m`）
- 支持 `--all` 显示全部

**输出格式**：
```
Session                          Title/Type           Logs  Duration  Last Activity
session-...-a1b2c3  Agent: 代码审查      156   12m       10:30
session-...-d4e5f6  Terminal 1           423   2h        10:25
server-20260317_100445           (server start)        104   1h        10:44
```

### query 子命令

```bash
python -m mutbot log query [options]
```

**选项**：
- `-s/--session <id_or_title>` — 按 session ID（前缀匹配）或 title 模糊匹配
- `-p/--pattern <regex>` — 正则匹配消息内容
- `-l/--level <LEVEL>` — 最低日志级别
- `-n/--limit <N>` — 最大条数（默认 50）
- `-e/--expand` — 展开多行日志
- `--logger <name>` — 按 logger name 前缀过滤
- `--since <duration>` — 只看最近一段时间的日志
- 不指定 session 时默认查最新的 session

### errors 子命令

```bash
python -m mutbot log errors [options]
```

快捷命令，等价于 `query -l WARNING`。额外选项：
- `-n/--limit <N>` — 条数（默认 20）
- `--since <duration>` — 时间范围
- `-e/--expand` — 展开 traceback

### tail 子命令

```bash
python -m mutbot log tail [options]
```

实时跟踪日志输出。选项与 query 相同（`-l`、`-p`、`--logger`），但持续运行直到 Ctrl+C。

### 时间范围解析

`--since` 参数支持简写格式：
- `30m` → 30 分钟
- `2h` → 2 小时
- `1d` → 1 天
- `1h30m` → 1.5 小时

### session 匹配策略

`-s` 参数智能匹配，按优先级：
1. session ID 前缀匹配（如 `a1b2c3`）
2. session title 模糊匹配（如 `Terminal`）
3. 日志文件时间戳前缀（如 `20260317`）
4. `latest`（默认）— 最新的 session

### 日志目录

硬编码 `~/.mutbot/logs/`（mutbot 的标准日志路径），不需要 `--dir` 参数。

## 关键参考

### 源码
- `mutagent/src/mutagent/cli/log_query.py` — 现有 CLI 实现（参考输出格式和解析逻辑）
- `mutagent/src/mutagent/runtime/log_query.py` — LogQueryEngine（可复用日志解析逻辑）
- `mutbot/src/mutbot/__main__.py` — CLI 入口点，需注册 `log` 子命令
- `mutbot/src/mutbot/cli/proxy_log.py` — 现有 mutbot CLI 参考（风格参考）
- `mutbot/src/mutbot/runtime/session_manager.py` — session 日志文件创建逻辑
- `mutbot/src/mutbot/session.py` — Session 声明（metadata 字段）
- `mutbot/src/mutbot/runtime/storage.py` — session JSON 持久化读取

### 文件命名模式
- `server-YYYYMMDD_HHMMSS.log` — 服务器日志
- `supervisor-YYYYMMDD_HHMMSS.log` — Supervisor 日志
- `session-YYYYMMDD_HHMMSS-HEXID.log` — Session 日志
- `session-YYYYMMDD_HHMMSS-HEXID-api.jsonl` — API 录制

## 实施步骤清单

- [x] **Task 1**: 创建 `mutbot/src/mutbot/cli/log_query.py`
  - [x] 实现 `--since` 时间范围解析工具函数
  - [x] 实现 session 元数据加载（从 `~/.mutbot/sessions/` 读取 JSON）
  - [x] 实现 `sessions` 子命令（关联元数据、`--last`、`--since`、`--all`）
  - [x] 实现 `query` 子命令（session 智能匹配、日志过滤）
  - [x] 实现 `errors` 子命令
  - [x] 实现 `tail` 子命令
  - 状态：✅ 已完成

- [x] **Task 2**: 注册到 `__main__.py`
  - [x] 在 `python -m mutbot log` 入口注册子命令
  - 状态：✅ 已完成

- [x] **Task 3**: 更新文档
  - [x] 更新 `D:\ai\CLAUDE.md` — mutbot 日志章节改用新命令
  - [x] 更新 `D:\ai\TODO.md` — 更新相关项状态
  - 状态：✅ 已完成

- [x] **Task 4**: 验收测试
  - [x] `sessions` 默认显示、`--last`、`--since`、`--all`、filter 参数
  - [x] `query` session 智能匹配（标题、hex ID）
  - [x] `errors` 子命令
  - 状态：✅ 已完成
