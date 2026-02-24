"""测试 runtime 模块重组后的 import 路径正确性（Phase 1）"""

import importlib


# ---------------------------------------------------------------------------
# 模块可导入性
# ---------------------------------------------------------------------------

def test_import_runtime_storage():
    """runtime.storage 模块可导入且包含核心 API"""
    from mutbot.runtime.storage import (
        save_json, load_json, append_jsonl, load_jsonl,
        save_workspace, load_all_workspaces,
        save_session_metadata, load_session_metadata, load_all_sessions,
        append_session_event, load_session_events,
        MUTBOT_DIR,
    )
    assert MUTBOT_DIR == ".mutbot"


def test_import_runtime_workspace():
    """runtime.workspace 模块可导入且包含 WorkspaceManager"""
    from mutbot.runtime.workspace import WorkspaceManager, Workspace
    wm = WorkspaceManager()
    assert hasattr(wm, "create")
    assert hasattr(wm, "get")
    assert hasattr(wm, "list_all")


def test_import_runtime_session():
    """runtime.session 模块可导入且包含 Session 相关类"""
    from mutbot.runtime.session import (
        Session, AgentSession, TerminalSession, DocumentSession,
        SessionManager, SessionRuntime, AgentSessionRuntime,
        get_session_type_map, get_session_class,
    )
    assert issubclass(AgentSession, Session)
    assert issubclass(TerminalSession, Session)
    assert issubclass(DocumentSession, Session)


def test_import_runtime_terminal():
    """runtime.terminal 模块可导入且包含 TerminalManager"""
    from mutbot.runtime.terminal import (
        TerminalManager, TerminalSession, OutputCallback,
    )
    tm = TerminalManager()
    assert hasattr(tm, "attach")
    assert hasattr(tm, "detach")
    assert hasattr(tm, "create")


def test_import_web_server():
    """web.server 整体导入链正常（间接验证所有内部 import）"""
    from mutbot.web.server import app
    assert app is not None
    assert app.title == "MutBot"


def test_import_web_auth():
    """web.auth 使用新的 storage import 路径"""
    from mutbot.web.auth import AuthManager
    am = AuthManager()
    assert hasattr(am, "load_config")


# ---------------------------------------------------------------------------
# 旧 import 路径已不存在
# ---------------------------------------------------------------------------

def test_old_storage_import_removed():
    """旧路径 mutbot.storage 不再存在"""
    try:
        importlib.import_module("mutbot.storage")
        assert False, "mutbot.storage should not exist"
    except (ModuleNotFoundError, ImportError):
        pass


def test_old_workspace_import_removed():
    """旧路径 mutbot.workspace 不再存在"""
    try:
        importlib.import_module("mutbot.workspace")
        assert False, "mutbot.workspace should not exist"
    except (ModuleNotFoundError, ImportError):
        pass


def test_old_session_import_removed():
    """旧路径 mutbot.session 不再存在"""
    try:
        importlib.import_module("mutbot.session")
        assert False, "mutbot.session should not exist"
    except (ModuleNotFoundError, ImportError):
        pass


def test_old_web_terminal_import_removed():
    """旧路径 mutbot.web.terminal 不再存在"""
    try:
        importlib.import_module("mutbot.web.terminal")
        assert False, "mutbot.web.terminal should not exist"
    except (ModuleNotFoundError, ImportError):
        pass


# ---------------------------------------------------------------------------
# terminal 回调抽象
# ---------------------------------------------------------------------------

def test_terminal_no_fastapi_dependency():
    """runtime.terminal 不依赖 fastapi.WebSocket"""
    import inspect
    from mutbot.runtime import terminal as mod

    source = inspect.getsource(mod)
    assert "from fastapi" not in source
    assert "import WebSocket" not in source


def test_terminal_output_callback_type():
    """OutputCallback 类型定义正确"""
    from mutbot.runtime.terminal import OutputCallback
    import typing
    # OutputCallback 应该是 Callable[[bytes], Awaitable[None]]
    assert OutputCallback is not None
