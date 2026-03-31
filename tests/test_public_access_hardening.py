"""公网访问安全加固 — 单元测试

涵盖：
- setup_token 模块：生成、验证、失效、跨进程传递
- middleware 拦截逻辑：本地放行、远程拦截、白名单放行
- network 工具：loopback 判断、XFF IP 解析、trusted proxy
- SSRF 防护：relay_url 校验
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# setup_token 测试
# ---------------------------------------------------------------------------


class TestSetupToken:
    """setup_token 模块测试。"""

    def setup_method(self):
        """每个测试前重置 token 状态和环境变量。"""
        import mutbot.auth.setup_token as st
        st.invalidate()

    def teardown_method(self):
        import mutbot.auth.setup_token as st
        st.invalidate()

    def test_generate_returns_uuid(self):
        import mutbot.auth.setup_token as st
        token = st.generate()
        assert token is not None
        assert len(token) == 36  # UUID4 格式
        assert "-" in token

    def test_verify_correct_token(self):
        import mutbot.auth.setup_token as st
        token = st.generate()
        assert st.verify(token) is True

    def test_verify_wrong_token(self):
        import mutbot.auth.setup_token as st
        st.generate()
        assert st.verify("wrong-token") is False

    def test_verify_empty_token(self):
        import mutbot.auth.setup_token as st
        st.generate()
        assert st.verify("") is False

    def test_verify_no_active_token(self):
        import mutbot.auth.setup_token as st
        assert st.verify("any-token") is False

    def test_invalidate(self):
        import mutbot.auth.setup_token as st
        token = st.generate()
        assert st.is_active() is True
        st.invalidate()
        assert st.is_active() is False
        assert st.verify(token) is False

    def test_verify_uses_constant_time_comparison(self):
        """验证使用 hmac.compare_digest 而非 == 比较。"""
        import mutbot.auth.setup_token as st
        import inspect
        source = inspect.getsource(st.verify)
        assert "compare_digest" in source, "verify() 应使用 hmac.compare_digest"

    def test_generate_sets_env_var(self):
        """generate() 应同时写入环境变量，供子进程继承。"""
        import mutbot.auth.setup_token as st
        token = st.generate()
        env_val = os.environ.get("MUTBOT_SETUP_TOKEN")
        assert env_val == token, "generate() 应将 token 写入 MUTBOT_SETUP_TOKEN 环境变量"

    def test_invalidate_clears_env_var(self):
        """invalidate() 应同时清理环境变量。"""
        import mutbot.auth.setup_token as st
        st.generate()
        assert "MUTBOT_SETUP_TOKEN" in os.environ
        st.invalidate()
        assert os.environ.get("MUTBOT_SETUP_TOKEN") is None

    def test_cross_process_token_inheritance(self):
        """子进程应能通过环境变量继承 token 并验证。

        模拟 supervisor → worker 场景。
        """
        import mutbot.auth.setup_token as st
        token = st.generate()

        # 启动子进程，验证 token
        result = subprocess.run(
            [sys.executable, "-c", f"""
