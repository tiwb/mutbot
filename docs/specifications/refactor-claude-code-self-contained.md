# Claude Code 接入自包含重构 设计规范

**状态**：🔄 实施中（第一步 ✅ 已完成，第二步待后续）
**日期**：2026-03-17
**类型**：重构

## 背景

Claude Code Web 界面集成（`feature-claude-code-webui.md`）已完成初版，但实现方式违反了 mutobj 的**自包含原则**：

- `ClaudeCodeSession` 声明混在核心 `session.py` 中
- `builtins/__init__.py` 强制 import `mutbot.runtime.claude_code`
- `mutbot/__init__.py` 强制导出 `ClaudeCodeSession`
- 前端硬编码了 `claudecode` 类型和面板工厂

按 mutobj 设计原则，**不 import 就不存在**。Claude Code 功能应该完全自包含——去掉相关 import 后，所有功能（后端声明、@impl、前端入口、菜单项）都不应生效。

**当前决策**：功能尚不完善，先去掉入口并移动到独立包，不要求能运行。后续继续重构完善。

## 设计方案

### 总体规划

| 阶段 | 内容 | 状态 |
|------|------|------|
| 第一步（本次） | 去掉入口，后端移到 `claude_code/` 包，前端去掉引用保留文件 | ✅ 已完成 |
| 第二步（后续） | 自包含模块可插拔接入，配置驱动加载，功能完善 | ⏸️ 未开始 |

### 第一步：去掉入口 + 移动到独立包

**后端**：
- 从 `session.py` 删除 `ClaudeCodeSession` 声明
- 从 `__init__.py` 移除 `ClaudeCodeSession` 导出
- 从 `builtins/__init__.py` 删除 `import mutbot.runtime.claude_code`
- 将 `runtime/claude_code.py` 移动到 `claude_code/` 包，`ClaudeCodeSession` 声明放入包内
- 不要求能运行，import 路径等问题留给第二步

**前端**：
- 去掉 `App.tsx`、`PanelFactory.tsx`、`layout.ts` 中的 `claudecode` 引用
- 保留 `ClaudeCodePanel.tsx` 文件不动

### 第二步（后续，不在本次实施）

- 整理 `claude_code/` 包结构，修复 import 路径
- 配置驱动加载：`resolve_class` 自动 import
- 前端动态面板注册机制
- 功能完善和测试

## 关键参考

### 源码
- `mutbot/src/mutbot/session.py:161-170` — ClaudeCodeSession 声明
- `mutbot/src/mutbot/builtins/__init__.py:13` — 强制 import
- `mutbot/src/mutbot/__init__.py:5` — 包级导出
- `mutbot/src/mutbot/runtime/claude_code.py` — 完整运行时实现
- `mutbot/frontend/src/App.tsx:405-408, 505-511` — 前端路由
- `mutbot/frontend/src/panels/PanelFactory.tsx:14, 75-83` — 面板工厂
- `mutbot/frontend/src/lib/layout.ts:6` — 面板常量

### 相关规范
- `docs/specifications/feature-claude-code-webui.md` — 原始设计规范（✅ 已完成）

## 实施步骤清单

### Phase 1: 后端 — 移动到独立包 [✅ 已完成]

- [x] **Task 1.1**: 创建 `mutbot/src/mutbot/claude_code/` 包
  - [x] 创建 `__init__.py`（包说明，暂不做自动注册）
  - [x] 将 `runtime/claude_code.py` 移动为 `claude_code/runtime.py`（import 路径已改为 `mutbot.claude_code.session`）
  - [x] 将 `ClaudeCodeSession` 声明从 `session.py` 移入 `claude_code/session.py`
  - 状态：✅ 已完成

- [x] **Task 1.2**: 清理原有入口
  - [x] `builtins/__init__.py` — 删除 `import mutbot.runtime.claude_code` 行
  - [x] `__init__.py` — 从导出列表移除 `ClaudeCodeSession`
  - [x] `session.py` — 删除 `ClaudeCodeSession` 类定义
  - [x] `runtime/claude_code.py` — 删除旧文件
  - 状态：✅ 已完成

### Phase 2: 前端 — 去掉引用 [✅ 已完成]

- [x] **Task 2.1**: 去掉前端硬编码引用
  - [x] `layout.ts` — 删除 `PANEL_CLAUDE_CODE` 常量
  - [x] `PanelFactory.tsx` — 删除 lazy import 和 case 分支
  - [x] `App.tsx` — 删除 `PANEL_CLAUDE_CODE` import、`claudecode` case 和 componentMap 条目
  - 状态：✅ 已完成

### Phase 3: 构建验证 [✅ 已完成]

- [x] **Task 3.1**: 前端构建验证
  - [x] `npm run build` 通过，无编译错误
  - 状态：✅ 已完成

## 测试验证

- 前端 `tsc -b && vite build` 通过（2402 modules，无 error）
- `ClaudeCodePanel.tsx` 文件保留但不再被引用，不参与构建产物
- 后端 `claude_code/` 包存在但未被任何代码 import，不影响运行
