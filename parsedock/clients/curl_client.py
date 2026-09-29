"""GET через curl_cffi. Пакет необязательный.

Клиент выбирается в профиле. Если запрос через requests не удался,
этот модуль сам не запускается.

impersonate — имя отпечатка из установленной версии curl_cffi:
библиотека подстраивает TLS и заголовки под выбранный браузер.
Это не открытый браузер и не обход защиты сайта. Имя tor145 в списке
библиотеки тоже только отпечаток, а не выход в сеть Tor.
"""

from __future__ import annotations

from typing import get_args

from parsedock.clients.addresses import resolve_overrides
from parsedock.clients.headers import headers_for_url
from parsedock.errors import NetworkError, ProfileError
from parsedock.profile import UrlSource

_USER_AGENT = "ParseDock/0.1"


def impersonate_targets() -> frozenset[str] | None:
    """Имена impersonate у установленного curl_cffi. None — пакета нет."""

    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
    except ImportError:
        return None
    return frozenset(str(item) for item in get_args(BrowserTypeLiteral))


def open_curl_session(source: UrlSource):
    """Открыть сессию curl_cffi. Прокси из окружения не используются."""

    session_cls = _session_class()
    return session_cls(
        impersonate=source.impersonate,
        trust_env=False,
        verify=True,
        allow_redirects=False,
        retry=0,
        default_headers=source.impersonate is not None,
    )


def send_curl(session, source: UrlSource, url: str, *, send_params: bool, pinned: tuple):
    """Один GET без автоматических повторов библиотеки и без её перенаправлений.

    pinned — адреса, которые уже прошли проверку. curl соединяется с ними
    и не спрашивает имя заново.
    """

    from curl_cffi import CurlOpt

    previous = dict(session.curl_options)
    options = dict(previous)
    options[CurlOpt.RESOLVE] = resolve_overrides(url, pinned)
    session.curl_options = options
    try:
        return session.request(
            source.method,
            url,
            headers=_headers(source, url),
            params=dict(source.params) if send_params else None,
            timeout=(source.timeout_seconds, source.timeout_seconds),
            verify=True,
            allow_redirects=False,
            stream=True,
        )
    except Exception as exc:
        mapped = map_curl_error(exc, url)
        if mapped is not None:
            raise mapped from exc
        raise
    finally:
        session.curl_options = previous


def map_curl_error(exc: BaseException, url: str) -> NetworkError | ProfileError | None:
    """Перевести ошибку curl_cffi в ошибку ParseDock. Чужие исключения не трогает."""

    if not type(exc).__module__.startswith("curl_cffi"):
        return None
    if type(exc).__name__ == "ImpersonateError":
        return ProfileError(
            "profile.source.impersonate: это имя не поддерживается установленным curl_cffi. "
            "Отпечаток не заменяет браузер и не обходит защиту сайта."
        )
    if type(exc).__name__ in {"Timeout", "ConnectTimeout", "ReadTimeout"}:
        return NetworkError(f"Превышено время ожидания: {url}", retryable=True)
    return NetworkError(f"Не удалось выполнить запрос: {url}", retryable=True)


def _session_class():
    try:
        from curl_cffi.requests import Session
    except ImportError as exc:
        raise NetworkError(
            "В профиле выбран клиент curl_cffi, но пакет не установлен. "
            "Установите дополнительную зависимость parsedock[curl]. "
            "Клиент requests вместо него не подставляется."
        ) from exc
    return Session


def _headers(source: UrlSource, url: str) -> dict[str, str]:
    headers = headers_for_url(dict(source.headers), source.url, url)
    if source.impersonate is None and not any(name.lower() == "user-agent" for name in headers):
        headers["User-Agent"] = _USER_AGENT
    return headers
