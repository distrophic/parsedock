"""Один GET-запрос через библиотеку requests.

Каждый одиночный вызов открывает свою сессию и закрывает её. Обход
страниц передаёт уже открытую сессию задания. Cookies между заданиями
не переносятся. Клиент берётся из профиля: requests или curl_cffi.
После ошибки другой клиент не подставляется.

Перенаправления обрабатываются по одному. Перед каждым новым адресом
проверка возвращает конкретные IP, и соединение открывается только на них.

Повтор того же GET выполняется только если профиль задал max_attempts
больше 1. Повторяются тайм-аут, обрыв соединения и выбранные коды HTTP.
Закрытый адрес, слишком большой ответ и ошибка извлечения не повторяются.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlsplit

import requests

from parsedock.clients.addresses import allowed_ips
from parsedock.clients.curl_client import map_curl_error, send_curl
from parsedock.clients.pin import PinnedAdapter
from parsedock.clients.sessions import open_session
from parsedock.clients.headers import headers_for_url
from parsedock.errors import Cancelled, HttpStatusError, NetworkError, ProfileError
from parsedock.jobs import FetchControl
from parsedock.profile import ReliabilitySpec, UrlSource
from parsedock.reliability import parse_retry_after, retry_delay

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_USER_AGENT = "ParseDock/0.1"


@dataclass(frozen=True)
class FetchedPage:
    """Ответ, который можно отдавать в разбор HTML."""

    url: str
    status_code: int
    text: str
    headers: tuple[tuple[str, str], ...]


def fetch_url(source: UrlSource, control: FetchControl | None = None) -> FetchedPage:
    """Загрузить страницу по правилам источника URL."""

    active = control or FetchControl(policy=ReliabilitySpec())
    session = open_session(source)
    if active.job is not None:
        active.job.attach_session(session)
    try:
        return _fetch_retrying(session, source, active)
    finally:
        if active.job is not None:
            active.job.attach_session(None)
        session.close()


def fetch_in_session(
    session: object,
    source: UrlSource,
    url: str,
    control: FetchControl | None = None,
) -> FetchedPage:
    """Загрузить конкретный адрес в уже открытой сессии задания.

    Параметры source.params не добавляются: адрес страницы обхода уже полный.
    """

    active = control or FetchControl(policy=ReliabilitySpec())
    return _fetch_retrying(session, replace(source, url=url, params=()), active)


def user_agent_for(source: UrlSource) -> str:
    """Имя, которым проверяется robots.txt.

    Если в профиле задан User-Agent, берётся он. Иначе ParseDock/0.1.
    При impersonate сам запрос может уйти с заголовком отпечатка, если
    свой User-Agent не указан. Для совпадения проверки и запроса имя
    нужно записать в заголовки профиля.
    """

    for name, value in source.headers:
        if name.lower() == "user-agent":
            return value
    return _USER_AGENT


def _fetch_retrying(
    session: object,
    source: UrlSource,
    control: FetchControl,
) -> FetchedPage:
    policy = control.policy
    attempts = policy.max_attempts if source.method == "GET" else 1
    for attempt in range(1, attempts + 1):
        _raise_if_cancelled(control)
        try:
            return _fetch_with_session(source, session, control)
        except Cancelled:
            raise
        except HttpStatusError as exc:
            if exc.status_code not in policy.retry_statuses or attempt >= attempts:
                raise
            if control.job is not None:
                control.job.note_retry()
            _wait(retry_delay(attempt, policy, control.rng, exc.retry_after), control)
        except NetworkError as exc:
            if not exc.retryable or attempt >= attempts:
                raise
            if control.job is not None:
                control.job.note_retry()
            _wait(retry_delay(attempt, policy, control.rng, None), control)
    raise NetworkError(f"Не удалось выполнить запрос: {source.url}")


def _fetch_with_session(
    source: UrlSource,
    session: object,
    control: FetchControl,
) -> FetchedPage:
    if source.client == "playwright":
        from parsedock.clients.browser_client import fetch_with_browser

        _pace(source.url, control)
        return fetch_with_browser(session, source, control)
    current = source.url
    seen: set[str] = set()
    redirects_left = source.max_redirects
    send_params = True
    while True:
        _raise_if_cancelled(control)
        pinned = allowed_ips(current, trust_private_networks=source.trust_private_networks)
        if current in seen:
            raise NetworkError(f"Цикл перенаправлений: {current}")
        seen.add(current)
        response = _send(
            session,
            source,
            current,
            send_params=send_params,
            control=control,
            pinned=pinned,
        )
        send_params = False
        try:
            status = response.status_code
            if status in _REDIRECTS:
                current = _next_url(current, response, redirects_left, source.max_redirects)
                redirects_left -= 1
                continue
            if status < 200 or status >= 300:
                raise HttpStatusError(
                    f"Сервер вернул статус {status}: {current}",
                    status_code=status,
                    url=current,
                    retry_after=parse_retry_after(response.headers.get("Retry-After")),
                )
            body = _read_limited(response, source.max_response_bytes, current, control)
            text = _decode(body, response.encoding, current)
            headers = tuple((str(key), str(value)) for key, value in response.headers.items())
            final_url = str(response.url or current)
            return FetchedPage(url=final_url, status_code=status, text=text, headers=headers)
        finally:
            response.close()


def _send(
    session: object,
    source: UrlSource,
    url: str,
    *,
    send_params: bool,
    control: FetchControl,
    pinned: tuple,
):
    _pace(url, control)
    _raise_if_cancelled(control)
    if source.client == "curl_cffi":
        try:
            return send_curl(session, source, url, send_params=send_params, pinned=pinned)
        except (NetworkError, ProfileError, Cancelled):
            raise
        except Exception as exc:
            if control.job is not None and control.job.cancel_requested:
                raise Cancelled("Задание отменено") from exc
            raise
    try:
        return _request_pinned(
            session,
            source.method,
            url,
            pinned,
            headers=_request_headers(source, url),
            params=dict(source.params) if send_params else None,
            timeout=(source.timeout_seconds, source.timeout_seconds),
            verify=True,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        if control.job is not None and control.job.cancel_requested:
            raise Cancelled("Задание отменено") from exc
        if isinstance(exc, requests.Timeout):
            raise NetworkError(f"Превышено время ожидания: {url}", retryable=True) from exc
        raise NetworkError(f"Не удалось выполнить запрос: {url}", retryable=True) from exc


def _request_pinned(session, method: str, url: str, pinned: tuple, **kwargs):
    adapter = PinnedAdapter(pinned)
    previous = {
        "https://": session.get_adapter("https://"),
        "http://": session.get_adapter("http://"),
    }
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    try:
        return session.request(method, url, **kwargs)
    finally:
        session.mount("https://", previous["https://"])
        session.mount("http://", previous["http://"])


def _pace(url: str, control: FetchControl) -> None:
    interval = control.policy.min_interval_seconds
    if interval <= 0:
        return
    host = urlsplit(url).hostname
    if not host:
        return
    key = host.casefold()
    previous = control.pace.get(key)
    now = control.clock()
    if previous is not None:
        wait = interval - (now - previous)
        if wait > 0:
            _wait(wait, control)
            now = control.clock()
    control.pace[key] = now


def _wait(seconds: float, control: FetchControl) -> None:
    _raise_if_cancelled(control)
    if seconds > 0:
        sleeper = control.sleeper if control.sleeper is not None else _fallback_sleep
        sleeper(seconds)
    _raise_if_cancelled(control)


def _raise_if_cancelled(control: FetchControl) -> None:
    if control.job is not None and control.job.cancel_requested:
        raise Cancelled("Задание отменено")


def _fallback_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _request_headers(source: UrlSource, url: str) -> dict[str, str]:
    headers = headers_for_url(dict(source.headers), source.url, url)
    if not any(name.lower() == "user-agent" for name in headers):
        headers["User-Agent"] = _USER_AGENT
    return headers


def _next_url(current: str, response: requests.Response, redirects_left: int, limit: int) -> str:
    if redirects_left <= 0:
        raise NetworkError(f"Превышен лимит перенаправлений ({limit}): {current}")
    location = response.headers.get("Location")
    if location is None or location == "":
        raise HttpStatusError(
            f"Сервер вернул статус {response.status_code} без адреса перенаправления: {current}",
            status_code=response.status_code,
            url=current,
        )
    if "\n" in location or "\r" in location:
        raise NetworkError(f"Заголовок Location содержит перевод строки: {current}")
    return urljoin(current, location.strip())


def _read_limited(response, limit: int, url: str, control: FetchControl) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise NetworkError(f"Ответ больше лимита {limit} байт: {url}")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in _body_chunks(response):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise NetworkError(f"Ответ больше лимита {limit} байт: {url}")
            chunks.append(chunk)
    except NetworkError:
        raise
    except Exception as exc:
        if control.job is not None and control.job.cancel_requested:
            raise Cancelled("Задание отменено") from exc
        mapped = map_curl_error(exc, url)
        if mapped is not None:
            raise mapped from exc
        raise
    return b"".join(chunks)


def _body_chunks(response):
    if type(response).__module__.startswith("curl_cffi"):
        return response.iter_content()
    return response.iter_content(chunk_size=65536)


def _decode(body: bytes, encoding: str | None, url: str) -> str:
    chosen = encoding or "utf-8"
    try:
        return body.decode(chosen)
    except (UnicodeDecodeError, LookupError) as exc:
        raise NetworkError(f"Ответ не удалось прочитать как текст ({chosen}): {url}") from exc
