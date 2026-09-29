"""Проверка адреса перед запросом.

Смотрим не на текст URL, а на адреса, которые вернул системный резолвер.
Если среди них есть хотя бы один закрытый адрес, запрос не выполняется:
нельзя надеяться, что соединение попадёт на «хороший» адрес из списка.

Клиенты requests и curl_cffi затем соединяются только с этими адресами
и имя больше не спрашивают. Браузер Chromium имя разрешает сам, поэтому
для него зазор между проверкой и соединением остаётся.

Прокси из переменных окружения клиент не использует, иначе запрос ушёл
бы в обход этой проверки.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from parsedock.errors import NetworkError

_TRUSTED_PRIVATE = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def address_allowed(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    trust_private_networks: bool,
) -> bool:
    """Разрешён ли конкретный IP для этого профиля."""

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        return False
    if ip.is_global:
        return True
    if not trust_private_networks:
        return False
    if ip.is_loopback:
        return True
    return any(ip in network for network in _TRUSTED_PRIVATE)


def ensure_target_allowed(url: str, *, trust_private_networks: bool) -> None:
    """Отклонить адрес, который этому профилю запрашивать нельзя."""

    allowed_ips(url, trust_private_networks=trust_private_networks)


def allowed_ips(
    url: str,
    *,
    trust_private_networks: bool,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Адреса, на которые можно открывать соединение. Имя больше не спрашивать."""

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise NetworkError(f"Адрес не поддерживается: {url}")
    if parts.username is not None or parts.password is not None:
        raise NetworkError("Адрес не должен содержать логин или пароль")
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    found = resolve_ips(parts.hostname, port)
    blocked = [ip for ip in found if not address_allowed(ip, trust_private_networks=trust_private_networks)]
    if blocked:
        shown = ", ".join(str(ip) for ip in blocked)
        raise NetworkError(
            f"Адрес {parts.hostname} ({shown}) закрыт для этого профиля. "
            "Параметр trust_private_networks разрешает localhost и частные сети "
            "10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 и IPv6 ULA. "
            "Link-local, в том числе 169.254.169.254, остаётся закрытым."
        )
    return found


def resolve_overrides(url: str, ips: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]) -> list[str]:
    """Строки CURLOPT_RESOLVE: имя и порт остаются, адрес берётся из проверки."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    lines: list[str] = []
    for ip in ips:
        text = str(ip)
        if isinstance(ip, ipaddress.IPv6Address):
            text = f"[{text}]"
        lines.append(f"{host}:{port}:{text}")
    return lines


def resolve_ips(
    host: str,
    port: int,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Все адреса хоста для TCP-соединения, без повторов."""

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkError(f"Не удалось определить адрес {host}", retryable=True) from exc

    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for info in infos:
        raw = info[4][0].split("%", 1)[0]
        ip = ipaddress.ip_address(raw)
        if ip in seen:
            continue
        seen.add(ip)
        found.append(ip)
    if not found:
        raise NetworkError(f"Не удалось определить адрес {host}")
    return tuple(found)
