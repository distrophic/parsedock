"""Загрузка и проверка профиля задания.

Профиль — обычный JSON. В нём нет исполняемого кода: программа читает
только известные поля и заранее заданные значения.

Схема версии 1 принимает локальный HTML-файл, HTTP-адрес и обход
страниц, а также экспорт JSONL или CSV. Старые профили остаются
действительными: новые поля можно не указывать. Преобразования —
только именованные операции, без произвольного кода. Повторы запросов
выключены, пока профиль явно не задаст reliability.max_attempts больше 1.

Пути внутри профиля считаются относительно каталога самого профиля.
Абсолютный путь используется как есть. Профиль на этом этапе доверенный:
его пишет сам пользователь, отдельной песочницы для путей нет.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from parsedock.errors import ProfileError

SCHEMA_VERSION = 1
RESERVED_FIELD_NAMES = frozenset({"source_url", "fetched_at"})
EXPORT_MODES = frozenset({"create", "replace", "append"})
FIELD_KINDS = frozenset({"text", "attribute", "html"})

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_DURATION_SECONDS = 300.0

_PROFILE_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "source",
        "extraction",
        "deduplicate",
        "crawl",
        "reliability",
        "export",
        "resume",
    }
)
_RESUME_KEYS = frozenset({"path"})
_RELIABILITY_KEYS = frozenset(
    {
        "max_attempts",
        "backoff_seconds",
        "backoff_multiplier",
        "max_backoff_seconds",
        "jitter",
        "retry_statuses",
        "min_interval_seconds",
        "max_retry_after_seconds",
    }
)
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)
_SOURCE_FILE_KEYS = frozenset({"type", "path"})
_SOURCE_URL_KEYS = frozenset(
    {
        "type",
        "url",
        "method",
        "timeout_seconds",
        "max_response_bytes",
        "max_redirects",
        "trust_private_networks",
        "headers",
        "params",
        "client",
        "impersonate",
    }
)
_EXTRACTION_KEYS = frozenset({"format", "container", "fields"})
_FIELD_KEYS = frozenset(
    {"name", "selector", "kind", "attribute", "multiple", "required", "transforms", "keep_original"}
)
_JSONL_EXPORT_KEYS = frozenset({"format", "path", "mode", "report_path", "report_mode"})
_CSV_EXPORT_KEYS = frozenset(
    {
        "format",
        "path",
        "mode",
        "encoding",
        "delimiter",
        "spreadsheet_safe",
        "bom",
        "report_path",
        "report_mode",
    }
)
_DEDUPLICATE_KEYS = frozenset({"keys"})
_TRANSFORM_OPTIONS = {
    "normalize_whitespace": frozenset({"op"}),
    "absolute_url": frozenset({"op", "base_url"}),
    "parse_number": frozenset({"op", "decimal_separator", "thousands_separator"}),
    "parse_currency": frozenset({"op", "decimal_separator", "thousands_separator", "symbol", "code"}),
    "parse_date": frozenset({"op", "format", "timezone"}),
}


@dataclass(frozen=True)
class Transform:
    """Именованная операция над значением поля.

    В профиле нет произвольного кода. Неизвестная операция и лишний
    параметр отклоняются при чтении профиля.
    """

    op: str
    decimal_separator: str | None = None
    thousands_separator: str | None = None
    symbol: str | None = None
    code: str | None = None
    date_format: str | None = None
    timezone_offset: timezone | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class FieldSpec:
    """Одно поле внутри карточки.

    kind:
    - text — текст элемента;
    - attribute — значение атрибута, имя задаётся в attribute;
    - html — внутренняя HTML-разметка элемента.

    multiple=false ждёт ноль или одно совпадение. Два совпадения — ошибка
    карточки, первое значение само не выбирается.
    multiple=true собирает список. Пустой список означает, что совпадений нет.
    Если элемент найден, но атрибута нет, необязательное поле получает null:
    неполный список не записывается.

    required=false и ноль совпадений при multiple=false дают значение null.
    required=true и ноль совпадений отправляют карточку в ошибки.
    """

    name: str
    selector: str
    kind: str
    attribute: str | None = None
    multiple: bool = False
    required: bool = False
    transforms: tuple[Transform, ...] = ()
    keep_original: bool = False


@dataclass(frozen=True)
class ExtractionSpec:
    """Правило извлечения.

    format html ищет контейнер CSS-селектором. format xml ищет записи
    путём из имён элементов, например catalog/item. Поле selector при xml
    тоже путь, а не CSS.
    """

    container: str
    fields: tuple[FieldSpec, ...]
    format: str = "html"


@dataclass(frozen=True)
class FileSource:
    """Локальный HTML-файл. Путь уже раскрыт относительно каталога профиля."""

    path: Path


@dataclass(frozen=True)
class UrlSource:
    """Один HTTP GET.

    Проверка TLS не отключается. По умолчанию закрыты localhost, частные
    сети и link-local адреса, включая 169.254.169.254. Параметр
    trust_private_networks открывает localhost и сети 10/8, 172.16/12,
    192.168/16, а также IPv6 ULA. Link-local он не открывает.

    Тайм-аут — это и соединение, и чтение ответа. По умолчанию 20 секунд.
    Ответ больше max_response_bytes не сохраняется. По умолчанию это
    2 000 000 байт уже после распаковки. max_redirects по умолчанию 5.

    client — requests, curl_cffi или playwright. Дополнительные пакеты
    не подставляются сами, если выбранный клиент получил ошибку.
    impersonate задаёт отпечаток curl_cffi. Это не запуск браузера и не
    обход защиты. playwright открывает отдельный Chromium только когда
    клиент указан явно. Ошибка HTTP браузер не включает.
    """

    url: str
    method: str = "GET"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    trust_private_networks: bool = False
    headers: tuple[tuple[str, str], ...] = ()
    params: tuple[tuple[str, str], ...] = ()
    client: str = "requests"
    impersonate: str | None = None


@dataclass(frozen=True)
class ExportSettings:
    """Куда и в каком виде записать результат."""

    path: Path
    mode: str
    format: str = "jsonl"
    encoding: str = "utf-8"
    delimiter: str = ","
    spreadsheet_safe: bool = False
    bom: bool = False
    report_path: Path | None = None
    report_mode: str = "create"


@dataclass(frozen=True)
class NextLink:
    """Ссылка на следующую страницу. Ищется по всей странице, не внутри карточки."""

    selector: str
    attribute: str = "href"


@dataclass(frozen=True)
class PageParam:
    """Номер страницы в query-параметре. Остальные параметры адреса сохраняются."""

    name: str
    start: int = 1
    step: int = 1


@dataclass(frozen=True)
class CrawlSpec:
    """Правила обхода нескольких страниц.

    Без этого блока профиль читает одну страницу, как раньше.
    robots.txt не даёт юридического разрешения на сбор. Это только
    опубликованные сайтом правила для автоматических клиентов.
    Если файл недоступен, действует robots_on_unavailable: error, allow или deny.
    """

    mode: str
    urls: tuple[str, ...] = ()
    next_link: NextLink | None = None
    page_param: PageParam | None = None
    max_pages: int = DEFAULT_MAX_PAGES
    max_depth: int = DEFAULT_MAX_DEPTH
    allowed_domains: tuple[str, ...] = ()
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS
    stop_when_no_new_records: bool = True
    obey_robots: bool = True
    robots_on_unavailable: str = "error"


@dataclass(frozen=True)
class ReliabilitySpec:
    """Повторы GET и минимальная пауза между запросами к одному хосту.

    Запросы одного задания идут по одному. max_attempts по умолчанию 1:
    повтор начинается, только если профиль его включил. Повторяется только
    GET, поэтому повтор не создаёт второе действие на сайте.

    backoff — растущая пауза после неудачи. jitter — случайный разброс
    этой паузы. Retry-After — заголовок, в котором сайт сам говорит,
    сколько секунд подождать. Дольше max_retry_after_seconds программа
    не ждёт. min_interval_seconds — пауза между запросами к одному хосту,
    даже когда ошибок нет.
    """

    max_attempts: int = 1
    backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    jitter: bool = True
    retry_statuses: tuple[int, ...] = DEFAULT_RETRY_STATUSES
    min_interval_seconds: float = 0.0
    max_retry_after_seconds: float = 30.0


@dataclass(frozen=True)
class ResumeSpec:
    """Файл контрольной точки для продолжения прерванного обхода.

    Без этого блока задание каждый раз начинается сначала.
    Контрольная точка хранит уже открытые адреса и очередь оставшихся.
    Это не обещание, что каждая карточка будет записана ровно один раз
    при любом сбое питания: запись страницы и обновление точки идут
    друг за другом, не одной операцией файловой системы.
    """

    path: Path


@dataclass(frozen=True)
class Profile:
    """Проверенный профиль с уже раскрытыми путями."""

    schema_version: int
    name: str
    source: FileSource | UrlSource
    export_path: Path
    export_mode: str
    extraction: ExtractionSpec
    export_format: str = "jsonl"
    encoding: str = "utf-8"
    delimiter: str = ","
    spreadsheet_safe: bool = False
    bom: bool = False
    report_path: Path | None = None
    report_mode: str = "create"
    deduplicate_keys: tuple[str, ...] = ()
    crawl: CrawlSpec | None = None
    reliability: ReliabilitySpec = ReliabilitySpec()
    resume: ResumeSpec | None = None

    @property
    def source_path(self) -> Path:
        """Путь локального файла. У URL-профиля такого пути нет."""

        if isinstance(self.source, FileSource):
            return self.source.path
        raise ProfileError("У профиля с адресом URL нет локального файла")

    @property
    def source_url(self) -> str:
        """Адрес, записанный в профиле. Для файла это file-URI абсолютного пути."""

        if isinstance(self.source, FileSource):
            return self.source.path.as_uri()
        return self.source.url


def load_profile(path: Path | str) -> Profile:
    """Прочитать профиль с диска и проверить его структуру."""

    profile_path = Path(path)
    try:
        raw = profile_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProfileError(f"Профиль не найден: {profile_path}") from exc
    except OSError as exc:
        raise ProfileError(f"Не удалось прочитать профиль: {profile_path}") from exc
    except UnicodeDecodeError as exc:
        raise ProfileError(f"Профиль не в кодировке UTF-8: {profile_path}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"Профиль содержит неверный JSON: {exc.msg}") from exc

    return parse_profile(data, base_dir=profile_path.parent)


def parse_profile(data: object, *, base_dir: Path | str) -> Profile:
    """Проверить уже загруженный объект JSON.

    base_dir — каталог, от которого считаются относительные пути.
    Обычно это каталог файла профиля.
    """

    body = _object(data, "profile")
    _reject_unknown(body, _PROFILE_KEYS, "profile")

    version = _required(body, "schema_version", "profile")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ProfileError("profile.schema_version: ожидалось число 1")

    name = _non_empty_string(_required(body, "name", "profile"), "profile.name")
    source = _parse_source(_required(body, "source", "profile"), Path(base_dir))
    extraction = _parse_extraction(_required(body, "extraction", "profile"))
    deduplicate_keys = _parse_deduplicate(body.get("deduplicate", None), extraction)
    crawl = _parse_crawl(body.get("crawl", None), source)
    reliability = _parse_reliability(body.get("reliability", None))
    export = _parse_export(_required(body, "export", "profile"), Path(base_dir))
    resume = _parse_resume(body.get("resume", None), source, crawl, Path(base_dir))

    return Profile(
        schema_version=version,
        name=name,
        source=source,
        export_path=export.path,
        export_mode=export.mode,
        extraction=extraction,
        export_format=export.format,
        encoding=export.encoding,
        delimiter=export.delimiter,
        spreadsheet_safe=export.spreadsheet_safe,
        bom=export.bom,
        report_path=export.report_path,
        report_mode=export.report_mode,
        deduplicate_keys=deduplicate_keys,
        crawl=crawl,
        reliability=reliability,
        resume=resume,
    )


def _parse_source(value: object, base_dir: Path) -> FileSource | UrlSource:
    body = _object(value, "profile.source")
    source_type = _non_empty_string(_required(body, "type", "profile.source"), "profile.source.type")
    if source_type == "file":
        _reject_unknown(body, _SOURCE_FILE_KEYS, "profile.source")
        raw_path = _non_empty_string(_required(body, "path", "profile.source"), "profile.source.path")
        return FileSource(_resolve_path(raw_path, base_dir))
    if source_type == "url":
        _reject_unknown(body, _SOURCE_URL_KEYS, "profile.source")
        return _parse_url_source(body)
    raise ProfileError(
        "profile.source.type: доступны file и url. "
        f"Получено {source_type!r}."
    )


def _parse_url_source(body: dict) -> UrlSource:
    url = _http_url(_required(body, "url", "profile.source"), "profile.source.url")
    if "method" in body:
        method = _non_empty_string(body["method"], "profile.source.method").upper()
        if method != "GET":
            raise ProfileError("profile.source.method: на этом этапе доступен только GET")
    else:
        method = "GET"
    timeout = _positive_number(
        body.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        "profile.source.timeout_seconds",
    )
    max_bytes = _positive_int(
        body.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES),
        "profile.source.max_response_bytes",
    )
    max_redirects = _non_negative_int(
        body.get("max_redirects", DEFAULT_MAX_REDIRECTS),
        "profile.source.max_redirects",
    )
    trust_private = _bool_option(body, "trust_private_networks", False, "profile.source")
    headers = _string_pairs(body.get("headers", {}), "profile.source.headers")
    params = _string_pairs(body.get("params", {}), "profile.source.params")
    client = _client_name(body)
    impersonate = _impersonate_name(body, client)
    return UrlSource(
        url=url,
        method=method,
        timeout_seconds=timeout,
        max_response_bytes=max_bytes,
        max_redirects=max_redirects,
        trust_private_networks=trust_private,
        headers=headers,
        params=params,
        client=client,
        impersonate=impersonate,
    )


def _client_name(body: dict) -> str:
    if "client" not in body:
        return "requests"
    client = _non_empty_string(body["client"], "profile.source.client")
    if client not in {"requests", "curl_cffi", "playwright"}:
        raise ProfileError("profile.source.client: доступны requests, curl_cffi и playwright")
    return client


def _impersonate_name(body: dict, client: str) -> str | None:
    if "impersonate" not in body:
        return None
    value = _non_empty_string(body["impersonate"], "profile.source.impersonate")
    if client != "curl_cffi":
        raise ProfileError(
            "profile.source.impersonate: параметр доступен только при client=curl_cffi. "
            "Это отпечаток клиента, а не запуск браузера и не обход защиты сайта."
        )
    from parsedock.clients.curl_client import impersonate_targets

    targets = impersonate_targets()
    if targets is not None and value not in targets:
        raise ProfileError(
            f"profile.source.impersonate: {value!r} не поддерживается установленным curl_cffi. "
            "Короткие имена этой возможности: chrome, edge, safari, safari_ios, firefox, chrome_android. "
            "Отпечаток не заменяет браузер и не обходит защиту сайта."
        )
    return value


def _http_url(value: object, path: str) -> str:
    raw = _non_empty_string(value, path)
    if any(char in raw for char in "\r\n\t "):
        raise ProfileError(f"{path}: в адресе не должно быть пробелов и переводов строки")
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ProfileError(f"{path}: некорректный адрес") from exc
    if parts.scheme not in {"http", "https"}:
        raise ProfileError(f"{path}: ожидался адрес http или https")
    if parts.username is not None or parts.password is not None:
        raise ProfileError(f"{path}: адрес не должен содержать логин или пароль")
    if not hostname:
        raise ProfileError(f"{path}: в адресе нет хоста")
    if port == 0:
        raise ProfileError(f"{path}: некорректный порт")
    return raw


def _string_pairs(value: object, path: str) -> tuple[tuple[str, str], ...]:
    body = _object(value, path)
    pairs: list[tuple[str, str]] = []
    for key, item in body.items():
        if _has_line_break(key) or key == "":
            raise ProfileError(f"{path}: имя параметра не должно быть пустым или содержать перевод строки")
        if not isinstance(item, str) or _has_line_break(item):
            raise ProfileError(f"{path}.{key}: ожидалась строка без перевода строки")
        pairs.append((key, item))
    return tuple(pairs)


def _has_line_break(value: str) -> bool:
    return "\n" in value or "\r" in value


def _positive_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProfileError(f"{path}: ожидалось положительное число")
    return float(value)


def _positive_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfileError(f"{path}: ожидалось целое число больше нуля")
    return value


def _non_negative_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfileError(f"{path}: ожидалось целое число от нуля")
    return value


def _parse_extraction(value: object) -> ExtractionSpec:
    body = _object(value, "profile.extraction")
    _reject_unknown(body, _EXTRACTION_KEYS, "profile.extraction")
    data_format = _non_empty_string(
        _required(body, "format", "profile.extraction"),
        "profile.extraction.format",
    )
    if data_format not in {"html", "xml"}:
        raise ProfileError(
            "profile.extraction.format: доступны html и xml. "
            f"Получено {data_format!r}."
        )
    container = _non_empty_string(
        _required(body, "container", "profile.extraction"),
        "profile.extraction.container",
    )
    if data_format == "xml":
        _require_element_path(container, "profile.extraction.container", allow_self=False)
    raw_fields = _required(body, "fields", "profile.extraction")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ProfileError("profile.extraction.fields: ожидался непустой список полей")

    fields: list[FieldSpec] = []
    seen: set[str] = set()
    for index, raw_field in enumerate(raw_fields):
        field = _parse_field(raw_field, f"profile.extraction.fields[{index}]", data_format)
        if field.name in seen:
            raise ProfileError(f"profile.extraction.fields[{index}].name: имя {field.name!r} повторяется")
        seen.add(field.name)
        if field.keep_original:
            original_name = f"{field.name}_original"
            if original_name in seen or original_name in RESERVED_FIELD_NAMES:
                raise ProfileError(
                    f"profile.extraction.fields[{index}].keep_original: имя {original_name} уже занято"
                )
            seen.add(original_name)
        fields.append(field)
    return ExtractionSpec(container=container, fields=tuple(fields), format=data_format)


def _require_element_path(value: str, path: str, *, allow_self: bool) -> None:
    from parsedock.extract.xml import element_path

    try:
        element_path(value, allow_self=allow_self)
    except ValueError as exc:
        raise ProfileError(f"{path}: {exc}") from exc


def _parse_field(value: object, path: str, data_format: str = "html") -> FieldSpec:
    body = _object(value, path)
    _reject_unknown(body, _FIELD_KEYS, path)
    name = _field_name(_non_empty_string(_required(body, "name", path), f"{path}.name"), f"{path}.name")
    selector = _non_empty_string(_required(body, "selector", path), f"{path}.selector")
    kind = _non_empty_string(_required(body, "kind", path), f"{path}.kind")
    if data_format == "xml":
        if kind not in {"text", "attribute", "xml"}:
            raise ProfileError(
                f"{path}.kind: для XML доступны text, attribute и xml. Получено {kind!r}."
            )
        _require_element_path(selector, f"{path}.selector", allow_self=True)
    elif kind not in FIELD_KINDS:
        raise ProfileError(f"{path}.kind: ожидалось text, attribute или html. Получено {kind!r}.")

    if "attribute" in body:
        attribute = _non_empty_string(body["attribute"], f"{path}.attribute")
    else:
        attribute = None
    if kind == "attribute" and attribute is None:
        raise ProfileError(f"{path}.attribute: параметр обязателен при kind=attribute")
    if kind != "attribute" and attribute is not None:
        raise ProfileError(f"{path}.attribute: параметр допустим только при kind=attribute")

    multiple = _bool_option(body, "multiple", False, path)
    required = _bool_option(body, "required", False, path)
    keep_original = _bool_option(body, "keep_original", False, path)
    transforms = _parse_transforms(body.get("transforms", []), path)
    return FieldSpec(
        name=name,
        selector=selector,
        kind=kind,
        attribute=attribute,
        multiple=multiple,
        required=required,
        transforms=transforms,
        keep_original=keep_original,
    )


def _parse_transforms(value: object, path: str) -> tuple[Transform, ...]:
    if not isinstance(value, list):
        raise ProfileError(f"{path}.transforms: ожидался список операций")
    return tuple(_parse_transform(item, f"{path}.transforms[{index}]") for index, item in enumerate(value))


def _parse_transform(value: object, path: str) -> Transform:
    body = _object(value, path)
    op = _non_empty_string(_required(body, "op", path), f"{path}.op")
    allowed = _TRANSFORM_OPTIONS.get(op)
    if allowed is None:
        raise ProfileError(
            f"{path}.op: неизвестная операция {op!r}. "
            "Доступны normalize_whitespace, absolute_url, parse_number, parse_currency и parse_date."
        )
    _reject_unknown(body, allowed, path)
    if op == "normalize_whitespace":
        return Transform(op=op)
    if op == "absolute_url":
        base_url = body.get("base_url")
        if base_url is not None:
            base_url = _absolute_base_url(base_url, f"{path}.base_url")
        return Transform(op=op, base_url=base_url)
    if op == "parse_number":
        decimal_separator, thousands_separator = _number_separators(body, path)
        return Transform(
            op=op,
            decimal_separator=decimal_separator,
            thousands_separator=thousands_separator,
        )
    if op == "parse_currency":
        decimal_separator, thousands_separator = _number_separators(body, path)
        symbol = body.get("symbol")
        code = body.get("code")
        if (symbol is None) == (code is None):
            raise ProfileError(f"{path}: укажите ровно одно из полей symbol и code")
        if symbol is not None:
            symbol = _non_empty_string(symbol, f"{path}.symbol")
        if code is not None:
            code = _non_empty_string(code, f"{path}.code")
        return Transform(
            op=op,
            decimal_separator=decimal_separator,
            thousands_separator=thousands_separator,
            symbol=symbol,
            code=code,
        )
    date_format = _date_format(_required(body, "format", path), f"{path}.format")
    offset = _timezone_offset(body.get("timezone"), date_format, path)
    return Transform(op=op, date_format=date_format, timezone_offset=offset)


def _number_separators(body: dict, path: str) -> tuple[str, str | None]:
    decimal_separator = _one_character(
        _required(body, "decimal_separator", path),
        f"{path}.decimal_separator",
    )
    if "thousands_separator" in body:
        thousands_separator = _one_character(body["thousands_separator"], f"{path}.thousands_separator")
    else:
        thousands_separator = None
    if thousands_separator is not None and thousands_separator == decimal_separator:
        raise ProfileError(f"{path}: разделители тысяч и дроби должны различаться")
    return decimal_separator, thousands_separator


def _one_character(value: object, path: str) -> str:
    if not isinstance(value, str) or len(value) != 1:
        raise ProfileError(f"{path}: ожидался один символ")
    return value


def _absolute_base_url(value: object, path: str) -> str:
    raw = _non_empty_string(value, path)
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https", "file"} or not (parts.netloc or parts.path):
        raise ProfileError(f"{path}: ожидался абсолютный адрес http, https или file")
    return raw


def _date_format(value: object, path: str) -> str:
    fmt = _non_empty_string(value, path)
    if re.search(r"%(?![aAbBcdfGHIjmMpSUuVwWxXyYzZ%])", fmt):
        raise ProfileError(f"{path}: неизвестный код формата даты")
    if not re.search(r"%[dmbByYj]", fmt):
        raise ProfileError(f"{path}: формат должен содержать дату, например %d.%m.%Y")
    return fmt


def _timezone_offset(value: object, date_format: str, path: str) -> timezone | None:
    has_time = re.search(r"%[HIMpSXf]", date_format) is not None
    has_zone_in_format = "%z" in date_format or "%Z" in date_format
    if value is None:
        return None
    if not has_time:
        raise ProfileError(f"{path}.timezone: смещение допустимо, когда format содержит время")
    if has_zone_in_format:
        raise ProfileError(f"{path}.timezone: смещение уже задано кодом %z или %Z в format")
    if not isinstance(value, str) or re.fullmatch(r"Z|[+-](?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ProfileError(f"{path}.timezone: ожидалось смещение +ЧЧ:ММ, -ЧЧ:ММ или Z")
    if value == "Z":
        return timezone.utc
    sign = 1 if value[0] == "+" else -1
    hours = int(value[1:3])
    minutes = int(value[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _parse_deduplicate(value: object, extraction: ExtractionSpec) -> tuple[str, ...]:
    if value is None:
        return ()
    body = _object(value, "profile.deduplicate")
    _reject_unknown(body, _DEDUPLICATE_KEYS, "profile.deduplicate")
    raw_keys = _required(body, "keys", "profile.deduplicate")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ProfileError("profile.deduplicate.keys: ожидался непустой список полей")
    known = {field.name for field in extraction.fields}
    known.update(f"{field.name}_original" for field in extraction.fields if field.keep_original)
    keys: list[str] = []
    for index, key in enumerate(raw_keys):
        name = _field_name(key if isinstance(key, str) else "", f"profile.deduplicate.keys[{index}]")
        if name not in known:
            raise ProfileError(f"profile.deduplicate.keys[{index}]: поля {name!r} нет в профиле")
        keys.append(name)
    return tuple(keys)


def _parse_reliability(value: object) -> ReliabilitySpec:
    if value is None:
        return ReliabilitySpec()
    body = _object(value, "profile.reliability")
    _reject_unknown(body, _RELIABILITY_KEYS, "profile.reliability")
    attempts = _positive_int(body.get("max_attempts", 1), "profile.reliability.max_attempts")
    if attempts > 5:
        raise ProfileError(
            "profile.reliability.max_attempts: не больше 5. Бесконечные повторы не поддерживаются."
        )
    backoff = _positive_number(body.get("backoff_seconds", 0.5), "profile.reliability.backoff_seconds")
    if backoff > 30:
        raise ProfileError("profile.reliability.backoff_seconds: не больше 30 секунд")
    multiplier = _bounded_number(
        body.get("backoff_multiplier", 2),
        "profile.reliability.backoff_multiplier",
        low=1,
        high=5,
    )
    max_backoff = _positive_number(
        body.get("max_backoff_seconds", 30),
        "profile.reliability.max_backoff_seconds",
    )
    if max_backoff > 120:
        raise ProfileError("profile.reliability.max_backoff_seconds: не больше 120 секунд")
    if max_backoff < backoff:
        raise ProfileError("profile.reliability.max_backoff_seconds: не короче backoff_seconds")
    interval = _non_negative_number(
        body.get("min_interval_seconds", 0),
        "profile.reliability.min_interval_seconds",
    )
    if interval > 60:
        raise ProfileError("profile.reliability.min_interval_seconds: не больше 60 секунд")
    retry_after_cap = _positive_number(
        body.get("max_retry_after_seconds", 30),
        "profile.reliability.max_retry_after_seconds",
    )
    if retry_after_cap > 120:
        raise ProfileError("profile.reliability.max_retry_after_seconds: не больше 120 секунд")
    return ReliabilitySpec(
        max_attempts=attempts,
        backoff_seconds=backoff,
        backoff_multiplier=multiplier,
        max_backoff_seconds=max_backoff,
        jitter=_bool_option(body, "jitter", True, "profile.reliability"),
        retry_statuses=_retry_statuses(body),
        min_interval_seconds=interval,
        max_retry_after_seconds=retry_after_cap,
    )


def _retry_statuses(body: dict) -> tuple[int, ...]:
    if "retry_statuses" not in body:
        return DEFAULT_RETRY_STATUSES
    raw = body["retry_statuses"]
    if not isinstance(raw, list):
        raise ProfileError("profile.reliability.retry_statuses: ожидался список кодов HTTP")
    statuses: list[int] = []
    for index, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, int) or item not in _RETRYABLE_STATUSES:
            raise ProfileError(
                f"profile.reliability.retry_statuses[{index}]: код {item!r} не повторяется. "
                "Повтор доступен только для 408, 429, 500, 502, 503 и 504."
            )
        statuses.append(item)
    return tuple(dict.fromkeys(statuses))


def _bounded_number(value: object, path: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
        raise ProfileError(f"{path}: ожидалось число от {low:g} до {high:g}")
    return float(value)


def _non_negative_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ProfileError(f"{path}: ожидалось число от нуля")
    return float(value)


def _parse_crawl(value: object, source: FileSource | UrlSource) -> CrawlSpec | None:
    if value is None:
        return None
    if isinstance(source, FileSource):
        raise ProfileError("profile.crawl: обход доступен для источника type=url")
    body = _object(value, "profile.crawl")
    _reject_unknown(
        body,
        frozenset(
            {
                "mode",
                "urls",
                "next_link",
                "page_param",
                "max_pages",
                "max_depth",
                "allowed_domains",
                "max_duration_seconds",
                "stop_when_no_new_records",
                "obey_robots",
                "robots_on_unavailable",
            }
        ),
        "profile.crawl",
    )
    mode = _non_empty_string(_required(body, "mode", "profile.crawl"), "profile.crawl.mode")
    if mode not in {"urls", "next_link", "page_param"}:
        raise ProfileError("profile.crawl.mode: доступны urls, next_link и page_param")
    if source.params:
        raise ProfileError("profile.source.params: при обходе укажите параметры прямо в адресе")
    urls = _crawl_urls(body, mode)
    next_link = _parse_next_link(body, mode)
    page_param = _parse_page_param(body, mode)
    max_pages = _positive_int(body.get("max_pages", DEFAULT_MAX_PAGES), "profile.crawl.max_pages")
    max_depth = _positive_int(body.get("max_depth", DEFAULT_MAX_DEPTH), "profile.crawl.max_depth")
    max_duration = _positive_number(
        body.get("max_duration_seconds", DEFAULT_MAX_DURATION_SECONDS),
        "profile.crawl.max_duration_seconds",
    )
    allowed = _allowed_domains(body.get("allowed_domains"), source.url, urls)
    stop_when_no_new = _bool_option(body, "stop_when_no_new_records", True, "profile.crawl")
    obey_robots = _bool_option(body, "obey_robots", True, "profile.crawl")
    if "robots_on_unavailable" in body:
        unavailable = _non_empty_string(body["robots_on_unavailable"], "profile.crawl.robots_on_unavailable")
        if unavailable not in {"error", "allow", "deny"}:
            raise ProfileError("profile.crawl.robots_on_unavailable: доступны error, allow и deny")
    else:
        unavailable = "error"
    return CrawlSpec(
        mode=mode,
        urls=urls,
        next_link=next_link,
        page_param=page_param,
        max_pages=max_pages,
        max_depth=max_depth,
        allowed_domains=allowed,
        max_duration_seconds=max_duration,
        stop_when_no_new_records=stop_when_no_new,
        obey_robots=obey_robots,
        robots_on_unavailable=unavailable,
    )


def _crawl_urls(body: dict, mode: str) -> tuple[str, ...]:
    if mode != "urls":
        if "urls" in body:
            raise ProfileError("profile.crawl.urls: параметр допустим только при mode=urls")
        return ()
    raw = _required(body, "urls", "profile.crawl")
    if not isinstance(raw, list) or not raw:
        raise ProfileError("profile.crawl.urls: ожидался непустой список адресов")
    return tuple(_http_url(item, f"profile.crawl.urls[{index}]") for index, item in enumerate(raw))


def _parse_next_link(body: dict, mode: str) -> NextLink | None:
    if mode != "next_link":
        if "next_link" in body:
            raise ProfileError("profile.crawl.next_link: параметр допустим только при mode=next_link")
        return None
    raw = _object(_required(body, "next_link", "profile.crawl"), "profile.crawl.next_link")
    _reject_unknown(raw, frozenset({"selector", "attribute"}), "profile.crawl.next_link")
    selector = _non_empty_string(_required(raw, "selector", "profile.crawl.next_link"), "profile.crawl.next_link.selector")
    attribute = "href"
    if "attribute" in raw:
        attribute = _non_empty_string(raw["attribute"], "profile.crawl.next_link.attribute")
    return NextLink(selector=selector, attribute=attribute)


def _parse_page_param(body: dict, mode: str) -> PageParam | None:
    if mode != "page_param":
        if "page_param" in body:
            raise ProfileError("profile.crawl.page_param: параметр допустим только при mode=page_param")
        return None
    raw = _object(_required(body, "page_param", "profile.crawl"), "profile.crawl.page_param")
    _reject_unknown(raw, frozenset({"name", "start", "step"}), "profile.crawl.page_param")
    name = _non_empty_string(_required(raw, "name", "profile.crawl.page_param"), "profile.crawl.page_param.name")
    start = raw.get("start", 1)
    step = raw.get("step", 1)
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ProfileError("profile.crawl.page_param.start: ожидалось целое число от нуля")
    if isinstance(step, bool) or not isinstance(step, int) or step < 1:
        raise ProfileError("profile.crawl.page_param.step: ожидалось целое число от 1")
    return PageParam(name=name, start=start, step=step)


def _allowed_domains(value: object, start_url: str, urls: tuple[str, ...]) -> tuple[str, ...]:
    start_host = _url_host(start_url, "profile.source.url")
    if value is None:
        allowed = (start_host,)
    else:
        if not isinstance(value, list) or not value:
            raise ProfileError("profile.crawl.allowed_domains: ожидался непустой список доменов")
        domains: list[str] = []
        for index, item in enumerate(value):
            host = _non_empty_string(item, f"profile.crawl.allowed_domains[{index}]").casefold()
            if "/" in host or ":" in host or " " in host:
                raise ProfileError(f"profile.crawl.allowed_domains[{index}]: укажите только имя хоста")
            domains.append(host)
        allowed = tuple(dict.fromkeys(domains))
        if start_host not in allowed:
            raise ProfileError("profile.crawl.allowed_domains: стартовый адрес должен входить в список")
    for index, url in enumerate(urls):
        host = _url_host(url, f"profile.crawl.urls[{index}]")
        if host not in allowed:
            raise ProfileError(f"profile.crawl.urls[{index}]: хост {host} не входит в разрешённые домены")
    return allowed


def _url_host(url: str, path: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise ProfileError(f"{path}: в адресе нет хоста")
    return host.casefold()


def _parse_resume(
    value: object,
    source: FileSource | UrlSource,
    crawl: CrawlSpec | None,
    base_dir: Path,
) -> ResumeSpec | None:
    if value is None:
        return None
    if not isinstance(source, UrlSource) or crawl is None:
        raise ProfileError("profile.resume: продолжение доступно только для URL-профиля с обходом")
    body = _object(value, "profile.resume")
    _reject_unknown(body, _RESUME_KEYS, "profile.resume")
    raw = _non_empty_string(_required(body, "path", "profile.resume"), "profile.resume.path")
    return ResumeSpec(path=_resolve_path(raw, base_dir))


def _parse_export(value: object, base_dir: Path) -> ExportSettings:
    body = _object(value, "profile.export")
    export_format = _non_empty_string(_required(body, "format", "profile.export"), "profile.export.format")
    if export_format == "jsonl":
        allowed = _JSONL_EXPORT_KEYS
    elif export_format == "csv":
        allowed = _CSV_EXPORT_KEYS
    elif export_format == "sqlite":
        allowed = _JSONL_EXPORT_KEYS
    else:
        raise ProfileError(
            "profile.export.format: доступны jsonl и csv, а также sqlite. "
            f"Получено {export_format!r}."
        )
    _reject_unknown(body, allowed, "profile.export")
    raw_path = _non_empty_string(_required(body, "path", "profile.export"), "profile.export.path")
    mode = _export_mode(body, "mode", "profile.export")
    encoding = "utf-8"
    delimiter = ","
    spreadsheet_safe = False
    bom = False
    if export_format == "csv":
        if "encoding" in body:
            encoding = _non_empty_string(body["encoding"], "profile.export.encoding")
            try:
                codecs.lookup(encoding)
            except LookupError as exc:
                raise ProfileError(f"profile.export.encoding: неизвестная кодировка {encoding!r}") from exc
        delimiter = _csv_delimiter(body.get("delimiter", ","))
        spreadsheet_safe = _bool_option(body, "spreadsheet_safe", False, "profile.export")
        bom = _bool_option(body, "bom", False, "profile.export")
        if bom and encoding.lower().replace("-", "") not in {"utf8", "utf8sig"}:
            raise ProfileError("profile.export.bom: метка BOM поддерживается для кодировки utf-8")
    report_path = None
    report_mode = "create"
    if "report_path" in body:
        report_path = _resolve_path(
            _non_empty_string(body["report_path"], "profile.export.report_path"),
            base_dir,
        )
        report_mode = _export_mode(body, "report_mode", "profile.export")
    elif "report_mode" in body:
        raise ProfileError("profile.export.report_mode: параметр допустим вместе с report_path")
    return ExportSettings(
        path=_resolve_path(raw_path, base_dir),
        mode=mode,
        format=export_format,
        encoding=encoding,
        delimiter=delimiter,
        spreadsheet_safe=spreadsheet_safe,
        bom=bom,
        report_path=report_path,
        report_mode=report_mode,
    )


def _export_mode(body: dict, key: str, path: str) -> str:
    if key not in body:
        return "create"
    mode = _non_empty_string(body[key], f"{path}.{key}")
    if mode not in EXPORT_MODES:
        raise ProfileError(f"{path}.{key}: ожидалось create, replace или append. Получено {mode!r}.")
    return mode


def _csv_delimiter(value: object) -> str:
    if not isinstance(value, str) or len(value) != 1 or value in {'"', "\r", "\n"}:
        raise ProfileError("profile.export.delimiter: ожидался один символ, кроме кавычки и перевода строки")
    return value


def _resolve_path(raw_path: str, base_dir: Path) -> Path:
    return (base_dir.resolve() / raw_path).resolve()


def _field_name(name: str, path: str) -> str:
    if name in RESERVED_FIELD_NAMES:
        raise ProfileError(f"{path}: имя {name!r} занято служебным полем")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise ProfileError(
            f"{path}: имя поля должно начинаться с буквы или _ и содержать только латиницу, цифры и _"
        )
    return name


def _bool_option(body: dict, key: str, default: bool, path: str) -> bool:
    if key not in body:
        return default
    value = body[key]
    if not isinstance(value, bool):
        raise ProfileError(f"{path}.{key}: ожидалось true или false")
    return value


def _required(body: dict, key: str, path: str) -> object:
    if key not in body:
        raise ProfileError(f"{path}.{key}: параметр обязателен")
    return body[key]


def _object(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileError(f"{path}: ожидался объект JSON")
    return value


def _reject_unknown(body: dict, allowed: frozenset[str], path: str) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ProfileError(f"{path}.{unknown[0]}: неизвестный параметр")


def _non_empty_string(value: object, path: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ProfileError(f"{path}: ожидалась непустая строка")
    return value
