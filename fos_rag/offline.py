"""Runtime outbound boundary; setup/download scripts do not import this module."""

import ipaddress
import os
import socket


def _host(value):
    return value.decode("ascii") if isinstance(value, bytes) else value


def check_connection(address):
    if not isinstance(address, tuple):
        return  # Unix domain socket, not internet transport.
    host = _host(address[0])
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
        if name not in {None, "", "localhost"}:
            try:
                ipaddress.ip_address(name)  # Numeric bind addresses need no external DNS.
            except ValueError as exc:
                raise PermissionError("离线运行：禁止查询外部域名") from exc
        return getaddrinfo(host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex = local_connect, local_connect_ex
    socket.getaddrinfo = local_resolve
    socket._fos_offline_installed = True
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