import mutbot.auth.setup_token as st
print(st.is_active())
print(st.verify("{token}"))
"""],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        assert lines[0] == "True", f"子进程中 is_active() 应为 True，实际: {lines[0]}"
        assert lines[1] == "True", f"子进程中 verify() 应为 True，实际: {lines[1]}"


# ---------------------------------------------------------------------------
# network 工具测试
# ---------------------------------------------------------------------------


class TestNetwork:
    """network 模块测试。"""

    def test_is_loopback_only_all_loopback(self):
        from mutbot.auth.network import is_loopback_only
        assert is_loopback_only([("127.0.0.1", 8741)]) is True
        assert is_loopback_only([("::1", 8741)]) is True
        assert is_loopback_only([("localhost", 8741)]) is True
        assert is_loopback_only([("127.0.0.1", 8741), ("::1", 8741)]) is True

    def test_is_loopback_only_with_non_loopback(self):
        from mutbot.auth.network import is_loopback_only
        assert is_loopback_only([("0.0.0.0", 8741)]) is False
        assert is_loopback_only([("10.0.0.1", 8741)]) is False
        assert is_loopback_only([("127.0.0.1", 8741), ("0.0.0.0", 8741)]) is False

    def test_is_loopback_ip(self):
        from mutbot.auth.network import is_loopback_ip
        assert is_loopback_ip("127.0.0.1") is True
        assert is_loopback_ip("::1") is True
        assert is_loopback_ip("localhost") is True
        assert is_loopback_ip("10.0.0.1") is False
        assert is_loopback_ip("0.0.0.0") is False

    def test_resolve_client_ip_direct(self):
        """无 XFF 时返回 direct IP。"""
        from mutbot.auth.network import resolve_client_ip
        scope = {"client": ("10.0.0.1", 12345), "headers": []}
        assert resolve_client_ip(scope) == "10.0.0.1"

    def test_resolve_client_ip_with_xff_from_trusted(self):
        """来自 trusted proxy 的请求，从 XFF 取真实 IP。"""
        from mutbot.auth.network import resolve_client_ip
        scope = {
            "client": ("127.0.0.1", 12345),
            "headers": [(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
        }
        # 默认 trusted = ["127.0.0.1", "::1"]
        assert resolve_client_ip(scope) == "10.0.0.1"

    def test_resolve_client_ip_xff_right_to_left(self):
        """XFF 从右往左扫描，跳过 trusted IP。"""
        from mutbot.auth.network import resolve_client_ip
        scope = {
            "client": ("127.0.0.1", 12345),
            "headers": [(b"x-forwarded-for", b"1.2.3.4, 10.0.0.5, 127.0.0.1")],
        }
        trusted = ["127.0.0.1", "::1", "10.0.0.0/8"]
        assert resolve_client_ip(scope, trusted) == "1.2.3.4"

    def test_resolve_client_ip_untrusted_direct_ignores_xff(self):
        """direct IP 不在 trusted 中时，忽略 XFF（防止伪造）。"""
        from mutbot.auth.network import resolve_client_ip
        scope = {
            "client": ("10.0.0.1", 12345),
            "headers": [(b"x-forwarded-for", b"1.2.3.4")],
        }
        # 10.0.0.1 不在默认 trusted 中
        assert resolve_client_ip(scope) == "10.0.0.1"

    def test_resolve_client_ip_cidr_trusted(self):
        """支持 CIDR 网段匹配。"""
        from mutbot.auth.network import resolve_client_ip
        scope = {
            "client": ("10.0.0.5", 12345),
            "headers": [(b"x-forwarded-for", b"203.0.113.50")],
        }
        trusted = ["10.0.0.0/8"]
        assert resolve_client_ip(scope, trusted) == "203.0.113.50"


# ---------------------------------------------------------------------------
# middleware 拦截逻辑测试
# ---------------------------------------------------------------------------


def _make_scope(
    path: str,
    client_ip: str = "127.0.0.1",
    scope_type: str = "http",
    headers: list | None = None,
) -> dict[str, Any]:
    """构造 ASGI scope。"""
    return {
        "type": scope_type,
        "path": path,
        "client": (client_ip, 12345),
        "headers": headers or [],
        "query_string": b"",
    }


class TestMiddleware:
    """middleware 拦截逻辑测试。"""

    @pytest.mark.asyncio
    async def test_local_no_auth_allows(self):
        """本地访问 + 无 auth → 放行。"""
        from mutbot.auth.middleware import _mutbot_before_route

        with patch("mutbot.auth.middleware._get_auth_config", return_value=None):
            scope = _make_scope("/", client_ip="127.0.0.1")
            result = await _mutbot_before_route(None, scope, "/")
            assert result is None  # 放行

    @pytest.mark.asyncio
    async def test_remote_no_auth_redirects_to_setup(self):
        """远程访问 + 无 auth → 重定向到 /auth/setup。

        这是核心安全测试：非 loopback 请求在无 auth 时必须被拦截。
        """
        import mutbot.auth.setup_token as st
        from mutbot.auth.middleware import _mutbot_before_route
        st.invalidate()  # 确保无 token

        with patch("mutbot.auth.middleware._get_auth_config", return_value=None), \
             patch("mutbot.auth.middleware._get_trusted_proxies", return_value=["127.0.0.1", "::1"]):
            scope = _make_scope("/", client_ip="10.0.0.1")
            result = await _mutbot_before_route(None, scope, "/")
            assert result is not None, "远程 + 无 auth 应拦截请求"
            assert result.status == 302
            assert "/auth/setup" in result.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_remote_no_auth_with_active_token_redirects(self):
        """远程访问 + 无 auth + 有活跃 token → 也应重定向。"""
        import mutbot.auth.setup_token as st
        from mutbot.auth.middleware import _mutbot_before_route
        st.generate()

        try:
            with patch("mutbot.auth.middleware._get_auth_config", return_value=None), \
                 patch("mutbot.auth.middleware._get_trusted_proxies", return_value=["127.0.0.1", "::1"]):
                scope = _make_scope("/", client_ip="10.0.0.1")
                result = await _mutbot_before_route(None, scope, "/")
                assert result is not None, "远程 + 无 auth + 有 token 应拦截请求"
                assert result.status == 302
        finally:
            st.invalidate()

    @pytest.mark.asyncio
    async def test_remote_no_auth_whitelist_allows(self):
        """远程 + 无 auth，但白名单路径（/auth/）应放行。"""
        from mutbot.auth.middleware import _mutbot_before_route

        with patch("mutbot.auth.middleware._get_auth_config", return_value=None), \
             patch("mutbot.auth.middleware._get_trusted_proxies", return_value=["127.0.0.1", "::1"]):
            scope = _make_scope("/auth/setup", client_ip="10.0.0.1")
            result = await _mutbot_before_route(None, scope, "/auth/setup")
            assert result is None  # 白名单放行

    @pytest.mark.asyncio
    async def test_remote_no_auth_websocket_rejected(self):
        """远程 + 无 auth + WebSocket → 返回 4401。"""
        from mutbot.auth.middleware import _mutbot_before_route

        with patch("mutbot.auth.middleware._get_auth_config", return_value=None), \
             patch("mutbot.auth.middleware._get_trusted_proxies", return_value=["127.0.0.1", "::1"]):
            scope = _make_scope("/ws/app", client_ip="10.0.0.1", scope_type="websocket")
            result = await _mutbot_before_route(None, scope, "/ws/app")
            assert result is not None
            assert result.status == 4401

    @pytest.mark.asyncio
    async def test_remote_with_auth_uses_oidc(self):
        """远程 + 有 auth → 走正常 OIDC 流程（不影响）。"""
        from mutbot.auth.middleware import _mutbot_before_route

        auth_config = {"relay": "https://mutbot.ai"}
        with patch("mutbot.auth.middleware._get_auth_config", return_value=auth_config), \
             patch("mutbot.auth.middleware._get_trusted_proxies", return_value=["127.0.0.1", "::1"]):
            # 根路径放行（让前端加载）
            scope = _make_scope("/", client_ip="10.0.0.1")
            result = await _mutbot_before_route(None, scope, "/")
            assert result is None  # 根路径放行

    @pytest.mark.asyncio
    async def test_internal_path_blocked_for_remote(self):
        """/internal/ 非本地请求应返回 403。"""
        from mutbot.auth.middleware import _mutbot_before_route

        auth_config = {"relay": "https://mutbot.ai"}
        with patch("mutbot.auth.middleware._get_auth_config", return_value=auth_config), \
             patch("mutbot.auth.middleware._get_trusted_proxies", return_value=["127.0.0.1", "::1"]):
            scope = _make_scope("/internal/drain", client_ip="10.0.0.1")
            result = await _mutbot_before_route(None, scope, "/internal/drain")
            assert result is not None
            assert result.status == 403

    @pytest.mark.asyncio
    async def test_mcp_path_blocked_for_remote(self):
        """/mcp 非本地请求应返回 403。"""
        from mutbot.auth.middleware import _mutbot_before_route

        auth_config = {"relay": "https://mutbot.ai"}
        with patch("mutbot.auth.middleware._get_auth_config", return_value=auth_config), \
             patch("mutbot.auth.middleware._get_trusted_proxies", return_value=["127.0.0.1", "::1"]):
            scope = _make_scope("/mcp", client_ip="10.0.0.1")
            result = await _mutbot_before_route(None, scope, "/mcp")
            assert result is not None
            assert result.status == 403

    @pytest.mark.asyncio
    async def test_mcp_allowed_for_local(self):
        """/mcp 本地请求应放行。"""
        from mutbot.auth.middleware import _mutbot_before_route

        auth_config = {"relay": "https://mutbot.ai"}
        with patch("mutbot.auth.middleware._get_auth_config", return_value=auth_config), \
             patch("mutbot.auth.middleware._get_trusted_proxies", return_value=["127.0.0.1", "::1"]):
            scope = _make_scope("/mcp", client_ip="127.0.0.1")
            result = await _mutbot_before_route(None, scope, "/mcp")
            assert result is None


# ---------------------------------------------------------------------------
# SSRF 防护测试
# ---------------------------------------------------------------------------


class TestSSRFValidation:
    """relay_url SSRF 防护测试。"""

    def test_https_allowed(self):
        from mutbot.auth.views import _validate_relay_url
        assert _validate_relay_url("https://mutbot.ai") is None

    def test_http_localhost_allowed(self):
        from mutbot.auth.views import _validate_relay_url
        assert _validate_relay_url("http://localhost:8080") is None
        assert _validate_relay_url("http://127.0.0.1:8080") is None

    def test_http_remote_rejected(self):
        from mutbot.auth.views import _validate_relay_url
        result = _validate_relay_url("http://evil.com")
        assert result is not None
        assert "HTTPS" in result

    def test_private_ip_rejected(self):
        from mutbot.auth.views import _validate_relay_url
        result = _validate_relay_url("https://10.0.0.1")
        assert result is not None
        assert "private" in result.lower()

    def test_no_scheme_rejected(self):
        from mutbot.auth.views import _validate_relay_url
        result = _validate_relay_url("ftp://mutbot.ai")
        assert result is not None


# ---------------------------------------------------------------------------
# Supervisor 时序测试（复现 bug）
# ---------------------------------------------------------------------------


class TestSupervisorTokenTiming:
    """验证 supervisor 启动时 token 在 worker spawn 之前生成。

    这是复现当前 bug 的关键测试：
    supervisor._print_banner() 调用 generate()，但在 _spawn_worker() 之后。
    Worker spawn 时环境变量中没有 token。
    """

    def test_token_exists_before_worker_spawn(self):
        """token 应在 worker spawn 之前就存在于环境变量中。

        当前实现中 _print_banner（含 generate）在 _spawn_worker 之后调用，
        导致 worker 继承不到 token。这个测试应该 FAIL。
        """
        import mutbot.auth.setup_token as st
        st.invalidate()

        # 模拟 supervisor 启动序列：读取源码确认调用顺序
        import inspect
        from mutbot.web.supervisor import Supervisor
        source = inspect.getsource(Supervisor._serve)

        # 找到 _spawn_worker 和 _print_banner 的调用位置
        spawn_pos = source.find("_spawn_worker")
        banner_pos = source.find("_print_banner")

        assert spawn_pos > 0, "找不到 _spawn_worker 调用"
        assert banner_pos > 0, "找不到 _print_banner 调用"

        # token 生成在 _print_banner 中，_print_banner 应在 _spawn_worker 之前
        assert banner_pos < spawn_pos, (
            f"_print_banner（含 token 生成）应在 _spawn_worker 之前调用，"
            f"但当前 banner 在位置 {banner_pos}，spawn 在位置 {spawn_pos}"
        )
