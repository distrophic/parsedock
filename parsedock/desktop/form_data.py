"""Сбор профиля из полей окна. Здесь нет виджетов Qt."""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

from parsedock.errors import ProfileError
from parsedock.profile import DEFAULT_RETRY_STATUSES, Profile, ReliabilitySpec, Transform, parse_profile

PREVIEW_LIMIT = 20


def profile_document(
    *,
    source: dict,
    container: str,
    fields: list[dict],
    output_path: str,
    replace_output: bool,
    export_format: str = "jsonl",
    delimiter: str = ",",
    encoding: str = "utf-8",
    crawl: dict | None = None,
    deduplicate_keys: list[str] | None = None,
    reliability: dict | None = None,
    resume: dict | None = None,
    extraction_format: str = "html",
) -> dict:
    """JSON профиля в том виде, в каком его можно сохранить и открыть снова."""

    export = {
        "format": export_format,
        "path": output_path,
        "mode": "replace" if replace_output else "create",
    }
    if export_format == "csv":
        export["delimiter"] = delimiter
        export["encoding"] = encoding
    document = {
        "schema_version": 1,
        "name": "desktop",
        "source": source,
        "extraction": {
            "format": extraction_format,
            "container": container,
            "fields": [_field(item) for item in fields if _filled(item)],
        },
        "export": export,
    }
    if crawl is not None:
        document["crawl"] = crawl
    if deduplicate_keys:
        document["deduplicate"] = {"keys": deduplicate_keys}
    if reliability is not None:
        document["reliability"] = reliability
    if resume is not None:
        document["resume"] = resume
    return document


def build_profile(
    *,
    base_dir: Path,
    source: dict,
    container: str,
    fields: list[dict],
    output_path: str,
    replace_output: bool,
    export_format: str = "jsonl",
    delimiter: str = ",",
    encoding: str = "utf-8",
    crawl: dict | None = None,
    deduplicate_keys: list[str] | None = None,
    reliability: dict | None = None,
    resume: dict | None = None,
    extraction_format: str = "html",
) -> Profile:
    """Проверить данные окна тем же разбором, что и JSON-профиль."""

    return parse_profile(
        profile_document(
            source=source,
            container=container,
            fields=fields,
            output_path=output_path,
            replace_output=replace_output,
            export_format=export_format,
            delimiter=delimiter,
            encoding=encoding,
            crawl=crawl,
            deduplicate_keys=deduplicate_keys,
            reliability=reliability,
            resume=resume,
            extraction_format=extraction_format,
        ),
        base_dir=base_dir,
    )


def resume_document(path: str, *, crawl_enabled: bool) -> dict | None:
    """Путь контрольной точки. Пустая строка означает, что обход каждый раз сначала."""

    text = path.strip()
    if not text:
        return None
    if not crawl_enabled:
        raise ProfileError("Продолжение записывается только вместе с обходом нескольких страниц.")
    return {"path": text}


def reliability_from_form(
    attempts: str,
    interval: str,
    backoff: str,
    extra: dict | None = None,
) -> dict | None:
    """Блок повторов. Значения по умолчанию в профиль не записываются."""

    kept = dict(extra or {})
    try:
        count = int(attempts.strip())
    except ValueError:
        raise ProfileError("Повторы: укажите целое число от 1 до 5.") from None
    if count < 1 or count > 5:
        raise ProfileError("Повторы: укажите целое число от 1 до 5.")
    pause = _form_number(interval, "Пауза между запросами")
    if pause < 0 or pause > 60:
        raise ProfileError("Пауза между запросами: число от 0 до 60 секунд.")
    delay = _form_number(backoff, "Пауза перед повтором")
    if delay <= 0 or delay > 30:
        raise ProfileError("Пауза перед повтором: число больше 0 и не больше 30 секунд.")
    cap = kept.get("max_backoff_seconds", 30)
    if delay > float(cap):
        raise ProfileError(
            f"Пауза перед повтором не длиннее верхнего предела {float(cap):g} с из профиля."
        )
    body = {
        "max_attempts": count,
        "min_interval_seconds": pause,
        "backoff_seconds": delay,
        "backoff_multiplier": kept.get("backoff_multiplier", 2),
        "max_backoff_seconds": cap,
        "jitter": kept.get("jitter", True),
        "retry_statuses": list(kept.get("retry_statuses", DEFAULT_RETRY_STATUSES)),
        "max_retry_after_seconds": kept.get("max_retry_after_seconds", 30),
    }
    compact = _compact_reliability(body)
    return compact or None


