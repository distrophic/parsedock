"""Сессия на время одного задания.

Какой класс открывать, решает поле source.client. После ошибки
другой клиент не подставляется.
"""

from __future__ import annotations

import requests

from parsedock.clients.curl_client import open_curl_session
from parsedock.errors import NetworkError
from parsedock.profile import UrlSource


def open_session(source: UrlSource):
    """Новая сессия выбранного клиента. Прокси из окружения выключены."""

    if source.client == "requests":
        session = requests.Session()
        session.trust_env = False
        return session
    if source.client == "curl_cffi":
        return open_curl_session(source)
    if source.client == "playwright":
        from parsedock.clients.browser_client import open_browser_session

        return open_browser_session()
    raise NetworkError(f"Клиент {source.client!r} не поддерживается")
