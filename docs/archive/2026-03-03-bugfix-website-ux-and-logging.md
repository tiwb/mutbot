# 官网关闭工作区导航 & 控制台日志级别 设计规范

**状态**：✅ 已完成
**日期**：2026-03-03
**类型**：Bug修复 / 功能改进

## 背景

两个小改进：

1. **官网导航**：mutbot.ai 上从 `#workspace` 关闭工作区时，退到了本地的 WorkspaceSelector 界面，期望回到官网 Landing 首页（与刷新行为一致）。根因：`location.hash = ""` 只清空 hash，动态注入的 React 仍在运行。

2. **控制台日志**：`python -m mutbot` 默认 INFO 级别输出过多。期望默认安静，本地调试时通过 config 配置日志级别。

## 设计方案

### 修复一：远程模式关闭工作区整页跳转

`App.tsx` 的 `close_workspace` 和 `onHashChange` 中清空 hash 时，远程模式下改为整页跳转：

```typescript
// 抽取统一函数
function exitWorkspace() {
  if (isRemote()) {
    location.href = location.origin + "/";
  } else {
    location.hash = "";
  }
}
```

影响两处：`close_workspace` 动作（行 427）和 `onHashChange` 中 workspace 不存在时（行 211）。

### 修复二：控制台日志默认 WARNING + config 可配

- 默认控制台级别从 INFO 改为 WARNING
- 支持 `config.json` 中 `logging.console_level` 配置（如 `"DEBUG"` / `"INFO"`）
- `--debug` 标志优先级最高，始终 DEBUG
- uvicorn `log_level` 同步调整为 `"warning"`
- 启动 banner（`print()`）不受影响
- 文件/内存日志保持 DEBUG 不变

```json
// ~/.mutbot/config.json 调试时
{ "logging": { "console_level": "DEBUG" } }
```

## 关键参考

### 源码
- `mutbot/frontend/src/App.tsx:426-427` — `close_workspace` 处理
- `mutbot/frontend/src/App.tsx:210-212` — hashchange 中清空 hash
- `mutbot/frontend/src/lib/connection.ts:29-32` — `isRemote()`
- `mutbot/src/mutbot/__main__.py:17-22` — 控制台日志初始化
- `mutbot/src/mutbot/__main__.py:28` — uvicorn log_level
- `mutbot/src/mutbot/runtime/config.py` — `load_mutbot_config()`

## 实施步骤清单

### 阶段一：前端 — 远程模式关闭工作区整页跳转 [✅ 已完成]

- [x] **Task 1.1**: 在 `App.tsx` 中添加 `exitWorkspace()` 辅助函数
  - 在 Helpers 区域组件外部定义 `exitWorkspace()`
  - 远程模式 → `location.href = location.origin + "/"`；本地模式 → `location.hash = ""`
  - 状态：✅ 已完成

- [x] **Task 1.2**: 替换 `close_workspace` 中的 `location.hash = ""`
  - 改为调用 `exitWorkspace()`
  - 状态：✅ 已完成

- [x] **Task 1.3**: 替换 `onHashChange` 中的 `location.hash = ""`
  - 改为调用 `exitWorkspace()`
  - 状态：✅ 已完成

### 阶段二：后端 — 控制台日志默认 WARNING + config 可配 [✅ 已完成]

- [x] **Task 2.1**: 修改 `__main__.py` 日志初始化逻辑
  - `--debug` → DEBUG；否则读 `config.get("logging.console_level", "WARNING")`
  - 状态：✅ 已完成

- [x] **Task 2.2**: uvicorn `log_level` 与 console_level 同步
  - DEBUG → "debug"，INFO → "info"，WARNING+ → "warning"
  - 状态：✅ 已完成

### 阶段三：验证 [✅ 已完成]

- [x] **Task 3.1**: 前端构建验证
  - `npm run build` 成功，无报错
  - 状态：✅ 已完成

- [x] **Task 3.2**: 后端配置加载验证
  - `config.get("logging.console_level")` 默认返回 WARNING，正确解析
  - 状态：✅ 已完成

## 测试验证

- 前端构建：✅ `npm run build` 成功
- 后端配置加载：✅ `logging.console_level` 默认 WARNING，`getattr` 解析正确
- uvicorn log_level 同步逻辑：✅ 三档映射正确
