# MutBot PySandbox 集成 设计规范

**状态**：✅ 已完成
**日期**：2026-04-14
**类型**：功能设计

## 需求

1. mutbot 的 agent 将 pysandbox 作为工具接入
2. MCP endpoint 也暴露 pysandbox（与 agent 共享同一个 SandboxApp）
3. 能力源配置在 `~/.mutbot/config.json` 中全局定义
4. WebToolkit 的 search/fetch 作为验证迁入 sandbox 的 NamespaceTools
5. ConfigToolkit 和 UIToolkit 保持不变

## 关键参考

- `mutbot/src/mutbot/runtime/session_manager.py:309-365` — `build_default_agent()` 工具组装
- `mutbot/src/mutbot/web/server.py` — `_on_startup()` 服务初始化
- `mutbot/src/mutbot/web/mcp.py` — MutBotMCP endpoint + ExecTools
- `mutbot/src/mutbot/builtins/config_toolkit.py` — ConfigToolkit（保留）
- `mutbot/src/mutbot/ui/toolkit.py` — UIToolkit（保留）
- `mutagent/toolkits/web_toolkit.py` — WebToolkit（search/fetch，迁入 sandbox）
- `mutagent/sandbox/app.py` — SandboxApp Declaration（依赖 mutagent 重构）

## 实施步骤清单

- [x] Config 支持 `mcp_sources` / `cli_sources` 字段
- [x] `_on_startup()` 中初始化 SandboxApp + shutdown 清理
- [x] 创建 WebTools（NamespaceTools 子类）
- [x] 创建 PySandboxToolkit（agent 工具）
- [x] 修改 `build_default_agent()` — 替换 WebToolkit 为 PySandboxToolkit
- [x] MCP endpoint 连接 PySandboxTools
- [x] 端到端测试

## 设计方案

### SandboxApp 生命周期

Server 级单例，在 `_on_startup()` 中创建，与 SessionManager 同级：

```python
async def _on_startup():
    global sandbox_app
    sandbox_app = SandboxApp(config=config)
    await sandbox_app.setup()
```

shutdown 时关闭：

```python
async def _on_shutdown():
    if sandbox_app:
        await sandbox_app.shutdown()
```

### 配置格式

`~/.mutbot/config.json` 新增 `mcp_sources` 和 `cli_sources` 字段（与 providers 同级，snake_case）：

```json
{
  "providers": { "...": "..." },
  "mcp_sources": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--cdp-endpoint=http://127.0.0.1:9222"],
      "shell": true
    }
  },
  "cli_sources": {
    "redmine": {
      "command": "python",
      "args": ["/path/to/redmine_tool.py"]
    }
  }
}
```

SandboxApp 通过 Config 属性读取这两个字段。格式与独立 pysandbox 相同（少 `port` 字段）。

Config 路径映射（SandboxApp 内部读取）：
- `config.get("mcp_sources")` → MCP 能力源配置
- `config.get("cli_sources")` → CLI 能力源配置

### Agent 工具注册

在 `build_default_agent()` 中将 pysandbox 注册为 agent 工具：

- 使用 Toolkit 子类，设置 `_tool_prefix = ""` 使工具名为 `pysandbox`
- Toolkit 持有 SandboxApp 引用，exec_code 时传入 per-agent state dict
- state dict 绑定在 Toolkit 实例上（实例随 ToolSet 随 Agent 走，天然 per-agent）

工具集变更：
- WebToolkit → 移除（search/fetch 迁入 sandbox NamespaceTools）
- ConfigToolkit → 保留
- UIToolkit → 保留
- PySandboxToolkit → 新增

### MCP 共享

mutbot 的 MCP endpoint 也暴露 pysandbox tool（复用现有 PySandboxTools MCPToolSet）：
- `PySandboxTools._app = sandbox_app`（在 `_on_startup` 中连接）
- MCP 调用时 state 为 None（每次独立执行，不保留跨步骤变量）
- 后续可扩展为 per-MCP-session state

### WebToolkit 迁入 sandbox

在 mutbot 中新增 NamespaceTools 子类，将 WebToolkit 的能力注册为 sandbox 命名空间函数：

```python
class WebTools(NamespaceTools):
    """Web 搜索和内容获取。"""

    def search(self, query: str, max_results: int = 5) -> str:
        """搜索网页。"""
        ...

    def fetch(self, url: str, format: str = "markdown") -> str:
        """获取网页内容。"""
        ...
```

- namespace 名自动推导为 `Web`（类名去 Tools 后缀）
- agent 在 sandbox 中调用：`web.search(query="...")` / `web.fetch(url="...")`
- 实现复用 WebToolkit 现有的 SearchImpl / FetchImpl 发现机制
