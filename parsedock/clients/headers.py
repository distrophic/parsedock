"""Заголовки запроса с учётом перенаправления.

Authorization, Proxy-Authorization и Cookie остаются только на том же
происхождении, что и стартовый адрес: та же схема, тот же хост и тот же порт.
На другой адрес эти заголовки не отправляются. Остальные заголовки сохраняются.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_SENSITIVE = frozenset({"authorization", "proxy-authorization", "cookie"})


def same_origin(left: str, right: str) -> bool:
    """Совпадают схема, хост и порт. Порт по умолчанию: 80 для http и 443 для https."""

    first, second = urlsplit(left), urlsplit(right)
    return (
        first.scheme.casefold() == second.scheme.casefold()
        and (first.hostname or "").casefold() == (second.hostname or "").casefold()
        and _port(first) == _port(second)
    )


def headers_for_url(headers: dict[str, str], start_url: str, url: str) -> dict[str, str]:
    """Копия заголовков для конкретного адреса запроса."""

    if same_origin(start_url, url):
        return dict(headers)
    return {name: value for name, value in headers.items() if name.lower() not in _SENSITIVE}


def _port(parts) -> int | None:
    if parts.port is not None:
        return parts.port
    if parts.scheme.casefold() == "https":
        return 443
    if parts.scheme.casefold() == "http":
        return 80
    return None
