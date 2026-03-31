"""mutbot.auth.network — 网络安全工具函数。

提供 loopback 地址判断、客户端 IP 解析（支持反向代理 XFF）等。
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

logger = logging.getLogger(__name__)

# loopback 地址集合
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def is_loopback_only(listen_addresses: list[tuple[str, int]]) -> bool:
    """判断 listen 地址列表是否全部为 loopback。

    ``listen_addresses`` 是 ``[(host, port), ...]`` 格式。
    如果全部 host 都是 127.0.0.1、::1 或 localhost 则返回 True。
    0.0.0.0 和 :: 属于通配地址，视为非 loopback。
    """
    if not listen_addresses:
        return True
    for host, _port in listen_addresses:
        if host not in _LOOPBACK_HOSTS:
            return False
    return True


def is_loopback_ip(ip: str) -> bool:
    """判断一个 IP 地址是否为 loopback。"""
    if ip in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _parse_trusted_proxies(trusted: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """将配置的 trusted_proxies 列表解析为 IP 网段。

    支持单个 IP（如 "127.0.0.1"）和 CIDR（如 "10.0.0.0/8"）。
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in trusted:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid trusted_proxies entry: %s", entry)
    return networks


def _is_trusted(ip: str, networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    """检查 IP 是否在 trusted 网段列表中。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in networks:
        if addr in net:
            return True
    return False


def resolve_client_ip(scope: dict[str, Any], trusted_proxies: list[str] | None = None) -> str:
    """从 ASGI scope 解析客户端真实 IP。

    当请求来自 trusted proxy 时，从 X-Forwarded-For 从右往左扫描，
    跳过 trusted IP，第一个非 trusted 的即为真实 IP。
    无 XFF 或全部为 trusted 时 fallback 到 direct IP。

    默认 trusted_proxies = ["127.0.0.1", "::1"]。
    """
    client = scope.get("client")
    direct_ip = client[0] if client else ""

    if trusted_proxies is None:
        trusted_proxies = ["127.0.0.1", "::1"]

    networks = _parse_trusted_proxies(trusted_proxies)

    # 只有当 direct IP 是 trusted 时才检查 XFF
    if not _is_trusted(direct_ip, networks):
        return direct_ip

    # 从 headers 提取 X-Forwarded-For
    raw_headers = scope.get("headers", [])
    xff = ""
    for k, v in raw_headers:
        if k == b"x-forwarded-for":
            xff = v.decode("latin-1")
            break

    if not xff:
        return direct_ip

    # 从右往左扫描 XFF，跳过 trusted IP
    parts = [p.strip() for p in xff.split(",")]
    for ip in reversed(parts):
        if not _is_trusted(ip, networks):
            return ip

    # 全部为 trusted，fallback 到 direct IP
    return direct_ip
