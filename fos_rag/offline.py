"""Runtime outbound boundary; setup/download scripts do not import this module."""

import ipaddress
import os
import socket
import threading
from urllib.parse import urlsplit

_lock = threading.RLock()
_endpoints = frozenset()
_addresses = set()


def configure_api_endpoints(urls):
    """Replace the process allowlist with explicitly configured API endpoints."""
    global _endpoints
    endpoints = set()
    for url in urls:
        try:
            parsed = urlsplit(url or "")
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                endpoints.add((parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)))
        except ValueError:
            continue
    endpoints = frozenset(endpoints)
    with _lock:
        if endpoints != _endpoints:
            _endpoints = endpoints
            _addresses.clear()


def configure_answer_endpoint(url, enabled=False):
    configure_api_endpoints([url] if enabled else [])


def _allowed(host, port):
    with _lock:
        return (host, port) in _endpoints or (host, port) in _addresses


def _host(value):
    return value.decode("ascii") if isinstance(value, bytes) else value


def check_connection(address):
    if not isinstance(address, tuple):
        return  # Unix domain socket, not internet transport.
    host = _host(address[0])
    if _allowed(host, address[1]):
        return
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise PermissionError("离线运行：禁止连接部署主机以外的地址")


def install():
    if getattr(socket, "_fos_offline_installed", False):
        return
    connect, connect_ex, getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def local_connect(sock, address):
        check_connection(address)
        return connect(sock, address)

    def local_connect_ex(sock, address):
        check_connection(address)
        return connect_ex(sock, address)

    def local_resolve(host, *args, **kwargs):
        name = _host(host)
        port = args[0] if args else kwargs.get("port")
        with _lock:
            endpoints = _endpoints
        approved = (name, port) in endpoints
        if name not in {None, "", "localhost"} and not approved:
            try:
                ipaddress.ip_address(name)
            except ValueError as exc:
                raise PermissionError("离线运行：禁止查询外部域名；请先启用远程 API") from exc
        records = getaddrinfo(host, *args, **kwargs)
        if approved:
            with _lock:
                if endpoints == _endpoints:
                    _addresses.update((record[4][0], record[4][1]) for record in records)
        return records

    socket.socket.connect, socket.socket.connect_ex = local_connect, local_connect_ex
    socket.getaddrinfo = local_resolve
    socket._fos_offline_installed = True
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