def reliability_extra(spec: ReliabilitySpec) -> dict:
    """Поля повторов, которых нет в трёх строках окна."""

    extra: dict = {}
    if spec.backoff_multiplier != 2:
        extra["backoff_multiplier"] = spec.backoff_multiplier
    if spec.max_backoff_seconds != 30:
        extra["max_backoff_seconds"] = spec.max_backoff_seconds
    if spec.jitter is not True:
        extra["jitter"] = spec.jitter
    if spec.retry_statuses != DEFAULT_RETRY_STATUSES:
        extra["retry_statuses"] = list(spec.retry_statuses)
    if spec.max_retry_after_seconds != 30:
        extra["max_retry_after_seconds"] = spec.max_retry_after_seconds
    return extra


def number_text(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _compact_reliability(body: dict) -> dict:
    compact: dict = {}
    if body["max_attempts"] != 1:
        compact["max_attempts"] = body["max_attempts"]
    if body["min_interval_seconds"] != 0:
        compact["min_interval_seconds"] = body["min_interval_seconds"]
    if body["backoff_seconds"] != 0.5:
        compact["backoff_seconds"] = body["backoff_seconds"]
    if body["backoff_multiplier"] != 2:
        compact["backoff_multiplier"] = body["backoff_multiplier"]
    if body["max_backoff_seconds"] != 30:
        compact["max_backoff_seconds"] = body["max_backoff_seconds"]
    if body["jitter"] is not True:
        compact["jitter"] = body["jitter"]
    if tuple(body["retry_statuses"]) != DEFAULT_RETRY_STATUSES:
        compact["retry_statuses"] = list(body["retry_statuses"])
    if body["max_retry_after_seconds"] != 30:
        compact["max_retry_after_seconds"] = body["max_retry_after_seconds"]
    return compact


def _form_number(text: str, label: str) -> float:
    raw = text.strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise ProfileError(f"{label}: укажите число.") from None
    return value


def dedup_keys_from_form(text: str) -> list[str]:
    """Имена полей через запятую. Пустая строка означает, что дубли не снимаются."""

    return [part.strip() for part in text.split(",") if part.strip()]


def transform_from_cells(op: str, argument: str) -> list[dict]:
    """Одна операция поля. Пустая операция означает, что значение не меняется."""

    argument = argument.strip()
    if op in {"", "none"}:
        return []
    if op == "normalize_whitespace":
        return [{"op": op}]
    if op == "absolute_url":
        item: dict = {"op": op}
        if argument:
            item["base_url"] = argument
        return [item]
    if op == "parse_number":
        decimal_separator, thousands_separator = _separators(argument, "Число")
        item = {"op": op, "decimal_separator": decimal_separator}
        if thousands_separator is not None:
            item["thousands_separator"] = thousands_separator
        return [item]
    if op == "parse_date":
        date_format, zone = _split_timezone(argument)
        if not date_format:
            raise ProfileError("Дата: укажите формат, например %d.%m.%Y")
        item = {"op": op, "format": date_format}
        if zone is not None:
            item["timezone"] = zone
        return [item]
    if op == "parse_currency":
        return [_currency(argument)]
    raise ProfileError(
        "Операция: доступны пробелы, абсолютный адрес, число, валюта и дата."
    )


def transform_document(item: Transform) -> dict:
    """Операция в том виде, в каком её принимает профиль."""

    body: dict = {"op": item.op}
    if item.op == "absolute_url" and item.base_url:
        body["base_url"] = item.base_url
    if item.op in {"parse_number", "parse_currency"}:
        body["decimal_separator"] = item.decimal_separator
        if item.thousands_separator:
            body["thousands_separator"] = item.thousands_separator
    if item.op == "parse_currency":
        if item.symbol:
            body["symbol"] = item.symbol
        if item.code:
            body["code"] = item.code
    if item.op == "parse_date":
        body["format"] = item.date_format
        if item.timezone_offset is not None:
            body["timezone"] = _format_offset(item.timezone_offset)
    return body


def argument_text(item: Transform) -> str:
    """Текст параметра для одной операции в таблице полей."""

    if item.op == "absolute_url":
        return item.base_url or ""
    if item.op == "parse_number":
        if item.thousands_separator:
            return f"decimal={item.decimal_separator} thousands={item.thousands_separator}"
        return item.decimal_separator or ""
    if item.op == "parse_currency":
        parts: list[str] = []
        if item.symbol:
            parts.append(f"symbol={item.symbol}")
        if item.code:
            parts.append(f"code={item.code}")
        parts.append(f"decimal={item.decimal_separator}")
        if item.thousands_separator:
            parts.append(f"thousands={item.thousands_separator}")
        return " ".join(parts)
    if item.op == "parse_date":
        text = item.date_format or ""
        if item.timezone_offset is not None:
            text += f" timezone={_format_offset(item.timezone_offset)}"
        return text
    return ""


def _separators(argument: str, label: str) -> tuple[str, str | None]:
    if len(argument) == 1:
        return argument, None
    if "=" not in argument:
        raise ProfileError(f"{label}: укажите один символ десятичного разделителя, например . или ,")
    parts = _pairs(argument, label)
    decimal_separator = parts.get("decimal", "")
    if len(decimal_separator) != 1:
        raise ProfileError(f"{label}: укажите один символ десятичного разделителя, например . или ,")
    thousands = parts.get("thousands")
    if thousands is not None and len(thousands) != 1:
        raise ProfileError(f"{label}: разделитель тысяч должен быть одним символом.")
    return decimal_separator, thousands


def _currency(argument: str) -> dict:
    parts = _pairs(argument, "Валюта")
    decimal_separator, thousands_separator = _separators(argument, "Валюта")
    symbol = parts.get("symbol")
    code = parts.get("code")
    if (symbol is None) == (code is None):
        raise ProfileError("Валюта: укажите ровно одно из symbol=₽ или code=RUB и decimal=.")
    item: dict = {"op": "parse_currency", "decimal_separator": decimal_separator}
    if symbol is not None:
        item["symbol"] = symbol
    if code is not None:
        item["code"] = code
    if thousands_separator is not None:
        item["thousands_separator"] = thousands_separator
    return item


def _pairs(argument: str, label: str) -> dict[str, str]:
    if not argument:
        raise ProfileError(f"{label}: укажите параметр операции.")
    parts: dict[str, str] = {}
    for piece in argument.split():
        if "=" not in piece:
            raise ProfileError(f"{label}: параметр записывается как имя=значение.")
        key, value = piece.split("=", 1)
        parts[key] = value
    return parts


def _split_timezone(argument: str) -> tuple[str, str | None]:
    marker = " timezone="
    if marker not in argument:
        return argument.strip(), None
    date_format, zone = argument.rsplit(marker, 1)
    return date_format.strip(), zone.strip()


def _format_offset(offset: timezone) -> str:
    delta = offset.utcoffset(None)
    if delta is None or delta == timedelta(0):
        return "Z"
    total = int(delta.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def crawl_from_form(
    mode: str,
    target: str,
    max_pages: str,
    *,
    obey_robots: bool,
    urls: tuple[str, ...] = (),
) -> dict | None:
    """Блок обхода для профиля. Пустой режим означает одну страницу."""

    if mode in {"", "off"}:
        return None
    try:
        pages = int(max_pages.strip())
    except ValueError:
        raise ProfileError("Лимит страниц: укажите целое число от 1.") from None
    if pages < 1:
        raise ProfileError("Лимит страниц: укажите целое число от 1.")
    if mode == "page_param":
        return {
            "mode": "page_param",
            "page_param": {"name": target.strip(), "start": 1, "step": 1},
            "max_pages": pages,
            "obey_robots": obey_robots,
        }
    if mode == "next_link":
        return {
            "mode": "next_link",
            "next_link": {"selector": target.strip()},
            "max_pages": pages,
            "obey_robots": obey_robots,
        }
    if mode == "urls":
        return {
            "mode": "urls",
            "urls": list(urls),
            "max_pages": pages,
            "obey_robots": obey_robots,
        }
    raise ProfileError("Обход: доступны одна страница, параметр страницы и ссылка далее.")


def url_source(
    url: str,
    client: str,
    *,
    trust_private_networks: bool,
    impersonate: str,
    headers: dict[str, str] | None = None,
) -> dict:
    source = {
        "type": "url",
        "url": url,
        "client": client,
        "trust_private_networks": trust_private_networks,
    }
    if client == "curl_cffi" and impersonate.strip():
        source["impersonate"] = impersonate.strip()
    if headers:
        source["headers"] = headers
    return source


def file_source(path: Path) -> dict:
    return {"type": "file", "path": str(path)}


def _filled(item: dict) -> bool:
    return bool(str(item.get("name", "")).strip() or str(item.get("selector", "")).strip())


def _field(item: dict) -> dict:
    kind = str(item.get("kind") or "text")
    field = {
        "name": str(item.get("name", "")).strip(),
        "selector": str(item.get("selector", "")).strip(),
        "kind": kind,
    }
    if kind == "attribute":
        field["attribute"] = str(item.get("attribute", "")).strip()
    if item.get("required"):
        field["required"] = True
    if item.get("multiple"):
        field["multiple"] = True
    if item.get("keep_original"):
        field["keep_original"] = True
    transforms = item.get("transforms") or []
    if transforms:
        field["transforms"] = list(transforms)
    return field
