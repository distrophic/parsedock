"""Соединение только с уже проверенными адресами.

urllib3 иначе спросит имя ещё раз в момент connect. Здесь сокет открывается
на адреса из allowed_ips. Имя в запросе и в проверке сертификата не меняется.
"""

from __future__ import annotations

import ipaddress
import socket

import urllib3
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util.connection import create_connection

_IP = ipaddress.IPv4Address | ipaddress.IPv6Address


class PinnedAdapter(HTTPAdapter):
    """Пул requests, который не разрешает имя повторно."""

    def __init__(self, ips: tuple[_IP, ...], **kwargs) -> None:
        self._pinned_ips = tuple(str(ip) for ip in ips)
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs) -> None:
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        http_pool, https_pool = _pool_classes(self._pinned_ips)
        self.poolmanager.pool_classes_by_scheme = {
            "http": http_pool,
            "https": https_pool,
        }


def _pool_classes(ips: tuple[str, ...]):
    class PinHTTP(urllib3.connection.HTTPConnection):
        def _new_conn(self) -> socket.socket:
            return _open_checked(self, ips)

    class PinHTTPS(urllib3.connection.HTTPSConnection):
        def _new_conn(self) -> socket.socket:
            return _open_checked(self, ips)

    class PinHTTPPool(urllib3.HTTPConnectionPool):
        ConnectionCls = PinHTTP

    class PinHTTPSPool(urllib3.HTTPSConnectionPool):
        ConnectionCls = PinHTTPS

    return PinHTTPPool, PinHTTPSPool


def _open_checked(conn, ips: tuple[str, ...]) -> socket.socket:
    last: Exception | None = None
    for ip in ips:
        try:
            return create_connection(
                (ip, conn.port),
                conn.timeout,
                source_address=conn.source_address,
                socket_options=conn.socket_options,
            )
        except socket.gaierror as exc:
            raise NameResolutionError(conn.host, conn, exc) from exc
        except TimeoutError as exc:
            last = exc
        except OSError as exc:
            last = exc
    if isinstance(last, TimeoutError):
        raise ConnectTimeoutError(
            conn,
            f"Connection to {conn.host} timed out. (connect timeout={conn.timeout})",
        ) from last
    raise NewConnectionError(conn, f"Failed to establish a new connection: {last}") from last
