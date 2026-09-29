"""GET через Playwright Chromium. Пакет и браузер необязательны.

Клиент выбирается в профиле полем client=playwright. Если requests или
curl_cffi получили ошибку, браузер сам не открывается.

Это настоящий браузер, поэтому страница успевает выполнить скрипты до
чтения HTML. Он не обходит CAPTCHA и защиту сайта. Профиль пользователя
с диска не читается: у задания свой пустой контекст, без сохранённых
cookies и без каталога Chrome.

Каждый адрес, включая перенаправления и вложенные запросы, проходит ту
же проверку, что и обычный HTTP. Закрытые адреса обрываются до отправки.
Сам Chromium имя разрешает отдельно: закрепление адреса из requests и
curl_cffi на браузер не переносится.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from parsedock.clients.addresses import ensure_target_allowed
from parsedock.clients.headers import headers_for_url
from parsedock.errors import Cancelled, HttpStatusError, NetworkError
from parsedock.jobs import FetchControl
from parsedock.profile import UrlSource
from parsedock.reliability import parse_retry_after

_INSTALL = (
    "Браузерный режим не запущен. Установите дополнительную зависимость "
    "parsedock[browser], затем выполните playwright install chromium. "
    "Клиент requests вместо браузера не подставляется. "
    "Браузер не обходит CAPTCHA и защиту сайта."
)


class BrowserSession:
    """Один браузер на задание. close закрывает страницы, браузер и драйвер."""

    def __init__(self, playwright, browser, context) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._context.close()
        finally:
            try:
                self._browser.close()
            finally:
                self._playwright.stop()


def open_browser_session() -> BrowserSession:
    """Запустить отдельный Chromium без данных пользователя."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise NetworkError(_INSTALL) from exc
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=True)
    except Exception as exc:
        playwright.stop()
        if _missing_browser(exc):
            raise NetworkError(_INSTALL) from exc
        raise NetworkError("Не удалось запустить браузер Chromium.", retryable=True) from exc
    context = browser.new_context()
    return BrowserSession(playwright, browser, context)


def fetch_with_browser(session: BrowserSession, source: UrlSource, control: FetchControl):
    """Открыть одну страницу и вернуть HTML после загрузки DOM."""

    from parsedock.clients.requests_client import FetchedPage

    if not isinstance(session, BrowserSession):
        raise NetworkError("Для client=playwright нужна браузерная сессия этого задания.")
    _raise_if_cancelled(control)
    target = _with_params(source.url, source.params)
    ensure_target_allowed(target, trust_private_networks=source.trust_private_networks)
    if source.headers:
        session._context.set_extra_http_headers(dict(source.headers))
    denied: list[BaseException] = []
    page = session._context.new_page()
    page.route("**/*", lambda route: _filter_request(route, source, control, denied))
    timeout_ms = max(1, int(source.timeout_seconds * 1000))
    try:
        response = page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
        _raise_if_cancelled(control)
        if denied:
            raise denied[0]
        if response is None:
            raise NetworkError(f"Браузер не получил ответ: {target}", retryable=True)
        hops = _redirect_count(response)
        if hops > source.max_redirects:
            raise NetworkError(f"Слишком много перенаправлений: {response.url}")
        ensure_target_allowed(response.url, trust_private_networks=source.trust_private_networks)
        status = int(response.status)
        if status < 200 or status >= 300:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            raise HttpStatusError(
                f"Сервер вернул статус {status}: {response.url}",
                status_code=status,
                url=response.url,
                retry_after=retry_after,
            )
        text = page.content()
        size = len(text.encode("utf-8"))
        if size > source.max_response_bytes:
            raise NetworkError(f"Ответ больше допустимого размера: {response.url}")
        headers = tuple((str(key), str(value)) for key, value in response.headers.items())
        return FetchedPage(url=response.url, status_code=status, text=text, headers=headers)
    except (NetworkError, HttpStatusError, Cancelled):
        raise
    except Exception as exc:
        if denied:
            raise denied[0] from exc
        if control.job is not None and control.job.cancel_requested:
            raise Cancelled("Задание отменено") from exc
        if _missing_browser(exc):
            raise NetworkError(_INSTALL) from exc
        if type(exc).__name__ == "TimeoutError":
            raise NetworkError(f"Превышено время ожидания: {target}", retryable=True) from exc
        raise NetworkError(f"Не удалось открыть страницу в браузере: {target}", retryable=True) from exc
    finally:
        page.close()


def _filter_request(route, source: UrlSource, control: FetchControl, denied: list[BaseException]) -> None:
    if control.job is not None and control.job.cancel_requested:
        denied.append(Cancelled("Задание отменено"))
        route.abort()
        return
    url = route.request.url
    scheme = urlsplit(url).scheme
    if scheme in {"about", "data", "blob"}:
        route.continue_()
        return
    if scheme not in {"http", "https"}:
        route.abort()
        return
    try:
        ensure_target_allowed(url, trust_private_networks=source.trust_private_networks)
    except NetworkError as exc:
        denied.append(exc)
        route.abort()
        return
    route.continue_(headers=headers_for_url(dict(route.request.headers), source.url, url))


def _redirect_count(response) -> int:
    hops = 0
    request = response.request.redirected_from
    while request is not None:
        hops += 1
        request = request.redirected_from
    return hops


def _with_params(url: str, params: tuple[tuple[str, str], ...]) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    pairs.extend(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def _raise_if_cancelled(control: FetchControl) -> None:
    if control.job is not None and control.job.cancel_requested:
        raise Cancelled("Задание отменено")


def _missing_browser(exc: BaseException) -> bool:
    text = str(exc)
    return "Executable doesn't exist" in text or "playwright install" in text
