# Chrome CDP 远程调试 CLI 设计规范

**状态**：✅ 已完成
**日期**：2026-03-09
**类型**：Bug修复（调试辅助）

## 背景

在 bugfix-workspace-hash-routing 实施过程中，`main.tsx` 的 `replaceState + pushState` 代码已确认存在于 build 产物中（`dev-build-verify.sh` 检查通过），但浏览器中 `console.debug` 未输出。怀疑浏览器缓存导致加载了旧 JS。

需要通过 Chrome DevTools Protocol (CDP) 远程调试，直接连接浏览器确认：
1. 页面实际加载的 JS 文件 URL
2. JS 文件内容是否包含新代码
3. console 输出是否被过滤或未触发

## 设计方案

### 实现位置

`mutbot.cli.cdp_debug` — 遵循现有 CLI 模块模式（参考 `mutbot.cli.proxy_log`）。

用法：`python -m mutbot.cli.cdp_debug <command> [options]`

### Chrome 启动

手动启动，脚本只负责连接和调试（自动启动需处理端口占用、进程管理等复杂问题）。

```bash
# Windows — 用独立 user-data-dir 避免冲突
"C:/Program Files/Google/Chrome/Application/chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:/tmp/chrome-debug"
```

### 子命令设计

#### `pages` — 列出可调试页面

```bash
python -m mutbot.cli.cdp_debug pages
# 输出：
# ID        TYPE        URL
# a1b2c3d4  page        http://localhost:4321/
# e5f6g7h8  page        http://localhost:8741/#workspace
```

#### `eval` — 在目标页面执行 JS 表达式

```bash
python -m mutbot.cli.cdp_debug eval "location.href"
python -m mutbot.cli.cdp_debug eval "history.length"
python -m mutbot.cli.cdp_debug eval --page 0 "document.title"
```

- `--page N`：选择第 N 个页面（默认 0）
- `--url PATTERN`：按 URL 模式匹配页面（如 `mutbot`、`localhost:4321`）

#### `console` — 监听 console 输出

```bash
python -m mutbot.cli.cdp_debug console
python -m mutbot.cli.cdp_debug console --reload          # 强制刷新后监听
python -m mutbot.cli.cdp_debug console --filter mutbot    # 过滤关键字
python -m mutbot.cli.cdp_debug console --timeout 10       # 10 秒后自动退出
```

- `--reload`：连接后立即 `Page.reload(ignoreCache=true)`
- `--filter PATTERN`：只输出匹配的 console 消息
- `--timeout N`：N 秒后自动退出（默认无限，Ctrl+C 退出）

#### `check` — 一键诊断（组合命令）

```bash
python -m mutbot.cli.cdp_debug check
python -m mutbot.cli.cdp_debug check --marker ensureLandingInHistory
```

执行流程：
1. 连接 CDP，找到目标页面
2. 获取 `Performance.getEntriesByType('resource')` 列出已加载 JS 文件
3. 获取 `history.length`、`location.hash`、`location.href` 等状态
4. 如指定 `--marker`，用 `Page.searchInResource` 或 fetch+grep 检查 JS 内容
5. 输出汇总报告

### 依赖

仅用标准库（`urllib.request`、`asyncio`、`json`），零外部依赖。CDP WebSocket 通信用 `asyncio` 直接实现简易 WebSocket 客户端，或用标准库 `http.client` 做 WebSocket upgrade。

考虑到 CDP WebSocket 只需简单的文本帧收发，可以用一个最小化的 WebSocket 实现（~50 行），无需 `websockets` 库。

### 模块结构

```
mutbot/cli/cdp_debug.py   # 单文件，包含所有子命令
```

遵循 `proxy_log.py` 的模式：顶层 `main()` 用 argparse，子命令分别实现。

## 关键参考

### 源码
- `mutbot/src/mutbot/cli/proxy_log.py` — CLI 模块模式参考
- `mutbot/src/mutbot/cli/__init__.py` — CLI 包
- `mutbot/frontend/src/main.tsx:6-13` — replaceState + pushState 代码（待验证目标）
- `mutbot.ai/docs/specifications/bugfix-workspace-hash-routing.md` — 父问题规范

### 外部参考
- Chrome DevTools Protocol: https://chromedevtools.github.io/devtools-protocol/
- CDP Runtime domain: https://chromedevtools.github.io/devtools-protocol/tot/Runtime/

## 实施步骤清单

### 阶段一：核心实现 [✅ 已完成]

- [x] **Task 1.1**: 实现最小化 WebSocket 客户端
  - 状态：✅ 已完成

- [x] **Task 1.2**: 实现 CDP 连接层
  - 状态：✅ 已完成

- [x] **Task 1.3**: 实现 `pages` 子命令
  - 状态：✅ 已完成

- [x] **Task 1.4**: 实现 `eval` 子命令（含 `--await` 支持）
  - 状态：✅ 已完成

- [x] **Task 1.5**: 实现 `console` 子命令
  - 状态：✅ 已完成

- [x] **Task 1.6**: 实现 `check` 子命令
  - 状态：✅ 已完成

### 阶段二：验证 [✅ 已完成]

- [x] **Task 2.1**: 启动 Chrome + 连接测试
  - `python -m mutbot.cli.cdp_debug pages` 正常列出页面
  - 状态：✅ 已完成

- [x] **Task 2.2**: 用 CDP 诊断 hash routing 问题
  - 确认 JS 包含 `ensureLandingInHistory` 标记
  - 确认 `console.debug` 正常输出
  - **发现根因**：Chrome 反劫持机制跳过初始加载时的 replaceState+pushState 条目
  - 状态：✅ 已完成

## 测试验证

（实施阶段填写）
