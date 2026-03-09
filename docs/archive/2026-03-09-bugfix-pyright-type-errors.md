# mutbot pyright 类型错误修复

**状态**：✅ 已完成
**日期**：2026-03-09
**类型**：Bug修复

## 背景

mutbot 当前有 22 个 pyright 错误。上次修复（commit `5542294`）处理了一部分，但仍有不少 Optional 访问和类型不匹配问题。

## 现状分析

### 错误分类与统计

| 类别 | 错误数 | 文件 | 说明 |
|------|--------|------|------|
| Optional 成员访问 | 14 | `config_toolkit.py`(4), `agent_bridge.py`(6), `toolkit.py`(2), `routes.py`(2) | 访问可能为 None 的对象属性 |
| 类型不匹配 | 4 | `routes.py`(2), `terminal.py`(1), `server.py`(1) | 参数类型与声明不符 |
| 属性不存在 | 2 | `server.py`(2) | `SessionRuntime.agent` 属性未声明 |
| Awaitable vs Coroutine | 1 | `terminal.py` | `run_coroutine_threadsafe` 参数类型 |

### 错误详情

**config_toolkit.py（4 个错误）**
```
line 172: context.agent.config — context 可能为 None
line 1004-1005: context.agent / context.llm — 同上
```
`context` 来自某个 Optional 返回值，需要加 None 检查。

**agent_bridge.py（6 个错误）**
```
line 379/382/385/390/395/417: 访问可能为 None 的 bridge 对象
```
大量对 Optional 对象的直接属性访问，需要 None guard。

**routes.py（4 个错误）**
```
line 198/203: session 可能为 None
line 286: last_seq 是 int | None 但参数要求 int
line 293: data 是 dict | bytes 但参数要求 bytes
```

**server.py（3 个错误）**
```
line 207: _runtimes 可能为 None
line 208/210: SessionRuntime 没有 agent 属性声明
```

**toolkit.py（2 个错误）**
```
line 70: context.agent.session — context 可能为 None
```

**terminal.py（1 个错误）**
```
line 317: Awaitable[None] 不符合 Coroutine[Any, Any, _T] 参数类型
```
`run_coroutine_threadsafe` 要求 `Coroutine`，但传入的是 `Awaitable`。

## 设计方案

### 核心设计

大部分错误是 Optional 安全访问问题，修复方式比较直接：

1. **Optional 成员访问** — 加 `assert` 或 `if x is not None` guard
2. **类型不匹配** — 修正类型注解或添加类型转换
3. **属性不存在** — 在声明类中添加属性声明

**决策：Optional 访问统一用 `assert`**（运行时注入的属性不适合改为非 Optional，assert 更准确）。

示例：
```python
sm = ctx.session_manager
assert sm is not None
# 后续 sm.get_bridge / sm.start / sm.stop 不再报错
```

**决策**：用 `isinstance(rt, AgentSessionRuntime)` 类型收窄（A 方案，更精确；不污染基类）。

## 关键参考

### 源码
- `mutbot/src/mutbot/builtins/config_toolkit.py:172,1004-1005`
- `mutbot/src/mutbot/runtime/agent_bridge.py:379-417`
- `mutbot/src/mutbot/web/routes.py:198,203,286,293,375,393`
- `mutbot/src/mutbot/web/server.py:207-210`
- `mutbot/src/mutbot/ui/toolkit.py:70`
- `mutbot/src/mutbot/runtime/terminal.py:317`

## 实施步骤清单

### Phase 1: Optional 成员访问 — assert guard [待开始]

- [ ] 修复 `config_toolkit.py` 的 Optional 访问（`self.owner` assert）
- [ ] 修复 `agent_bridge.py` 的 Optional 访问（`ctx.session_manager` assert）
- [ ] 修复 `toolkit.py` 的 Optional 访问（`self.owner` assert）
- [ ] 修复 `routes.py` 的 Optional 访问（`_get_managers` 等处 assert）
  - 状态：⏸️ 待开始

### Phase 2: 类型不匹配修复 [待开始]

- [ ] 修复 `routes.py` 的 2 个类型不匹配（`int | None` 传参、`dict | bytes` 传参）
- [ ] 修复 `terminal.py` 的 `Awaitable` vs `Coroutine` 类型问题
  - 状态：⏸️ 待开始

### Phase 3: SessionRuntime 类型收窄 [待开始]

- [ ] 修复 `server.py` 的 SessionRuntime 属性访问（`isinstance(rt, AgentSessionRuntime)` 收窄）
  - 状态：⏸️ 待开始

### Phase 4: 验证 [待开始]

- [ ] 运行 `npx pyright src/mutbot/` 验证 0 errors
  - 状态：⏸️ 待开始
