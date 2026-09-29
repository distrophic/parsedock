"""Ошибки, которые программа сообщает вызывающему коду.

Это ожидаемые сбои профиля, извлечения, чтения файла и записи результата.
Их имеет смысл показать пользователю. Неожиданные ошибки программы
остаются обычными исключениями Python.
"""


class ParseDockError(Exception):
    """Базовая ошибка ParseDock."""


class ProfileError(ParseDockError):
    """Профиль JSON не соответствует схеме или его нельзя прочитать."""


class ExtractionError(ParseDockError):
    """Страницу нельзя разобрать с указанными правилами."""


class RunError(ParseDockError):
    """Локальную страницу не удалось прочитать."""


class NetworkError(ParseDockError):
    """Сбой сети: адрес, соединение, тайм-аут, размер ответа или перенаправление.

    retryable означает, что тот же GET можно повторить: соединение или
    тайм-аут. Закрытый адрес, слишком большой ответ и ошибка перенаправления
    повторно не запрашиваются.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class Cancelled(ParseDockError):
    """Задание остановлено до чтения следующей страницы."""


class HttpStatusError(ParseDockError):
    """Сервер ответил кодом, при котором страницу не разбираем.

    Код 200 сюда не относится: страница может открыться, а поля при этом
    не найтись. Такая ситуация остаётся результатом извлечения.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        url: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.retry_after = retry_after


class ExportError(ParseDockError):
    """Результат не удалось записать."""
